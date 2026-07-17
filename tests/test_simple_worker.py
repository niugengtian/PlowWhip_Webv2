from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.simple_worker import (
    DeepSeekClient,
    SimpleWorker,
    WorkerFailure,
    _final_payload,
    credential_names,
)


class FakeClient:
    input_tokens = 12
    output_tokens = 5
    last_key_ref = "deepseek_api_key/****test/fp-12345678"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: list[dict[str, object]]) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"input.txt"}',
                    },
                }],
            }
        assert any(message.get("role") == "tool" for message in messages)
        return {
            "role": "assistant",
            "content": (
                '{"status":"completed","summary":"inspected input",'
                '"verify_commands":["git diff --check"]}'
            ),
        }


def test_credentials_are_environment_only_and_missing_key_is_explicit() -> None:
    assert credential_names({"DEEPSEEK_API_KEY_02": "two", "OTHER": "ignored"}) == [
        "DEEPSEEK_API_KEY_02"
    ]
    with pytest.raises(WorkerFailure, match="DEEPSEEK_API_KEY"):
        DeepSeekClient({}).chat([{"role": "user", "content": "work"}])


def test_worker_uses_tools_persists_session_and_returns_usage() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "input.txt").write_text("bounded evidence", encoding="utf-8")
        client = FakeClient()
        worker = SimpleWorker(
            root, "session-1", client=client, state_root=root / "state"
        )
        result = worker.run("inspect before completion")
        assert result["status"] == "completed"
        assert result["session_id"] == "session-1"
        assert result["input_tokens"] == 12
        assert client.calls == 2
        persisted = worker.session_path.read_text(encoding="utf-8")
        assert '"role":"tool"' in persisted
        assert "bounded evidence" in persisted


def test_worker_rejects_escape_shell_and_git_lifecycle_commands() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        worker = SimpleWorker(
            root, "session-1", client=FakeClient(), state_root=root / "state"
        )
        assert "error" in worker._tool_result("read_file", {"path": "../secret"})
        assert "error" in worker._tool_result(
            "run_command", {"command": "pytest | env", "timeout": 1}
        )
        assert "error" in worker._tool_result(
            "run_command", {"command": "git commit -am bad", "timeout": 1}
        )


def test_final_payload_accepts_plain_or_fenced_json() -> None:
    assert _final_payload('{"status":"completed","summary":"done"}')["status"] == "completed"
    assert _final_payload('```json\n{"status":"needs_planner","reason":"large"}\n```')[
        "status"
    ] == "needs_planner"
