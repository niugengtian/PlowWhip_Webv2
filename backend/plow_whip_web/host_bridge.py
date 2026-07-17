from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
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
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".plow-whip-web" / "host-bridge",
        help="Persistent sanitized Host Job state",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("PLOW_WHIP_BRIDGE_TOKEN must contain at least 24 characters")
    roots = tuple(path.expanduser().resolve() for path in args.project_root)
    jobs = HostJobManager(args.state_dir, roots)
    server = ThreadingHTTPServer((args.bind, args.port), _handler(token, roots, jobs))
    print(json.dumps({
        "status": "ready", "bind": args.bind, "port": args.port,
        "roots": [str(p) for p in roots], "state_dir": str(jobs.root),
    }))
    server.serve_forever()


def _handler(
    token: str, roots: tuple[Path, ...], jobs: "HostJobManager"
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "plow-whip-host-bridge/2"

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
                elif self.path == "/v1/jobs/start":
                    self._send(202, jobs.start(payload))
                elif self.path == "/v1/jobs/status":
                    self._send(200, jobs.status(str(payload["job_id"])))
                elif self.path == "/v1/jobs/cancel":
                    self._send(202, jobs.cancel(str(payload["job_id"])))
                else:
                    self._send(404, {"detail": "not found"})
            except (OSError, ValueError, KeyError, TypeError) as error:
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


class HostJobManager:
    """Durable, bounded host-process lifecycle without arbitrary command access."""

    ACTIVE = {"dispatching", "running", "orphan_running", "cancelling", "recovery_hold"}

    def __init__(self, state_dir: Path, roots: tuple[Path, ...]) -> None:
        self.root = state_dir.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.roots = tuple(root.expanduser().resolve() for root in roots)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._recover_records()

    def start(self, payload: dict[str, Any]) -> dict[str, object]:
        job_id = _job_id(payload.get("job_id"))
        with self._lock:
            existing = self._read(job_id, required=False)
            if existing is not None:
                return self._refresh(existing)

        adapter = _adapter(payload)
        executable = _resolve_executable(str(payload.get("executable") or ""), adapter)
        if executable is None:
            raise ValueError("executable is not available")
        project_path = Path(str(payload["project_path"])).expanduser().resolve()
        if not project_path.is_dir() or not any(
            _is_within(project_path, root) for root in self.roots
        ):
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
        record: dict[str, Any] = {
            "job_id": job_id,
            "status": "dispatching",
            "pid": None,
            "process_identity": None,
            "adapter": adapter,
            "project_path": str(project_path),
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
            "started_at": _utc_now(),
            "heartbeat_at": _utc_now(),
            "finished_at": None,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "failure_class": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cancel_requested": False,
        }
        with self._lock:
            existing = self._read(job_id, required=False)
            if existing is not None:
                return self._refresh(existing)
            # The durable idempotency marker exists before a process can exist.
            self._write(record)
        try:
            process = subprocess.Popen(
                argv,
                cwd=project_path,
                stdin=subprocess.DEVNULL if adapter == "cursor" else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=_safe_environment(),
                start_new_session=True,
            )
        except OSError as error:
            with self._lock:
                record.update({
                    "status": "completed", "returncode": 126,
                    "failure_class": "command_unavailable",
                    "stderr": Redactor.redact(str(error))[:1000],
                    "finished_at": _utc_now(), "heartbeat_at": _utc_now(),
                })
                self._write(record)
            raise
        with self._lock:
            record.update({
                "status": "running", "pid": process.pid,
                "process_identity": _process_identity(process.pid),
            })
            self._processes[job_id] = process
            self._write(record)
        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except BrokenPipeError:
                pass
        threading.Thread(
            target=self._monitor,
            args=(job_id, process, started, timeout_seconds),
            name=f"plow-whip-job-{job_id[:8]}",
            daemon=True,
        ).start()
        return dict(record)

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._refresh(self._read(_job_id(job_id)))

    def cancel(self, job_id: str) -> dict[str, object]:
        job_id = _job_id(job_id)
        with self._lock:
            record = self._refresh(self._read(job_id))
            if record["status"] not in self.ACTIVE:
                return record
            record["status"] = "cancelling"
            record["cancel_requested"] = True
            record["heartbeat_at"] = _utc_now()
            self._write(record)
            process = self._processes.get(job_id)
            pid = int(record["pid"])
        if process is not None or _same_process(record):
            _terminate_process(process, pid)
        threading.Thread(
            target=self._enforce_cancel,
            args=(job_id, process, pid),
            name=f"plow-whip-cancel-{job_id[:8]}",
            daemon=True,
        ).start()
        return record

    def _monitor(
        self,
        job_id: str,
        process: subprocess.Popen[str],
        started: float,
        timeout_seconds: int,
    ) -> None:
        stdout_thread = threading.Thread(
            target=self._read_stream, args=(job_id, process.stdout, "stdout"), daemon=True
        )
        stderr_thread = threading.Thread(
            target=self._read_stream, args=(job_id, process.stderr, "stderr"), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(process, process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process(process, process.pid)
                process.wait(timeout=5)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        with self._lock:
            record = self._read(job_id)
            cancelled = bool(record.get("cancel_requested"))
            if timed_out:
                status, returncode, failure = "completed", 124, "timeout"
            elif cancelled:
                status, returncode, failure = "cancelled", 130, "cancelled"
            else:
                returncode = int(process.returncode or 0)
                status = "completed"
                failure = None if returncode == 0 else "command_failed"
            record.update({
                "status": status,
                "returncode": returncode,
                "failure_class": failure,
                "duration_ms": int((monotonic() - started) * 1000),
                "heartbeat_at": _utc_now(),
                "finished_at": _utc_now(),
            })
            self._processes.pop(job_id, None)
            self._write(record)

    def _read_stream(self, job_id: str, stream: Any, field: str) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            with self._lock:
                record = self._read(job_id)
                record[field] = _append_bounded(str(record.get(field) or ""), Redactor.redact(line))
                record["heartbeat_at"] = _utc_now()
                if field == "stdout":
                    parsed = _parse_stream(line)
                    record["session_id"] = record.get("session_id") or parsed["session_id"]
                    record["input_tokens"] = max(
                        int(record.get("input_tokens") or 0), int(parsed["input_tokens"])
                    )
                    record["output_tokens"] = max(
                        int(record.get("output_tokens") or 0), int(parsed["output_tokens"])
                    )
                self._write(record)
        stream.close()

    def _enforce_cancel(
        self, job_id: str, process: subprocess.Popen[str] | None, pid: int
    ) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _process_alive(pid):
            time.sleep(0.25)
        if _process_alive(pid) and (
            process is not None or _same_process(self._read(job_id, required=False) or {})
        ):
            _kill_process(process, pid)
        if process is None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _process_alive(pid):
                time.sleep(0.1)
            with self._lock:
                record = self._read(job_id, required=False)
                if record is None:
                    return
                record.update({
                    "status": "cancelled" if not _same_process(record) else "recovery_hold",
                    "returncode": 130 if not _process_alive(pid) else None,
                    "failure_class": "cancelled" if not _process_alive(pid) else "process_unconfirmed",
                    "heartbeat_at": _utc_now(),
                    "finished_at": _utc_now() if not _process_alive(pid) else None,
                })
                self._write(record)

    def _recover_records(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                job_id = _job_id(record.get("job_id"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if record.get("status") in {"dispatching", "running", "orphan_running", "recovery_hold"}:
                if record.get("status") == "dispatching" and not record.get("pid"):
                    record.update({
                        "status": "recovery_hold",
                        "failure_class": "dispatch_outcome_unknown",
                        "heartbeat_at": _utc_now(),
                    })
                    self._write(record)
                    continue
                record["status"] = (
                    "orphan_running" if _same_process(record)
                    else "interrupted"
                )
                if record["status"] == "interrupted":
                    record.update({
                        "returncode": 125,
                        "failure_class": "external_interruption",
                        "finished_at": _utc_now(),
                    })
                record["heartbeat_at"] = _utc_now()
                self._write(record)
            elif record.get("status") == "cancelling" and not _same_process(record):
                record.update({
                    "status": "cancelled", "returncode": 130,
                    "failure_class": "cancelled", "finished_at": _utc_now(),
                })
                self._write(record)
            if path.name != f"{job_id}.json":
                try:
                    path.unlink()
                except OSError:
                    pass

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = str(record["job_id"])
        if (
            record.get("status") == "recovery_hold"
            and not record.get("pid")
            and record.get("failure_class") == "dispatch_outcome_unknown"
        ):
            return dict(record)
        if record.get("status") in {
            "running", "orphan_running", "recovery_hold"
        } and job_id not in self._processes:
            if _same_process(record):
                record["status"] = (
                    "cancelling" if record.get("cancel_requested") else "orphan_running"
                )
            else:
                record.update({
                    "status": "cancelled" if record.get("cancel_requested") else "interrupted",
                    "returncode": 130 if record.get("cancel_requested") else 125,
                    "failure_class": "cancelled" if record.get("cancel_requested") else "external_interruption",
                    "heartbeat_at": _utc_now(),
                    "finished_at": _utc_now(),
                })
            self._write(record)
        return dict(record)

    def _read(self, job_id: str, *, required: bool = True) -> dict[str, Any] | None:
        path = self.root / f"{job_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise ValueError(f"host job not found: {job_id}") from None
            return None

    def _write(self, record: dict[str, Any]) -> None:
        job_id = _job_id(record.get("job_id"))
        target = self.root / f"{job_id}.json"
        temporary = self.root / f".{job_id}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)


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


def _job_id(value: object) -> str:
    candidate = str(value or uuid.uuid4())
    try:
        parsed = uuid.UUID(candidate)
    except ValueError:
        raise ValueError("job_id must be a UUID") from None
    return str(parsed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_bounded(current: str, value: str) -> str:
    combined = current + value
    encoded = combined.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return combined
    marker = "\n[older host job output truncated]\n"
    room = MAX_OUTPUT_BYTES - len(marker.encode("utf-8"))
    tail = encoded[-max(room, 0):]
    while tail:
        try:
            return marker + tail.decode("utf-8")
        except UnicodeDecodeError:
            tail = tail[1:]
    return marker[:MAX_OUTPUT_BYTES]


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _process_identity(pid: int) -> str | None:
    if not _process_alive(pid):
        return None
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None


def _same_process(record: dict[str, Any]) -> bool:
    pid = int(record.get("pid") or 0)
    expected = record.get("process_identity")
    return bool(expected and _process_identity(pid) == expected)


def _terminate_process(process: subprocess.Popen[str] | None, pid: int) -> None:
    if process is not None and process.poll() is not None:
        return
    _signal_process(process, pid, signal.SIGTERM)


def _kill_process(process: subprocess.Popen[str] | None, pid: int) -> None:
    if process is not None and process.poll() is not None:
        return
    _signal_process(process, pid, signal.SIGKILL)


def _signal_process(
    process: subprocess.Popen[str] | None, pid: int, requested_signal: signal.Signals
) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), requested_signal)
        elif process is not None:
            process.terminate() if requested_signal == signal.SIGTERM else process.kill()
        else:
            os.kill(pid, requested_signal)
    except (OSError, ProcessLookupError):
        pass


if __name__ == "__main__":
    main()
