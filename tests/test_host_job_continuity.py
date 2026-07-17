from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import uuid
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import (
    BudgetExceededError,
    ProviderUnavailableError,
    ResourceBusyError,
)
from plow_whip_web.host_bridge import (
    HostJobManager,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_TAIL_BYTES,
    inspect_artifacts,
    open_artifact,
    verify,
)
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.runtime.verification import VerificationEngine
from plow_whip_web.store.task_repository import (
    EXECUTION_DEADLINE_GRACE_SECONDS,
    task_lease_seconds,
)


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


def test_host_job_output_rotates_redacted_segments_and_survives_restart() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        executable = root / "large-output-codex"
        secret = "sk-test-secret-1234567890"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdin.read()\n"
            f"secret = {secret!r}\n"
            "for index in range(72):\n"
            "    print(f'{index:03d}|' + ('界' * 3070) + '|' + (secret if index == 35 else 'safe'), flush=True)\n"
            "print('Authorization: Bearer stderr-secret-value', file=sys.stderr, flush=True)\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        manager = HostJobManager(root / "jobs", (root,))
        job_id = str(uuid.uuid4())

        manager.start({
            "job_id": job_id, "adapter": "codex", "executable": str(executable),
            "project_path": str(root), "prompt": "emit output", "timeout_seconds": 10,
        })
        completed = _wait_status(manager, job_id, {"completed"})

        assert completed["returncode"] == 0
        assert len(completed["stdout"].encode("utf-8")) <= MAX_OUTPUT_TAIL_BYTES
        assert len(completed["stderr"].encode("utf-8")) <= MAX_OUTPUT_TAIL_BYTES
        assert completed["output_bytes"]["total"] > MAX_OUTPUT_BYTES * 2
        assert completed["output_ref"] == f"{job_id}/"
        assert secret not in completed["stdout"]
        assert "stderr-secret-value" not in completed["stderr"]

        streams: dict[str, bytes] = {}
        for stream in ("stdout", "stderr"):
            segments = [
                segment for segment in completed["output_segments"]
                if segment["stream"] == stream
            ]
            assert [segment["index"] for segment in segments] == list(range(len(segments)))
            payload = b""
            for segment in segments:
                segment_bytes = (root / "jobs" / segment["ref"]).read_bytes()
                assert len(segment_bytes) == segment["bytes"] <= MAX_OUTPUT_BYTES
                assert hashlib.sha256(segment_bytes).hexdigest() == segment["sha256"]
                payload += segment_bytes
            assert len(payload) == completed["output_bytes"][stream]
            streams[stream] = payload

        expected_stdout = "".join(
            f"{index:03d}|{'界' * 3070}|{'[REDACTED]' if index == 35 else 'safe'}\n"
            for index in range(72)
        ).encode("utf-8")
        assert streams["stdout"] == expected_stdout
        assert streams["stderr"] == b"Authorization: Bearer [REDACTED]\n"
        assert secret.encode() not in streams["stdout"]

        rebuilt = HostJobManager(root / "jobs", (root,)).status(job_id)
        assert rebuilt["output_segments"] == completed["output_segments"]
        assert rebuilt["output_bytes"] == completed["output_bytes"]
        carry = json.loads(
            (root / "jobs" / job_id / "carry-forward.json").read_text(encoding="utf-8")
        )
        assert carry["status"] == "completed"
        assert carry["generation_model_tokens"] == 0
        assert carry["output_segments"] == completed["output_segments"]


def test_host_job_timeout_writes_deterministic_carry_forward() -> None:
    class TimedOutProcess:
        pid = 424242
        stdout = io.StringIO("last valid output\n")
        stderr = io.StringIO("sk-timeout-secret-1234567890\n")
        returncode = -15

        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fake-codex", timeout)
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

    with TemporaryDirectory() as directory:
        root = Path(directory)
        manager = HostJobManager(root / "jobs", (root,))
        job_id = str(uuid.uuid4())
        manager._write({
            "job_id": job_id, "status": "running", "pid": 424242,
            "session_id": "timeout-session", "started_at": "fixed",
            "stdout": "", "stderr": "", "input_tokens": 7, "output_tokens": 3,
            "cancel_requested": False,
        })

        with patch("plow_whip_web.host_bridge._terminate_process"):
            manager._monitor(job_id, TimedOutProcess(), time.monotonic(), 0)

        snapshot = manager.status(job_id)
        carry_path = root / "jobs" / job_id / "carry-forward.json"
        first = carry_path.read_bytes()
        carry = json.loads(first)
        manager._write(snapshot)

        assert snapshot["failure_class"] == "timeout"
        assert carry_path.read_bytes() == first
        assert carry == {
            "failure_class": "timeout",
            "generation_model_tokens": 0,
            "last_valid_output": {
                "stderr": "[REDACTED]\n", "stdout": "last valid output\n",
            },
            "output_bytes": snapshot["output_bytes"],
            "output_segments": snapshot["output_segments"],
            "session_id": "timeout-session",
            "status": "completed",
            "tokens": {
                "input": 7,
                "cached_input": 0,
                "cached_input_in_total": True,
                "output": 3,
                "total": 10,
            },
        }


def test_host_job_repository_keeps_only_output_metadata_in_sqlite() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "bounded-result-json")
        app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-bounded-json"
        )
        job = app.state.host_jobs.active()[0]
        full_output = "old-output-that-must-not-remain\n" + ("x" * (MAX_OUTPUT_BYTES * 3))
        snapshot = {
            **bridge.snapshots[job["job_id"]], "status": "completed", "returncode": 0,
            "stdout": full_output, "stderr": "", "output_ref": f"{job['job_id']}/",
            "output_segments": [{
                "stream": "stdout", "index": 0, "ref": f"{job['job_id']}/stdout.000000.log",
                "bytes": len(full_output.encode()), "sha256": hashlib.sha256(full_output.encode()).hexdigest(),
                "content": full_output,
            }],
            "output_bytes": {"stdout": len(full_output.encode()), "stderr": 0, "total": len(full_output.encode())},
        }

        app.state.host_jobs.record(job["job_id"], snapshot)
        connection = app.state.database.connect()
        try:
            stored_text = connection.execute(
                "SELECT result_json FROM host_jobs WHERE job_id = ?", (job["job_id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        stored = json.loads(stored_text)

        assert len(stored_text.encode("utf-8")) < 32_768
        assert {"stdout", "stderr", "prompt", "prompt_text"}.isdisjoint(stored)
        assert "old-output-that-must-not-remain" not in stored_text
        assert stored["output_ref"] == snapshot["output_ref"]
        assert "content" not in stored["output_segments"][0]
        assert stored["output_segments"][0] == {
            key: value for key, value in snapshot["output_segments"][0].items()
            if key != "content"
        }
        assert stored["output_bytes"] == snapshot["output_bytes"]
        assert stored["stdout_len"] == snapshot["output_bytes"]["stdout"]
        assert stored["stderr_len"] == snapshot["output_bytes"]["stderr"]
        assert stored["error_summary"] is None


class FakeAsyncBridge:
    token = "configured-for-test"

    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, object]] = {}
        self.starts = 0
        self.verify_calls = 0
        self.start_sessions: list[str | None] = []
        self.start_timeouts: list[int] = []
        self.verify_error: str | None = None

    def start_job(
        self, *, job_id: str, session_id: str | None = None, timeout_seconds: int = 0,
        **_kwargs: object
    ) -> dict[str, object]:
        self.starts += 1
        self.start_sessions.append(session_id)
        self.start_timeouts.append(timeout_seconds)
        return self.snapshots.setdefault(job_id, {
            "job_id": job_id, "status": "running", "pid": 4242,
            "session_id": session_id or "cli-session-1",
            "heartbeat_at": "2026-07-17T00:00:00+00:00",
        })

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "available for test"

    def job_status(self, job_id: str) -> dict[str, object]:
        return self.snapshots[job_id]

    def cancel_job(self, job_id: str) -> dict[str, object]:
        self.snapshots[job_id] = {
            **self.snapshots[job_id], "status": "cancelled",
            "returncode": 130, "failure_class": "cancelled",
        }
        return self.snapshots[job_id]

    def verify(
        self, *, project_path: str, execution: ExecutionResult,
        verification: list[dict[str, object]],
    ):
        self.verify_calls += 1
        if self.verify_error:
            raise ProviderUnavailableError(self.verify_error)
        return VerificationEngine().verify(Path(project_path), execution, verification)

    result = staticmethod(HostBridgeClient.result)


def _codex_task(app: object, root: Path, key: str, *, token_budget: int = 100):
    project_path = root / key
    project_path.mkdir()
    project = app.state.project_repository.create(
        name=key, path=str(project_path), host_path=str(project_path)
    )
    role_id = app.state.project_repository.resolve_role(
        project["id"], "fullstack"
    )["role_id"]
    return app.state.task_repository.create(
        title=key, objective="perform bounded work", project_path=str(project_path),
        project_id=project["id"], role_id=role_id, provider="codex",
        command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2, token_budget=token_budget, idempotency_key=key,
    )


def _estimated_budget_m() -> tuple[dict[str, object], dict[str, object]]:
    preview = estimate_task_sizing(TaskSizingInputs(
        layers_touched=2,
        components_touched=3,
        estimated_files_changed=5,
        has_migration=True,
        has_deploy=False,
        verification_commands_count=3,
        estimated_verification_seconds=120,
        external_dependencies_count=1,
        risk_level="medium",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    sizing = {
        "status": preview["status"],
        "size_class": preview["size_class"],
        "rationale": preview["rationale"],
        "estimated_input_tokens": preview["estimated_input_tokens"],
        "estimated_output_tokens": preview["estimated_output_tokens"],
        "bootstrap_version": preview["bootstrap_version"],
    }
    execution_budget = {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
        "total_token_hard_cap": preview["total_token_hard_cap"],
        "reserved_tokens": preview["reserved_tokens"],
    }
    return sizing, execution_budget


def _estimated_codex_task(app: object, root: Path, key: str):
    project_path = root / key
    project_path.mkdir()
    project = app.state.project_repository.create(
        name=key, path=str(project_path), host_path=str(project_path)
    )
    role_id = app.state.project_repository.resolve_role(
        project["id"], "fullstack"
    )["role_id"]
    sizing, execution_budget = _estimated_budget_m()
    return app.state.task_repository.create(
        title=key, objective="perform bounded work", project_path=str(project_path),
        project_id=project["id"], role_id=role_id, provider="codex",
        command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=3, token_budget=int(execution_budget["total_token_hard_cap"]),
        idempotency_key=key, sizing=sizing, execution_budget=execution_budget,
    )


def test_estimated_host_dispatch_uses_execution_budget_deadline_and_reservation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _estimated_codex_task(app, root, "estimated-runtime-budget")
        _, execution_budget = _estimated_budget_m()
        assert execution_budget["hard_deadline_seconds"] == 1200
        assert execution_budget["reserved_tokens"] == 150_000

        running = app.state.task_service.drive(
            task.id, expected_revision=task.revision,
            idempotency_key="drive-estimated-runtime-budget",
        )
        assert running.status.value == "running"
        assert bridge.start_timeouts == [1200]

        connection = app.state.database.connect()
        try:
            lease = connection.execute(
                "SELECT expires_at FROM task_leases WHERE task_id = ?",
                (task.id,),
            ).fetchone()
            reservation = connection.execute(
                "SELECT reserved_tokens FROM token_reservations WHERE task_id = ? AND status = 'active'",
                (task.id,),
            ).fetchone()
        finally:
            connection.close()
        assert reservation["reserved_tokens"] == 150_000
        assert task_lease_seconds(running) == 1200 + EXECUTION_DEADLINE_GRACE_SECONDS
        assert lease is not None


def test_legacy_host_dispatch_keeps_command_timeout_and_remaining_reservation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "legacy-runtime-timeout", token_budget=100)

        app.state.task_service.drive(
            task.id, expected_revision=task.revision,
            idempotency_key="drive-legacy-runtime-timeout",
        )

        assert bridge.start_timeouts == [60]
        connection = app.state.database.connect()
        try:
            reservation = connection.execute(
                "SELECT reserved_tokens FROM token_reservations WHERE task_id = ? AND status = 'active'",
                (task.id,),
            ).fetchone()
        finally:
            connection.close()
        assert reservation["reserved_tokens"] == 100


def test_host_task_zero_budget_is_rejected_before_claim_and_dispatch() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "host-zero-budget", token_budget=0)

        with pytest.raises(BudgetExceededError, match="no reservable tokens"):
            app.state.task_service.drive(
                task.id, expected_revision=task.revision,
                idempotency_key="drive-zero-budget",
            )

        unchanged = app.state.task_repository.get(task.id)
        assert unchanged.status.value == "ready"
        assert unchanged.attempts_used == 0
        assert bridge.starts == 0


def test_host_budget_reservation_prevents_global_oversubscription() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        settings = app.state.runtime_settings.get()
        settings["values"]["global_daily_token_budget"] = 150
        app.state.runtime_settings.update(
            settings["values"], expected_revision=settings["revision"]
        )
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        first = _codex_task(app, root, "host-reserve-first")
        second = _codex_task(app, root, "host-reserve-second")

        app.state.task_service.drive(
            first.id, expected_revision=first.revision,
            idempotency_key="drive-reserve-first",
        )
        with pytest.raises(BudgetExceededError, match="global daily"):
            app.state.task_service.drive(
                second.id, expected_revision=second.revision,
                idempotency_key="drive-reserve-second",
            )

        job = app.state.host_jobs.active()[0]
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "completed",
            "returncode": 0, "input_tokens": 12, "output_tokens": 8,
        }
        app.state.task_service.reconcile_host_jobs()
        app.state.task_service.drive(
            second.id, expected_revision=second.revision,
            idempotency_key="drive-reserve-second-after-settle",
        )

        connection = app.state.database.connect()
        try:
            reservations = connection.execute(
                "SELECT status, reserved_tokens, actual_tokens FROM token_reservations ORDER BY created_at, run_id"
            ).fetchall()
        finally:
            connection.close()
        assert {
            row["status"]: (row["reserved_tokens"], row["actual_tokens"])
            for row in reservations
        } == {
            "settled": (100, 20),
            "active": (100, None),
        }


def test_recovery_releases_reservation_if_claim_crashes_before_host_job() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        task = _codex_task(app, root, "reservation-recovery")
        claim = app.state.task_repository.claim(
            task.id, expected_revision=task.revision,
            idempotency_key="claim-before-crash",
            reserved_tokens=100,
        )
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE task_leases SET expires_at = datetime('now', '-1 second') WHERE task_id = ?",
                (task.id,),
            )

        result = app.state.recovery.reconcile()

        assert result["recovered_tasks"] == [task.id]
        assert app.state.task_repository.get(task.id).status.value == "ready"
        connection = app.state.database.connect()
        try:
            reservation = connection.execute(
                "SELECT status FROM token_reservations WHERE run_id = ?",
                (claim.run_id,),
            ).fetchone()
        finally:
            connection.close()
        assert reservation["status"] == "released"


def test_scheduler_parallel_limit_counts_existing_host_jobs_across_ticks() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        settings = app.state.runtime_settings.get()
        settings["values"]["max_parallel_workers"] = 1
        app.state.runtime_settings.update(
            settings["values"], expected_revision=settings["revision"]
        )
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        _codex_task(app, root, "parallel-first")
        _codex_task(app, root, "parallel-second")

        first_tick = app.state.scheduler_service.tick(owner="parallel-limit")
        second_tick = app.state.scheduler_service.tick(owner="parallel-limit")

        assert first_tick["selected"] == 1
        assert second_tick["active"] == 1
        assert second_tick["available_slots"] == 0
        assert second_tick["selected"] == 0
        assert bridge.starts == 1
        assert app.state.task_repository.in_flight_count() == 1
        ready = app.state.task_repository.list_ready()[0]
        with pytest.raises(ResourceBusyError, match="parallel worker limit"):
            app.state.task_service.drive(
                ready.id, expected_revision=ready.revision,
                idempotency_key="manual-drive-over-limit",
            )
        assert bridge.starts == 1


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


def test_host_verification_uses_host_project_path() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        container_project = root / "container-project"
        host_project = root / "host-project"
        container_project.mkdir()
        host_project.mkdir()
        (host_project / "报告.md").write_text("项目设计思维", encoding="utf-8")
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        project = app.state.project_repository.create(
            name="split-paths", path=str(container_project), host_path=str(host_project)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "verification"
        )["role_id"]
        task = app.state.task_repository.create(
            title="host-artifact", objective="write report on host",
            project_path=str(container_project), project_id=project["id"],
            role_id=role_id, provider="codex",
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
            verification=[
                {"kind": "exit_code", "expected": 0},
                {"kind": "file_exists", "path": "报告.md"},
                {"kind": "file_contains", "path": "报告.md", "contains": "项目设计思维"},
            ],
            max_attempts=1, token_budget=100, idempotency_key="host-artifact",
            quality_profile="strict",
        )
        app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-host-artifact"
        )
        job = app.state.host_jobs.active()[0]
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "completed",
            "returncode": 0, "stdout": "", "stderr": "", "duration_ms": 12,
            "input_tokens": 5, "output_tokens": 2,
        }
        bridge.verify_error = "bridge verification temporarily unavailable"

        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == []
        assert result["active"] == 1
        assert app.state.task_repository.get(task.id).status.value == "running"
        assert app.state.host_jobs.active()[0]["status"] == "recovery_hold"
        assert bridge.verify_calls == 1

        bridge.verify_error = None
        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == [{"task_id": task.id, "status": "completed"}]
        completed = app.state.task_repository.get(task.id)
        assert completed.status.value == "completed"
        assert completed.last_error is None
        assert bridge.verify_calls == 2


def test_host_bridge_verification_accepts_host_artifact() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / "报告.md").write_text("项目设计思维", encoding="utf-8")
        payload = {
            "project_path": str(project),
            "execution": {"returncode": 0},
            "verification": [
                {"kind": "exit_code", "expected": 0},
                {"kind": "file_exists", "path": "报告.md"},
                {"kind": "file_contains", "path": "报告.md", "contains": "项目设计思维"},
            ],
        }

        result = verify(payload, (root.resolve(),))

        assert result["passed"] is True
        assert len(result["checks"]) == 3
        artifact = result["checks"][1]["artifact"]
        assert artifact["sha256"] == hashlib.sha256(
            "项目设计思维".encode("utf-8")
        ).hexdigest()
        assert artifact["bytes"] == len("项目设计思维".encode("utf-8"))
        assert artifact["modified_at_ns"] > 0


def test_evidence_hash_changes_with_verified_artifact_content() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "result.txt"
        artifact.write_text("first", encoding="utf-8")
        execution = ExecutionResult(0, "", "", 1)
        specs = [{"kind": "file_contains", "path": "result.txt", "contains": "first"}]

        first = VerificationEngine().verify(root, execution, specs)
        artifact.write_text("first plus second", encoding="utf-8")
        second = VerificationEngine().verify(root, execution, specs)

        assert first.passed is True
        assert second.passed is True
        assert first.evidence_hash != second.evidence_hash
        assert first.checks[0]["artifact"]["bytes"] == 5
        assert second.checks[0]["artifact"]["bytes"] == 17


def test_host_bridge_artifact_index_stays_inside_host_project() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        report = project / "报告.md"
        report.write_text("主机目录中的报告", encoding="utf-8")

        result = inspect_artifacts({
            "project_path": str(project),
            "paths": ["报告.md"],
        }, (root.resolve(),))

        artifact = result["artifacts"][0]
        assert artifact["host_path"] == str(report.resolve())
        assert artifact["exists"] is True
        assert artifact["bytes"] == len("主机目录中的报告".encode("utf-8"))
        assert len(artifact["sha256"]) == 64
        assert "finder" in artifact["actions"]

        try:
            inspect_artifacts({
                "project_path": str(project),
                "paths": ["../outside.md"],
            }, (root.resolve(),))
        except ValueError as error:
            assert "escapes project root" in str(error)
        else:
            raise AssertionError("artifact path escape must be rejected")


def test_host_bridge_artifact_open_uses_fixed_argv_without_shell() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        report = project / "报告.md"
        report.write_text("ready", encoding="utf-8")

        with (
            patch("plow_whip_web.host_bridge._resolve_executable", return_value="/usr/local/bin/cursor"),
            patch("plow_whip_web.host_bridge.subprocess.Popen") as popen,
        ):
            popen.return_value.pid = 4242
            result = open_artifact({
                "project_path": str(project),
                "relative_path": "报告.md",
                "action": "cursor",
            }, (root.resolve(),))

        assert result["status"] == "opened"
        assert result["host_path"] == str(report.resolve())
        assert popen.call_args.args[0] == ["/usr/local/bin/cursor", str(report.resolve())]
        assert "shell" not in popen.call_args.kwargs


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
            "input_tokens": 4, "output_tokens": 1,
        }
        app.state.task_service.reconcile_host_jobs()
        resumed = app.state.task_repository.get(task.id)
        assert resumed.status.value == "ready"
        assert resumed.attempts_used == 0
        assert resumed.tokens_used == 5
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
        connection = app.state.database.connect()
        try:
            reservation = connection.execute(
                "SELECT status, actual_tokens FROM token_reservations WHERE run_id = ?",
                (job["run_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert (reservation["status"], reservation["actual_tokens"]) == ("settled", 5)


def test_timeout_completed_snapshot_resumes_same_session_without_verification() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "continuity-timeout")
        running = app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-timeout"
        )
        job = app.state.host_jobs.active()[0]
        assert running.status.value == "running"
        assert bridge.starts == 1
        assert bridge.verify_calls == 0

        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "completed",
            "returncode": 124, "failure_class": "timeout",
            "session_id": "timeout-session",
            "input_tokens": 7, "output_tokens": 3,
        }
        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == [{"task_id": task.id, "status": "ready"}]
        assert bridge.verify_calls == 0
        assert app.state.host_jobs.active() == []
        resumed = app.state.task_repository.get(task.id)
        assert resumed.status.value == "ready"
        assert resumed.attempts_used == 0
        assert resumed.tokens_used == 10
        connection = app.state.database.connect()
        try:
            worker = connection.execute(
                "SELECT external_session_id, last_error FROM workers WHERE id = ?",
                (running.worker_id,),
            ).fetchone()
            reservation = connection.execute(
                "SELECT status, actual_tokens FROM token_reservations WHERE run_id = ?",
                (job["run_id"],),
            ).fetchone()
            usage_count = connection.execute(
                "SELECT COUNT(*) FROM token_usage WHERE run_id = ?", (job["run_id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        assert worker["external_session_id"] == "timeout-session"
        assert worker["last_error"] == "external_execution_interrupted"
        assert (reservation["status"], reservation["actual_tokens"]) == ("settled", 10)
        assert usage_count == 1

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
            replay_usage = connection.execute(
                "SELECT COUNT(*) FROM token_usage WHERE run_id = ?", (job["run_id"],)
            ).fetchone()[0]
            replay_reservation = connection.execute(
                "SELECT status, actual_tokens FROM token_reservations WHERE run_id = ?",
                (job["run_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert replay_usage == 1
        assert (replay_reservation["status"], replay_reservation["actual_tokens"]) == ("settled", 10)
        assert bridge.verify_calls == 0

        continuation = app.state.task_service.drive(
            task.id, expected_revision=resumed.revision,
            idempotency_key="drive-timeout-resume",
        )
        assert continuation.status.value == "running"
        assert bridge.starts == 2
        assert bridge.start_sessions[-1] == "timeout-session"
        assert bridge.verify_calls == 0


def test_command_failed_completed_still_runs_verification_path() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        bridge = FakeAsyncBridge()
        app.state.provider_pool.bridge = bridge
        task = _codex_task(app, root, "continuity-command-failed", token_budget=100)
        app.state.task_service.drive(
            task.id, expected_revision=task.revision, idempotency_key="drive-command-failed"
        )
        job = app.state.host_jobs.active()[0]
        bridge.snapshots[job["job_id"]] = {
            **bridge.snapshots[job["job_id"]], "status": "completed",
            "returncode": 1, "failure_class": "command_failed",
            "stdout": "command error", "stderr": "", "input_tokens": 2, "output_tokens": 1,
        }

        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == [{"task_id": task.id, "status": "ready"}]
        assert bridge.verify_calls >= 1
        failed = app.state.task_repository.get(task.id)
        assert failed.status.value == "ready"
        assert failed.attempts_used == 1
        assert failed.last_error is not None


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
