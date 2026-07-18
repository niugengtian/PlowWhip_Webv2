from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import HostBridgeRejectedError, ProviderUnavailableError
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.host_reconciliation import (
    dispatch_outcome,
    reconciliation_deadline_modifier,
    requires_reconciliation,
)
from plow_whip_web.runtime.verification import VerificationEngine


class BridgeFixture:
    token = "configured-for-test"

    def __init__(self, *, unknown: bool = False, rejected: bool = False) -> None:
        self.unknown = unknown
        self.rejected = rejected
        self.snapshots: dict[str, dict[str, object]] = {}
        self.last_output_request: dict[str, object] = {}

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "available"

    def start_job(self, *, job_id: str, **_kwargs: object) -> dict[str, object]:
        if self.rejected:
            raise HostBridgeRejectedError("invalid dispatch contract")
        if self.unknown:
            raise ProviderUnavailableError("dispatch response lost")
        return self.snapshots.setdefault(job_id, {
            "job_id": job_id,
            "status": "running",
            "pid": 4242,
            "session_id": "canonical-session",
            "heartbeat_at": "2026-07-18T00:00:00Z",
        })

    def job_status(self, job_id: str) -> dict[str, object]:
        if self.unknown:
            raise ProviderUnavailableError("bridge unavailable during reconciliation")
        return self.snapshots[job_id]

    def job_output(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.last_output_request = kwargs
        return {
            "job_id": job_id,
            "chunks": [{
                "kind": "stdout",
                "stream": "stdout",
                "offset": 0,
                "next_offset": 38,
                "text": "progress\nAuthorization: Bearer secret-value",
                "refs": [f"{job_id}/stdout.000000.log"],
            }],
            "next_offsets": {"stdout": 38, "stderr": 0},
            "has_more": False,
        }

    def execute(self, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            returncode=0,
            stdout="- refined and verifiable",
            stderr="",
            duration_ms=1,
            input_tokens=11,
            cached_input_tokens=3,
            output_tokens=5,
            external_session_id="refine-session",
        )

    def verify(
        self, *, project_path: str, execution: ExecutionResult,
        verification: list[dict[str, object]],
    ):
        return VerificationEngine().verify(
            Path(project_path), execution, verification
        )

    result = staticmethod(HostBridgeClient.result)


def _host_runtime(root: Path, *, unknown: bool = False, rejected: bool = False):
    app = create_app(Settings(
        data_dir=root / "runtime",
        host_bridge_token="test-token-is-long-enough-123",
    ))
    bridge = BridgeFixture(unknown=unknown, rejected=rejected)
    app.state.provider_pool.bridge = bridge
    project_path = root / "project"
    project_path.mkdir()
    project = app.state.project_repository.create(
        name="host", path=str(project_path), host_path=str(project_path)
    )
    role_id = app.state.project_repository.resolve_role(
        project["id"], "fullstack"
    )["role_id"]
    task = app.state.task_repository.create(
        title="host task",
        objective="show real progress",
        project_path=str(project_path),
        project_id=project["id"],
        role_id=role_id,
        provider="codex",
        command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2,
        idempotency_key="host-task-create",
    )
    running = app.state.task_service.drive(
        task.id, expected_revision=task.revision,
        idempotency_key="host-task-drive",
    )
    connection = app.state.database.connect()
    try:
        job = dict(connection.execute(
            "SELECT * FROM host_jobs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task.id,),
        ).fetchone())
    finally:
        connection.close()
    return app, bridge, running, job


def test_unknown_dispatch_deadline_is_deterministic_and_never_redispatched() -> None:
    with TemporaryDirectory() as directory:
        app, _bridge, running, job = _host_runtime(Path(directory), unknown=True)
        assert job["dispatch_outcome"] == "unknown"
        assert job["reconciliation_deadline_at"]
        connection = app.state.database.connect()
        try:
            connection.execute(
                """
                UPDATE host_jobs
                SET reconciliation_deadline_at = datetime('now', '-1 second')
                WHERE job_id = ?
                """,
                (job["job_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        result = app.state.task_service.reconcile_host_jobs()
        current = app.state.task_repository.get(running.id)
        assert result["settled"] == [{"task_id": running.id, "status": "needs_human"}]
        assert current.status.value == "needs_human"
        assert current.last_error == "dispatch_reconciliation_deadline_exceeded"
        assert app.state.host_jobs.active() == []
        connection = app.state.database.connect()
        try:
            assert connection.execute(
                "SELECT COUNT(*) count FROM host_jobs WHERE task_id = ?",
                (running.id,),
            ).fetchone()["count"] == 1
            call = connection.execute(
                "SELECT status, error_class FROM model_calls WHERE call_id = ?",
                (job["run_id"],),
            ).fetchone()
            assert tuple(call) == ("failed", "dispatch_reconciliation_timeout")
        finally:
            connection.close()


def test_host_reconciliation_reduces_to_three_states_and_bounds_recovery_hold() -> None:
    assert dispatch_outcome("running", host_pid=42) == "accepted"
    assert dispatch_outcome("rejected") == "rejected"
    assert dispatch_outcome("unknown") == "unknown"
    assert dispatch_outcome("recovery_hold", host_pid=42) == "accepted"
    assert requires_reconciliation("accepted", "recovery_hold")
    assert not requires_reconciliation("accepted", "running")
    assert reconciliation_deadline_modifier() == "+120 seconds"


def test_rejected_dispatch_is_terminal_fact_not_unknown() -> None:
    with TemporaryDirectory() as directory:
        app, _bridge, task, job = _host_runtime(Path(directory), rejected=True)
        assert task.status.value == "needs_human"
        assert job["dispatch_outcome"] == "rejected"
        assert job["dispatch_decided_at"]
        assert job["consumed_at"]
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                "SELECT status, error_class FROM model_calls WHERE call_id = ?",
                (job["run_id"],),
            ).fetchone()
            assert tuple(call) == ("failed", "dispatch_rejected")
        finally:
            connection.close()


def test_reconciliation_recreates_missing_executor_receipt_from_host_job() -> None:
    with TemporaryDirectory() as directory:
        app, bridge, running, job = _host_runtime(Path(directory))
        connection = app.state.database.connect()
        try:
            connection.execute(
                "DELETE FROM model_calls WHERE call_id = ?",
                (job["run_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 0,
            "input_tokens": 23,
            "cached_input_tokens": 17,
            "output_tokens": 5,
        }

        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == [{
            "task_id": running.id,
            "status": "completed",
        }]
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                """
                SELECT idempotency_key, host_job_id, status, input_tokens,
                       cached_input_tokens, output_tokens
                FROM model_calls WHERE call_id = ?
                """,
                (job["run_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert tuple(call) == (
            f"task-run:{job['run_id']}",
            job["job_id"],
            "completed",
            23,
            17,
            5,
        )


def test_missing_pre_execution_baseline_reschedules_a_fresh_run() -> None:
    with TemporaryDirectory() as directory:
        app, bridge, running, job = _host_runtime(Path(directory))
        connection = app.state.database.connect()
        try:
            connection.execute(
                "INSERT INTO task_deletion_permits(task_id) VALUES (?)",
                (running.id,),
            )
            connection.execute(
                "DELETE FROM run_evidence_baselines WHERE run_id = ?",
                (job["run_id"],),
            )
            connection.execute(
                "DELETE FROM task_deletion_permits WHERE task_id = ?",
                (running.id,),
            )
            connection.commit()
        finally:
            connection.close()
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]],
            "status": "completed",
            "returncode": 0,
            "input_tokens": 31,
            "cached_input_tokens": 29,
            "output_tokens": 7,
        }

        result = app.state.task_service.reconcile_host_jobs()
        rescheduled = app.state.task_repository.get(running.id)

        assert result["settled"] == [{
            "task_id": running.id,
            "status": "ready",
        }]
        assert rescheduled.status.value == "ready"
        assert rescheduled.attempts_used == 0
        assert rescheduled.last_error == "evidence_baseline_missing_requires_fresh_run"
        assert app.state.host_jobs.active() == []

        next_run = app.state.task_service.drive(
            rescheduled.id,
            expected_revision=rescheduled.revision,
            idempotency_key="fresh-run-after-missing-baseline",
        )
        assert next_run.status.value == "running"
        connection = app.state.database.connect()
        try:
            jobs = connection.execute(
                "SELECT run_id FROM host_jobs WHERE task_id = ?",
                (running.id,),
            ).fetchall()
            fresh_run_id = next(
                item["run_id"]
                for item in jobs
                if item["run_id"] != job["run_id"]
            )
            baseline = connection.execute(
                "SELECT run_id FROM run_evidence_baselines WHERE run_id = ?",
                (fresh_run_id,),
            ).fetchone()
            usage = connection.execute(
                """
                SELECT input_tokens, cached_input_tokens, output_tokens
                FROM token_usage WHERE call_id = ?
                """,
                (job["run_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert len(jobs) == 2
        assert baseline["run_id"] == fresh_run_id
        assert tuple(usage) == (31, 29, 7)


def test_accepted_job_recovery_hold_deadline_is_not_extended() -> None:
    with TemporaryDirectory() as directory:
        app, bridge, running, job = _host_runtime(Path(directory))
        assert job["dispatch_outcome"] == "accepted"
        app.state.host_jobs.hold(job["job_id"], "bridge disconnected")
        first = app.state.host_jobs.active()[0]["reconciliation_deadline_at"]
        app.state.host_jobs.hold(job["job_id"], "bridge still disconnected")
        second = app.state.host_jobs.active()[0]["reconciliation_deadline_at"]
        assert first == second
        app.state.host_jobs.renew(job["job_id"], seconds=86_400)
        connection = app.state.database.connect()
        try:
            lease = connection.execute(
                "SELECT expires_at FROM task_leases WHERE task_id = ?",
                (running.id,),
            ).fetchone()
            assert lease["expires_at"] <= first
            connection.execute(
                """
                UPDATE host_jobs
                SET reconciliation_deadline_at = datetime('now', '-1 second')
                WHERE job_id = ?
                """,
                (job["job_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        bridge.unknown = True

        app.state.task_service.reconcile_host_jobs()
        assert app.state.task_repository.get(running.id).status.value == "needs_human"
        assert app.state.host_jobs.active() == []


def test_worker_detail_exposes_identity_and_bounded_redacted_cursor_stream() -> None:
    with TemporaryDirectory() as directory:
        app, bridge, running, job = _host_runtime(Path(directory))
        assert running.worker_id
        with TestClient(app) as client:
            detail = client.get(f"/api/workers/{running.worker_id}").json()
            stream = client.get(
                f"/api/workers/{running.worker_id}/stream?cursor=0:0:0&limit=4096"
            ).json()
            sse = client.get("/api/events/stream?once=true").text

        assert detail["task"]["status"] == "running"
        assert detail["host_job"]["job_id"] == job["job_id"]
        assert detail["host_job"]["dispatch_outcome"] == "accepted"
        assert detail["ownership"]["external_session_id"] == "canonical-session"
        assert stream["job_id"] == job["job_id"]
        assert stream["next_cursor"].count(":") == 2
        assert bridge.last_output_request == {
            "stdout_offset": -1,
            "stderr_offset": -1,
            "limit": 4096,
            "tail_lines": 20,
        }
        text = "\n".join(item["text"] for item in stream["items"])
        assert "secret-value" not in text
        assert "[REDACTED]" in text
        assert all(item["kind"] in {"stdout", "stderr", "tool", "status"} for item in stream["items"])
        assert "event: aggregate.updated" in sse
        assert '"revision":' in sse


def test_all_component_calls_have_idempotent_receipts_and_dimension_lists() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        app.state.provider_pool.bridge = BridgeFixture()
        with TestClient(app) as client:
            project = client.post("/api/projects", json={
                "name": "ledger", "path": str(project_path),
                "host_path": str(project_path),
            }).json()
            goal_payload = {
                "title": "ledger goal",
                "objective": "record every component call",
                "project_id": project["id"],
                "provider": "generic-command",
                "sizing_inputs": {
                    "layers_touched": 1, "components_touched": 1,
                    "estimated_files_changed": 1, "has_migration": False,
                    "has_deploy": False, "verification_commands_count": 1,
                    "estimated_verification_seconds": 1,
                    "external_dependencies_count": 0, "risk_level": "low",
                    "independent_review_required": False,
                    "gate_artifact": True, "gate_boundary": True,
                    "gate_verification": True, "gate_dependency": True,
                },
                "command": {"argv": [sys.executable, "-c", "print('ok')"]},
                "verification": [{"kind": "exit_code", "expected": 0}],
            }
            first = client.post(
                "/api/goals", headers={"Idempotency-Key": "ledger-goal-create"},
                json=goal_payload,
            )
            duplicate = client.post(
                "/api/goals", headers={"Idempotency-Key": "ledger-goal-create"},
                json=goal_payload,
            )
            assert first.status_code == duplicate.status_code == 201
            task = app.state.task_repository.list_ready()[0]
            completed = app.state.task_service.drive(
                task.id, expected_revision=task.revision,
                idempotency_key="ledger-executor-drive",
            )
            assert completed.status.value == "completed"
            refined = client.post(
                "/api/conventions/global/global/refine",
                headers={"Idempotency-Key": "ledger-convention-refine"},
                json={
                    "provider": "simple-worker", "project_id": project["id"],
                    "instruction": "make it verifiable",
                },
            )
            assert refined.status_code == 200
            usage = client.get("/api/usage").json()

        kinds = {call["call_kind"] for call in usage["calls"]}
        assert kinds >= {
            "executor", "butler_planner", "router", "verifier",
            "convention_refinement",
        }
        assert all(call["status"] in {"completed", "failed"} for call in usage["calls"])
        assert len([call for call in usage["calls"] if call["call_kind"] == "router"]) == 1
        assert usage["total_tokens"] == 16
        for dimension in (
            "projects", "tasks", "workers", "providers", "models",
            "call_kinds", "sessions",
        ):
            assert dimension in usage
