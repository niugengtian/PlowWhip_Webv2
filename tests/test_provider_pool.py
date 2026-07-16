from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.host_bridge import _execution_argv, _parse_stream
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.providers.pool import _last_text


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
            max_attempts=1, token_budget=100, idempotency_key="provider-binding",
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
                "expected_revision": 0,
            }).json()
            response = client.post("/api/conventions/global/global/refine", json={
                "provider": "simple-worker", "project_id": project["id"],
                "instruction": "去重并变成可验证约束",
            })
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
    assert cursor[-2:] == ["chat-1", "do work"]
    assert "--sandbox" in cursor and "enabled" in cursor
    parsed = _parse_stream(
        '{"type":"thread.started","thread_id":"session-1"}\n'
        '{"usage":{"input_tokens":17,"output_tokens":9}}\n'
    )
    assert parsed == {"session_id": "session-1", "input_tokens": 17, "output_tokens": 9}


def test_convention_output_extracts_nested_cli_agent_message() -> None:
    output = (
        '{"type":"thread.started","thread_id":"session-1"}\n'
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"content":[{"type":"output_text","text":"# 精炼结果\\n\\n- 必须附验证证据。"}]}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":17,"output_tokens":9}}\n'
    )
    assert _last_text(output) == "# 精炼结果\n\n- 必须附验证证据。"
