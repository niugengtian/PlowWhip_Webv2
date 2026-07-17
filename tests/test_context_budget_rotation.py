from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.roles import (
    CAPABILITY_ROLE_KINDS,
    LEGACY_ROLE_KINDS,
    ROLE_PROMPTS,
)
from plow_whip_web.runtime.budget import BudgetExceededError, BudgetManager
from plow_whip_web.runtime.journal import SessionJournal
from plow_whip_web.runtime.task_service import TaskService


def _bound_task(app, root: Path):
    project_path = root / "project"
    project_path.mkdir()
    project = app.state.project_repository.create(name="context", path=str(project_path))
    binding = app.state.project_repository.resolve_role(project["id"], "web3")
    task = app.state.task_repository.create(
        title="safe transfer", objective="verify a Web3 transfer without broadcasting", project_path=str(project_path),
        project_id=project["id"], role_id=binding["role_id"], resource_key="repo:context",
        command={"argv": [sys.executable, "-c", "pass"]},
        verification=[{"kind": "exit_code", "expected": 0}], max_attempts=1, token_budget=100,
        idempotency_key="context-task",
    )
    return project, task


def test_context_compiler_layers_three_scopes_without_model_replay() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        project, task = _bound_task(app, root)
        conventions = app.state.conventions
        conventions.put(scope="global", scope_id="global", content="GLOBAL QUALITY RULE", expected_revision=0)
        conventions.put(scope="project", scope_id=project["id"], content="PROJECT RPC RULE", expected_revision=0)
        conventions.put(scope="task", scope_id=task.id, content="TASK NO BROADCAST RULE", expected_revision=0)

        first = app.state.context_compiler.compile(task.id)
        second = app.state.context_compiler.compile(task.id)
        assert first["content_hash"] == second["content_hash"]
        assert first["model_invoked"] is False
        assert first["role"] == "web3"
        assert first["content"].index("GLOBAL QUALITY RULE") < first["content"].index("PROJECT RPC RULE")
        assert first["content"].index("PROJECT RPC RULE") < first["content"].index("TASK NO BROADCAST RULE")
        assert (root / "runtime" / first["relative_path"]).is_file()
        connection = app.state.database.connect()
        try:
            assert connection.execute("SELECT COUNT(*) FROM context_packs").fetchone()[0] == 1
        finally:
            connection.close()


def test_context_is_deterministically_bounded() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        project, task = _bound_task(app, root)
        current = app.state.runtime_settings.get()
        values = dict(current["values"])
        values["context_max_bytes"] = 4096
        app.state.runtime_settings.update(values, expected_revision=0)
        app.state.conventions.put(
            scope="project", scope_id=project["id"], content="约束" * 3000, expected_revision=0
        )
        compiled = app.state.context_compiler.compile(task.id)
        assert compiled["byte_size"] <= 4096
        assert "context sections truncated deterministically by scope priority" in compiled["content"]
        assert "## Convention: project" in compiled["content"]
        assert "## Boundaries" in compiled["content"]
        assert compiled["content"].endswith(
            "Only verification evidence can move this task to completed.\n"
        )


def test_context_truncation_prefers_task_and_project_rules_over_global_rules() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        project, task = _bound_task(app, root)
        current = app.state.runtime_settings.get()
        values = dict(current["values"])
        values["context_max_bytes"] = 4096
        app.state.runtime_settings.update(values, expected_revision=0)
        app.state.conventions.put(
            scope="global", scope_id="global",
            content="GLOBAL-LOW-PRIORITY " * 1000, expected_revision=0,
        )
        app.state.conventions.put(
            scope="project", scope_id=project["id"],
            content="PROJECT-SAFETY-RULE " * 1000, expected_revision=0,
        )
        app.state.conventions.put(
            scope="task", scope_id=task.id,
            content="TASK-COMPLETION-RULE " * 1000, expected_revision=0,
        )

        content = app.state.context_compiler.compile(task.id)["content"]

        assert "TASK-COMPLETION-RULE" in content
        assert "PROJECT-SAFETY-RULE" in content
        assert "GLOBAL-LOW-PRIORITY" not in content
        assert f"Task id: {task.id}" in content
        assert "Only verification evidence can move this task to completed." in content


def test_capability_roles_and_legacy_aliases_are_exposed() -> None:
    assert set(CAPABILITY_ROLE_KINDS) == {
        "coordination", "backend", "frontend", "ui", "devops_sre", "verification",
    }
    assert set(LEGACY_ROLE_KINDS) == {"fullstack", "web3"}
    assert set(ROLE_PROMPTS) == set(CAPABILITY_ROLE_KINDS) | set(LEGACY_ROLE_KINDS)
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            response = client.get("/api/roles")
        assert response.status_code == 200
        assert response.json() == ROLE_PROMPTS


class TokenProvider:
    name = "token-test"
    model_invoked = True

    def __init__(
        self, estimate: int, actual: int = 0, *, cached: int = 0, output: int = 0
    ) -> None:
        self.estimate = estimate
        self.actual = actual
        self.cached = cached
        self.output = output
        self.executions = 0

    def estimate_tokens(self, _command):
        return self.estimate

    def execute(self, _project_path, _command):
        self.executions += 1
        return ExecutionResult(
            0, "", "", 1, input_tokens=self.actual,
            cached_input_tokens=self.cached, output_tokens=self.output,
        )


def test_budget_rejects_before_claim_or_provider_invocation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="budget", objective="must stop before spend", project_path=str(project),
            command={"argv": ["unused"]}, verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=50, idempotency_key="budget-stop", provider="token-test",
        )
        provider = TokenProvider(estimate=60)
        service = TaskService(
            app.state.task_repository, provider=provider, budget=BudgetManager(app.state.database, app.state.runtime_settings)
        )
        with pytest.raises(BudgetExceededError, match="task token budget"):
            service.drive(task.id, expected_revision=0, idempotency_key="budget-drive")
        unchanged = app.state.task_repository.get(task.id)
        assert unchanged.status.value == "ready"
        assert unchanged.attempts_used == 0
        assert provider.executions == 0


def test_usage_ledger_tracks_work_tokens_but_control_tokens_stay_zero() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="usage", objective="record tokens", project_path=str(project),
            command={"argv": ["unused"]}, verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=100, idempotency_key="usage-task", provider="token-test",
        )
        service = TaskService(
            app.state.task_repository, provider=TokenProvider(estimate=9, actual=9),
            budget=app.state.budget,
        )
        completed = service.drive(task.id, expected_revision=0, idempotency_key="usage-drive")
        summary = app.state.budget.summary()
        assert completed.tokens_used == 9
        assert summary["total_tokens"] == 9
        assert summary["control_tokens"] == 0


def test_cached_input_is_persisted_as_input_subset_without_double_counting() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="cached usage", objective="record provider usage", project_path=str(project),
            command={"argv": ["unused"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=200, idempotency_key="cached-usage",
            provider="token-test",
        )
        service = TaskService(
            app.state.task_repository,
            provider=TokenProvider(estimate=0, actual=100, cached=80, output=20),
            budget=app.state.budget,
        )

        completed = service.drive(
            task.id, expected_revision=task.revision, idempotency_key="cached-drive"
        )
        summary = app.state.budget.summary()

        assert completed.tokens_used == 120
        assert summary["input_tokens"] == 100
        assert summary["cached_input_tokens"] == 80
        assert summary["output_tokens"] == 20
        assert summary["total_tokens"] == 120
        assert summary["cached_input_tokens_in_total"] is True
        assert summary["calls"][0]["uncached_input_tokens"] == 20
        assert summary["calls"][0]["attribution_granularity"] == "turn"
        assert summary["calls"][0]["value_classification"] == "unknown"
        assert not any(key.startswith("new_") for key in summary["calls"][0])
        connection = app.state.database.connect()
        try:
            row = connection.execute(
                """
                SELECT input_tokens, cached_input_tokens, output_tokens
                FROM token_usage WHERE task_id = ?
                """,
                (task.id,),
            ).fetchone()
        finally:
            connection.close()
        assert tuple(row) == (100, 80, 20)


def test_dispatch_guard_reuses_high_cached_context_with_unknown_attribution() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="provider-pressure", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        worker_id = str(uuid.uuid4())
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id,
                    external_session_id, last_input_tokens,
                    last_cached_input_tokens, last_output_tokens,
                    last_context_pressure_tokens, last_context_pressure_reason,
                    last_context_session_generation
                ) VALUES (?, ?, ?, 'codex', 'local-session-1', 'external-session-1',
                          5316377, 4939008, 5, 5316377, 'turn_usage_observed', 1)
                """,
                (worker_id, project["id"], role_id),
            )
        task = app.state.task_repository.create(
            title="next task", objective="guard before dispatch",
            project_path=str(project_path), project_id=project["id"], role_id=role_id,
            provider="codex", command={"argv": ["unused"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=150, idempotency_key="pressure-task",
            sizing={"status": "estimated"},
            execution_budget={
                "total_token_hard_cap": 150,
                "reserved_tokens": 100,
                "hard_deadline_seconds": 60,
                "max_attempts": 1,
            },
        )

        app.state.task_service._evaluate_context_guard(task, reserved_tokens=100)
        guarded = app.state.project_repository.get(project["id"])["workers"][0]
        assert guarded["session_generation"] == 1
        assert guarded["external_session_id"] == "external-session-1"

        app.state.task_service._evaluate_context_guard(task, reserved_tokens=100)
        app.state.task_service._evaluate_context_guard(task, reserved_tokens=100)
        final = app.state.project_repository.get(project["id"])["workers"][0]
        connection = app.state.database.connect()
        try:
            archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert final["session_generation"] == 1
        assert final["external_session_id"] == "external-session-1"
        assert final["rotation_reason"] is None
        assert final["last_context_guard_decision"] == "reuse"
        assert final["last_context_guard_reason"] == "turn_attribution_unknown_no_rotation"
        assert final["last_guard_relation"] == "unknown"
        assert final["last_guard_estimated_new_tokens"] == 100
        assert final["last_guard_carry_in_cached_tokens"] == 4939008
        assert archives == 0


def test_dispatch_guard_does_not_project_unknown_carry_in_against_cap() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="projected-cap", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        worker_id = str(uuid.uuid4())
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id,
                    external_session_id, last_input_tokens,
                    last_cached_input_tokens, last_context_pressure_tokens,
                    last_context_pressure_reason, last_context_session_generation
                ) VALUES (?, ?, ?, 'codex', 'local-session', 'external-session',
                          100, 100, 100, 'turn_usage_observed', 1)
                """,
                (worker_id, project["id"], role_id),
            )
        task = app.state.task_repository.create(
            title="projected cap", objective="rotate before dispatch",
            project_path=str(project_path), project_id=project["id"], role_id=role_id,
            provider="codex", command={"argv": ["unused"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=150, idempotency_key="projected-cap-task",
            sizing={"status": "estimated"},
            execution_budget={
                "total_token_hard_cap": 150,
                "reserved_tokens": 60,
                "hard_deadline_seconds": 60,
                "max_attempts": 1,
            },
        )

        app.state.task_service._evaluate_context_guard(task, reserved_tokens=60)
        worker = app.state.project_repository.get(project["id"])["workers"][0]

        assert worker["session_generation"] == 1
        assert worker["external_session_id"] == "external-session"
        assert worker["rotation_reason"] is None
        assert worker["last_context_guard_decision"] == "reuse"
        assert worker["last_context_guard_reason"] == "turn_attribution_unknown_no_rotation"
        assert worker["last_guard_relation"] == "unknown"


def test_dispatch_guard_reuses_low_pressure_same_role_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="low-pressure", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        worker_id = str(uuid.uuid4())
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id,
                    external_session_id, last_input_tokens,
                    last_cached_input_tokens, last_context_pressure_tokens,
                    last_context_pressure_reason, last_context_session_generation
                ) VALUES (?, ?, ?, 'codex', 'local-session', 'external-session',
                          100, 80, 100, 'turn_usage_observed', 1)
                """,
                (worker_id, project["id"], role_id),
            )
        task = app.state.task_repository.create(
            title="low pressure", objective="reuse provider session",
            project_path=str(project_path), project_id=project["id"], role_id=role_id,
            provider="codex", command={"argv": ["unused"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=150, idempotency_key="low-pressure-task",
            sizing={"status": "estimated"},
            execution_budget={
                "total_token_hard_cap": 150,
                "reserved_tokens": 50,
                "hard_deadline_seconds": 60,
                "max_attempts": 1,
            },
        )

        app.state.task_service._evaluate_context_guard(task, reserved_tokens=50)
        worker = app.state.project_repository.get(project["id"])["workers"][0]

        assert worker["session_generation"] == 1
        assert worker["external_session_id"] == "external-session"
        assert worker["rotation_reason"] is None
        assert worker["last_context_guard_decision"] == "reuse"
        assert worker["last_context_guard_reason"] == "turn_attribution_unknown_no_rotation"
        assert worker["last_guard_relation"] == "unknown"


def test_session_journal_rotates_with_hash_and_carry_forward() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        current = app.state.runtime_settings.get()
        values = dict(current["values"])
        values["rotation_max_bytes"] = 16_384
        app.state.runtime_settings.update(values, expected_revision=0)
        journal = SessionJournal(root / "runtime", app.state.runtime_settings)
        worker_id = "worker-rotation"
        assert journal.append(worker_id, {"payload": "a" * 10_000}) is None
        rotated = journal.append(worker_id, {"payload": "b" * 10_000})
        assert rotated and rotated["reason"] == "size_limit"
        session_dir = root / "runtime" / "sessions" / worker_id
        archive = session_dir / rotated["archive"]
        carry = json.loads((session_dir / "carry-forward.json").read_text(encoding="utf-8"))
        assert archive.is_file()
        assert carry["sha256"] == rotated["sha256"]
        assert (session_dir / "events.current.jsonl").is_file()


def test_hot_file_journal_rotates_without_discarding_provider_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="journal-role", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        current = app.state.runtime_settings.get()
        values = dict(current["values"])
        values["rotation_max_bytes"] = 16_384
        app.state.runtime_settings.update(values, expected_revision=current["revision"])

        def run(key: str):
            task = app.state.task_repository.create(
                title=key,
                objective="same role child",
                project_path=str(project_path),
                project_id=project["id"],
                role_id=role_id,
                command={"argv": [sys.executable, "-c", "pass"]},
                verification=[{"kind": "exit_code", "expected": 0}],
                max_attempts=1,
                token_budget=100,
                idempotency_key=f"create-{key}",
            )
            return app.state.task_service.drive(
                task.id,
                expected_revision=task.revision,
                idempotency_key=f"drive-{key}",
            )

        first = run("first-child")
        worker_id = first.worker_id
        connection = app.state.database.connect()
        try:
            initial = connection.execute(
                "SELECT session_id, session_generation FROM workers WHERE id = ?",
                (worker_id,),
            ).fetchone()
        finally:
            connection.close()
        second = run("second-child")
        assert second.worker_id == worker_id

        session_dir = root / "runtime" / "sessions" / worker_id
        (session_dir / "events.current.jsonl").write_text(
            "x" * 16_384, encoding="utf-8"
        )
        third = run("third-child")
        fourth = run("fourth-child")

        connection = app.state.database.connect()
        try:
            current_worker = connection.execute(
                "SELECT session_id, session_generation FROM workers WHERE id = ?",
                (worker_id,),
            ).fetchone()
            archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert {first.worker_id, second.worker_id, third.worker_id, fourth.worker_id} == {
            worker_id
        }
        assert initial["session_generation"] == 1
        assert current_worker["session_generation"] == 1
        assert current_worker["session_id"] == initial["session_id"]
        assert archives == 0
        assert len(list(session_dir.glob("events.[0-9]*.jsonl"))) == 1
