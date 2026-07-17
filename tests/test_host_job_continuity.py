from __future__ import annotations

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
    inspect_artifacts,
    open_artifact,
    verify,
)
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.verification import VerificationEngine


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
        self.verify_error: str | None = None

    def start_job(self, *, job_id: str, **_kwargs: object) -> dict[str, object]:
        self.starts += 1
        return self.snapshots.setdefault(job_id, {
            "job_id": job_id, "status": "running", "pid": 4242,
            "session_id": "cli-session-1", "heartbeat_at": "2026-07-17T00:00:00+00:00",
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

        bridge.verify_error = None
        result = app.state.task_service.reconcile_host_jobs()

        assert result["settled"] == [{"task_id": task.id, "status": "completed"}]
        completed = app.state.task_repository.get(task.id)
        assert completed.status.value == "completed"
        assert completed.last_error is None


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
