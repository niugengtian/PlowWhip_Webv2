from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.roles import ROLE_PROMPTS
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
        assert compiled["content"].endswith("[context truncated deterministically]\n")


def test_only_five_practical_role_prompts_are_exposed() -> None:
    assert set(ROLE_PROMPTS) == {"coordination", "fullstack", "web3", "devops_sre", "verification"}
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            response = client.get("/api/roles")
        assert response.status_code == 200
        assert response.json() == ROLE_PROMPTS


class TokenProvider:
    name = "token-test"
    model_invoked = True

    def __init__(self, estimate: int, actual: int = 0) -> None:
        self.estimate = estimate
        self.actual = actual
        self.executions = 0

    def estimate_tokens(self, _command):
        return self.estimate

    def execute(self, _project_path, _command):
        self.executions += 1
        return ExecutionResult(0, "", "", 1, input_tokens=self.actual, output_tokens=0)


def test_budget_rejects_before_claim_or_provider_invocation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="budget", objective="must stop before spend", project_path=str(project),
            command={"argv": ["unused"]}, verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=50, idempotency_key="budget-stop",
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
            max_attempts=1, token_budget=100, idempotency_key="usage-task",
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
