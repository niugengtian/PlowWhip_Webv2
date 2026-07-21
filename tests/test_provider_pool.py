from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.host_bridge import (
    _execution_argv,
    _parse_stream,
    _provider_failure_class,
    _resolve_executable,
    _safe_environment,
    _version_argv,
    probe,
)
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.providers.pool import ProviderPool, _last_text
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.store.task_repository import (
    EXECUTION_DEADLINE_GRACE_SECONDS,
    MAX_HARD_DEADLINE_SECONDS,
    task_hard_deadline_seconds,
    task_lease_seconds,
)


class FakeBridge:
    token = "configured-for-test"

    def execute(self, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            returncode=0,
            stdout="# 质量约束\n\n- 每个完成声明必须附带可复现验证证据。",
            stderr="",
            duration_ms=12,
            input_tokens=21,
            output_tokens=13,
            external_session_id="simple-session-1",
        )


def test_provider_presets_and_revision_guarded_registration() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory)))
        with TestClient(app) as client:
            providers = client.get("/api/providers").json()
            assert {item["name"] for item in providers} >= {
                "generic-command", "codex", "cursor", "simple-worker"
            }
            simple = next(item for item in providers if item["name"] == "simple-worker")
            assert simple["adapter"] == "json-worker"
            assert "refine_convention" in simple["capabilities"]
            assert simple["credential_env"] == "DEEPSEEK_API_KEY"

            created = client.put("/api/providers/local-runner", json={
                "name": "local-runner",
                "display_name": "本机 Runner",
                "adapter": "json-worker",
                "transport": "host-bridge",
                "executable": "local-runner",
                "enabled": True,
                "credential_env": None,
                "capabilities": ["new_session", "resume_session"],
                "expected_revision": 0,
            })
            assert created.status_code == 200
            assert created.json()["revision"] == 1

            conflict = client.put("/api/providers/local-runner", json={
                **created.json(), "expected_revision": 0,
            })
            assert conflict.status_code == 409


def test_provider_switches_only_after_three_consecutive_failed_probes() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        project = app.state.project_repository.create(
            name="three-strikes",
            path=str(project_path),
            host_path=str(project_path),
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "fullstack"
        )["role_id"]
        task = app.state.task_repository.create(
            title="route after threshold",
            objective="do not switch on a single transient probe",
            project_path=str(project_path),
            project_id=project["id"],
            role_id=role_id,
            provider="cursor",
            provider_policy="auto",
            fallback_enabled=True,
            provider_order=["cursor", "codex"],
            command={"argv": [sys.executable, "-c", "pass"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="provider-three-strikes",
        )
        providers = app.state.provider_pool.providers
        providers.record_probe(
            "cursor", available=True, detail="healthy",
            failure_threshold=3,
        )
        providers.record_probe(
            "codex", available=True, detail="healthy",
            failure_threshold=3,
        )

        for failure in (1, 2):
            cursor = providers.record_probe(
                "cursor",
                available=False,
                detail=f"transient failure {failure}",
                failure_threshold=3,
                failure_class="provider_probe_failed",
            )
            assert cursor["status"] == "available"
            assert cursor["circuit_state"] == "closed"
            assert cursor["consecutive_failures"] == failure
            assert app.state.provider_pool.route_task(
                task,
                zone_availability={"domestic": True, "overseas": True},
            )["name"] == "cursor"

        cursor = providers.record_probe(
            "cursor",
            available=False,
            detail="transient failure 3",
            failure_threshold=3,
            failure_class="provider_probe_failed",
        )
        assert cursor["status"] == "unavailable"
        assert cursor["circuit_state"] == "open"
        assert cursor["consecutive_failures"] == 3
        assert app.state.provider_pool.route_task(
            task,
            zone_availability={"domestic": True, "overseas": True},
        )["name"] == "codex"


def test_worker_binding_uses_task_provider_and_can_rebind_when_idle() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="alpha", path=str(project_path), host_path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(project["id"], "fullstack")["role_id"]
        task = app.state.task_repository.create(
            title="codex task", objective="work", project_path=str(project_path),
            project_id=project["id"], role_id=role_id, provider="codex",
            command={"argv": [sys.executable, "-c", "pass"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, idempotency_key="provider-binding",
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="provider-binding-claim"
        )
        assert claim.task.worker_id
        context = app.state.task_repository.worker_execution_context(claim.task.worker_id)
        assert context["provider"] == "codex"
        assert context["host_path"] == str(project_path)

        connection = app.state.database.connect()
        try:
            connection.execute("UPDATE workers SET status = 'idle', active_task_id = NULL WHERE id = ?", (claim.task.worker_id,))
            connection.commit()
        finally:
            connection.close()
        rebound = app.state.project_repository.rebind_worker(
            claim.task.worker_id, provider="cursor", reason="test"
        )
        assert rebound["provider"] == "cursor"
        assert rebound["session_generation"] == 2
        assert rebound["external_session_id"] is None


def test_convention_refinement_returns_suggestion_without_overwriting() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        app.state.provider_pool.bridge = FakeBridge()
        with TestClient(app) as client:
            project = client.post("/api/projects", json={
                "name": "alpha", "path": str(project_path), "host_path": str(project_path),
            }).json()
            saved = client.put("/api/conventions", json={
                "scope": "global", "scope_id": "global", "content": "必须保证质量。必须保证质量。",
                "expected_revision": 1,
            }).json()
            response = client.post(
                "/api/conventions/global/global/refine",
                headers={"Idempotency-Key": "refine-global-test"},
                json={
                    "provider": "simple-worker", "project_id": project["id"],
                    "instruction": "去重并变成可验证约束",
                },
            )
            assert response.status_code == 200
            suggestion = response.json()
            assert suggestion["source_revision"] == saved["revision"]
            assert suggestion["input_tokens"] == 21
            assert suggestion["applied"] is False
            assert client.get("/api/conventions/global/global").json()["content"] == saved["content"]


def test_host_bridge_argv_is_fixed_and_stream_parser_keeps_session_and_usage() -> None:
    project = Path("/tmp/project")
    codex = _execution_argv("codex", "/bin/codex", project, None, "do work")
    cursor = _execution_argv("cursor", "/bin/cursor", project, "chat-1", "do work")
    assert codex[:3] == ["/bin/codex", "exec", "--json"]
    assert "workspace-write" in codex and 'approval_policy="never"' in codex
    assert codex[codex.index("--disable") + 1] == "multi_agent"
    resumed_codex = _execution_argv(
        "codex", "/bin/codex", project, "session-1", "continue"
    )
    assert resumed_codex[:4] == ["/bin/codex", "exec", "resume", "--json"]
    assert resumed_codex[resumed_codex.index("--disable") + 1] == "multi_agent"
    assert cursor[-2:] == ["chat-1", "do work"]
    assert "--sandbox" in cursor and "enabled" in cursor
    parsed = _parse_stream(
        '{"type":"thread.started","thread_id":"session-1"}\n'
        '{"usage":{"input_tokens":17,"cached_input_tokens":13,"output_tokens":9}}\n'
    )
    assert parsed == {
        "session_id": "session-1",
        "input_tokens": 17,
        "cached_input_tokens": 13,
        "output_tokens": 9,
    }
    cursor_usage = _parse_stream(
        '{"type":"result","usage":{"inputTokens":23696,'
        '"cacheReadTokens":1064192,"outputTokens":10927}}\n'
    )
    assert cursor_usage == {
        "session_id": None,
        "input_tokens": 1_087_888,
        "cached_input_tokens": 1_064_192,
        "output_tokens": 10_927,
    }
    assert _provider_failure_class(
        1, "", "Selected model is at capacity"
    ) == "provider_capacity"
    assert _provider_failure_class(1, "", "application assertion failed") == "command_failed"
    simple_version = _version_argv("json-worker", "/bin/simple-worker")
    assert simple_version[0] == sys.executable
    assert simple_version[1].endswith("/plow_whip_web/simple_worker.py")
    assert simple_version[2:] == ["--probe"]


def test_host_bridge_probe_has_a_bounded_cli_startup_window() -> None:
    with patch("plow_whip_web.host_bridge.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "cursor-agent 1.0\n"
        run.return_value.stderr = ""
        result = probe({"adapter": "cursor", "executable": "/bin/echo"})

    assert result == {"available": True, "detail": "cursor-agent 1.0"}
    assert run.call_args.kwargs["timeout"] == 15


def test_host_bridge_passes_only_declared_deepseek_credentials(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-one")
    monkeypatch.setenv("DEEPSEEK_API_KEY_02", "secret-two")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    environment = _safe_environment()
    assert environment["DEEPSEEK_API_KEY"] == "secret-one"
    assert environment["DEEPSEEK_API_KEY_02"] == "secret-two"
    assert environment["DEEPSEEK_MODEL"] == "deepseek-v4-flash"
    assert "UNRELATED_SECRET" not in environment


def test_host_bridge_finds_worker_next_to_its_python(monkeypatch, tmp_path: Path) -> None:
    worker = tmp_path / "simple-worker"
    worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    worker.chmod(0o700)
    monkeypatch.setattr("plow_whip_web.host_bridge.sys.executable", str(tmp_path / "python"))
    monkeypatch.setenv("PATH", "")
    assert _resolve_executable("simple-worker", "json-worker") == str(worker)


def test_convention_output_extracts_nested_cli_agent_message() -> None:
    output = (
        '{"type":"thread.started","thread_id":"session-1"}\n'
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"content":[{"type":"output_text","text":"# 精炼结果\\n\\n- 必须附验证证据。"}]}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":17,"output_tokens":9}}\n'
    )
    assert _last_text(output) == "# 精炼结果\n\n- 必须附验证证据。"


def _estimated_execution_policy(size_class: str) -> dict[str, object]:
    preview = estimate_task_sizing(TaskSizingInputs(
        layers_touched=2 if size_class == "M" else 6,
        components_touched=3 if size_class == "M" else 8,
        estimated_files_changed=5 if size_class == "M" else 12,
        has_migration=True,
        has_deploy=size_class in {"L", "XL"},
        verification_commands_count=3 if size_class == "M" else 5,
        estimated_verification_seconds=120 if size_class == "M" else 420,
        external_dependencies_count=1 if size_class == "M" else 3,
        risk_level="medium" if size_class == "M" else "high",
        independent_review_required=size_class == "XL",
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    assert preview["status"] == "estimated"
    assert preview["size_class"] == size_class
    return {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
    }


def _estimated_sizing(size_class: str) -> dict[str, object]:
    preview = estimate_task_sizing(TaskSizingInputs(
        layers_touched=2 if size_class == "M" else 6,
        components_touched=3 if size_class == "M" else 8,
        estimated_files_changed=5 if size_class == "M" else 12,
        has_migration=True,
        has_deploy=size_class in {"L", "XL"},
        verification_commands_count=3 if size_class == "M" else 5,
        estimated_verification_seconds=120 if size_class == "M" else 420,
        external_dependencies_count=1 if size_class == "M" else 3,
        risk_level="medium" if size_class == "M" else "high",
        independent_review_required=size_class == "XL",
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    return {
        "status": preview["status"],
        "size_class": preview["size_class"],
        "rationale": preview["rationale"],
        "bootstrap_version": preview["bootstrap_version"],
    }


def test_execution_deadline_helpers_use_policy_or_legacy_command() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        execution_policy = _estimated_execution_policy("M")
        estimated = app.state.task_repository.create(
            title="estimated-m", objective="work", project_path=str(project_path),
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=3, idempotency_key="estimated-m",
            sizing=_estimated_sizing("M"), execution_policy=execution_policy,
        )
        legacy = app.state.task_repository.create(
            title="legacy", objective="work", project_path=str(project_path),
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 180},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, idempotency_key="legacy-timeout",
        )

        assert task_hard_deadline_seconds(estimated) == 1200
        assert task_lease_seconds(estimated) == 1200 + EXECUTION_DEADLINE_GRACE_SECONDS
        assert task_hard_deadline_seconds(legacy) == 180
        assert task_lease_seconds(legacy) == max(300, 180 + EXECUTION_DEADLINE_GRACE_SECONDS)


def test_host_bridge_client_http_timeout_allows_m_hard_deadline() -> None:
    client = HostBridgeClient(
        base_url="http://127.0.0.1:1", token="configured-for-test",
    )
    assert client.timeout_seconds == MAX_HARD_DEADLINE_SECONDS + 20
    assert min(client.timeout_seconds, max(1200 + 20, 10)) == 1220


class CapturingBridge:
    token = "configured-for-test"
    last_timeout: int | None = None

    def start_job(self, *, timeout_seconds: int, **_kwargs: object) -> dict[str, object]:
        self.last_timeout = timeout_seconds
        return {"job_id": _kwargs["job_id"], "status": "running", "pid": 1}


def test_provider_pool_passes_execution_policy_hard_deadline_to_host() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime", host_bridge_token="test-token-is-long-enough-123"
        ))
        project = app.state.project_repository.create(
            name="alpha", path=str(project_path), host_path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(project["id"], "fullstack")["role_id"]
        execution_policy = _estimated_execution_policy("M")
        task = app.state.task_repository.create(
            title="host-m", objective="work", project_path=str(project_path),
            project_id=project["id"], role_id=role_id, provider="codex",
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=3, idempotency_key="host-m",
            sizing=_estimated_sizing("M"), execution_policy=execution_policy,
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="host-m-claim",
        )
        bridge = CapturingBridge()
        pool = ProviderPool(
            app.state.database, app.state.providers,
            app.state.task_repository, bridge,
        )
        pool.start_task_job(claim.task, job_id="job-m", prompt="work")
        assert bridge.last_timeout == 1200

        connection = app.state.database.connect()
        try:
            connection.execute(
                "UPDATE workers SET status = 'idle', active_task_id = NULL WHERE id = ?",
                (claim.task.worker_id,),
            )
            connection.execute("DELETE FROM task_leases WHERE task_id = ?", (task.id,))
            connection.execute("DELETE FROM resource_locks WHERE task_id = ?", (task.id,))
            connection.commit()
        finally:
            connection.close()

        for size_class, hard_deadline in (("L", 2400), ("XL", 4800)):
            policy = {
                **_estimated_execution_policy("M"),
                "hard_deadline_seconds": hard_deadline,
                "soft_deadline_seconds": hard_deadline // 2,
            }
            sizing = {
                **_estimated_sizing("M"),
                "size_class": size_class,
            }
            sized = app.state.task_repository.create(
                title=f"host-{size_class.lower()}",
                objective="work", project_path=str(project_path),
                project_id=project["id"], role_id=role_id, provider="codex",
                command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
                verification=[{"kind": "exit_code", "expected": 0}],
                max_attempts=3, idempotency_key=f"host-{size_class.lower()}",
                sizing=sizing, execution_policy=policy,
            )
            sized_claim = app.state.task_repository.claim(
                sized.id, expected_revision=0,
                idempotency_key=f"host-{size_class.lower()}-claim",
            )
            pool.start_task_job(
                sized_claim.task, job_id=f"job-{size_class.lower()}", prompt="work",
            )
            assert bridge.last_timeout == hard_deadline
            assert bridge.last_timeout > 620
            connection = app.state.database.connect()
            try:
                connection.execute(
                    "UPDATE workers SET status = 'idle', active_task_id = NULL WHERE id = ?",
                    (sized_claim.task.worker_id,),
                )
                connection.execute("DELETE FROM task_leases WHERE task_id = ?", (sized.id,))
                connection.execute("DELETE FROM resource_locks WHERE task_id = ?", (sized.id,))
                connection.commit()
            finally:
                connection.close()

        legacy = app.state.task_repository.create(
            title="host-legacy", objective="work", project_path=str(project_path),
            project_id=project["id"], role_id=role_id, provider="codex",
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 240},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, idempotency_key="host-legacy",
        )
        legacy_claim = app.state.task_repository.claim(
            legacy.id, expected_revision=0, idempotency_key="host-legacy-claim",
        )
        pool.start_task_job(legacy_claim.task, job_id="job-legacy", prompt="work")
        assert bridge.last_timeout == 240


def test_recent_execution_health_requires_a_successful_terminal_host_job() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        project = app.state.project_repository.create(
            name="readiness", path=str(project_path), host_path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            project["id"], "fullstack"
        )["role_id"]
        task = app.state.task_repository.create(
            title="cursor readiness",
            objective="prove one real execution",
            project_path=str(project_path),
            project_id=project["id"],
            role_id=role_id,
            provider="cursor",
            command={"argv": None, "timeout_seconds": 60},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="cursor-readiness-task",
        )
        claim = app.state.task_repository.claim(
            task.id,
            expected_revision=task.revision,
            idempotency_key="cursor-readiness-claim",
        )
        assert claim.attempt_id is not None
        assert claim.run_id is not None
        job = app.state.host_jobs.prepare(
            task_id=task.id,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            provider="cursor",
        )

        assert app.state.provider_pool._recent_execution_health("cursor") == "unknown"

        app.state.host_jobs.record(job["job_id"], {
            "job_id": job["job_id"],
            "status": "completed",
            "returncode": 0,
            "stdout": "fixture complete",
            "stderr": "",
            "input_tokens": 1,
            "output_tokens": 1,
        })

        assert app.state.provider_pool._recent_execution_health("cursor") == "healthy"
