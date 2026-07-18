from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import ProviderUnavailableError
from plow_whip_web.runtime.orchestration import plan_goal_work_items
from plow_whip_web.runtime.sizing import TaskSizingInputs
from plow_whip_web.store.database import Database


def _sizing(**overrides: object) -> TaskSizingInputs:
    payload = {
        "layers_touched": 1,
        "components_touched": 2,
        "estimated_files_changed": 3,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 2,
        "estimated_verification_seconds": 120,
        "external_dependencies_count": 0,
        "risk_level": "low",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    payload.update(overrides)
    return TaskSizingInputs(**payload)  # type: ignore[arg-type]


def test_butler_routing_is_deterministic_and_bounded() -> None:
    first = plan_goal_work_items(
        title="支付网关",
        objective="完成可验证交付",
        sizing_inputs=_sizing(
            has_deploy=True, layers_touched=4, components_touched=8,
            estimated_files_changed=10, risk_level="medium",
        ),
    )
    second = plan_goal_work_items(
        title="支付网关",
        objective="完成可验证交付",
        sizing_inputs=_sizing(
            has_deploy=True, layers_touched=4, components_touched=8,
            estimated_files_changed=10, risk_level="medium",
        ),
    )
    assert first.status == "planned"
    assert first.model_invoked is False
    assert [item.role for item in first.items] == [item.role for item in second.items]
    assert first.route == "capability-milestones"
    assert {item.role for item in first.items} == {"capability"}
    assert {item.kind for item in first.items} == {"implementation"}
    assert 2 <= len(first.items) <= 6
    assert all(item.acceptance == () for item in first.items)
    assert all(
        item.depends_on_ordinals == ((item.ordinal - 1,) if item.ordinal > 1 else ())
        for item in first.items
    )


def test_fresh_and_idempotent_migration_adds_goals() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "control.db"
        first = Database(db_path).migrate()
        second = Database(db_path).migrate()
        assert "0017_goal_orchestration.sql" in first
        assert second == []
        connection = Database(db_path).connect()
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "goals" in tables
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)")
            }
            assert {
                "goal_id", "parent_task_id", "depends_on_json",
                "work_item_kind", "ordinal", "blocked_reason", "handoff_json",
            } <= columns
        finally:
            connection.close()


def test_goal_to_auto_advance_e2e_with_real_http() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "goal-demo", "path": str(project_path)},
            ).json()
            payload = {
                "title": "写出验收文件",
                "objective": "fullstack 交付后由只读确定性 Gate 验收",
                "scope": ["backend"],
                "acceptance": ["manifest_bound_completion"],
                "artifacts": ["release-evidence.json"],
                "constraints": ["verification_worker_not_independent"],
                "project_id": project["id"],
                "provider": "generic-command",
                "sizing_inputs": {
                    "layers_touched": 1,
                    "components_touched": 1,
                    "estimated_files_changed": 1,
                    "has_migration": False,
                    "has_deploy": False,
                    "verification_commands_count": 1,
                    "estimated_verification_seconds": 30,
                    "external_dependencies_count": 0,
                    "risk_level": "low",
                    "independent_review_required": False,
                    "gate_artifact": True,
                    "gate_boundary": True,
                    "gate_verification": True,
                    "gate_dependency": True,
                },
                "command": {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('release-evidence.json').write_text('{\"status\":\"ok\"}')",
                    ]
                },
                "verification": [
                    {"kind": "exit_code", "expected": 0},
                    {
                        "kind": "file_contains",
                        "path": "release-evidence.json",
                        "contains": "\"status\":\"ok\"",
                    },
                ],
            }
            created = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "goal-e2e-1"},
                json=payload,
            )
            assert created.status_code == 201, created.text
            goal = created.json()
            assert goal["status"] == "running"
            assert goal["spec_revision"] == 1
            assert goal["spec"]["scope"] == ["backend"]
            assert goal["spec"]["acceptance"] == ["manifest_bound_completion"]
            assert goal["spec"]["artifacts"] == ["release-evidence.json"]
            assert goal["plan"]["model_invoked"] is False
            children = [
                item for item in goal["work_items"]
                if item["work_item_kind"] in {"implementation", "verification"}
            ]
            assert children[0]["status"] == "ready"
            assert all(item["status"] == "paused" for item in children[1:])
            assert all(item["execution_policy"] is not None for item in children)
            assert children[0]["sizing"]["status"] == "estimated"
            assert children[0]["spec"]["acceptance"] == [
                "manifest_bound_completion"
            ]
            assert children[0]["spec"]["artifacts"] == [
                "release-evidence.json"
            ]

            # Provider not ready path uses host provider separately; generic-command is always local.
            first_tick = client.post("/api/scheduler/tick")
            assert first_tick.status_code == 200
            assert first_tick.json()["model_tokens"] == 0

            # Drive remaining items via repeated ticks until goal completes.
            for index in range(8):
                tick = client.post("/api/scheduler/tick")
                assert tick.status_code == 200
                goal = client.get(f"/api/goals/{goal['id']}").json()
                if goal["status"] == "completed":
                    break
            else:
                raise AssertionError(f"goal did not complete: {goal}")

            assert goal["status"] == "completed"
            kinds = {item["work_item_kind"] for item in goal["work_items"]}
            assert kinds == {"implementation"}
            assert goal["parent_task_id"] is None
            assert all(
                item["status"] == "completed"
                for item in goal["work_items"]
            )

            # The task-local worker is released at verified terminal state.
            project_state = client.get(f"/api/projects/{project['id']}").json()
            assert len(project_state["workers"]) == 1
            assert project_state["workers"][0]["status"] == "released"
            assert project_state["workers"][0]["rotation_reason"] == "task_terminal"

            # SQLite must not store full stdout/stderr/prompt blobs for these tasks.
            connection = app.state.database.connect()
            try:
                rows = connection.execute(
                    "SELECT result_json FROM task_runs WHERE result_json IS NOT NULL"
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["result_json"])
                    assert "stdout" not in payload.get("execution", {})
                    assert "stderr" not in payload.get("execution", {})
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute(
                        "UPDATE goal_specs SET spec_json = '{}' WHERE goal_id = ?",
                        (goal["id"],),
                    )
            finally:
                connection.close()

            # Repeated tick is idempotent after completion.
            again = client.post("/api/scheduler/tick").json()
            assert again["status"] in {"completed", "skipped_lease_busy"}
            assert again["model_tokens"] == 0


def test_reviewer_item_is_not_a_dispatch_dependency() -> None:
    plan = plan_goal_work_items(
        title="不创建 reviewer",
        objective="任务自己的 Gate 决定完成",
        sizing_inputs=_sizing(independent_review_required=True),
    )
    assert plan.status == "planned"
    assert plan.route == "ephemeral-fullstack"
    assert [item.role for item in plan.items] == ["fullstack"]
    assert [item.kind for item in plan.items] == ["implementation"]


def test_goal_uses_one_provider_decision_and_probes_before_write() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="mixed-provider", path=str(project_path)
        )
        probes: list[str] = []
        app.state.provider_pool.require_ready = lambda name: probes.append(name) or {}
        payload = {
            "title": "single provider",
            "objective": "one provider decision for the routed task",
            "project_id": project["id"],
            "provider": "generic-command",
            "sizing_inputs": asdict(_sizing()),
            "verification": [{"kind": "exit_code", "expected": 0}],
        }
        with TestClient(app) as client:
            created = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "mixed-provider-goal"},
                json=payload,
            )
        assert created.status_code == 201, created.text
        assert {item["provider"] for item in created.json()["work_items"]} == {
            "generic-command"
        }
        assert probes == ["generic-command"]

        blocked_path = root / "blocked"
        blocked_path.mkdir()
        blocked = app.state.project_repository.create(
            name="blocked-provider", path=str(blocked_path)
        )
        failed_probes: list[str] = []

        def require_ready(name: str):
            failed_probes.append(name)
            raise ProviderUnavailableError(f"{name} not ready")

        app.state.provider_pool.require_ready = require_ready
        blocked_payload = {
            **payload,
            "project_id": blocked["id"],
            "provider": "cursor",
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "blocked-provider-goal"},
                json=blocked_payload,
            )
        assert response.status_code == 409
        assert failed_probes == ["cursor"]
        connection = app.state.database.connect()
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM goals WHERE project_id = ?", (blocked["id"],)
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (blocked["id"],)
            ).fetchone()[0] == 0
        finally:
            connection.close()
