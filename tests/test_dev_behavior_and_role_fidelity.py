from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError
from plow_whip_web.runtime.behavior_packs import (
    KARPATHY_MANDATORY_RESERVE_BYTES,
    behavior_baseline_for_role,
    principles_intact,
    role_receives_dev_behavior_baseline,
)
from plow_whip_web.runtime.goal_semantics import assess_goal_semantics
from plow_whip_web.runtime.orchestration import plan_goal_work_items
from plow_whip_web.runtime.sizing import TaskSizingInputs
from plow_whip_web.store.database import Database


def _sizing(**overrides: object) -> TaskSizingInputs:
    payload = {
        "layers_touched": 4,
        "components_touched": 8,
        "estimated_files_changed": 12,
        "has_migration": True,
        "has_deploy": True,
        "verification_commands_count": 4,
        "estimated_verification_seconds": 300,
        "external_dependencies_count": 0,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    payload.update(overrides)
    return TaskSizingInputs(**payload)  # type: ignore[arg-type]


def test_dev_behavior_role_include_exclude_matrix() -> None:
    for role in (
        "backend", "frontend", "ui", "fullstack", "devops_sre", "verification",
        "capability:implementation:custom",
    ):
        assert role_receives_dev_behavior_baseline(role) is True
        assert behavior_baseline_for_role(role)["inject"] is True
        assert behavior_baseline_for_role(role)["mandatory"] is True
        assert behavior_baseline_for_role(role)["effective_reserve_bytes"] > 0
    assert role_receives_dev_behavior_baseline("web3") is False
    for role in (
        "butler", "global_butler", "project_butler", "coordination",
        "scheduler", "router", "reducer", "web3",
    ):
        assert role_receives_dev_behavior_baseline(role) is False
        preview = behavior_baseline_for_role(role)
        assert preview["not_applicable"] is True
        assert preview["inject"] is False
        assert preview["applicability"] == "not_applicable"


def test_context_compiler_keeps_mandatory_principles_and_excludes_butler() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project_record = app.state.project_repository.create(
            name="behavior", path=str(project)
        )
        backend_role = app.state.project_repository.resolve_role(
            project_record["id"], "backend"
        )
        butler_roles = [
            item for item in app.state.project_repository.get(project_record["id"])["roles"]
            if item["kind"] == "butler"
        ]
        assert butler_roles
        backend_task = app.state.task_repository.create(
            title="backend work",
            objective="implement api",
            project_path=str(project),
            project_id=project_record["id"],
            role_id=backend_role["role_id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="behavior-backend",
        )
        butler_task = app.state.task_repository.create(
            title="butler work",
            objective="coordinate only",
            project_path=str(project),
            project_id=project_record["id"],
            role_id=butler_roles[0]["id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="behavior-butler",
        )
        compiled = app.state.context_compiler.compile(backend_task.id)
        assert compiled["role"] == "backend"
        assert compiled["dev_behavior_applicable"] is True
        assert principles_intact(compiled["content"])
        assert compiled["behavior_baseline"]["mandatory"] is True
        assert compiled["behavior_baseline"]["effective_reserve_bytes"] >= (
            KARPATHY_MANDATORY_RESERVE_BYTES
        )
        assert "Think Before Coding" in compiled["content"]
        preview = app.state.conventions.effective_context(
            project_id=project_record["id"],
            task_id=backend_task.id,
            role_id=backend_role["role_id"],
            role_kind="backend",
        )
        baseline = preview["behavior_baseline"]
        assert baseline["source"]
        assert baseline["role"] == "backend"
        assert baseline["revision"] == 1
        assert baseline["mandatory"] is True
        assert baseline["effective_reserve_bytes"] > 0
        assert baseline["config_source"]

        butler_compiled = app.state.context_compiler.compile(butler_task.id)
        assert butler_compiled["role"] == "butler"
        assert butler_compiled["dev_behavior_applicable"] is False
        assert "Think Before Coding" not in butler_compiled["content"]
        assert butler_compiled["behavior_baseline"]["not_applicable"] is True
        butler_preview = app.state.conventions.effective_context(
            project_id=project_record["id"],
            task_id=butler_task.id,
            role_id=butler_roles[0]["id"],
            role_kind="butler",
        )
        assert butler_preview["behavior_baseline"]["applicability"] == "not_applicable"


def test_context_conflict_fails_instead_of_dropping_principles() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project_record = app.state.project_repository.create(
            name="tight", path=str(project)
        )
        role = app.state.project_repository.resolve_role(project_record["id"], "backend")
        task = app.state.task_repository.create(
            title="tight context",
            objective="implement a large verifiable backend change with evidence",
            project_path=str(project),
            project_id=project_record["id"],
            role_id=role["role_id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="tight-context",
            constraints=["C" * 3000],
            acceptance=["exit 0 and preserve principles"],
        )
        app.state.conventions.put(
            scope="task_role",
            scope_id=f"{task.id}:{role['role_id']}",
            content="DIRECT HUMAN OVERRIDE\n" + ("Z" * 6000),
            expected_revision=0,
        )
        app.state.runtime_settings.update_override(
            scope="task_role",
            scope_id=task.id,
            values={
                "context_max_bytes": 8192,
                "checkpoint_max_bytes": 512,
                "handoff_max_bytes": 256,
            },
            expected_revision=0,
        )
        with pytest.raises(DomainError, match="configuration conflict|cannot preserve"):
            app.state.context_compiler.compile(task.id)


def test_structured_plan_preserves_named_roles_and_serializes_shared_tree() -> None:
    plan = plan_goal_work_items(
        title="role fidelity",
        objective="keep backend frontend devops_sre",
        sizing_inputs=_sizing(layers_touched=1, components_touched=1, has_deploy=False),
        structured_items=[
            {
                "ordinal": 1,
                "role": "backend",
                "kind": "implementation",
                "title": "api",
                "objective": "build api",
                "depends_on_ordinals": [],
            },
            {
                "ordinal": 2,
                "role": "frontend",
                "kind": "implementation",
                "title": "ui page",
                "objective": "build page",
                "depends_on_ordinals": [],
            },
            {
                "ordinal": 3,
                "role": "devops_sre",
                "kind": "implementation",
                "title": "deploy",
                "objective": "ship",
                "depends_on_ordinals": [],
            },
        ],
    )
    assert [item.role for item in plan.items] == [
        "backend", "frontend", "devops_sre", "verification"
    ]
    assert plan.route == "capability-milestones"
    assert plan.items[0].depends_on_ordinals == ()
    assert plan.items[1].depends_on_ordinals == (1,)
    assert plan.items[2].depends_on_ordinals == (2,)


def test_goal_persist_and_context_keep_named_roles() -> None:
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
            created = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "role-fidelity-goal-1"},
                json={
                    "title": "named roles",
                    "objective": "preserve backend frontend devops_sre identity",
                    "project_id": project["id"],
                    "provider": "generic-command",
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "scope": ["backend", "frontend", "devops_sre"],
                    "acceptance": ["roles stay named"],
                    "artifacts": [],
                    "constraints": [],
                    "sizing_inputs": {
                        "layers_touched": 1,
                        "components_touched": 1,
                        "estimated_files_changed": 2,
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
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "backend",
                            "kind": "implementation",
                            "title": "api",
                            "objective": "api",
                            "depends_on_ordinals": [],
                        },
                        {
                            "ordinal": 2,
                            "role": "frontend",
                            "kind": "implementation",
                            "title": "web",
                            "objective": "web",
                            "depends_on_ordinals": [],
                        },
                        {
                            "ordinal": 3,
                            "role": "devops_sre",
                            "kind": "implementation",
                            "title": "ops",
                            "objective": "ops",
                            "depends_on_ordinals": [],
                        },
                    ],
                    "command": {
                        "argv": ["python3", "-c", "print('ok')"],
                        "timeout_seconds": 30,
                    },
                },
            )
            assert created.status_code == 201, created.text
            goal = created.json()
            roles = [item["role"] for item in goal["work_items"]]
            assert roles == ["backend", "frontend", "devops_sre", "verification"]
            assert goal["work_items"][0]["status"] == "ready"
            assert goal["work_items"][1]["status"] == "paused"
            assert goal["work_items"][2]["status"] == "paused"
            for item in goal["work_items"]:
                context = client.get(f"/api/tasks/{item['id']}/context").json()
                assert context["role"] == item["role"]
                assert context["role"] not in {"fullstack", "capability", "simple-worker"}
                assert principles_intact(context["content"])


def test_goal_semantics_not_field_nonempty_and_one_gap() -> None:
    emptyish = assess_goal_semantics({
        "objective": "做好",
        "boundaries": ["随便"],
        "acceptance": ["能通过验收"],
    })
    assert emptyish["confidence"] < 95
    assert emptyish["ready"] is False
    assert emptyish["gaps"]
    rich = assess_goal_semantics({
        "objective": "实现项目管家语义澄清并交付可验证 API",
        "boundaries": [
            "只改控制平面与 Web UI",
            "不得共享跨项目 Session",
            "不得提交或推送",
        ],
        "acceptance": [
            "pytest 证明一次一问与 95% 门",
            "Context 编译保留四原则",
            "exit_code=0",
        ],
    })
    assert rich["ready"] is True
    assert rich["confidence"] >= 95


def test_project_butler_never_auto_routes_when_planner_is_unavailable() -> None:
    """Sizing must not bypass the mandatory natural-language planning model."""
    small_sizing = {
        "layers_touched": 1,
        "components_touched": 1,
        "estimated_files_changed": 2,
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
    }
    large_sizing = {
        **small_sizing,
        "layers_touched": 5,
        "components_touched": 12,
        "estimated_files_changed": 20,
        "has_migration": True,
        "has_deploy": True,
        "verification_commands_count": 6,
        "estimated_verification_seconds": 600,
        "risk_level": "high",
    }
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "auto-route", "path": str(project_path)},
            ).json()
            # Omit objective so this is mid/small auto-route, not structured GoalSpec.
            medium = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "medium-auto-route-1"},
                json={
                    "instruction": "实现中小目标自动路由并交付可验证结果",
                    "provider": "cursor",
                    "boundaries": ["只改本仓库后端与测试", "不得强推或部署"],
                    "acceptance": ["自动派发 Goal", "pytest exit 0"],
                    "sizing_inputs": small_sizing,
                },
            )
            assert medium.status_code == 201, medium.text
            medium_body = medium.json()
            assert medium_body["structured_goal_spec"] is False
            assert medium_body["auto_dispatch"] is False
            assert medium_body["status"] == "provider_suspended"
            assert medium_body["goal_id"] is None
            assert medium_body["planner"]["status"] == "provider_suspended"

            large = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "large-await-confirm-1"},
                json={
                    "instruction": "实现大型目标语义澄清并等待主人确认",
                    "provider": "cursor",
                    "boundaries": ["只改本仓库后端与测试", "不得强推或部署"],
                    "acceptance": ["一次一问", "主人确认后才派发", "pytest exit 0"],
                    "sizing_inputs": large_sizing,
                },
            )
            assert large.status_code == 201, large.text
            large_body = large.json()
            assert large_body["structured_goal_spec"] is False
            assert large_body["auto_dispatch"] is False
            assert large_body["status"] == "provider_suspended"
            assert large_body["goal_id"] is None


def test_project_butler_structured_passthrough_and_worker_help() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "structured", "path": str(project_path)},
            ).json()
            started = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "structured-goal-1"},
                json={
                    "instruction": "结构化直通",
                    "provider": "cursor",
                    "objective": "实现 Worker 结构化求助与极端升级路径",
                    "boundaries": [
                        "只改本仓库后端与测试",
                        "不得强推或部署",
                    ],
                    "acceptance": [
                        "求助 API 返回 open",
                        "极端升级暂停 Task",
                        "pytest exit 0",
                    ],
                    "sizing_inputs": {
                        "layers_touched": 1,
                        "components_touched": 2,
                        "estimated_files_changed": 3,
                        "has_migration": False,
                        "has_deploy": False,
                        "verification_commands_count": 2,
                        "estimated_verification_seconds": 60,
                        "external_dependencies_count": 0,
                        "risk_level": "low",
                        "independent_review_required": False,
                        "gate_artifact": True,
                        "gate_boundary": True,
                        "gate_verification": True,
                        "gate_dependency": True,
                    },
                },
            )
            assert started.status_code == 201, started.text
            conversation = started.json()
            assert conversation["structured_goal_spec"] is True
            assert conversation["status"] == "dispatched"
            assert conversation["goal_id"]
            goal = client.get(f"/api/goals/{conversation['goal_id']}").json()
            task_id = goal["work_items"][0]["id"]
            help_created = client.post(
                f"/api/projects/{project['id']}/worker-help",
                json={
                    "task_id": task_id,
                    "blocker": "missing credential for provider",
                    "evidence": {"attempt": 1, "log_tail": "auth failed"},
                    "attempted_actions": ["retried with existing token"],
                    "minimal_question": "是否有可用的只读凭据？",
                },
            )
            assert help_created.status_code == 201, help_created.text
            help_row = help_created.json()
            assert help_row["status"] == "open"
            resolved = client.post(
                f"/api/projects/{project['id']}/worker-help/{help_row['id']}/resolve",
                json={"resolution": "answered", "detail": {"answer": "use vault path"}},
            )
            assert resolved.status_code == 200
            assert resolved.json()["status"] == "answered"
            escalated = client.post(
                f"/api/projects/{project['id']}/task-escalations",
                json={
                    "task_id": task_id,
                    "reason_class": "credential_or_permission",
                    "detail": "owner must grant vault access",
                    "help_request_id": help_row["id"],
                },
            )
            assert escalated.status_code == 201, escalated.text
            task = client.get(f"/api/tasks/{task_id}").json()
            assert task["status"] == "paused"


def test_migration_0029_fresh_and_idempotent_preserves_global_convention() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "control.db"
        first = Database(db_path)
        applied = first.migrate()
        assert "0029_convention_task_role_help.sql" in applied
        with first.connect() as connection:
            connection.execute(
                """
                INSERT INTO conventions(id, scope, scope_id, content, revision)
                VALUES ('g1', 'global', 'global', 'KEEP_REVISION_ONE', 1)
                """
            )
            connection.commit()
        second = Database(db_path).migrate()
        assert second == []
        with Database(db_path).connect() as connection:
            row = connection.execute(
                "SELECT content, revision FROM conventions WHERE scope='global'"
            ).fetchone()
            assert row["content"] == "KEEP_REVISION_ONE"
            assert row["revision"] == 1
            tables = {
                item[0]
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "worker_help_requests" in tables
            assert "task_escalations" in tables
            check = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='conventions'"
            ).fetchone()["sql"]
            assert "task_role" in check
