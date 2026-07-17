from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import ProviderUnavailableError
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.fault_policy import FaultPolicy
from plow_whip_web.runtime.verification import VerificationEngine


class FakeHostBridge:
    token = "configured-for-test"

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, object]] = {}
        self.verify_calls = 0
        self.start_sessions: list[str | None] = []

    def start_job(
        self, *, job_id: str, session_id: str | None = None, **_kwargs: object
    ) -> dict[str, object]:
        self.start_sessions.append(session_id)
        return self.snapshots.setdefault(job_id, {
            "job_id": job_id,
            "status": "running",
            "pid": 4242,
            "session_id": session_id or "confirmed-session",
        })

    def job_status(self, job_id: str) -> dict[str, object]:
        return self.snapshots[job_id]

    def verify(
        self, *, project_path: str, execution: ExecutionResult,
        verification: list[dict[str, object]],
    ):
        self.verify_calls += 1
        return VerificationEngine().verify(Path(project_path), execution, verification)

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "available"

    def cancel_job(self, _job_id: str) -> dict[str, object]:
        raise ProviderUnavailableError("unused")

    result = staticmethod(HostBridgeClient.result)


def _runtime(key: str):
    directory = TemporaryDirectory()
    root = Path(directory.name)
    app = create_app(Settings(
        data_dir=root / "runtime",
        host_bridge_token="test-token-is-long-enough-123",
    ))
    bridge = FakeHostBridge()
    app.state.provider_pool.bridge = bridge
    project_path = root / key
    project_path.mkdir()
    project = app.state.project_repository.create(
        name=key, path=str(project_path), host_path=str(project_path)
    )
    role_id = app.state.project_repository.resolve_role(
        project["id"], "fullstack"
    )["role_id"]
    task = app.state.task_repository.create(
        title=key,
        objective="bounded Host work",
        project_path=str(project_path),
        project_id=project["id"],
        role_id=role_id,
        provider="codex",
        command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2,
        token_budget=100,
        idempotency_key=key,
    )
    running = app.state.task_service.drive(
        task.id, expected_revision=task.revision, idempotency_key=f"drive-{key}"
    )
    return directory, app, bridge, running, app.state.host_jobs.active()[0]


@pytest.mark.parametrize("marker", [
    "Error: [aborted] socket hang up",
    "read ECONNRESET",
    "TLS handshake failed",
    "websocket EOF",
    "bridge temporary unavailable",
])
def test_fault_policy_classifies_only_known_transport_signatures(marker: str) -> None:
    decision = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "returncode": 1,
        "failure_class": "command_failed",
        "stderr": marker,
    })

    assert decision.action == "defer"
    assert decision.failure_class == "transient_transport"
    assert decision.reason == "transient_provider_transport"
    assert FaultPolicy.model_invoked is False


def test_command_output_that_mentions_transport_text_is_not_misclassified() -> None:
    decision = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "returncode": 1,
        "failure_class": "command_failed",
        "stderr": "assertion failed: expected 'socket hang up' in fixture output",
    })

    assert decision.action == "verify"
    assert decision.failure_class == "command_failed"


def test_fault_policy_uses_last_error_when_stderr_is_empty() -> None:
    decision = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "returncode": 1,
        "failure_class": "command_failed",
        "stderr": "",
        "last_error": "bridge temporary unavailable",
    })

    assert decision.reason == "transient_provider_transport"


def test_fault_policy_classifies_provider_capacity_as_transient() -> None:
    decision = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "returncode": 1,
        "failure_class": "command_failed",
        "stderr": "Selected model is at capacity",
    })

    assert decision.action == "defer"
    assert decision.failure_class == "provider_capacity"
    assert decision.reason == "transient_provider_capacity"


def test_internal_tool_abort_requires_missing_exit_status() -> None:
    no_exit = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "failure_class": "command_failed",
        "stderr": "internal tool aborted while awaiting tool",
    })
    exited = FaultPolicy.from_host_snapshot({
        "status": "completed",
        "returncode": 1,
        "failure_class": "command_failed",
        "stderr": "internal tool aborted while awaiting tool",
    })

    assert (no_exit.action, no_exit.failure_class) == ("resume", "no_progress")
    assert (exited.action, exited.failure_class) == ("verify", "command_failed")


def test_socket_hang_up_replays_to_ready_without_spending_attempt() -> None:
    directory, app, bridge, running, job = _runtime("socket-hang-up")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 1,
            "failure_class": "command_failed",
            "stderr": "Error: [aborted] socket hang up",
            "session_id": "confirmed-session",
            "input_tokens": 0,
            "output_tokens": 0,
        }

        first = app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        events = app.state.task_repository.events(running.id)
        connection = app.state.database.connect()
        try:
            reservation = connection.execute(
                "SELECT status, actual_tokens FROM token_reservations WHERE run_id = ?",
                (job["run_id"],),
            ).fetchone()
            worker = connection.execute(
                "SELECT external_session_id, last_error FROM workers WHERE id = ?",
                (running.worker_id,),
            ).fetchone()
            counts = tuple(connection.execute(query, (running.id,)).fetchone()[0] for query in (
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                "SELECT COUNT(*) FROM token_usage WHERE task_id = ?",
                "SELECT COUNT(*) FROM task_leases WHERE task_id = ?",
                "SELECT COUNT(*) FROM resource_locks WHERE task_id = ?",
            ))
            connection.execute(
                "UPDATE host_jobs SET consumed_at = NULL WHERE job_id = ?",
                (job["job_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        assert first["settled"] == [{"task_id": running.id, "status": "ready"}]
        assert recovered.status.value == "ready"
        assert recovered.attempts_used == 0
        assert recovered.tokens_used == 0
        assert recovered.last_error == "transient_provider_transport"
        assert recovered.next_eligible_at is not None
        assert bridge.verify_calls == 0
        assert (reservation["status"], reservation["actual_tokens"]) == ("released", None)
        assert worker["external_session_id"] == "confirmed-session"
        assert worker["last_error"] == "transient_provider_transport"
        assert counts[1:] == (0, 0, 0)
        assert [
            event for event in events
            if event["payload"].get("reason") == "transient_provider_transport"
        ]

        revision = recovered.revision
        event_count = counts[0]
        app.state.task_service.reconcile_host_jobs()
        replayed = app.state.task_repository.get(running.id)
        connection = app.state.database.connect()
        try:
            replay_counts = tuple(connection.execute(query, (running.id,)).fetchone()[0] for query in (
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                "SELECT COUNT(*) FROM token_usage WHERE task_id = ?",
            ))
            replay_worker = connection.execute(
                "SELECT last_error FROM workers WHERE id = ?", (running.worker_id,)
            ).fetchone()
        finally:
            connection.close()
        assert replayed.revision == revision
        assert replayed.attempts_used == 0
        assert replayed.tokens_used == 0
        assert replay_counts == (event_count, 0)
        assert replay_worker["last_error"] == "transient_provider_transport"

        continued = app.state.task_service.drive(
            running.id,
            expected_revision=replayed.revision,
            idempotency_key="continue-same-task-after-transport",
        )
        assert continued.status.value == "running"
        assert continued.attempts_used == 1
        assert bridge.start_sessions[-1] == "confirmed-session"
    finally:
        directory.cleanup()


def test_capacity_text_overrides_command_failed_and_retains_session() -> None:
    directory, app, bridge, running, job = _runtime("provider-capacity")
    try:
        worker_id = running.worker_id
        first_session = bridge.snapshots[job["job_id"]]["session_id"]
        current = running
        for occurrence in range(1, 4):
            job = app.state.host_jobs.active()[0]
            bridge.snapshots[job["job_id"]] = {
                **bridge.snapshots[job["job_id"]],
                "status": "completed",
                "returncode": 1,
                "failure_class": "command_failed",
                "stderr": "Selected model is at capacity",
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            }
            app.state.task_service.reconcile_host_jobs()
            current = app.state.task_repository.get(current.id)
            if occurrence < 3:
                assert current.status.value == "ready"
                assert current.last_error == "transient_provider_capacity"
                current = app.state.task_service.drive(
                    current.id,
                    expected_revision=current.revision,
                    idempotency_key=f"capacity-retry-{occurrence}",
                )

        worker = app.state.project_repository.list()[0]["workers"][0]
        connection = app.state.database.connect()
        try:
            archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert current.status.value == "needs_human"
        assert current.last_error == "provider_capacity_repeated"
        assert worker["session_generation"] == 1
        assert worker["external_session_id"] == first_session
        assert archives == 0
        assert bridge.verify_calls == 0
    finally:
        directory.cleanup()


def test_real_hard_cap_breach_rotates_once_across_reconcile_replay() -> None:
    directory, app, bridge, running, job = _runtime("hard-cap-rotate")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 0,
            "failure_class": None,
            "stdout": "",
            "stderr": "",
            "input_tokens": 120,
            "cached_input_tokens": 100,
            "output_tokens": 1,
        }
        app.state.task_service.reconcile_host_jobs()
        failed = app.state.task_repository.get(running.id)

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE host_jobs SET consumed_at = NULL WHERE job_id = ?",
                (job["job_id"],),
            )
        app.state.task_service.reconcile_host_jobs()

        worker = app.state.project_repository.list()[0]["workers"][0]
        connection = app.state.database.connect()
        try:
            archives = connection.execute(
                """
                SELECT reason, trigger_key FROM worker_session_archives
                WHERE worker_id = ?
                """,
                (running.worker_id,),
            ).fetchall()
            usage = connection.execute(
                """
                SELECT input_tokens, cached_input_tokens, output_tokens,
                       attribution_granularity, value_classification,
                       rotation_reason, session_generation
                FROM token_usage WHERE task_id = ?
                """,
                (running.id,),
            ).fetchall()
        finally:
            connection.close()
        assert failed.status.value == "terminal_failed"
        assert failed.last_error == "budget_exceeded"
        assert worker["session_generation"] == 2
        assert worker["external_session_id"] is None
        assert [(row["reason"], row["trigger_key"]) for row in archives] == [
            ("budget_exceeded", f"budget-exceeded:{job['run_id']}")
        ]
        assert [tuple(row) for row in usage] == [
            (
                120, 100, 1, "turn", "unknown",
                "budget_exceeded", 1,
            )
        ]
    finally:
        directory.cleanup()


def test_provider_capacity_defers_boundedly_without_rotating_session() -> None:
    directory, app, bridge, running, job = _runtime("bounded-capacity")
    try:
        worker_id = running.worker_id
        for occurrence in range(1, 4):
            bridge.snapshots[job["job_id"]] = {
                **bridge.snapshots[job["job_id"]],
                "status": "completed",
                "returncode": 1,
                "failure_class": "command_failed",
                "stderr": "Selected model is at capacity",
                "session_id": "confirmed-session",
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            }
            app.state.task_service.reconcile_host_jobs()
            task = app.state.task_repository.get(running.id)
            if occurrence < 3:
                assert task.status.value == "ready"
                assert task.last_error == "transient_provider_capacity"
                running = app.state.task_service.drive(
                    task.id,
                    expected_revision=task.revision,
                    idempotency_key=f"capacity-retry-{occurrence}",
                )
                job = app.state.host_jobs.active()[0]
            else:
                assert task.status.value == "needs_human"
                assert task.last_error == "provider_capacity_repeated"

        worker = app.state.project_repository.get(
            app.state.task_repository.get(running.id).project_id
        )["workers"][0]
        connection = app.state.database.connect()
        try:
            archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert worker["session_generation"] == 1
        assert worker["external_session_id"] == "confirmed-session"
        assert archives == 0
    finally:
        directory.cleanup()


def test_budget_exceeded_settlement_rotates_once_across_reconcile_replay() -> None:
    directory, app, bridge, running, job = _runtime("budget-rotate-once")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 0,
            "failure_class": None,
            "stderr": "",
            "session_id": "oversized-session",
            "input_tokens": 120,
            "cached_input_tokens": 100,
            "output_tokens": 0,
        }
        app.state.task_service.reconcile_host_jobs()
        failed = app.state.task_repository.get(running.id)
        assert failed.status.value == "terminal_failed"
        assert failed.last_error == "budget_exceeded"

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE host_jobs SET consumed_at = NULL WHERE job_id = ?",
                (job["job_id"],),
            )
        app.state.task_service.reconcile_host_jobs()

        worker = app.state.project_repository.get(failed.project_id)["workers"][0]
        connection = app.state.database.connect()
        try:
            archives = connection.execute(
                """
                SELECT reason, COUNT(*) count
                FROM worker_session_archives WHERE worker_id = ?
                GROUP BY reason
                """,
                (running.worker_id,),
            ).fetchall()
        finally:
            connection.close()
        assert worker["session_generation"] == 2
        assert worker["external_session_id"] is None
        assert worker["last_input_tokens"] == 120
        assert worker["last_cached_input_tokens"] == 100
        assert worker["last_context_pressure_reason"] == "budget_exceeded"
        assert [(row["reason"], row["count"]) for row in archives] == [
            ("budget_exceeded", 1)
        ]
    finally:
        directory.cleanup()


def test_consecutive_internal_tool_aborts_rotate_only_at_threshold() -> None:
    directory, app, bridge, running, first_job = _runtime("bounded-no-progress")
    try:
        first_session = bridge.snapshots[first_job["job_id"]]["session_id"]
        bridge.snapshots[first_job["job_id"]] = {
            **bridge.snapshots[first_job["job_id"]],
            "status": "completed",
            "failure_class": "command_failed",
            "stderr": "internal tool aborted while awaiting tool",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        worker_id = first_job["worker_id"]
        connection = app.state.database.connect()
        try:
            first_worker = connection.execute(
                """
                SELECT session_id, external_session_id, session_generation
                FROM workers WHERE id = ?
                """,
                (worker_id,),
            ).fetchone()
            first_archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert recovered.status.value == "ready"
        assert recovered.last_error == "internal_tool_no_progress"
        assert first_worker["session_generation"] == 1
        assert first_worker["external_session_id"] == first_session
        assert first_archives == 0

        running_again = app.state.task_service.drive(
            recovered.id,
            expected_revision=recovered.revision,
            idempotency_key="drive-bounded-no-progress-again",
        )
        second_job = app.state.host_jobs.active()[0]
        assert bridge.start_sessions[-1] == first_session
        bridge.snapshots[second_job["job_id"]] = {
            **bridge.snapshots[second_job["job_id"]],
            "status": "completed",
            "failure_class": "command_failed",
            "stderr": "internal tool aborted while awaiting tool",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        app.state.task_service.reconcile_host_jobs()
        second_recovery = app.state.task_repository.get(running_again.id)
        connection = app.state.database.connect()
        try:
            rotated_worker = connection.execute(
                """
                SELECT session_id, external_session_id, session_generation
                FROM workers WHERE id = ?
                """,
                (worker_id,),
            ).fetchone()
            archives = connection.execute(
                "SELECT COUNT(*) FROM worker_session_archives WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()

        assert second_recovery.status.value == "ready"
        assert rotated_worker["session_generation"] == 2
        assert rotated_worker["session_id"] != first_worker["session_id"]
        assert rotated_worker["external_session_id"] is None
        assert archives == 1
    finally:
        directory.cleanup()


def test_nonzero_transient_tokens_are_settled_exactly_once() -> None:
    directory, app, bridge, running, job = _runtime("nonzero-transient")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 1,
            "failure_class": "command_failed",
            "stderr": "read ECONNRESET",
            "input_tokens": 7,
            "output_tokens": 3,
        }
        app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        connection = app.state.database.connect()
        try:
            reservation = connection.execute(
                "SELECT status, actual_tokens FROM token_reservations WHERE run_id = ?",
                (job["run_id"],),
            ).fetchone()
            usage = connection.execute(
                "SELECT input_tokens, output_tokens FROM token_usage WHERE run_id = ?",
                (job["run_id"],),
            ).fetchall()
        finally:
            connection.close()

        assert recovered.status.value == "ready"
        assert recovered.attempts_used == 0
        assert recovered.tokens_used == 10
        assert (reservation["status"], reservation["actual_tokens"]) == ("settled", 10)
        assert [(row["input_tokens"], row["output_tokens"]) for row in usage] == [(7, 3)]
    finally:
        directory.cleanup()


@pytest.mark.parametrize("failure_class", ["provider_auth", "permission_denied"])
def test_auth_and_permission_faults_need_human_without_model_retry(
    failure_class: str,
) -> None:
    directory, app, bridge, running, job = _runtime(failure_class)
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 1,
            "failure_class": failure_class,
            "stderr": "request rejected",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        app.state.task_service.reconcile_host_jobs()
        failed = app.state.task_repository.get(running.id)

        assert failed.status.value == "needs_human"
        assert failed.attempts_used == 0
        assert failed.last_error == failure_class
        assert failed.next_eligible_at is None
        assert bridge.verify_calls == 0
    finally:
        directory.cleanup()


def test_ordinary_command_failure_still_enters_verification() -> None:
    directory, app, bridge, running, job = _runtime("ordinary-command-failure")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 1,
            "failure_class": "command_failed",
            "stderr": "application assertion failed",
            "input_tokens": 2,
            "output_tokens": 1,
        }
        app.state.task_service.reconcile_host_jobs()
        failed = app.state.task_repository.get(running.id)

        assert bridge.verify_calls == 1
        assert failed.status.value == "ready"
        assert failed.attempts_used == 1
        assert failed.tokens_used == 3
        assert failed.last_error != "transient_provider_transport"
    finally:
        directory.cleanup()
