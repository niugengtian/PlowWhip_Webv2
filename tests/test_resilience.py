from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.connectivity import ConnectivityResult, classify_connectivity, network_available
from plow_whip_web.runtime.fault_policy import FaultPolicy


class FixedProbe:
    model_invoked = False

    def __init__(self, state: str, domestic: bool = False, overseas: bool = False) -> None:
        self.result = ConnectivityResult(state, domestic, overseas)

    def check(self) -> ConnectivityResult:
        return self.result


def _task(app, path: Path, *, key: str, network: str = "none", attempts: int = 1, pass_check: bool = True):
    code = "from pathlib import Path; Path('result.txt').write_text('actual')"
    expected = "actual" if pass_check else "never"
    return app.state.task_repository.create(
        title=key, objective=key, project_path=str(path), command={"argv": [sys.executable, "-c", code]},
        verification=[{"kind": "file_contains", "path": "result.txt", "contains": expected}],
        max_attempts=attempts, token_budget=0, idempotency_key=key, network_requirement=network,
    )


def test_pause_resume_needs_human_outbox_and_cancel() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            task = _task(app, project, key="control-task")
            paused = client.post(
                f"/api/tasks/{task.id}/control", headers={"Idempotency-Key": "control-pause"},
                json={"action": "pause", "reason": "operator inspection", "expected_revision": 0},
            )
            assert paused.status_code == 200 and paused.json()["status"] == "paused"
            resumed = client.post(
                f"/api/tasks/{task.id}/control", headers={"Idempotency-Key": "control-resume"},
                json={"action": "resume", "reason": "inspection passed", "expected_revision": 1},
            )
            assert resumed.json()["status"] == "ready"
            human = client.post(
                f"/api/tasks/{task.id}/control", headers={"Idempotency-Key": "control-human"},
                json={"action": "needs_human", "reason": "credential required", "expected_revision": 2},
            )
            assert human.json()["status"] == "needs_human"
            outbox = client.get("/api/outbox").json()
            assert outbox[0]["event_type"] == "task.needs_human"
            assert outbox[0]["payload"]["reason"] == "credential required"
            stream = client.get("/api/events/stream?once=true")
            assert stream.status_code == 200
            assert "event: task.needs_human" in stream.text
            assert client.post(f"/api/outbox/{outbox[0]['sequence']}/ack").json()["acknowledged"] is True
            cancelled = client.post(
                f"/api/tasks/{task.id}/control", headers={"Idempotency-Key": "control-cancel"},
                json={"action": "cancel", "reason": "no longer needed", "expected_revision": 3},
            )
            assert cancelled.json()["status"] == "cancelled"


@pytest.mark.parametrize(
    ("domestic", "overseas", "state"),
    [(True, True, "online"), (True, False, "domestic_only"), (False, True, "overseas_only"), (False, False, "offline")],
)
def test_connectivity_classification(domestic: bool, overseas: bool, state: str) -> None:
    result = classify_connectivity(domestic, overseas)
    assert result.state == state
    assert result.model_invoked is False


def test_flight_mode_defers_network_work_but_runs_local_work() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        local = _task(app, project, key="flight-local", network="none")
        remote = _task(app, project, key="flight-overseas", network="overseas")
        app.state.scheduler_service.connectivity = FixedProbe("offline")
        result = app.state.scheduler_service.tick(owner="flight-mode")
        assert app.state.task_repository.get(local.id).status.value == "completed"
        assert app.state.task_repository.get(remote.id).status.value == "ready"
        assert {item["task_id"] for item in result["deferred"]} == {remote.id}
        assert result["model_tokens"] == 0


def test_network_requirement_matrix_is_explicit() -> None:
    assert network_available("none", "offline") is True
    assert network_available("any", "offline") is False
    assert network_available("domestic", "domestic_only") is True
    assert network_available("domestic", "overseas_only") is False
    assert network_available("overseas", "overseas_only") is True
    assert network_available("overseas", "domestic_only") is False


def test_sleep_resume_is_detected_without_catch_up_loop() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        old = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute("UPDATE runtime_health SET last_tick_at = ? WHERE id = 'global'", (old,))
        result = app.state.health_repository.record(
            classify_connectivity(True, True), expected_interval_seconds=30
        )
        assert result["sleep_resumed"] is True
        assert result["model_invoked"] is False


def test_repeated_identical_failure_stops_at_guard_threshold() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = _task(app, project, key="loop-guard", attempts=8, pass_check=False)
        revisions = [0, 3, 6]
        statuses = []
        for index, revision in enumerate(revisions):
            with app.state.database.transaction(immediate=True) as connection:
                connection.execute("UPDATE tasks SET next_eligible_at = NULL WHERE id = ?", (task.id,))
            current = app.state.task_service.drive(
                task.id, expected_revision=revision, idempotency_key=f"loop-drive-{index}"
            )
            statuses.append(current.status.value)
        assert statuses == ["ready", "ready", "terminal_failed"]
        final = app.state.task_repository.get(task.id)
        assert final.same_failure_count == 3
        assert final.attempts_used == 3
        assert [event["event_type"] for event in app.state.task_repository.events(task.id)][-3:] == [
            "attempt.started", "verification.started", "task.terminal_failed"
        ]


def test_stale_running_task_and_worker_are_reconciled_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(name="recover", path=str(project_path))
        role = app.state.project_repository.resolve_role(project["id"], "fullstack")
        task = app.state.task_repository.create(
            title="recover", objective="recover", project_path=str(project_path), project_id=project["id"],
            role_id=role["role_id"], command={"argv": [sys.executable, "-c", "pass"]},
            verification=[{"kind": "exit_code", "expected": 0}], max_attempts=2, token_budget=0,
            idempotency_key="recover-create",
        )
        claim = app.state.task_repository.claim(task.id, expected_revision=0, idempotency_key="recover-claim")
        assert claim.task.status.value == "running"
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute("UPDATE task_leases SET expires_at = datetime('now', '-1 second') WHERE task_id = ?", (task.id,))
        first = app.state.recovery.reconcile()
        second = app.state.recovery.reconcile()
        assert first["recovered_tasks"] == [task.id]
        assert second["recovered_tasks"] == []
        assert app.state.task_repository.get(task.id).status.value == "ready"
        assert app.state.project_repository.get(project["id"])["workers"][0]["status"] == "idle"


def test_database_lock_becomes_safe_skip_not_recursive_retry() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with patch.object(app.state.scheduler_repository, "acquire", side_effect=sqlite3.OperationalError("database is locked")):
            result = app.state.scheduler_service.tick(owner="locked")
        assert result == {"status": "skipped_database_busy", "model_tokens": 0, "reason": "database_locked"}


@pytest.mark.parametrize("case", range(100))
def test_one_hundred_fault_injection_policy_cases_are_bounded(case: int) -> None:
    classes = [
        "timeout", "command_failed", "verification_failed", "no_progress",
        "database_locked", "domestic_unavailable", "overseas_unavailable", "offline",
        "provider_auth", "permission_denied", "budget_exceeded", "unknown",
    ]
    failure = classes[case % len(classes)]
    occurrences = case % 5 + 1
    attempts_left = case % 4
    decision = FaultPolicy.decide(failure, occurrences=occurrences, attempts_left=attempts_left)
    assert decision in {"defer", "needs_human", "terminal_failed", "retry_backoff"}
    if failure in {"database_locked", "domestic_unavailable", "overseas_unavailable", "offline"}:
        assert decision == "defer"
    if failure in {"provider_auth", "permission_denied", "budget_exceeded"}:
        assert decision == "needs_human"
    assert FaultPolicy.model_invoked is False
