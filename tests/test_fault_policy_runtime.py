from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import ProviderUnavailableError
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.execution_episode import ExecutionEpisodeWatchdog
from plow_whip_web.runtime.fault_policy import FaultPolicy
from plow_whip_web.runtime.verification import VerificationEngine


class FakeHostBridge:
    token = "configured-for-test"

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, object]] = {}
        self.verify_calls = 0
        self.start_sessions: list[str | None] = []
        self.cancel_available = False
        self.verify_unavailable = False

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
        acceptance: list[str] | None = None,
        require_structured_verdict: bool = False,
    ):
        self.verify_calls += 1
        if self.verify_unavailable:
            raise ProviderUnavailableError("verification bridge unavailable")
        return VerificationEngine().verify(
            Path(project_path),
            execution,
            verification,
            acceptance=acceptance,
            require_structured_verdict=require_structured_verdict,
        )

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "available"

    def cancel_job(self, job_id: str) -> dict[str, object]:
        if not self.cancel_available:
            raise ProviderUnavailableError("unused")
        snapshot = {
            **self.snapshots[job_id],
            "status": "cancelled",
            "returncode": 124,
        }
        self.snapshots[job_id] = snapshot
        return snapshot

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
    "RetriableError: Connection stalled",
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
        assert recovered.attempts_used == 1
        assert recovered.tokens_used == 0
        assert recovered.last_error == "transient_provider_transport"
        assert recovered.next_eligible_at is not None
        assert bridge.verify_calls == 0
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
        assert replayed.attempts_used == 1
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
        for occurrence in range(1, 9):
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
            if occurrence < 8:
                assert current.status.value == "ready"
                assert current.last_error in {
                    "transient_provider_capacity",
                    "execution_episode_resume:host_processes",
                    "execution_episode_replan:host_processes",
                    "execution_episode_replacement:host_processes",
                }
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
        assert current.last_error == "execution_episode_circuit_open:host_processes"
        assert worker["session_generation"] == 2
        assert worker["external_session_id"] == "confirmed-session"
        assert archives == 1
        assert bridge.verify_calls == 0
        connection = app.state.database.connect()
        try:
            hot_jobs = connection.execute(
                "SELECT COUNT(*) FROM host_jobs WHERE task_id = ?", (current.id,)
            ).fetchone()[0]
            archived_jobs = connection.execute(
                "SELECT COUNT(*) FROM host_job_archives WHERE task_id = ?",
                (current.id,),
            ).fetchone()[0]
            episodes = connection.execute(
                """
                SELECT status, checkpoint_json FROM execution_episodes
                WHERE task_id = ? ORDER BY ordinal
                """,
                (current.id,),
            ).fetchall()
        finally:
            connection.close()
        assert hot_jobs == 1
        assert archived_jobs == 7
        assert [row["status"] for row in episodes] == [
            "terminated", "terminated", "terminated", "circuit_open",
        ]
        assert [
            json.loads(row["checkpoint_json"])["recovery_action"]
            for row in episodes
        ] == ["resume", "replan", "replacement", "circuit_open"]

        restarted = app.state.task_repository.control(
            current.id,
            action="restart",
            reason="explicit new TaskSpec revision",
            expected_revision=current.revision,
            idempotency_key="restart-after-episode-circuit",
        )
        running_again = app.state.task_service.drive(
            restarted.id,
            expected_revision=restarted.revision,
            idempotency_key="drive-after-episode-circuit",
        )
        next_episode = app.state.host_jobs.latest_episode(current.id)
        assert running_again.status.value == "running"
        assert restarted.spec_revision == 2
        assert next_episode["status"] == "active"
        assert next_episode["spec_revision"] == 2
    finally:
        directory.cleanup()


def test_large_token_usage_completes_without_rotation_across_reconcile_replay() -> None:
    directory, app, bridge, running, job = _runtime("large-token-usage")
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
        completed = app.state.task_repository.get(running.id)

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
        assert completed.status.value == "completed"
        assert completed.last_error is None
        assert completed.tokens_used == 121
        assert worker["session_generation"] == 1
        assert [row for row in archives if row["reason"] != "task_terminal"] == []
        assert [tuple(row) for row in usage] == [
            (
                120, 100, 1, "turn", "unknown", None, 1,
            )
        ]
    finally:
        directory.cleanup()


def test_provider_capacity_keeps_backoff_at_episode_boundary() -> None:
    directory, app, bridge, running, job = _runtime("bounded-capacity")
    try:
        for occurrence in range(1, 3):
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
            assert task.status.value == "ready"
            assert task.next_eligible_at is not None
            if occurrence == 1:
                assert task.last_error == "transient_provider_capacity"
                running = app.state.task_service.drive(
                    task.id,
                    expected_revision=task.revision,
                    idempotency_key=f"capacity-retry-{occurrence}",
                )
                job = app.state.host_jobs.active()[0]
            else:
                assert task.last_error == "execution_episode_resume:host_processes"
        connection = app.state.database.connect()
        try:
            checkpoint = connection.execute(
                """
                SELECT checkpoint_json FROM execution_episodes
                WHERE task_id = ? ORDER BY ordinal DESC LIMIT 1
                """,
                (running.id,),
            ).fetchone()["checkpoint_json"]
        finally:
            connection.close()
        assert '"recovery_action": "resume"' in checkpoint
        context = app.state.context_compiler.compile(running.id)["content"]
        assert "## ExecutionEpisode checkpoint" in context
        assert "Recovery action: resume" in context
        assert "Do not replay steps already reflected in the workspace." in context
    finally:
        directory.cleanup()


def test_recovery_episode_inherits_task_spec_hard_deadline() -> None:
    directory, app, bridge, running, job = _runtime("inherited-hard-deadline")
    try:
        connection = app.state.database.connect()
        try:
            first_deadline = connection.execute(
                """
                SELECT deadline_at FROM execution_episodes
                WHERE task_id = ? AND spec_revision = ?
                """,
                (running.id, running.spec_revision),
            ).fetchone()["deadline_at"]
        finally:
            connection.close()

        for occurrence in range(2):
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
            recovered = app.state.task_repository.get(running.id)
            assert recovered.status.value == "ready"
            if occurrence == 0:
                running = app.state.task_service.drive(
                    recovered.id,
                    expected_revision=recovered.revision,
                    idempotency_key="inherited-hard-deadline-second-process",
                )
                job = app.state.host_jobs.active()[0]

        running = app.state.task_service.drive(
            recovered.id,
            expected_revision=recovered.revision,
            idempotency_key="inherited-hard-deadline-recovery-episode",
        )
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE execution_episodes
                SET deadline_at = datetime(deadline_at, '+10 minutes'),
                    wall_deadline_at = datetime(wall_deadline_at, '+10 minutes')
                WHERE task_id = ? AND spec_revision = ? AND status = 'active'
                """,
                (running.id, running.spec_revision),
            )
        app.state.task_service.reconcile_host_jobs()
        connection = app.state.database.connect()
        try:
            deadlines = connection.execute(
                """
                SELECT deadline_at, wall_deadline_at
                FROM execution_episodes
                WHERE task_id = ? AND spec_revision = ?
                ORDER BY ordinal
                """,
                (running.id, running.spec_revision),
            ).fetchall()
        finally:
            connection.close()

        assert len(deadlines) == 2
        assert {row["deadline_at"] for row in deadlines} == {first_deadline}
        assert all(row["wall_deadline_at"] <= first_deadline for row in deadlines)
    finally:
        directory.cleanup()


def test_exhausted_task_deadline_stops_before_provider_and_requires_new_spec() -> None:
    directory, app, bridge, running, job = _runtime("exhausted-hard-deadline")
    try:
        for occurrence in range(2):
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
            recovered = app.state.task_repository.get(running.id)
            assert recovered.status.value == "ready"
            if occurrence == 0:
                running = app.state.task_service.drive(
                    recovered.id,
                    expected_revision=recovered.revision,
                    idempotency_key="exhausted-hard-deadline-second-process",
                )
                job = app.state.host_jobs.active()[0]

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE execution_episodes
                SET deadline_at = datetime('now', '-1 second'),
                    wall_deadline_at = datetime('now', '-1 second')
                WHERE task_id = ? AND spec_revision = ?
                """,
                (recovered.id, recovered.spec_revision),
            )
        starts_before = len(bridge.start_sessions)
        stopped = app.state.task_service.drive(
            recovered.id,
            expected_revision=recovered.revision,
            idempotency_key="exhausted-hard-deadline-blocked",
        )

        assert stopped.status.value == "needs_human"
        assert stopped.last_error.startswith("task_hard_deadline_exhausted:")
        assert len(bridge.start_sessions) == starts_before
        connection = app.state.database.connect()
        try:
            lease_count = connection.execute(
                "SELECT COUNT(*) FROM task_leases WHERE task_id = ?",
                (stopped.id,),
            ).fetchone()[0]
            lock_count = connection.execute(
                "SELECT COUNT(*) FROM resource_locks WHERE task_id = ?",
                (stopped.id,),
            ).fetchone()[0]
            active_runs = connection.execute(
                """
                SELECT COUNT(*) FROM task_runs
                WHERE task_id = ? AND status = 'running'
                """,
                (stopped.id,),
            ).fetchone()[0]
            call = connection.execute(
                """
                SELECT status, error_class FROM model_calls
                WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (stopped.id,),
            ).fetchone()
        finally:
            connection.close()
        assert lease_count == 0
        assert lock_count == 0
        assert active_runs == 0
        assert dict(call) == {
            "status": "failed",
            "error_class": "task_hard_deadline_exhausted",
        }

        restarted = app.state.task_repository.control(
            stopped.id,
            action="restart",
            reason="human approved a fresh hard deadline",
            expected_revision=stopped.revision,
            idempotency_key="exhausted-hard-deadline-human-restart",
        )
        resumed = app.state.task_service.drive(
            restarted.id,
            expected_revision=restarted.revision,
            idempotency_key="exhausted-hard-deadline-new-spec-drive",
        )
        assert resumed.status.value == "running"
        assert resumed.spec_revision == stopped.spec_revision + 1
        assert len(bridge.start_sessions) == starts_before + 1
    finally:
        directory.cleanup()


def test_internal_tool_aborts_rotate_only_at_replacement_episode() -> None:
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
        assert second_recovery.status.value == "ready"
        worker = app.state.project_repository.get(
            second_recovery.project_id
        )["workers"][0]
        assert worker["session_generation"] == 1

        current = second_recovery
        for occurrence in range(3, 7):
            current = app.state.task_service.drive(
                current.id,
                expected_revision=current.revision,
                idempotency_key=f"drive-no-progress-{occurrence}",
            )
            current_job = app.state.host_jobs.active()[0]
            bridge.snapshots[current_job["job_id"]] = {
                **bridge.snapshots[current_job["job_id"]],
                "status": "completed",
                "failure_class": "command_failed",
                "stderr": "internal tool aborted while awaiting tool",
                "input_tokens": 0,
                "output_tokens": 0,
            }
            app.state.task_service.reconcile_host_jobs()
            current = app.state.task_repository.get(current.id)

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
        assert rotated_worker["session_generation"] == 2
        assert rotated_worker["session_id"] != first_worker["session_id"]
        assert rotated_worker["external_session_id"] is None
        assert archives == 1
    finally:
        directory.cleanup()


def test_watchdog_token_burn_rate_is_alert_only() -> None:
    decision = ExecutionEpisodeWatchdog.evaluate(
        {
            "progress_bytes": 0,
            "zero_progress_rounds": 0,
            "last_fault_class": None,
            "same_fault_count": 0,
            "host_process_count": 1,
            "max_host_processes": 2,
        },
        {
            "status": "completed",
            "input_tokens": 2_000_000,
            "output_tokens": 1_000_000,
        },
        fault_class=None,
        elapsed_seconds=1,
        deadline_reached=False,
        wall_clock_reached=False,
        same_fault_limit=2,
        zero_progress_limit=3,
    )

    assert decision.burn_rate_alert is True
    assert decision.bounded is False
    assert decision.reason is None


def test_watchdog_wall_clock_is_a_hard_episode_boundary() -> None:
    decision = ExecutionEpisodeWatchdog.evaluate(
        {
            "progress_bytes": 1024,
            "zero_progress_rounds": 0,
            "last_fault_class": None,
            "same_fault_count": 0,
            "host_process_count": 1,
            "max_host_processes": 2,
        },
        {"status": "running", "output_bytes": {"total": 2048}},
        fault_class=None,
        elapsed_seconds=901,
        deadline_reached=False,
        wall_clock_reached=True,
        same_fault_limit=2,
        zero_progress_limit=3,
    )

    assert decision.bounded is True
    assert decision.reason == "wall_clock"


def test_watchdog_accepts_hashed_provider_output_but_not_raw_byte_growth() -> None:
    episode = {
        "progress_bytes": 0,
        "progress_evidence_json": "{}",
        "zero_progress_rounds": 0,
        "last_fault_class": None,
        "same_fault_count": 0,
        "host_process_count": 1,
        "max_host_processes": 2,
    }
    first = ExecutionEpisodeWatchdog.evaluate(
        episode,
        {
            "status": "running",
            "output_bytes": {"total": 1024},
            "progress_evidence": {
                "kind": "workspace",
                "available": True,
                "changed_files": 0,
                "fingerprint": "unchanged-workspace",
            },
            "output_segments": [{
                "stream": "stdout",
                "ref": "job/stdout.000000.log",
                "sha256": "a" * 64,
                "bytes": 1024,
            }],
        },
        fault_class=None,
        elapsed_seconds=60,
        deadline_reached=False,
        wall_clock_reached=False,
        same_fault_limit=2,
        zero_progress_limit=3,
    )
    assert first.verifiable_progress is False

    second = ExecutionEpisodeWatchdog.evaluate(
        {
            **episode,
            "progress_evidence_json": json.dumps(first.progress_evidence),
            "zero_progress_rounds": first.zero_progress_rounds,
        },
        {
            "status": "running",
            "output_bytes": {"total": 2048},
            "progress_evidence": {
                "kind": "workspace",
                "available": True,
                "changed_files": 0,
                "fingerprint": "unchanged-workspace",
            },
            "output_segments": [{
                "stream": "stdout",
                "ref": "job/stdout.000000.log",
                "sha256": "b" * 64,
                "bytes": 2048,
            }],
        },
        fault_class=None,
        elapsed_seconds=120,
        deadline_reached=False,
        wall_clock_reached=False,
        same_fault_limit=2,
        zero_progress_limit=3,
    )
    assert second.verifiable_progress is True
    assert second.zero_progress_rounds == 0

    raw_bytes_only = ExecutionEpisodeWatchdog.evaluate(
        episode,
        {"status": "running", "output_bytes": {"total": 4096}},
        fault_class=None,
        elapsed_seconds=120,
        deadline_reached=False,
        wall_clock_reached=False,
        same_fault_limit=2,
        zero_progress_limit=3,
    )
    assert raw_bytes_only.verifiable_progress is False
    assert raw_bytes_only.zero_progress_rounds == 1


def test_burn_rate_alert_is_reported_without_stopping_task() -> None:
    directory, app, bridge, running, job = _runtime("burn-rate-alert")
    try:
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "running",
            "input_tokens": 2_000_000,
            "output_tokens": 1_000_000,
            "output_bytes": {"total": 1024},
        }

        result = app.state.task_service.reconcile_host_jobs()
        current = app.state.task_repository.get(running.id)
        episode = app.state.host_jobs.latest_episode(running.id)

        assert result["active"] == 1
        assert len(result["burn_rate_alerts"]) == 1
        assert current.status.value == "running"
        assert current.execution_episode["burn_rate_alert"] == 1
        assert current.execution_episode["status"] == "active"
        assert episode["burn_rate_alert"] == 1
        assert episode["status"] == "active"
    finally:
        directory.cleanup()


def test_running_no_progress_terminates_only_current_episode() -> None:
    directory, app, bridge, running, job = _runtime("running-no-progress")
    try:
        bridge.cancel_available = True
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "running",
            "error_summary": "internal tool aborted while awaiting tool",
            "output_bytes": {"total": 0},
        }

        first = app.state.task_service.reconcile_host_jobs()
        second = app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        episode = app.state.host_jobs.latest_episode(running.id)

        assert first["active"] == 1
        assert second["settled"] == [{"task_id": running.id, "status": "ready"}]
        assert recovered.status.value == "ready"
        assert recovered.last_error.startswith("execution_episode_resume:")
        assert episode["status"] == "terminated"
        assert episode["checkpoint"]["recovery_action"] == "resume"
        assert len(bridge.snapshots) == 1
    finally:
        directory.cleanup()


def test_real_taskspec_deadline_cancels_only_current_episode() -> None:
    directory, app, bridge, running, job = _runtime("episode-deadline")
    try:
        bridge.cancel_available = True
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE execution_episodes
                SET deadline_at = datetime('now', '-1 second')
                WHERE id = ?
                """,
                (job["episode_id"],),
            )

        result = app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        episode = app.state.host_jobs.latest_episode(running.id)

        assert result["settled"] == [{"task_id": running.id, "status": "ready"}]
        assert recovered.status.value == "ready"
        assert recovered.last_error == "execution_episode_resume:deadline"
        assert recovered.attempts_used == 1
        assert episode["status"] == "terminated"
        assert episode["end_reason"] == "deadline"
    finally:
        directory.cleanup()


def test_verification_transport_fault_uses_same_episode_circuit() -> None:
    directory, app, bridge, running, job = _runtime("verification-transport")
    try:
        bridge.verify_unavailable = True
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

        first = app.state.task_service.reconcile_host_jobs()
        second = app.state.task_service.reconcile_host_jobs()
        recovered = app.state.task_repository.get(running.id)
        episode = app.state.host_jobs.latest_episode(running.id)

        assert first["active"] == 1
        assert second["settled"] == [{"task_id": running.id, "status": "ready"}]
        assert recovered.last_error == "execution_episode_resume:same_fault"
        assert episode["status"] == "terminated"
        assert episode["checkpoint"]["fault_class"] == "transient_transport"
        assert bridge.verify_calls == 2
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
            usage = connection.execute(
                "SELECT input_tokens, output_tokens FROM token_usage WHERE run_id = ?",
                (job["run_id"],),
            ).fetchall()
        finally:
            connection.close()

        assert recovered.status.value == "ready"
        assert recovered.attempts_used == 1
        assert recovered.tokens_used == 10
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
        assert failed.attempts_used == 1
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
