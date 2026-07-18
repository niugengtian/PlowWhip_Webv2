from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

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


def test_pm_split_is_deterministic_and_ends_with_verification() -> None:
    first = plan_goal_work_items(
        title="支付网关",
        objective="完成可验证交付",
        sizing_inputs=_sizing(
            has_deploy=True, layers_touched=3, components_touched=4
        ),
    )
    second = plan_goal_work_items(
        title="支付网关",
        objective="完成可验证交付",
        sizing_inputs=_sizing(
            has_deploy=True, layers_touched=3, components_touched=4
        ),
    )
    assert first.status == "planned"
    assert first.model_invoked is False
    assert [item.role for item in first.items] == [item.role for item in second.items]
    assert first.items[-1].role == "verification"
    assert first.items[-1].kind == "verification"
    assert {"backend", "frontend", "ui"} <= {item.role for item in first.items}
    assert 1 <= len(first.items) <= 7
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
                "objective": "fullstack 交付后由 verification 独立验收",
                "project_id": project["id"],
                "provider": "generic-command",
                "role_providers": {
                    "backend": "generic-command",
                    "verification": "generic-command",
                },
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
                        "from pathlib import Path; Path('goal-done.txt').write_text('ok')",
                    ]
                },
                "verification": [
                    {"kind": "exit_code", "expected": 0},
                    {"kind": "file_contains", "path": "goal-done.txt", "contains": "ok"},
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
            assert goal["plan"]["model_invoked"] is False
            children = [
                item for item in goal["work_items"]
                if item["work_item_kind"] in {"implementation", "verification"}
            ]
            assert children[0]["status"] == "ready"
            assert all(item["status"] == "paused" for item in children[1:])
            assert all(item["execution_policy"] is not None for item in children)
            assert children[0]["sizing"]["status"] == "estimated"

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
            assert "verification" in kinds
            assert all(
                item["status"] == "completed"
                for item in goal["work_items"]
                if item["work_item_kind"] in {"implementation", "verification", "coordination"}
            )

            # Different roles do not share workers/sessions.
            project_state = client.get(f"/api/projects/{project['id']}").json()
            workers = {
                worker["role"]: worker
                for worker in project_state["workers"]
                if worker["status"] != "released"
            }
            assert "backend" in workers
            assert "verification" in workers
            assert workers["backend"]["id"] != workers["verification"]["id"]
            assert workers["backend"]["session_id"] != workers["verification"]["session_id"]

            # SQLite must not store full stdout/stderr/prompt blobs for these tasks.
            connection = app.state.database.connect()
            try:
                rows = connection.execute(
                    "SELECT result_json FROM task_runs WHERE result_json IS NOT NULL"
                ).fetchall()
                for row in rows:
                    blob = row["result_json"]
                    assert "write_text(" not in blob
                    payload = json.loads(blob)
                    assert "stdout" not in payload.get("execution", {})
            finally:
                connection.close()

            # Repeated tick is idempotent after completion.
            again = client.post("/api/scheduler/tick").json()
            assert again["status"] in {"completed", "skipped_lease_busy"}
            assert again["model_tokens"] == 0


def test_explicit_legacy_role_plan_is_used_without_keyword_routing() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "roles", "path": str(project_path)},
            ).json()
            goal = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "goal-roles"},
                json={
                    "title": "显式能力边界",
                    "objective": "按结构化计划执行并独立验收",
                    "project_id": project["id"],
                    "provider": "generic-command",
                    "role_providers": {
                        "web3": "generic-command",
                        "verification": "generic-command",
                    },
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "web3",
                            "kind": "implementation",
                            "title": "实现链上边界",
                            "objective": "实现显式分配的链上边界",
                            "depends_on_ordinals": [],
                            "artifacts": ["web3-done.txt"],
                        },
                        {
                            "ordinal": 2,
                            "role": "verification",
                            "kind": "verification",
                            "title": "独立验收",
                            "objective": "验收显式计划产物",
                            "depends_on_ordinals": [1],
                        },
                    ],
                    "sizing_inputs": {
                        "layers_touched": 2,
                        "components_touched": 2,
                        "estimated_files_changed": 2,
                        "has_migration": False,
                        "has_deploy": False,
                        "verification_commands_count": 1,
                        "estimated_verification_seconds": 30,
                        "external_dependencies_count": 0,
                        "risk_level": "medium",
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
                            "from pathlib import Path; Path('web3-done.txt').write_text('ok')",
                        ]
                    },
                    "verification": [
                        {"kind": "file_contains", "path": "web3-done.txt", "contains": "ok"},
                    ],
                },
            ).json()
            roles = [
                next(
                    role["kind"]
                    for role in project["roles"]
                    if role["id"] == item["role_id"]
                )
                for item in goal["work_items"]
                if item["work_item_kind"] != "coordination"
            ]
            assert "web3" in roles
            assert roles[-1] == "verification"

            default = plan_goal_work_items(
                title="web3 钱包关键词不参与路由",
                objective="fullstack 旧词也不参与路由",
                sizing_inputs=_sizing(),
            )
            assert [item.role for item in default.items] == ["backend", "verification"]


def test_goal_provider_decisions_prefer_bindings_and_probe_before_write() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="mixed-provider", path=str(project_path)
        )
        backend_role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        connection = app.state.database.connect()
        try:
            connection.execute(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id
                ) VALUES (?, ?, ?, 'codex', ?)
                """,
                (
                    str(uuid.uuid4()),
                    project["id"],
                    backend_role,
                    str(uuid.uuid4()),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        probes: list[str] = []
        app.state.provider_pool.require_ready = lambda name: probes.append(name) or {}
        payload = {
            "title": "mixed provider",
            "objective": "respect bound roles and explicit unbound decisions",
            "project_id": project["id"],
            "provider": "generic-command",
            "role_providers": {
                "backend": "generic-command",
                "verification": "cursor",
            },
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
        children = {
            item["work_item_kind"]: item["provider"]
            for item in created.json()["work_items"]
            if item["work_item_kind"] != "coordination"
        }
        assert children == {"implementation": "codex", "verification": "cursor"}
        assert probes == ["codex", "cursor", "generic-command"]

        blocked_path = root / "blocked"
        blocked_path.mkdir()
        blocked = app.state.project_repository.create(
            name="blocked-provider", path=str(blocked_path)
        )
        failed_probes: list[str] = []

        def require_ready(name: str):
            failed_probes.append(name)
            if name == "cursor":
                raise ProviderUnavailableError("cursor not ready")
            return {}

        app.state.provider_pool.require_ready = require_ready
        blocked_payload = {
            **payload,
            "project_id": blocked["id"],
            "role_providers": {
                "backend": "generic-command",
                "verification": "cursor",
            },
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
