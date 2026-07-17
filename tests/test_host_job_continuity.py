from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.host_bridge import HostJobManager
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient


def _wait_status(manager: HostJobManager, job_id: str, terminal: set[str]) -> dict[str, object]:
    deadline = time.monotonic() + 5
    snapshot = manager.status(job_id)
    while snapshot["status"] not in terminal and time.monotonic() < deadline:
        time.sleep(0.03)
        snapshot = manager.status(job_id)
    return snapshot


def _worker_script(path: Path, sleep_seconds: float) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'thread_id':'early-session','usage':{'input_tokens':7}}), flush=True)\n"
        f"time.sleep({sleep_seconds!r})\n"
        "print(json.dumps({'usage':{'input_tokens':7,'output_tokens':3}}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_host_job_persists_pid_session_and_is_idempotent() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "fake-codex"
        _worker_script(executable, 0.25)
        manager = HostJobManager(root / "jobs", (root,))
        job_id = str(uuid.uuid4())
        payload = {
            "job_id": job_id, "adapter": "codex", "executable": str(executable),
            "project_path": str(root), "prompt": "bounded work", "timeout_seconds": 10,
        }
        started = manager.start(payload)
        duplicate = manager.start(payload)
        assert started["pid"] == duplicate["pid"]
        deadline = time.monotonic() + 2
        snapshot = manager.status(job_id)
        while snapshot["session_id"] != "early-session" and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = manager.status(job_id)
        assert snapshot["pid"] > 0
        assert snapshot["session_id"] == "early-session"
        completed = _wait_status(manager, job_id, {"completed"})
        assert completed["returncode"] == 0
        assert completed["input_tokens"] == 7
        assert completed["output_tokens"] == 3
        record = (root / "jobs" / f"{job_id}.json").read_text(encoding="utf-8")
        assert "bounded work" not in record


def test_bridge_restart_identifies_orphan_without_duplicate_and_can_cancel() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "fake-codex"
        _worker_script(executable, 10)
        first = HostJobManager(root / "jobs", (root,))
        job_id = str(uuid.uuid4())
        payload = {
            "job_id": job_id, "adapter": "codex", "executable": str(executable),
            "project_path": str(root), "prompt": "long work", "timeout_seconds": 20,
        }
        started = first.start(payload)
        second = HostJobManager(root / "jobs", (root,))
        recovered = second.start(payload)
        assert recovered["pid"] == started["pid"]
        assert recovered["status"] == "orphan_running"
        second.cancel(job_id)
        cancelled = _wait_status(second, job_id, {"cancelled"})
        assert cancelled["status"] == "cancelled"


class FakeAsyncBridge:
    token = "configured-for-test"

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, object]] = {}
        self.starts = 0

    def start_job(self, *, job_id: str, **_kwargs: object) -> dict[str, object]:
        self.starts += 1
        return self.snapshots.setdefault(job_id, {
            "job_id": job_id, "status": "running", "pid": 4242,
            "session_id": "cli-session-1", "heartbeat_at": "2026-07-17T00:00:00+00:00",
        })

    def job_status(self, job_id: str) -> dict[str, object]:
        return self.snapshots[job_id]

    def cancel_job(self, job_id: str) -> dict[str, object]:
        self.snapshots[job_id] = {
            **self.snapshots[job_id], "status": "cancelled",
            "returncode": 130, "failure_class": "cancelled",
        }
        return self.snapshots[job_id]

    result = staticmethod(HostBridgeClient.result)


def _codex_task(app: object, root: Path, key: str):
    project = app.state.project_repository.create(
        name=key, path=str(root), host_path=str(root)
    )
    role_id = app.state.project_repository.resolve_role(
        project["id"], "fullstack"
    )["role_id"]
    return app.state.task_repository.create(
        title=key, objective="perform bounded work", project_path=str(root),
        project_id=project["id"], role_id=role_id, provider="codex",
        command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2, token_budget=100, idempotency_key=key,
    )


def test_container_reconciles_completion_and_retains_active_lease() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "continuity-complete")
        running = app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-complete"
        )
        assert running.status.value == "running"
        job = app.state.host_jobs.active()[0]
        assert job["host_pid"] == 4242
        assert job["external_session_id"] == "cli-session-1"
        assert bridge.starts == 1

        connection = app.state.database.connect()
        try:
            connection.execute(
                "UPDATE task_leases SET expires_at = datetime('now', '-1 second') WHERE task_id = ?",
                (task.id,),
            )
            connection.commit()
        finally:
            connection.close()
        assert app.state.recovery.reconcile()["recovered_tasks"] == []

        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "completed",
            "returncode": 0, "stdout": "", "stderr": "", "duration_ms": 12,
            "input_tokens": 5, "output_tokens": 2,
        }
        result = app.state.task_service.reconcile_host_jobs()
        assert result["settled"] == [{"task_id": task.id, "status": "completed"}]
        assert app.state.task_repository.get(task.id).status.value == "completed"
        connection = app.state.database.connect()
        try:
            connection.execute(
                "UPDATE host_jobs SET consumed_at = NULL WHERE job_id = ?", (job["job_id"],)
            )
            connection.commit()
        finally:
            connection.close()
        app.state.task_service.reconcile_host_jobs()
        connection = app.state.database.connect()
        try:
            usage_count = connection.execute(
                "SELECT COUNT(*) FROM token_usage WHERE run_id = ?", (job["run_id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        assert usage_count == 1


def test_interrupted_host_job_reuses_session_without_spending_attempt() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "continuity-interrupted")
        running = app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-interrupted"
        )
        job = app.state.host_jobs.active()[0]
        app.state.host_jobs.hold(job["job_id"], "bridge unavailable")
        assert any(
            event["event_type"] == "host_job.recovery_hold"
            for event in app.state.task_repository.events(task.id)
        )
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "interrupted",
            "returncode": 125, "failure_class": "external_interruption",
        }
        app.state.task_service.reconcile_host_jobs()
        resumed = app.state.task_repository.get(task.id)
        assert resumed.status.value == "ready"
        assert resumed.attempts_used == 0
        continuation = app.state.context_compiler.compile(task.id)["content"]
        assert "previous host process was externally interrupted" in continuation
        connection = app.state.database.connect()
        try:
            worker = connection.execute(
                "SELECT external_session_id, last_error FROM workers WHERE id = ?",
                (running.worker_id,),
            ).fetchone()
        finally:
            connection.close()
        assert worker["external_session_id"] == "cli-session-1"
        assert worker["last_error"] == "external_execution_interrupted"


def test_running_cancel_waits_for_host_confirmation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "continuity-cancel")
        running = app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-cancel"
        )
        stopping = app.state.task_service.control(
            task.id, action="cancel", reason="operator request",
            expected_revision=running.revision, idempotency_key="cancel-running",
        )
        assert stopping.status.value == "stopping"
        app.state.task_service.reconcile_host_jobs()
        assert app.state.task_repository.get(task.id).status.value == "cancelled"
