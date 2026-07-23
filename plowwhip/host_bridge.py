from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .store import write_atomic


MAX_BODY_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 32_768
MAX_JOB_SECONDS = 86_400
SUPPORTED_EXECUTABLES = {
    "codex": {"codex"},
    "cursor": {"cursor", "cursor-agent"},
    "json-worker": {"simple-worker", "kimi-worker"},
    "git-publish": {"git-publish"},
}
KNOWN_EXECUTABLE_PATHS = {
    "codex": {Path("/Applications/ChatGPT.app/Contents/Resources/codex")},
    "cursor": {
        Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
    },
    "json-worker": set(),
    "git-publish": set(),
}
ACTIVE_STATUSES = {
    "dispatching",
    "running",
    "orphan_running",
    "cancelling",
    "recovery_hold",
}
_SECRET = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_-]?key|token|secret)\s*[=:]\s*)"
    r"[A-Za-z0-9._~+/=-]{12,}"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the restricted PlowWhip V1 host CLI bridge"
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", action="append", type=Path, required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="private 0600 environment file containing only Bridge/Provider variables",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".plow-whip-v1" / "host-bridge",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.env_file:
        _load_private_env(args.env_file)
    token = os.environ.get("PLOW_WHIP_BRIDGE_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("PLOW_WHIP_BRIDGE_TOKEN must contain at least 24 characters")
    server = make_server(
        args.bind,
        args.port,
        token,
        tuple(args.project_root),
        args.state_dir,
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "bind": args.bind,
                "port": server.server_address[1],
                "roots": [str(path) for path in server.manager.roots],  # type: ignore[attr-defined]
                "state_dir": str(server.manager.root),  # type: ignore[attr-defined]
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def make_server(
    bind: str,
    port: int,
    token: str,
    roots: tuple[Path, ...],
    state_dir: Path,
) -> ThreadingHTTPServer:
    if bind != "127.0.0.1":
        raise ValueError("Host Bridge must bind to 127.0.0.1")
    if len(token) < 24:
        raise ValueError("Host Bridge token must contain at least 24 characters")
    resolved_roots = tuple(path.expanduser().resolve() for path in roots)
    if not resolved_roots or any(not path.is_dir() for path in resolved_roots):
        raise ValueError("Host Bridge requires existing project roots")
    manager = HostJobManager(state_dir, resolved_roots)
    server = ThreadingHTTPServer((bind, port), _handler(token, manager))
    server.manager = manager  # type: ignore[attr-defined]
    return server


def _handler(token: str, manager: "HostJobManager") -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "plowwhip-v1-host-bridge/1"

        def do_POST(self) -> None:  # noqa: N802
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self._send(401, {"detail": "authentication required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be an object")
                if self.path == "/v1/probe":
                    status, result = 200, manager.probe(payload)
                elif self.path == "/v1/evidence/snapshot":
                    status, result = 200, manager.snapshot(payload)
                elif self.path == "/v1/jobs/start":
                    status, result = 202, manager.start(payload)
                elif self.path == "/v1/jobs/status":
                    status, result = 200, manager.status(payload["job_id"])
                elif self.path == "/v1/jobs/output":
                    status, result = 200, manager.output(
                        payload["job_id"],
                        int(payload.get("stdout_offset") or 0),
                        int(payload.get("stderr_offset") or 0),
                        int(payload.get("limit") or MAX_OUTPUT_BYTES),
                        int(payload.get("tail_lines") or 20),
                    )
                elif self.path == "/v1/jobs/cancel":
                    status, result = 202, manager.cancel(payload["job_id"])
                else:
                    self._send(404, {"detail": "not found"})
                    return
                self._send(status, result)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                self._send(400, {"detail": str(error)[:500]})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class HostJobManager:
    """One durable record per real host process; never accepts arbitrary argv."""

    def __init__(self, state_dir: Path, roots: tuple[Path, ...]) -> None:
        self.root = state_dir.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.roots = roots
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._recover()

    def probe(self, payload: dict[str, Any]) -> dict[str, object]:
        adapter = _adapter(payload)
        executable = _resolve_executable(payload.get("executable"), adapter)
        if executable is None:
            return {"available": False, "detail": "executable not found"}
        try:
            completed = subprocess.run(
                _version_argv(adapter, executable),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=_safe_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"available": False, "detail": _redact(str(error))[:500]}
        output = (completed.stdout or completed.stderr).strip().splitlines()
        return {
            "available": completed.returncode == 0,
            "detail": _redact(
                output[0] if output else f"exit {completed.returncode}"
            )[:500],
        }

    def snapshot(self, payload: dict[str, Any]) -> dict[str, object]:
        project = self._project(payload["project_path"])
        records: list[dict[str, object]] = []
        digest = hashlib.sha256()
        hashed_bytes = 0
        truncated = False
        for path in sorted(project.rglob("*")):
            if not path.is_file() or _excluded(path.relative_to(project)):
                continue
            if len(records) >= 256:
                truncated = True
                break
            stat = path.stat()
            relative = path.relative_to(project).as_posix()
            item: dict[str, object] = {"path": relative, "bytes": stat.st_size}
            if hashed_bytes + stat.st_size <= 16_777_216:
                item["sha256"] = _sha256(path)
                hashed_bytes += stat.st_size
            encoded = json.dumps(
                item, sort_keys=True, separators=(",", ":")
            ).encode()
            digest.update(encoded)
            records.append(item)
        git: dict[str, object] = {
            "kind": "workspace",
            "available": True,
            "fingerprint": digest.hexdigest(),
            "files": len(records),
            "sample": records[:20],
            "truncated": truncated,
        }
        try:
            head = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_safe_environment(),
            )
            status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_safe_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            git["available"] = False
        else:
            if head.returncode == 0 and status.returncode == 0:
                git["head"] = head.stdout.strip()
                git["status"] = _redact(status.stdout)[:8_192]
            else:
                git["available"] = False
        return {"git": git}

    def start(self, payload: dict[str, Any]) -> dict[str, object]:
        job_id = _job_id(payload.get("job_id"))
        with self._lock:
            existing = self._read(job_id, required=False)
            if existing:
                return self._refresh(existing)

        adapter = _adapter(payload)
        executable = _resolve_executable(payload.get("executable"), adapter)
        if executable is None:
            raise ValueError("executable is not available")
        project = self._project(payload["project_path"])
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip() or len(prompt.encode()) > MAX_BODY_BYTES:
            raise ValueError("prompt is empty or too large")
        access = str(payload.get("access") or "write")
        if access not in {"read", "write"}:
            raise ValueError("unsupported access mode")
        if access == "read" and adapter not in {"codex", "cursor"}:
            raise ValueError("read-only execution requires Codex or Cursor")
        timeout_seconds = min(
            max(int(payload.get("timeout_seconds") or 600), 10),
            MAX_JOB_SECONDS,
        )
        session_id = str(payload.get("session_id") or "") or None
        if adapter == "cursor" and session_id is None:
            session_id = _cursor_session(executable, project)
        if adapter == "json-worker" and session_id is None:
            session_id = uuid4().hex
        context_policy = _context_policy(payload.get("context_policy"))
        argv = _execution_argv(
            adapter,
            executable,
            project,
            session_id,
            prompt,
            access,
            context_policy,
        )
        directory = self._output_directory(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        stdout_path = directory / "stdout.segment-000001.log"
        stderr_path = directory / "stderr.segment-000001.log"
        now = time.time()
        record: dict[str, Any] = {
            "job_id": job_id,
            "status": "dispatching",
            "pid": None,
            "process_identity": None,
            "adapter": adapter,
            "project_path": str(project),
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
            "started_at": now,
            "ended_at": None,
            "returncode": None,
            "failure_class": None,
            "duration_ms": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "model": None,
            "cancel_requested": False,
            "output_ref": f"{job_id}/",
            "context_policy": context_policy,
        }
        with self._lock:
            existing = self._read(job_id, required=False)
            if existing:
                return self._refresh(existing)
            self._write(record)
        try:
            with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
                "ab", buffering=0
            ) as stderr:
                process = subprocess.Popen(
                    argv,
                    cwd=project,
                    stdin=subprocess.PIPE if adapter != "cursor" else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=_safe_environment(),
                    start_new_session=True,
                )
        except OSError as error:
            record.update(
                {
                    "status": "completed",
                    "ended_at": time.time(),
                    "returncode": 126,
                    "failure_class": "command_unavailable",
                }
            )
            stderr_path.write_text(_redact(str(error))[:500], encoding="utf-8")
            with self._lock:
                self._write(record)
            return dict(record)
        if process.stdin is not None:
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
            except BrokenPipeError:
                pass
        with self._lock:
            record.update(
                {
                    "status": "running",
                    "pid": process.pid,
                    "process_identity": _process_identity(process.pid),
                }
            )
            self._processes[job_id] = process
            self._write(record)
        threading.Thread(
            target=self._wait,
            args=(job_id, process, timeout_seconds),
            name=f"plowwhip-host-{job_id[:8]}",
            daemon=True,
        ).start()
        return dict(record)

    def status(self, value: object) -> dict[str, object]:
        with self._lock:
            return self._refresh(self._read(_job_id(value)))

    def output(
        self,
        value: object,
        stdout_offset: int,
        stderr_offset: int,
        limit: int,
        tail_lines: int,
    ) -> dict[str, object]:
        job_id = _job_id(value)
        bounded_limit = min(max(limit, 1_024), 65_536)
        bounded_lines = min(max(tail_lines, 1), 100)
        with self._lock:
            record = self._refresh(self._read(job_id))
        chunks = []
        next_offsets: dict[str, int] = {}
        for stream, offset in (
            ("stdout", stdout_offset),
            ("stderr", stderr_offset),
        ):
            path = self._output_directory(job_id) / f"{stream}.segment-000001.log"
            text, start, end = _read_output(
                path, offset, bounded_limit // 2, bounded_lines
            )
            next_offsets[stream] = end
            if text:
                chunks.append(
                    {
                        "kind": stream,
                        "stream": stream,
                        "offset": start,
                        "next_offset": end,
                        "text": _redact(text),
                        "refs": [f"{job_id}/{path.name}"],
                    }
                )
        return {
            "job_id": job_id,
            "status": record["status"],
            "output_ref": record["output_ref"],
            "chunks": chunks,
            "next_offsets": next_offsets,
            "has_more": False,
        }

    def cancel(self, value: object) -> dict[str, object]:
        job_id = _job_id(value)
        with self._lock:
            record = self._refresh(self._read(job_id))
            if record["status"] not in ACTIVE_STATUSES:
                return record
            record["status"] = "cancelling"
            record["cancel_requested"] = True
            self._write(record)
            process = self._processes.get(job_id)
            pid = int(record.get("pid") or 0)
        _signal_process(process, pid, signal.SIGTERM)
        return dict(record)

    def _wait(
        self,
        job_id: str,
        process: subprocess.Popen[bytes],
        timeout_seconds: int,
    ) -> None:
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_process(process, process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_process(process, process.pid, signal.SIGKILL)
                process.wait()
        with self._lock:
            record = self._read(job_id, required=False)
            self._processes.pop(job_id, None)
            if not record:
                return
            if timed_out:
                record.update(
                    {
                        "status": "completed",
                        "returncode": 124,
                        "failure_class": "timeout",
                    }
                )
            elif record.get("cancel_requested"):
                record.update(
                    {
                        "status": "cancelled",
                        "returncode": 130,
                        "failure_class": "cancelled",
                    }
                )
            else:
                returncode = int(process.returncode or 0)
                record.update(
                    {
                        "status": "completed",
                        "returncode": returncode,
                        "failure_class": None if returncode == 0 else "process",
                    }
                )
            self._finish(record)

    def _recover(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                _job_id(record.get("job_id"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if record.get("status") in ACTIVE_STATUSES:
                if not record.get("pid"):
                    record.update(
                        {
                            "status": "recovery_hold",
                            "failure_class": "dispatch_outcome_unknown",
                        }
                    )
                elif _same_process(record):
                    record["status"] = "orphan_running"
                else:
                    record.update(
                        {
                            "status": "interrupted",
                            "returncode": 125,
                            "failure_class": "external_interruption",
                        }
                    )
                    self._finish(record)
                self._write(record)

    def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = record["job_id"]
        process = self._processes.get(job_id)
        if process is not None and process.poll() is not None:
            self._processes.pop(job_id, None)
            if record.get("cancel_requested"):
                record.update(
                    {
                        "status": "cancelled",
                        "returncode": 130,
                        "failure_class": "cancelled",
                    }
                )
            else:
                returncode = int(process.returncode or 0)
                record.update(
                    {
                        "status": "completed",
                        "returncode": returncode,
                        "failure_class": None if returncode == 0 else "process",
                    }
                )
            self._finish(record)
        elif (
            record.get("status") in ACTIVE_STATUSES
            and process is None
            and record.get("pid")
        ):
            if _same_process(record):
                record["status"] = (
                    "cancelling"
                    if record.get("cancel_requested")
                    else "orphan_running"
                )
            else:
                record.update(
                    {
                        "status": (
                            "cancelled"
                            if record.get("cancel_requested")
                            else "interrupted"
                        ),
                        "returncode": 130 if record.get("cancel_requested") else 125,
                        "failure_class": (
                            "cancelled"
                            if record.get("cancel_requested")
                            else "external_interruption"
                        ),
                    }
                )
                self._finish(record)
        self._write(record)
        return dict(record)

    def _finish(self, record: dict[str, Any]) -> None:
        stdout = _read_all(
            self._output_directory(record["job_id"])
            / "stdout.segment-000001.log"
        )
        usage = _parse_usage(stdout)
        record.update(usage)
        record["ended_at"] = record.get("ended_at") or time.time()
        record["duration_ms"] = max(
            0, int((record["ended_at"] - record["started_at"]) * 1_000)
        )
        self._write(record)

    def _project(self, value: object) -> Path:
        project = Path(str(value)).expanduser().resolve()
        if not project.is_dir() or not any(
            _is_within(project, root) for root in self.roots
        ):
            raise ValueError("project_path is outside the configured project roots")
        return project

    def _record_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def _output_directory(self, job_id: str) -> Path:
        return self.root / job_id

    def _read(
        self, job_id: str, *, required: bool = True
    ) -> dict[str, Any] | None:
        try:
            value = json.loads(self._record_path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise ValueError(f"host job not found: {job_id}") from None
            return None
        if not isinstance(value, dict):
            raise ValueError("host job state is invalid")
        return value

    def _write(self, record: dict[str, Any]) -> None:
        body = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        write_atomic(self._record_path(record["job_id"]), body)
        self._record_path(record["job_id"]).chmod(0o600)


def _adapter(payload: dict[str, Any]) -> str:
    adapter = str(payload.get("adapter") or "")
    if adapter not in SUPPORTED_EXECUTABLES:
        raise ValueError("unsupported adapter")
    return adapter


def _resolve_executable(value: object, adapter: str) -> str | None:
    candidate = str(value or "")
    fallback = {
        "codex": "codex",
        "cursor": "cursor",
        "json-worker": "simple-worker",
        "git-publish": "git-publish",
    }[adapter]
    candidate = candidate or fallback
    if Path(candidate).name not in SUPPORTED_EXECUTABLES[adapter]:
        raise ValueError("executable is not allowed for this adapter")
    if adapter == "git-publish":
        return sys.executable
    if Path(candidate).is_absolute():
        path = Path(candidate).resolve()
        discovered = shutil.which(path.name)
        allowed = path in KNOWN_EXECUTABLE_PATHS[adapter] or bool(
            discovered and Path(discovered).resolve() == path
        )
        if not allowed:
            raise ValueError("absolute executable is not an approved adapter path")
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    discovered = shutil.which(candidate)
    if discovered is None and adapter == "cursor" and candidate == "cursor":
        discovered = shutil.which("cursor-agent")
    return discovered


def _version_argv(adapter: str, executable: str) -> list[str]:
    if adapter == "cursor":
        return [executable, "agent", "--version"]
    if adapter == "json-worker":
        return [executable, "--probe"]
    if adapter == "git-publish":
        return [executable, str(_git_publish_script()), "--version"]
    return [executable, "--version"]


def _execution_argv(
    adapter: str,
    executable: str,
    project: Path,
    session_id: str | None,
    prompt: str,
    access: str,
    context: dict[str, object],
) -> list[str]:
    if adapter == "git-publish":
        if access != "write":
            raise ValueError("Git publish requires external-write access")
        return [executable, str(_git_publish_script())]
    if adapter == "codex":
        sandbox = "read-only" if access == "read" else "workspace-write"
        compact = int(context["provider_compaction_token_limit"])
        shared = [
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--disable",
            "multi_agent",
            "-c",
            'approval_policy="never"',
            "-c",
            f"model_auto_compact_token_limit={compact}",
            "-c",
            'model_auto_compact_token_limit_scope="body_after_prefix"',
        ]
        if session_id:
            return [
                executable,
                "exec",
                "resume",
                *shared,
                "-c",
                f'sandbox_mode="{sandbox}"',
                session_id,
                "-",
            ]
        return [
            executable,
            "exec",
            *shared,
            "--sandbox",
            sandbox,
            "-C",
            str(project),
            "-",
        ]
    if adapter == "cursor":
        if not session_id:
            raise ValueError("Cursor session is required")
        argv = [
            executable,
            "agent",
            "-p",
            "--output-format",
            "stream-json",
            "--sandbox",
            "enabled",
            "--trust",
            "--workspace",
            str(project),
            "--resume",
            session_id,
        ]
        if access == "read":
            argv.extend(["--mode", "plan"])
        else:
            argv.append("--force")
        argv.append(prompt)
        return argv
    if not session_id:
        raise ValueError("JSON Worker session is required")
    return [
        executable,
        "--project",
        str(project),
        "--session",
        session_id,
        "--json",
        "--session-max-bytes",
        str(context["rotation_max_bytes"]),
        "--message-max-bytes",
        str(context["hot_max_bytes"]),
        "--warm-max-bytes",
        str(context["warm_max_bytes"]),
        "--max-turns",
        str(context["max_turns"]),
        "--tool-no-progress-limit",
        str(context["tool_no_progress_limit"]),
    ]


def _git_publish_script() -> Path:
    return Path(__file__).with_name("git_publish_worker.py").resolve()


def _context_policy(value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    return {
        "hot_max_bytes": min(1_048_576, max(4_096, int(raw.get("hot_max_bytes") or 16_384))),
        "warm_max_bytes": min(262_144, max(2_048, int(raw.get("warm_max_bytes") or 8_192))),
        "rotation_max_bytes": min(
            16_777_216, max(16_384, int(raw.get("rotation_max_bytes") or 65_536))
        ),
        "provider_compaction_token_limit": min(
            2_000_000,
            max(8_000, int(raw.get("provider_compaction_token_limit") or 120_000)),
        ),
        "max_turns": min(120, max(1, int(raw.get("max_turns") or 24))),
        "tool_no_progress_limit": min(
            20, max(1, int(raw.get("tool_no_progress_limit") or 6))
        ),
    }


def _cursor_session(executable: str, project: Path) -> str:
    completed = subprocess.run(
        [executable, "agent", "create-chat"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=_safe_environment(),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError("Cursor session creation failed")
    return completed.stdout.strip().splitlines()[-1]


def _safe_environment() -> dict[str, str]:
    exact = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "SSH_AUTH_SOCK",
        "CODEX_HOME",
        "CURSOR_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_BASE_URL",
        "KIMI_MODEL",
        "KIMI_BASE_URL",
        "PLOW_WHIP_SIMPLE_WORKER_STATE_DIR",
        "PLOW_WHIP_KIMI_WORKER_STATE_DIR",
        "PLOW_WHIP_GIT_SSH_IDENTITY_FILE",
    }
    provider_key = re.compile(r"^(?:DEEPSEEK|KIMI)_API_KEY(?:_\d+)?$")
    return {
        key: value
        for key, value in os.environ.items()
        if key in exact or provider_key.fullmatch(key)
    }


def _load_private_env(path: Path) -> None:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"private environment file not found: {path}")
    if path.stat().st_mode & 0o077:
        raise SystemExit("private environment file must not be group/world accessible")
    allowed = re.compile(
        r"^(?:PLOW_WHIP_BRIDGE_TOKEN|PLOW_WHIP_GIT_SSH_IDENTITY_FILE|CURSOR_API_KEY|"
        r"(?:DEEPSEEK|KIMI)_(?:API_KEY(?:_\d+)?|MODEL|BASE_URL)|"
        r"PLOW_WHIP_(?:SIMPLE|KIMI)_WORKER_STATE_DIR)$"
    )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not allowed.fullmatch(key):
            raise SystemExit(f"unsupported private environment entry: {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _parse_usage(output: str) -> dict[str, object]:
    session_id = None
    input_tokens = cached_tokens = output_tokens = 0
    model = None
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        session_id = session_id or _find_string(
            value,
            {"thread_id", "threadId", "session_id", "sessionId", "chat_id", "chatId"},
        )
        model = model or _find_string(value, {"model", "model_name", "modelName"})
        event_input = _find_int(value, {"input_tokens", "inputTokens"})
        explicit_cache = _find_int(
            value,
            {"cached_input_tokens", "cachedInputTokens"},
        )
        cache_read = _find_int(value, {"cacheReadTokens"})
        input_tokens = max(
            input_tokens,
            event_input + cache_read if cache_read else event_input,
        )
        cached_tokens = max(cached_tokens, explicit_cache, cache_read)
        output_tokens = max(
            output_tokens, _find_int(value, {"output_tokens", "outputTokens"})
        )
    return {
        "session_id": session_id,
        "input_tokens": input_tokens,
        "cached_input_tokens": min(input_tokens, cached_tokens),
        "output_tokens": output_tokens,
        "model": model,
    }


def _find_string(value: object, keys: set[str]) -> str | None:
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


def _find_int(value: object, keys: set[str]) -> int:
    result = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, int):
                result = max(result, item)
            result = max(result, _find_int(item, keys))
    elif isinstance(value, list):
        for item in value:
            result = max(result, _find_int(item, keys))
    return result


def _read_output(
    path: Path, offset: int, limit: int, tail_lines: int
) -> tuple[str, int, int]:
    if not path.is_file():
        return "", 0, 0
    size = path.stat().st_size
    if offset < 0:
        start = max(0, size - limit)
    else:
        start = min(max(offset, 0), size)
    with path.open("rb") as handle:
        handle.seek(start)
        body = handle.read(limit)
    text = body.decode(errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > tail_lines:
        text = "".join(lines[-tail_lines:])
        start = size - len(text.encode())
    return text, start, min(size, start + len(text.encode()))


def _read_all(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 262_144))
        return handle.read(262_144).decode(errors="replace")


def _excluded(relative: Path) -> bool:
    return (
        ".git" in relative.parts
        or relative.name in {".env", ".env.local", ".env.production", "credentials.json"}
        or relative.suffix in {".pem", ".key"}
    )


def _redact(value: str) -> str:
    return _SECRET.sub("[REDACTED]", value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _job_id(value: object) -> str:
    candidate = str(value or uuid4().hex)
    try:
        return UUID(candidate).hex
    except ValueError:
        raise ValueError("job_id must be a UUID") from None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) >= 20:
            return f"proc:{fields[19]}"
    except (OSError, ValueError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def _same_process(record: dict[str, Any]) -> bool:
    expected = record.get("process_identity")
    return bool(expected and _process_identity(int(record.get("pid") or 0)) == expected)


def _signal_process(
    process: subprocess.Popen[bytes] | None, pid: int, requested: signal.Signals
) -> None:
    if pid <= 0 or (process is not None and process.poll() is not None):
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), requested)
        elif process is not None:
            process.terminate() if requested == signal.SIGTERM else process.kill()
        else:
            os.kill(pid, requested)
    except (OSError, ProcessLookupError):
        return


if __name__ == "__main__":
    main()
