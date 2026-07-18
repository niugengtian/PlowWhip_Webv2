from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def _task_payload(project_id: str, title: str = "deletable") -> dict[str, object]:
    return {
        "title": title,
        "objective": "never execute this task",
        "project_id": project_id,
        "provider": "generic-command",
        "command": {"argv": [sys.executable, "-c", "print('ok')"]},
        "verification": [{"kind": "exit_code", "expected": 0}],
    }


def _goal_payload(project_id: str, title: str) -> dict[str, object]:
    return {
        "title": title,
        "objective": "Goal is the only coordination aggregate",
        "project_id": project_id,
        "provider": "generic-command",
        "sizing_inputs": {
            "layers_touched": 0,
            "components_touched": 0,
            "estimated_files_changed": 1,
            "has_migration": False,
            "has_deploy": False,
            "verification_commands_count": 1,
            "estimated_verification_seconds": 0,
            "external_dependencies_count": 0,
            "risk_level": "low",
            "independent_review_required": False,
            "gate_artifact": True,
            "gate_boundary": True,
            "gate_verification": True,
            "gate_dependency": True,
        },
        "command": {"argv": [sys.executable, "-c", "print('ok')"]},
        "verification": [{"kind": "exit_code", "expected": 0}],
    }


def test_never_executed_task_can_be_physically_deleted_idempotently() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "delete", "path": str(project_path)}
            ).json()
            task = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "delete-create-1"},
                json=_task_payload(project["id"]),
            ).json()
            assert client.get(
                f"/api/tasks/{task['id']}/deletion-eligibility"
            ).json() == {"deletable": True, "reason": None}

            headers = {"Idempotency-Key": "delete-request-1"}
            payload = {"expected_revision": task["revision"], "reason": "operator cleanup"}
            deleted = client.request(
                "DELETE", f"/api/tasks/{task['id']}", headers=headers, json=payload
            )
            assert deleted.status_code == 200, deleted.text
            assert client.get(f"/api/tasks/{task['id']}").status_code == 404
            assert client.request(
                "DELETE", f"/api/tasks/{task['id']}", headers=headers, json=payload
            ).json() == deleted.json()

            connection = app.state.database.connect()
            try:
                assert connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id = ?", (task["id"],)
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT COUNT(*) FROM task_specs WHERE task_id = ?", (task["id"],)
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT reason FROM task_deletion_tombstones WHERE task_id = ?",
                    (task["id"],),
                ).fetchone()[0] == "operator cleanup"
            finally:
                connection.close()


def test_executed_and_dependent_tasks_are_rejected() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "reject", "path": str(project_path)}
            ).json()
            executed = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "delete-create-executed"},
                json=_task_payload(project["id"], "executed"),
            ).json()
            completed = client.post(
                f"/api/tasks/{executed['id']}/drive",
                headers={"Idempotency-Key": "delete-drive-executed"},
                json={"expected_revision": executed["revision"]},
            ).json()
            assert client.request(
                "DELETE",
                f"/api/tasks/{executed['id']}",
                headers={"Idempotency-Key": "delete-executed"},
                json={"expected_revision": completed["revision"], "reason": "must reject"},
            ).status_code == 409

            parent = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "delete-create-parent"},
                json=_task_payload(project["id"], "parent"),
            ).json()
            child = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "delete-create-child"},
                json=_task_payload(project["id"], "child"),
            ).json()
            connection = app.state.database.connect()
            try:
                connection.execute(
                    "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
                    (parent["id"], child["id"]),
                )
                connection.commit()
            finally:
                connection.close()
            assert client.request(
                "DELETE",
                f"/api/tasks/{parent['id']}",
                headers={"Idempotency-Key": "delete-dependent"},
                json={"expected_revision": parent["revision"], "reason": "must reject"},
            ).status_code == 409


def test_legacy_coordination_parent_detaches_without_duplicate_or_goal_failure() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "legacy", "path": str(project_path)}
            ).json()
            title = "P0：管家驱动的工程任务无人值守控制平面"
            goal = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "legacy-goal-create"},
                json=_goal_payload(project["id"], title),
            ).json()
            parent = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "legacy-parent-create"},
                json=_task_payload(project["id"], title),
            ).json()
            child_id = goal["work_items"][0]["id"]
            connection = app.state.database.connect()
            try:
                connection.execute(
                    """
                    UPDATE tasks SET work_item_kind = 'coordination', goal_id = ?,
                        status = 'cancelled', revision = 1 WHERE id = ?
                    """,
                    (goal["id"], parent["id"]),
                )
                connection.execute(
                    "UPDATE goals SET parent_task_id = ? WHERE id = ?",
                    (parent["id"], goal["id"]),
                )
                connection.execute(
                    "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
                    (parent["id"], child_id),
                )
                connection.commit()
            finally:
                connection.close()

            assert all(item["id"] != parent["id"] for item in client.get("/api/tasks").json())
            current = client.get(f"/api/goals/{goal['id']}").json()
            assert all(item["id"] != parent["id"] for item in current["work_items"])

            deleted = client.request(
                "DELETE",
                f"/api/tasks/{parent['id']}",
                headers={"Idempotency-Key": "legacy-parent-delete"},
                json={"expected_revision": 1, "reason": "retire duplicate parent"},
            )
            assert deleted.status_code == 200, deleted.text
            current = client.get(f"/api/goals/{goal['id']}").json()
            assert current["status"] == "running"
            assert current["parent_task_id"] is None
            assert current["work_items"][0]["parent_task_id"] is None
