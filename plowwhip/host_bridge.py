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

from .io_phase import derive_io_phase
from .progress_policy import merge_context_policy
from .secret_policy import redact_secret, require_secret_safe
from .store import write_atomic


MAX_BODY_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 32_768
MAX_JOB_SECONDS = 86_400
RECOVERY_HOLD_SECONDS = 30
ORPHAN_WATCH_INTERVAL_SECONDS = 0.1
SUPPORTED_EXECUTABLES = {
    "codex": {"codex"},
    "cursor": {"cursor", "cursor-agent"},
    "json-worker": {"simple-worker", "kimi-worker"},
    "git-publish": {"git-publish"},
    "local-script": {"local-script"},
}
KNOWN_EXECUTABLE_PATHS = {
    "codex": {Path("/Applications/ChatGPT.app/Contents/Resources/codex")},
    "cursor": {
        Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
    },
    "json-worker": set(),
    "git-publish": set(),
    "local-script": set(),
}
ACTIVE_STATUSES = {
    "dispatching",
    "running",
    "orphan_running",
    "cancelling",
    "recovery_hold",
}
WORKSPACE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
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
                    status, result = 202, manager.cancel(
                        payload["job_id"],
                        force=bool(payload.get("force", False)),
                    )
                elif self.path == "/v1/jobs/extend_timeout":
                    status, result = 200, manager.extend_timeout(
                        payload["job_id"],
                        int(payload["timeout_seconds"]),
                    )
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
        requested_paths = payload.get("paths", [])
        include_text_max_bytes = payload.get("include_text_max_bytes", 0)
        if (
            isinstance(include_text_max_bytes, bool)
            or not isinstance(include_text_max_bytes, int)
            or include_text_max_bytes < 0
            or include_text_max_bytes > 262_144
        ):
            raise ValueError("include_text_max_bytes must be 0-262144")
        if (
            not isinstance(requested_paths, list)
            or len(requested_paths) > 100
            or any(
                not isinstance(value, str)
                or not value
                or Path(value).is_absolute()
                or ".." in Path(value).parts
                for value in requested_paths
            )
        ):
            raise ValueError("snapshot paths must be bounded safe relative paths")
        requested_set = set(requested_paths)
        requested: list[dict[str, object]] = []
        records: list[dict[str, object]] = []
        digest = hashlib.sha256()
        file_count = 0
        for path in sorted(project.rglob("*")):
            if not path.is_file() or _excluded(path.relative_to(project)):
                continue
            stat = path.stat()
            relative = path.relative_to(project).as_posix()
            item: dict[str, object] = {
                "path": relative,
                "bytes": stat.st_size,
                "sha256": _sha256(path),
            }
            encoded = json.dumps(
                item, sort_keys=True, separators=(",", ":")
            ).encode()
            digest.update(encoded)
            file_count += 1
            if len(records) < 20:
                records.append(item)
            if relative in requested_set:
                if (
                    include_text_max_bytes > 0
                    and 0 < stat.st_size <= include_text_max_bytes
                ):
                    try:
                        item = {
                            **item,
                            "text": path.read_text(
                                encoding="utf-8", errors="replace"
                            ),
                        }
                    except OSError:
                        pass
                requested.append(item)
        git: dict[str, object] = {
            "kind": "workspace",
            "available": True,
            "fingerprint": digest.hexdigest(),
            "files": file_count,
            "sample": records,
            "truncated": file_count > len(records),
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
            diff = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "diff",
                    "--no-ext-diff",
                    "--binary",
                    "HEAD",
                    "--",
                ],
                capture_output=True,
                timeout=30,
                check=False,
                env=_safe_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            git["available"] = False
        else:
            if (
                head.returncode == 0
                and status.returncode == 0
                and diff.returncode == 0
            ):
                status_body = status.stdout.encode()
                git["head"] = head.stdout.strip()
                git["status"] = _redact(status.stdout)[:8_192]
                git["status_bytes"] = len(status_body)
                git["status_sha256"] = hashlib.sha256(
                    status_body
                ).hexdigest()
                git["status_truncated"] = len(status_body) > 8_192
                git["diff_bytes"] = len(diff.stdout)
                git["diff_sha256"] = hashlib.sha256(diff.stdout).hexdigest()
            else:
                git["available"] = False
        return {
            "git": git,
            "requested": requested,
            "requested_complete": {
                "declared": requested_paths,
                "found": [item["path"] for item in requested],
                "complete": {item["path"] for item in requested}
                == requested_set,
            },
        }

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
        workspace_kind = str(payload.get("workspace_kind") or "project")
        if workspace_kind == "planner":
            workspace_key = str(payload.get("workspace_key") or "")
            if (
                str(payload.get("access") or "write") != "read"
                or not WORKSPACE_KEY.fullmatch(workspace_key)
            ):
                raise ValueError(
                    "Planner workspace requires a safe key and read-only access"
                )
            project = self.root / "planner-workspaces" / workspace_key
            project.mkdir(parents=True, exist_ok=True)
            project.chmod(0o700)
        elif workspace_kind == "project":
            project = self._project(payload["project_path"])
        else:
            raise ValueError("unsupported workspace kind")
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip() or len(prompt.encode()) > MAX_BODY_BYTES:
            raise ValueError("prompt is empty or too large")
        require_secret_safe(prompt, "HostJob prompt")
        access = str(payload.get("access") or "write")
        if access not in {"read", "write"}:
            raise ValueError("unsupported access mode")
        if access == "read" and adapter not in {
            "codex",
            "cursor",
            "json-worker",
            "git-publish",
            "local-script",
        }:
            raise ValueError(
                "read-only execution requires Codex, Cursor, JSON Worker, "
                "Git, or local-script"
            )
        capability = _validated_capability(
            payload.get("capability"), adapter, access
        )
        timeout_seconds = min(
            max(int(payload.get("timeout_seconds") or 600), 10),
            MAX_JOB_SECONDS,
        )
        session_id = str(payload.get("session_id") or "") or None
        if adapter == "cursor" and session_id is None:
            session_id = _cursor_session(executable, project)
        if adapter == "json-worker" and session_id is None:
            session_id = uuid4().hex
        context_policy = _context_policy(
            payload.get("context_policy"),
            access=access,
            workspace_kind=workspace_kind,
        )
        model = str(payload.get("model") or "default")
        if not MODEL_NAME.fullmatch(model):
            raise ValueError("model must be a safe bounded setting value")
        directory = self._output_directory(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        isolated = adapter not in {"git-publish", "local-script"}
        execution_project = project
        environment = _safe_environment()
        environment["PLOWWHIP_PROGRESS_MODE"] = str(
            context_policy["progress_mode"]
        )
        if isolated:
            if _is_within(directory, project):
                raise ValueError(
                    "Host Bridge state must be outside the project for read-only Codex"
                )
            execution_project = _isolated_workspace(
                project, directory / "workspace"
            )
            temporary = directory / "tmp"
            temporary.mkdir(mode=0o700, exist_ok=True)
            temporary.chmod(0o700)
            environment["TMPDIR"] = str(temporary)
        execution_prompt = (
            prompt.replace(str(project), str(execution_project))
            if isolated
            else prompt
        )
        argv = _execution_argv(
            adapter,
            executable,
            execution_project,
            session_id,
            execution_prompt,
            access,
            context_policy,
            isolated=isolated,
            model=model,
        )
        stdout_path = directory / "stdout.segment-000001.log"
        stderr_path = directory / "stderr.segment-000001.log"
        for stream_path in (stdout_path, stderr_path):
            stream_path.touch(mode=0o600, exist_ok=True)
            stream_path.chmod(0o600)
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
            "usage_kind": (
                "cumulative" if adapter in {"codex", "cursor"} else "single"
            ),
            "model": None if model == "default" else model,
            "requested_model": model,
            "cancel_requested": False,
            "cancel_signal_sent_at": None,
            "force_cancel_requested": False,
            "deadline_reached_at": None,
            "output_ref": f"{job_id}/",
            "context_policy": context_policy,
            "isolated_workspace": isolated,
            "apply_workspace_on_success": bool(
                isolated
                and access == "write"
                and capability["tier"] == "recoverable_workspace_write"
            ),
            "capability": capability,
        }
        capture_argv = [
            sys.executable,
            str(Path(__file__).with_name("stream_capture.py")),
            "--stdout",
            str(stdout_path),
            "--stderr",
            str(stderr_path),
            "--",
            *argv,
        ]
        with self._lock:
            existing = self._read(job_id, required=False)
            if existing:
                return self._refresh(existing)
            self._write(record)
        try:
            process = subprocess.Popen(
                capture_argv,
                cwd=execution_project,
                stdin=subprocess.PIPE if adapter != "cursor" else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
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
            with stderr_path.open("ab", buffering=0) as stderr:
                stderr.write((_redact(str(error))[:500] + "\n").encode())
            with self._lock:
                self._finish(record)
            return dict(record)
        if process.stdin is not None:
            try:
                process.stdin.write(execution_prompt.encode())
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
            record = self._refresh(self._read(_job_id(value)))
        return self._enrich_io_phase(record)

    def extend_timeout(self, value: object, timeout_seconds: int) -> dict[str, object]:
        """Butler soft-timeout: raise wall budget; do not kill a live HostJob."""
        job_id = _job_id(value)
        bounded = min(max(int(timeout_seconds), 10), MAX_JOB_SECONDS)
        with self._lock:
            record = self._read(job_id)
            if record.get("status") not in ACTIVE_STATUSES:
                raise ValueError("HostJob is not active")
            record["timeout_seconds"] = bounded
            record["deadline_reached_at"] = None
            record["timeout_extended_at"] = time.time()
            self._write(record)
            return self._enrich_io_phase(dict(record))

    def _enrich_io_phase(self, record: dict[str, Any]) -> dict[str, Any]:
        stdout_path = (
            self._output_directory(record["job_id"]) / "stdout.segment-000001.log"
        )
        raw = _read_all(stdout_path) if stdout_path.is_file() else ""
        derived = derive_io_phase(raw, now=time.time())
        enriched = dict(record)
        enriched["io_phase"] = derived["io_phase"]
        enriched["last_io_at"] = derived["last_io_at"]
        enriched["last_io_event"] = derived["last_event"]
        worker_failure = derived.get("failure_class")
        if (
            worker_failure
            and enriched.get("failure_class") in {None, "process"}
        ):
            enriched["failure_class"] = worker_failure
        return enriched

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
        stream_refs: dict[str, dict[str, object]] = {}
        has_more = False
        for stream, offset in (
            ("stdout", stdout_offset),
            ("stderr", stderr_offset),
        ):
            path = self._output_directory(job_id) / f"{stream}.segment-000001.log"
            text, start, end = _read_output(
                path, offset, bounded_limit // 2, bounded_lines
            )
            next_offsets[stream] = end
            size = path.stat().st_size if path.is_file() else 0
            has_more = has_more or end < size
            stream_refs[stream] = {
                "path": f"{job_id}/{path.name}",
                "bytes": size,
                "sha256": _file_sha256(path) if path.is_file() else None,
                "complete": end >= size,
            }
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
            "has_more": has_more,
            "stream_refs": stream_refs,
        }

    def cancel(
        self, value: object, *, force: bool = False
    ) -> dict[str, object]:
        job_id = _job_id(value)
        with self._lock:
            record = self._refresh(self._read(job_id))
            if record["status"] not in ACTIVE_STATUSES:
                return record
            record["status"] = "cancelling"
            record["cancel_requested"] = True
            record["force_cancel_requested"] = bool(force)
            record["cancel_signal_sent_at"] = time.time()
            self._write(record)
            process = self._processes.get(job_id)
            pid = int(record.get("pid") or 0)
        _signal_process(
            process,
            pid,
            signal.SIGKILL if force else signal.SIGTERM,
        )
        if force:
            # SIGKILL can be ignored by an uninterruptible host process.  Do not
            # let that leave the control-plane slot occupied forever.
            threading.Thread(
                target=self._force_cancel_deadline,
                args=(job_id,),
                name=f"plowwhip-force-cancel-{job_id[:8]}",
                daemon=True,
            ).start()
        return dict(record)

    def _force_cancel_deadline(self, job_id: str) -> None:
        """Bound force-cancel convergence even when the host never reaps."""
        time.sleep(3)
        with self._lock:
            record = self._read(job_id, required=False)
            if not record or record.get("status") not in ACTIVE_STATUSES:
                return
            if not record.get("force_cancel_requested"):
                return
            record.update(
                {
                    "status": "cancelled",
                    "returncode": 130,
                    "failure_class": "cancelled",
                    "force_cancel_converged_at": time.time(),
                }
            )
            self._finish(record)
            self._write(record)

    def _wait(
        self,
        job_id: str,
        process: subprocess.Popen[bytes],
        timeout_seconds: int,
    ) -> None:
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            with self._lock:
                record = self._read(job_id, required=False)
                if record and record.get("status") in ACTIVE_STATUSES:
                    record["deadline_reached_at"] = (
                        record.get("deadline_reached_at") or time.time()
                    )
                    self._write(record)
            process.wait()
        with self._lock:
            record = self._read(job_id, required=False)
            self._processes.pop(job_id, None)
            if not record:
                return
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

    def _recover(self) -> None:
        now = time.time()
        for path in self.root.glob("*.json"):
            watch_orphan = False
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                _job_id(record.get("job_id"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if record.get("status") in ACTIVE_STATUSES:
                if not record.get("pid"):
                    hold_until = min(
                        float(record.get("recovery_hold_until") or now + RECOVERY_HOLD_SECONDS),
                        now + RECOVERY_HOLD_SECONDS,
                    )
                    record.update({
                        "status": "recovery_hold",
                        "failure_class": "dispatch_outcome_unknown",
                        "recovery_hold_until": hold_until,
                    })
                elif _same_process(record):
                    record["status"] = "orphan_running"
                    watch_orphan = True
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
                if watch_orphan:
                    threading.Thread(
                        target=self._watch_orphan,
                        args=(record["job_id"],),
                        name=f"plowwhip-orphan-{record['job_id'][:8]}",
                        daemon=True,
                    ).start()

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
            record.get("status") == "recovery_hold"
            and time.time() >= float(record.get("recovery_hold_until") or 0)
        ):
            record.update(
                {
                    "status": "interrupted",
                    "returncode": 125,
                    "failure_class": "dispatch_outcome_unknown",
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
                        "returncode": (
                            130
                            if record.get("cancel_requested")
                            else 125
                        ),
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

    def _watch_orphan(self, job_id: str) -> None:
        """Report deadlines and observe explicit lifecycle stop requests."""
        while True:
            with self._lock:
                record = self._read(job_id, required=False)
                if not record or record.get("status") not in ACTIVE_STATUSES:
                    return
                pid = int(record.get("pid") or 0)
                alive = bool(pid and _same_process(record))
                timed_out = time.time() >= (
                    float(record.get("started_at") or 0)
                    + int(record.get("timeout_seconds") or 600)
                )
                cancel_requested = bool(record.get("cancel_requested"))
            if not alive:
                with self._lock:
                    current = self._read(job_id, required=False)
                    if current:
                        self._refresh(current)
                return
            if timed_out:
                with self._lock:
                    current = self._read(job_id, required=False)
                    if current and not current.get("deadline_reached_at"):
                        current["deadline_reached_at"] = time.time()
                        self._write(current)
            if cancel_requested:
                with self._lock:
                    current = self._read(job_id, required=False)
                    if not current:
                        return
                    force = bool(current.get("force_cancel_requested"))
                    signal_sent = current.get("cancel_signal_sent_at")
                    if not signal_sent or force:
                        current["cancel_signal_sent_at"] = time.time()
                        self._write(current)
                    else:
                        force = False
                if not signal_sent or force:
                    _signal_process(
                        None,
                        pid,
                        signal.SIGKILL if force else signal.SIGTERM,
                    )
            time.sleep(ORPHAN_WATCH_INTERVAL_SECONDS)

    def _finish(self, record: dict[str, Any]) -> None:
        directory = self._output_directory(record["job_id"])
        stdout_path = directory / "stdout.segment-000001.log"
        staged_workspace = directory / "workspace"
        if record.get("adapter") == "json-worker" and stdout_path.is_file():
            session_id = str(record.get("session_id") or "") or None
            if not session_id:
                session_id = _session_id_from_stdout(_read_all(stdout_path))
            if session_id:
                _append_json_worker_assistant_stdout(stdout_path, session_id)
                record["session_id"] = session_id
            # LIVE-DS-11: DeepSeek often write_file's PLOWWHIP_PLANNER_RESULT.md
            # into the isolated workspace instead of emitting stdout JSON.
            if staged_workspace.is_dir():
                _harvest_structured_result_files(staged_workspace, stdout_path)
        # LIVE-DS-26: apply staged workspace when the HostJob completed with a
        # payload even if the adapter exit code is nonzero (Cursor/json-worker
        # often write Artifact then exit≠0). Empty staged trees still require
        # exit 0 to avoid applying a no-op failure as success.
        staged_has_payload = (
            staged_workspace.is_dir()
            and _staged_workspace_has_payload(staged_workspace)
        )
        if (
            record.get("apply_workspace_on_success")
            and record.get("status") == "completed"
            and staged_workspace.is_dir()
            and (int(record.get("returncode") or 0) == 0 or staged_has_payload)
        ):
            try:
                record["workspace_apply"] = _apply_recoverable_workspace(
                    staged_workspace,
                    Path(str(record["project_path"])).resolve(),
                )
            except (OSError, ValueError) as error:
                record.update(
                    {
                        "returncode": 126,
                        "failure_class": "scope",
                        "workspace_apply": {
                            "applied": False,
                            "error": type(error).__name__,
                        },
                    }
                )
                with (
                    directory / "stderr.segment-000001.log"
                ).open("ab", buffering=0) as stderr:
                    stderr.write(
                        (
                            "workspace policy rejected staged changes: "
                            f"{type(error).__name__}\n"
                        ).encode()
                    )
        stdout = _read_all(stdout_path)
        usage = _parse_usage(
            stdout,
            default_usage_kind=(
                "cumulative"
                if record.get("adapter") in {"codex", "cursor"}
                else "single"
            ),
        )
        record.update(usage)
        # Preserve json-worker typed failure over generic process exit.
        derived = derive_io_phase(stdout, now=time.time())
        record["io_phase"] = derived["io_phase"]
        record["last_io_at"] = derived["last_io_at"]
        if derived.get("failure_class") and record.get("failure_class") in {
            None,
            "process",
        }:
            record["failure_class"] = derived["failure_class"]
        if (
            record.get("failure_class")
            in {"internal_tool_no_progress", "tool_no_progress_limit_exceeded"}
            and any(
                marker in stdout
                for marker in (
                    '"type": "model.request_started"',
                    '"type":"model.request_started"',
                    '"type": "model.response_started"',
                    '"type":"model.response_started"',
                )
            )
        ):
            # A worker-reported turn/tool ceiling during model I/O is advisory:
            # preserve it for diagnosis but never reinterpret it as a Bridge
            # transport or process failure.
            record["turn_limit_advisory"] = True
        record["ended_at"] = record.get("ended_at") or time.time()
        record["duration_ms"] = max(
            0, int((record["ended_at"] - record["started_at"]) * 1_000)
        )
        if record.get("isolated_workspace"):
            shutil.rmtree(
                self._output_directory(record["job_id"]) / "workspace",
                ignore_errors=True,
            )
            shutil.rmtree(
                self._output_directory(record["job_id"]) / "tmp",
                ignore_errors=True,
            )
        for stream_path in (stdout_path, directory / "stderr.segment-000001.log"):
            if stream_path.is_file():
                stream_path.chmod(0o600)
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
        "local-script": "local-script",
    }[adapter]
    candidate = candidate or fallback
    if Path(candidate).name not in SUPPORTED_EXECUTABLES[adapter]:
        raise ValueError("executable is not allowed for this adapter")
    if adapter in {"git-publish", "local-script"}:
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
    if discovered is None:
        discovered = next(
            (
                str(path)
                for path in KNOWN_EXECUTABLE_PATHS[adapter]
                if path.is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
    return discovered


def _isolated_workspace(source: Path, target: Path) -> Path:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if _workspace_ignored(Path(name))
        }

    try:
        shutil.copytree(source, target, symlinks=True, ignore=ignore)
        target.chmod(0o700)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _validated_capability(
    value: object, adapter: str, access: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("HostJob requires a structured capability")
    tier = str(value.get("tier") or "")
    expected = (
        "read_only"
        if access == "read"
        else (
            "authorized_external_effect"
            if adapter == "git-publish"
            else "recoverable_workspace_write"
        )
    )
    if adapter == "local-script" and access == "read":
        expected = "read_only"
    if tier != expected:
        raise ValueError(f"{access} HostJob requires capability tier {expected}")
    result = {
        "tier": tier,
        "project_id": str(value.get("project_id") or ""),
        "task_id": str(value.get("task_id") or ""),
        "spec_revision": int(value.get("spec_revision") or 0),
    }
    if tier == "read_only":
        actions = value.get("allowed_actions", ["read_workspace"])
        if actions != ["read_workspace"]:
            raise ValueError("read-only capability has invalid actions")
        result.update(
            {
                "allowed_actions": actions,
                "target_scope": str(
                    value.get("target_scope") or "project_workspace"
                ),
            }
        )
    elif tier == "recoverable_workspace_write":
        actions = value.get(
            "allowed_actions", ["write_workspace", "run_checks"]
        )
        if (
            actions != ["write_workspace", "run_checks"]
            or value.get("target_scope", "project_workspace")
            != "project_workspace"
        ):
            raise ValueError("recoverable capability has invalid scope or actions")
        result.update(
            {
                "allowed_actions": actions,
                "target_scope": "project_workspace",
            }
        )
    if tier == "authorized_external_effect":
        authorization = value.get("authorization")
        if not isinstance(authorization, dict):
            raise ValueError("external effect requires authorization facts")
        required = {
            "project_id",
            "task_id",
            "spec_revision",
            "action_kind",
            "target_scope",
            "expires_at",
        }
        if (
            any(authorization.get(key) in {None, ""} for key in required)
            or authorization.get("project_id") != result["project_id"]
            or authorization.get("task_id") != result["task_id"]
            or int(authorization.get("spec_revision") or 0)
            != result["spec_revision"]
            or float(authorization.get("expires_at") or 0) <= time.time()
            or str(authorization.get("action_kind") or "")
            not in {"git_publish", "git_publish_force_with_lease"}
        ):
            raise ValueError("external authorization is stale or out of scope")
        result["authorization"] = {
            key: authorization[key] for key in sorted(required)
        }
    return result


def _staged_workspace_has_payload(staged: Path) -> bool:
    """True when the staged sandbox contains at least one file or symlink."""
    if not staged.is_dir():
        return False
    for _relative, entry in _workspace_entries(staged).items():
        if entry[0] in {"file", "symlink"}:
            return True
    return False


def _apply_recoverable_workspace(staged: Path, source: Path) -> dict[str, object]:
    if not staged.is_dir() or not source.is_dir():
        raise ValueError("recoverable workspace roots must exist")
    staged_entries = _workspace_entries(staged)
    source_entries = _workspace_entries(source)
    removed = []
    missing = set(source_entries) - set(staged_entries)
    for relative in sorted(missing, key=lambda value: len(value.parts)):
        if any(parent in missing for parent in relative.parents):
            continue
        target = source / relative
        if target.exists() or target.is_symlink():
            removed.append(_move_to_recoverable_name(target).relative_to(source).as_posix())

    created = modified = 0
    for relative, staged_entry in sorted(
        staged_entries.items(), key=lambda item: (len(item[0].parts), item[0].as_posix())
    ):
        target = source / relative
        source_entry = source_entries.get(relative)
        if staged_entry[0] == "directory":
            if source_entry and source_entry[0] == "directory":
                continue
            if target.exists() or target.is_symlink():
                _move_to_recoverable_name(target)
                modified += 1
            else:
                created += 1
            target.mkdir(parents=True, exist_ok=True)
            continue
        if source_entry == staged_entry:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_entry and source_entry[0] != staged_entry[0]:
            _move_to_recoverable_name(target)
        if staged_entry[0] == "symlink":
            link = (staged / relative).readlink()
            resolved = (
                link
                if link.is_absolute()
                else (target.parent / link).resolve()
            )
            if link.is_absolute():
                raise ValueError("new absolute workspace symlink is forbidden")
            try:
                Path(resolved).relative_to(source)
            except ValueError as error:
                raise ValueError("workspace symlink escapes authorized scope") from error
            if target.exists() or target.is_symlink():
                _move_to_recoverable_name(target)
            target.symlink_to(link)
        elif staged_entry[0] == "file":
            temporary = target.with_name(
                f".{target.name}.plowwhip-{uuid4().hex}.tmp"
            )
            shutil.copy2(staged / relative, temporary, follow_symlinks=False)
            os.replace(temporary, target)
        else:
            raise ValueError("special workspace files are forbidden")
        if source_entry:
            modified += 1
        else:
            created += 1
    return {
        "applied": True,
        "created": created,
        "modified": modified,
        "recoverably_removed": removed,
    }


def _workspace_entries(root: Path) -> dict[Path, tuple[str, object]]:
    result: dict[Path, tuple[str, object]] = {}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for name in directory_names:
            path = current_path / name
            relative = path.relative_to(root)
            if _workspace_ignored(relative):
                continue
            if path.is_symlink():
                result[relative] = ("symlink", path.readlink().as_posix())
            else:
                result[relative] = ("directory", None)
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root)
            if _workspace_ignored(relative):
                continue
            if path.is_symlink():
                result[relative] = ("symlink", path.readlink().as_posix())
            elif path.is_file():
                result[relative] = (
                    "file",
                    path.stat().st_mode & 0o777,
                    _file_sha256(path),
                )
            else:
                result[relative] = ("special", None)
    return result


def _workspace_ignored(relative: Path) -> bool:
    return (
        "__pycache__" in relative.parts
        or ".git" in relative.parts
        or relative.name == ".env"
        or (
            relative.name.startswith(".env.")
            and relative.name
            not in {".env.example", ".env.sample", ".env.template"}
        )
        or relative.name == "credentials.json"
        or relative.suffix in {".pem", ".key"}
    )


def _move_to_recoverable_name(path: Path) -> Path:
    now = time.time()
    for offset in range(86_400):
        timestamp = time.strftime(
            "%Y%m%d%H%M%S", time.localtime(now + offset)
        )
        target = path.with_name(f"{path.name}.rm.{timestamp}")
        if not target.exists() and not target.is_symlink():
            os.replace(path, target)
            return target
    raise ValueError("cannot allocate recoverable removal name")


def _version_argv(adapter: str, executable: str) -> list[str]:
    if adapter == "cursor":
        return [executable, "agent", "--version"]
    if adapter == "json-worker":
        return [executable, "--probe"]
    if adapter == "git-publish":
        return [executable, str(_git_publish_script()), "--version"]
    if adapter == "local-script":
        return [executable, str(_local_script_worker()), "--version"]
    return [executable, "--version"]


def _execution_argv(
    adapter: str,
    executable: str,
    project: Path,
    session_id: str | None,
    prompt: str,
    access: str,
    context: dict[str, object],
    *,
    isolated: bool = False,
    model: str = "default",
) -> list[str]:
    if adapter == "git-publish":
        try:
            operation = json.loads(prompt).get("operation", "publish")
        except (AttributeError, json.JSONDecodeError) as error:
            raise ValueError("Git publish prompt is invalid") from error
        expected_access = "read" if operation == "inspect" else "write"
        if access != expected_access:
            raise ValueError(
                f"Git {operation} requires {expected_access} access"
            )
        return [executable, str(_git_publish_script())]
    if adapter == "local-script":
        try:
            payload = json.loads(prompt)
        except json.JSONDecodeError as error:
            raise ValueError("local_script prompt is invalid") from error
        if not isinstance(payload, dict) or payload.get("kind") != "local_script":
            raise ValueError("local_script prompt kind is required")
        return [executable, str(_local_script_worker())]
    if adapter == "codex":
        sandbox = (
            "workspace-write"
            if access == "write" or isolated
            else "read-only"
        )
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
        if model != "default":
            shared.extend(["--model", model])
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
            argv.extend(["--mode", "ask"])
        else:
            argv.append("--force")
        if model != "default":
            argv.extend(["--model", model])
        argv.append(prompt)
        return argv
    if not session_id:
        raise ValueError("JSON Worker session is required")
    argv = [
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
    # MECH-06: json-worker executables use model_transport=env (never --model).
    return argv


def _git_publish_script() -> Path:
    return Path(__file__).with_name("git_publish_worker.py").resolve()


def _local_script_worker() -> Path:
    return Path(__file__).with_name("local_script_worker.py").resolve()


def _context_policy(
    value: object,
    *,
    access: str = "write",
    workspace_kind: str = "project",
) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    merged = merge_context_policy(
        raw, access=access, workspace_kind=workspace_kind
    )
    return {
        "hot_max_bytes": min(
            1_048_576, max(4_096, int(merged.get("hot_max_bytes") or 16_384))
        ),
        "warm_max_bytes": min(
            262_144, max(2_048, int(merged.get("warm_max_bytes") or 8_192))
        ),
        "rotation_max_bytes": min(
            16_777_216,
            max(16_384, int(merged.get("rotation_max_bytes") or 65_536)),
        ),
        "provider_compaction_token_limit": min(
            2_000_000,
            max(
                8_000,
                int(merged.get("provider_compaction_token_limit") or 120_000),
            ),
        ),
        "max_turns": min(120, max(1, int(merged.get("max_turns") or 24))),
        "tool_no_progress_limit": min(
            256, max(1, int(merged.get("tool_no_progress_limit") or 12))
        ),
        "progress_mode": merged["progress_mode"],
        **(
            {"progress_delivery": "audit_report"}
            if merged.get("progress_delivery") == "audit_report"
            else {}
        ),
    }


def _session_id_from_stdout(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("session_id", "sessionId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _find_json_worker_session_file(
    session_id: str,
    *,
    search_roots: list[Path] | None = None,
) -> Path | None:
    if not session_id or any(ch in session_id for ch in "/\\"):
        return None
    roots = search_roots or [
        Path.home() / ".plow-whip-web" / "simple-worker",
        Path.home() / ".plow-whip-web" / "kimi-worker",
        Path.home() / ".plow-whip" / "simple-worker",
    ]
    name = f"{session_id}.jsonl"
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / name
        if direct.is_file():
            return direct
        try:
            matches = list(root.glob(f"**/{name}"))
        except OSError:
            continue
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)
    return None


def _structured_marker_texts(text: str) -> list[str]:
    markers = ("PLOWWHIP_PLANNER_RESULT ", "PLOWWHIP_CHECKER_RESULT ")
    found: list[str] = []
    for marker in markers:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            rest = text[index + len(marker) :].lstrip()
            # Ignore prose like "PLOWWHIP_PLANNER_RESULT written to temp file."
            if not rest.startswith("{"):
                start = index + len(marker)
                continue
            payload = _extract_json_object_text(rest)
            if payload is not None:
                found.append(marker + payload)
            start = index + len(marker)
    return found


def _extract_json_object_text(text: str) -> str | None:
    text = text.lstrip()
    if not text.startswith("{"):
        return None
    try:
        payload, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return text[:end]


def _harvest_structured_result_files(
    workspace: Path, stdout_path: Path
) -> bool:
    """Pull structured Planner/Checker payloads written into the job workspace."""
    if not workspace.is_dir():
        return False
    try:
        existing = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    harvested: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if not (
            name.startswith("PLOWWHIP_PLANNER_RESULT")
            or name.startswith("PLOWWHIP_CHECKER_RESULT")
        ):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not body:
            continue
        if name.startswith("PLOWWHIP_CHECKER_RESULT"):
            marker = "PLOWWHIP_CHECKER_RESULT "
        else:
            marker = "PLOWWHIP_PLANNER_RESULT "
        if body.startswith(marker):
            content = body
        elif body.lstrip().startswith("{"):
            payload = _extract_json_object_text(body)
            if payload is None:
                continue
            content = marker + payload
        else:
            markers = _structured_marker_texts(body)
            if not markers:
                continue
            content = markers[-1]
        if content in existing or content in harvested:
            continue
        harvested.append(
            json.dumps(
                {
                    "type": "message",
                    "message": {"role": "assistant", "content": content},
                },
                ensure_ascii=False,
            )
        )
    if not harvested:
        return False
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(harvested))
        handle.write("\n")
    try:
        stdout_path.chmod(0o600)
    except OSError:
        pass
    return True


def _tool_message_text(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(payload, dict):
        for key in ("content", "text", "body"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return content


def _append_json_worker_assistant_stdout(
    stdout_path: Path,
    session_id: str,
    *,
    search_roots: list[Path] | None = None,
) -> bool:
    """Append session texts so lifecycle can parse structured Provider results."""
    session_path = None
    for _attempt in range(8):
        session_path = _find_json_worker_session_file(
            session_id, search_roots=search_roots
        )
        if session_path is not None and session_path.is_file():
            break
        time.sleep(0.05)
    if session_path is None or not session_path.is_file():
        return False
    try:
        existing = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    # Only skip when a real JSON payload already exists (not prose claims).
    if _structured_marker_texts(existing):
        return False
    harvested: list[str] = []
    marker_lines: list[str] = []
    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            marker_lines.extend(_structured_marker_texts(line))
            continue
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        text = _tool_message_text(content) if role == "tool" else content
        # Prefer decoded tool payloads; avoid appending escaped JSON duplicates.
        marker_lines.extend(_structured_marker_texts(text))
        if role == "assistant":
            harvested.append(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                    },
                    ensure_ascii=False,
                )
            )
        elif role == "tool" and (
            "PLOWWHIP_PLANNER_RESULT " in text
            or "PLOWWHIP_CHECKER_RESULT " in text
        ):
            harvested.append(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": text,
                        },
                    },
                    ensure_ascii=False,
                )
            )
    seen_markers = set()
    for marker_line in marker_lines:
        if marker_line in seen_markers:
            continue
        seen_markers.add(marker_line)
        harvested.append(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": marker_line,
                    },
                },
                ensure_ascii=False,
            )
        )
    if not harvested:
        return False
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(harvested))
        handle.write("\n")
    try:
        stdout_path.chmod(0o600)
    except OSError:
        pass
    return True


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
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "KIMI_BASE_URL",
        "KIMI_MODEL",
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
        r"(?:DEEPSEEK|KIMI)_(?:API_KEY(?:_\d+)?|BASE_URL|MODEL)|"
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


def _parse_usage(
    output: str, *, default_usage_kind: str = "cumulative"
) -> dict[str, object]:
    if default_usage_kind not in {"single", "cumulative"}:
        raise ValueError("invalid default usage kind")
    session_id = None
    input_tokens = cached_tokens = output_tokens = 0
    model = None
    usage_kind = default_usage_kind
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        explicit_kind = _find_string(value, {"usage_kind", "usageKind"})
        if explicit_kind in {"single", "cumulative"}:
            usage_kind = explicit_kind
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
        "usage_kind": usage_kind,
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
    observation = offset < 0
    if observation:
        start = max(0, size - limit)
    else:
        start = min(max(offset, 0), size)
    with path.open("rb") as handle:
        handle.seek(start)
        body = handle.read(limit)
    raw_end = min(size, start + len(body))
    text = body.decode(errors="replace")
    lines = text.splitlines(keepends=True)
    if observation and len(lines) > tail_lines:
        text = "".join(lines[-tail_lines:])
        start = size - len(text.encode())
    return text, start, (
        min(size, start + len(text.encode())) if observation else raw_end
    )


def _read_all(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_bytes().decode(errors="replace")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relative: Path) -> bool:
    return (
        ".git" in relative.parts
        or relative.name in {".env", ".env.local", ".env.production", "credentials.json"}
        or relative.suffix in {".pem", ".key"}
    )


def _redact(value: str) -> str:
    return redact_secret(value)


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
