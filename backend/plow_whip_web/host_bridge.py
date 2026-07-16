from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any

from plow_whip_web.security import Redactor


MAX_BODY_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 262_144
SUPPORTED_ADAPTERS = {"codex", "cursor", "json-worker"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the restricted plow-whip host CLI bridge")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", action="append", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("PLOW_WHIP_BRIDGE_TOKEN must contain at least 24 characters")
    roots = tuple(path.expanduser().resolve() for path in args.project_root)
    server = ThreadingHTTPServer((args.bind, args.port), _handler(token, roots))
    print(json.dumps({"status": "ready", "bind": args.bind, "port": args.port, "roots": [str(p) for p in roots]}))
    server.serve_forever()


def _handler(token: str, roots: tuple[Path, ...]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "plow-whip-host-bridge/1"

        def do_POST(self) -> None:  # noqa: N802
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self._send(401, {"detail": "authentication required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/probe":
                    self._send(200, probe(payload))
                elif self.path == "/v1/execute":
                    self._send(200, execute(payload, roots))
                else:
                    self._send(404, {"detail": "not found"})
            except (ValueError, KeyError, TypeError) as error:
                self._send(400, {"detail": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            print(f"host-bridge {self.address_string()} {format % args}")

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def probe(payload: dict[str, Any]) -> dict[str, object]:
    adapter = _adapter(payload)
    executable = _resolve_executable(str(payload.get("executable") or ""), adapter)
    if executable is None:
        return {"available": False, "detail": "未找到可执行文件"}
    try:
        completed = subprocess.run(
            _version_argv(adapter, executable), capture_output=True, text=True,
            timeout=8, check=False, env=_safe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "detail": Redactor.redact(str(error))[:500]}
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = Redactor.redact(output[0] if output else f"exit {completed.returncode}")[:500]
    return {"available": completed.returncode == 0, "detail": detail}


def execute(payload: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, object]:
    adapter = _adapter(payload)
    executable = _resolve_executable(str(payload.get("executable") or ""), adapter)
    if executable is None:
        raise ValueError("executable is not available")
    project_path = Path(str(payload["project_path"])).expanduser().resolve()
    if not project_path.is_dir() or not any(_is_within(project_path, root) for root in roots):
        raise ValueError("project_path is outside the configured project roots")
    prompt = str(payload["prompt"])
    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("prompt is empty or too large")
    session_id = str(payload.get("session_id") or "") or None
    timeout_seconds = min(max(int(payload.get("timeout_seconds", 600)), 10), 3600)
    if adapter == "cursor" and session_id is None:
        session_id = _cursor_create_chat(executable, project_path)
    if adapter == "json-worker" and session_id is None:
        session_id = str(uuid.uuid4())
    argv = _execution_argv(adapter, executable, project_path, session_id, prompt)
    started = monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=project_path, input=None if adapter == "cursor" else prompt,
            capture_output=True, text=True,
            timeout=timeout_seconds, check=False, env=_safe_environment(),
        )
        stdout = Redactor.redact(completed.stdout[:MAX_OUTPUT_BYTES])
        stderr = Redactor.redact(completed.stderr[:MAX_OUTPUT_BYTES])
        parsed = _parse_stream(stdout)
        return {
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((monotonic() - started) * 1000),
            "failure_class": None if completed.returncode == 0 else "command_failed",
            "input_tokens": parsed["input_tokens"],
            "output_tokens": parsed["output_tokens"],
            "session_id": parsed["session_id"] or session_id,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "returncode": 124,
            "stdout": Redactor.redact(_text(error.stdout)[:MAX_OUTPUT_BYTES]),
            "stderr": Redactor.redact(_text(error.stderr)[:MAX_OUTPUT_BYTES]),
            "duration_ms": int((monotonic() - started) * 1000),
            "failure_class": "timeout",
            "input_tokens": 0,
            "output_tokens": 0,
            "session_id": session_id,
        }


def _execution_argv(
    adapter: str, executable: str, project: Path, session_id: str | None, prompt: str
) -> list[str]:
    if adapter == "codex":
        if session_id:
            return [executable, "exec", "resume", "--json", session_id, "-"]
        return [
            executable, "exec", "--json", "--sandbox", "workspace-write",
            "-c", 'approval_policy="never"', "-C", str(project), "-",
        ]
    if adapter == "cursor":
        assert session_id is not None
        return [
            executable, "agent", "-p", "--output-format", "stream-json",
            "--sandbox", "enabled", "--trust", "--force", "--workspace", str(project),
            "--resume", session_id, prompt,
        ]
    assert session_id is not None
    return [executable, "--project", str(project), "--session", session_id, "--json"]


def _cursor_create_chat(executable: str, project: Path) -> str:
    completed = subprocess.run(
        [executable, "agent", "create-chat"], cwd=project, capture_output=True,
        text=True, timeout=15, check=False, env=_safe_environment(),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError(f"Cursor 创建会话失败: {Redactor.redact(completed.stderr)[:500]}")
    return completed.stdout.strip().splitlines()[-1].strip()


def _parse_stream(output: str) -> dict[str, object]:
    session_id: str | None = None
    input_tokens = 0
    output_tokens = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or _find_string(event, {"thread_id", "threadId", "session_id", "sessionId", "chat_id", "chatId"})
        input_tokens = max(input_tokens, _find_int(event, {"input_tokens", "inputTokens"}))
        output_tokens = max(output_tokens, _find_int(event, {"output_tokens", "outputTokens"}))
    return {"session_id": session_id, "input_tokens": input_tokens, "output_tokens": output_tokens}


def _find_string(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item:
                return item
            found = _find_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_string(item, keys)
            if found:
                return found
    return None


def _find_int(value: Any, keys: set[str]) -> int:
    found = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, int):
                found = max(found, item)
            found = max(found, _find_int(item, keys))
    elif isinstance(value, list):
        for item in value:
            found = max(found, _find_int(item, keys))
    return found


def _adapter(payload: dict[str, Any]) -> str:
    adapter = str(payload.get("adapter") or "")
    if adapter not in SUPPORTED_ADAPTERS:
        raise ValueError("unsupported adapter")
    return adapter


def _resolve_executable(configured: str, adapter: str) -> str | None:
    fallback = {"codex": "codex", "cursor": "cursor", "json-worker": "simple-worker"}[adapter]
    candidate = configured or fallback
    if Path(candidate).is_absolute():
        path = Path(candidate)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(candidate)


def _version_argv(adapter: str, executable: str) -> list[str]:
    if adapter == "cursor":
        return [executable, "agent", "--version"]
    return [executable, "--version"]


def _safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CURSOR_API_KEY"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


if __name__ == "__main__":
    main()
