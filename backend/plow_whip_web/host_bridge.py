from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any

from plow_whip_web.config import load_private_env
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.runtime.verification import VerificationEngine
from plow_whip_web.runtime.evidence import snapshot_environment
from plow_whip_web.security import Redactor


MAX_BODY_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 262_144
MAX_OUTPUT_TAIL_BYTES = 16_384
MAX_ARTIFACT_HASH_BYTES = 67_108_864
PROBE_TIMEOUT_SECONDS = 15
SUPPORTED_ADAPTERS = {"codex", "cursor", "json-worker"}
_LOCAL_PROCESSES: dict[tuple[str, str], subprocess.Popen[str]] = {}
_LOCAL_PROCESSES_LOCK = threading.Lock()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the restricted plow-whip host CLI bridge")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", action="append", type=Path, required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Private local environment file (must not be accessible by group or others)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".plow-whip-web" / "host-bridge",
        help="Persistent sanitized Host Job state",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.env_file is not None:
        try:
            if not load_private_env(args.env_file):
                raise SystemExit(f"private environment file not found: {args.env_file}")
        except ValueError as error:
            raise SystemExit(str(error)) from error
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
                elif self.path == "/v1/verify":
                    self._send(200, verify(payload, roots))
                elif self.path == "/v1/artifacts/inspect":
                    self._send(200, inspect_artifacts(payload, roots))
                elif self.path == "/v1/evidence/snapshot":
                    self._send(200, evidence_snapshot(payload, roots))
                elif self.path == "/v1/artifacts/open":
                    self._send(200, open_artifact(payload, roots))
                elif self.path == "/v1/jobs/start":
                    self._send(202, jobs.start(payload))
                elif self.path == "/v1/jobs/status":
                    self._send(200, jobs.status(str(payload["job_id"])))
                elif self.path == "/v1/jobs/output":
                    self._send(200, jobs.output(
                        str(payload["job_id"]),
                        stdout_offset=int(payload.get("stdout_offset") or 0),
                        stderr_offset=int(payload.get("stderr_offset") or 0),
                        limit=int(payload.get("limit") or 32_768),
                        tail_lines=int(payload.get("tail_lines") or 20),
                    ))
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
            "output_ref": f"{job_id}/",
            "output_segments": [],
            "output_bytes": {"stdout": 0, "stderr": 0, "total": 0},
            "duration_ms": 0,
            "failure_class": None,
            "detected_failure_class": None,
            "input_tokens": 0,
            "cached_input_tokens": 0,
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
                sanitized_error = Redactor.redact(str(error))[:1000]
                self._append_output(record, "stderr", sanitized_error)
                record.update({
                    "status": "completed", "returncode": 126,
                    "failure_class": "command_unavailable",
                    "error_summary": sanitized_error,
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
            with _LOCAL_PROCESSES_LOCK:
                _LOCAL_PROCESSES[(str(self.root), job_id)] = process
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
            record = self._refresh(self._read(_job_id(job_id)))
            result = dict(record)
        project_path = result.get("project_path")
        result["progress_evidence"] = (
            _workspace_progress(
                Path(str(project_path)),
                excluded_roots=(self.root,),
            )
            if project_path
            else {"kind": "workspace", "available": False}
        )
        return result

    def output(
        self,
        job_id: str,
        *,
        stdout_offset: int,
        stderr_offset: int,
        limit: int,
        tail_lines: int = 20,
    ) -> dict[str, object]:
        job_id = _job_id(job_id)
        bounded_limit = min(max(limit, 1024), 262_144)
        bounded_lines = min(max(tail_lines, 1), 500)
        with self._lock:
            record = self._refresh(self._read(job_id))
            chunks: list[dict[str, object]] = []
            next_offsets: dict[str, int] = {}
            per_stream = max(512, bounded_limit // 2)
            for stream, offset in (
                ("stdout", stdout_offset),
                ("stderr", stderr_offset),
            ):
                if offset < 0:
                    offset = self._tail_offset(
                        job_id,
                        stream,
                        max_bytes=per_stream,
                        max_lines=bounded_lines,
                    )
                data, next_offset, refs = self._read_output_range(
                    job_id, stream, offset=offset, limit=per_stream
                )
                next_offsets[stream] = next_offset
                if data:
                    text = Redactor.redact(data.decode("utf-8", errors="replace"))
                    chunks.append({
                        "kind": _output_kind(stream, text),
                        "stream": stream,
                        "offset": offset,
                        "next_offset": next_offset,
                        "text": text,
                        "refs": refs,
                    })
            return {
                "job_id": job_id,
                "status": record.get("status"),
                "heartbeat_at": record.get("heartbeat_at"),
                "output_ref": record.get("output_ref"),
                "chunks": chunks,
                "next_offsets": next_offsets,
                "has_more": any(
                    int(record.get("output_bytes", {}).get(stream) or 0)
                    > next_offsets[stream]
                    for stream in ("stdout", "stderr")
                ),
            }

    def _tail_offset(
        self,
        job_id: str,
        stream: str,
        *,
        max_bytes: int,
        max_lines: int,
    ) -> int:
        paths = sorted(
            self._output_directory(job_id).glob(
                f"{stream}.[0-9][0-9][0-9][0-9][0-9][0-9].log"
            )
        )
        total = sum(path.stat().st_size for path in paths)
        start = max(0, total - max_bytes)
        data, _, _ = self._read_output_range(
            job_id, stream, offset=start, limit=max_bytes
        )
        if not data:
            return total
        lines = data.splitlines(keepends=True)
        if len(lines) <= max_lines:
            return start
        return total - sum(len(line) for line in lines[-max_lines:])

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
            record = self._read(job_id, required=False)
            if record is None:
                self._processes.pop(job_id, None)
                return
            cancelled = bool(record.get("cancel_requested"))
            if timed_out:
                status, returncode, failure = "completed", 124, "timeout"
            elif cancelled:
                status, returncode, failure = "cancelled", 130, "cancelled"
            else:
                returncode = int(process.returncode or 0)
                status = "completed"
                failure = None if returncode == 0 else (
                    record.get("detected_failure_class")
                    or _provider_failure_class(
                        returncode,
                        str(record.get("stdout") or ""),
                        str(record.get("stderr") or ""),
                    )
                )
            record.update({
                "status": status,
                "returncode": returncode,
                "failure_class": failure,
                "error_summary": failure,
                "duration_ms": int((monotonic() - started) * 1000),
                "heartbeat_at": _utc_now(),
                "finished_at": _utc_now(),
            })
            self._processes.pop(job_id, None)
            with _LOCAL_PROCESSES_LOCK:
                _LOCAL_PROCESSES.pop((str(self.root), job_id), None)
            self._write(record)

    def _read_stream(self, job_id: str, stream: Any, field: str) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            with self._lock:
                record = self._read(job_id, required=False)
                if record is None:
                    return
                sanitized = Redactor.redact(line)
                self._append_output(record, field, sanitized)
                if _provider_failure_class(1, sanitized, "") == "provider_capacity":
                    record["detected_failure_class"] = "provider_capacity"
                record["heartbeat_at"] = _utc_now()
                if field == "stdout":
                    parsed = _parse_stream(sanitized)
                    record["session_id"] = record.get("session_id") or parsed["session_id"]
                    record["input_tokens"] = max(
                        int(record.get("input_tokens") or 0), int(parsed["input_tokens"])
                    )
                    record["cached_input_tokens"] = max(
                        int(record.get("cached_input_tokens") or 0),
                        int(parsed["cached_input_tokens"]),
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
                    "error_summary": "cancelled" if not _process_alive(pid) else "process_unconfirmed",
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
            output_directory = self._output_directory(job_id)
            if not any(output_directory.glob("*.log")):
                for stream in ("stdout", "stderr"):
                    legacy_tail = str(record.get(stream) or "")
                    if legacy_tail:
                        self._append_output(record, stream, Redactor.redact(legacy_tail))
            self._sync_output_index(record)
            if record.get("status") in {"dispatching", "running", "orphan_running", "recovery_hold"}:
                if record.get("status") == "dispatching" and not record.get("pid"):
                    record.update({
                        "status": "recovery_hold",
                        "failure_class": "dispatch_outcome_unknown",
                        "error_summary": "dispatch_outcome_unknown",
                        "heartbeat_at": _utc_now(),
                    })
                    self._write(record)
                    continue
                with _LOCAL_PROCESSES_LOCK:
                    local_process = _LOCAL_PROCESSES.get((str(self.root), job_id))
                if local_process is not None and local_process.poll() is None:
                    self._processes[job_id] = local_process
                record["status"] = (
                    "orphan_running"
                    if local_process is not None or _same_process(record)
                    else "interrupted"
                )
                if record["status"] == "interrupted":
                    record.update({
                        "returncode": 125,
                        "failure_class": "external_interruption",
                        "error_summary": "external_interruption",
                        "finished_at": _utc_now(),
                    })
                record["heartbeat_at"] = _utc_now()
                self._write(record)
            elif record.get("status") == "cancelling" and not _same_process(record):
                record.update({
                    "status": "cancelled", "returncode": 130,
                    "failure_class": "cancelled", "error_summary": "cancelled",
                    "finished_at": _utc_now(),
                })
                self._write(record)
            else:
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
                    "error_summary": "cancelled" if record.get("cancel_requested") else "external_interruption",
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
        record.setdefault("stdout", "")
        record.setdefault("stderr", "")
        record.setdefault("output_ref", f"{job_id}/")
        record.setdefault("output_segments", [])
        record.setdefault("output_bytes", {"stdout": 0, "stderr": 0, "total": 0})
        if record.get("status") in {"completed", "cancelled", "interrupted"}:
            record["carry_forward_ref"] = f"{job_id}/carry-forward.json"
        target = self.root / f"{job_id}.json"
        self._write_json(target, record)
        if record.get("status") in {"completed", "cancelled", "interrupted"}:
            self._write_carry_forward(record)

    def _append_output(self, record: dict[str, Any], stream: str, value: str) -> None:
        job_id = _job_id(record.get("job_id"))
        self._output_directory(job_id)
        chunks = _utf8_chunks(value, MAX_OUTPUT_BYTES)
        segments = list(record.get("output_segments") or [])
        stream_segments = [item for item in segments if item.get("stream") == stream]
        for chunk in chunks:
            encoded = chunk.encode("utf-8")
            current = stream_segments[-1] if stream_segments else None
            if current is None or int(current["bytes"]) + len(encoded) > MAX_OUTPUT_BYTES:
                index = len(stream_segments)
                relative = f"{job_id}/{stream}.{index:06d}.log"
                current = {
                    "stream": stream, "index": index, "ref": relative,
                    "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                }
                segments.append(current)
                stream_segments.append(current)
            target = self.root / str(current["ref"])
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(descriptor, "ab", closefd=True) as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            current["bytes"] = target.stat().st_size
            current["sha256"] = _sha256_file(target)
        record["output_ref"] = f"{job_id}/"
        record["output_segments"] = sorted(
            segments,
            key=lambda item: (
                0 if item["stream"] == "stdout" else 1, int(item["index"])
            ),
        )
        totals = {
            name: sum(
                int(item["bytes"]) for item in segments if item.get("stream") == name
            )
            for name in ("stdout", "stderr")
        }
        record["output_bytes"] = {**totals, "total": totals["stdout"] + totals["stderr"]}
        record[stream] = _append_bounded(
            str(record.get(stream) or ""), value, MAX_OUTPUT_TAIL_BYTES
        )

    def _sync_output_index(self, record: dict[str, Any]) -> None:
        job_id = _job_id(record.get("job_id"))
        directory = self._output_directory(job_id)
        segments: list[dict[str, object]] = []
        totals = {"stdout": 0, "stderr": 0}
        for stream in ("stdout", "stderr"):
            paths = sorted(directory.glob(f"{stream}.[0-9][0-9][0-9][0-9][0-9][0-9].log"))
            for index, path in enumerate(paths):
                size = path.stat().st_size
                segments.append({
                    "stream": stream, "index": index,
                    "ref": f"{job_id}/{path.name}", "bytes": size,
                    "sha256": _sha256_file(path),
                })
                totals[stream] += size
            record[stream] = _tail_from_files(paths, MAX_OUTPUT_TAIL_BYTES)
        record["output_ref"] = f"{job_id}/"
        record["output_segments"] = segments
        record["output_bytes"] = {**totals, "total": totals["stdout"] + totals["stderr"]}

    def _output_directory(self, job_id: str) -> Path:
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        return directory

    def _read_output_range(
        self, job_id: str, stream: str, *, offset: int, limit: int
    ) -> tuple[bytes, int, list[str]]:
        paths = sorted(
            self._output_directory(job_id).glob(
                f"{stream}.[0-9][0-9][0-9][0-9][0-9][0-9].log"
            )
        )
        remaining_skip = offset
        remaining_read = limit
        chunks: list[bytes] = []
        refs: list[str] = []
        for path in paths:
            size = path.stat().st_size
            if remaining_skip >= size:
                remaining_skip -= size
                continue
            with path.open("rb") as source:
                source.seek(remaining_skip)
                value = source.read(remaining_read)
            if value:
                chunks.append(value)
                refs.append(f"{job_id}/{path.name}")
                remaining_read -= len(value)
            remaining_skip = 0
            if remaining_read <= 0:
                break
        data = b"".join(chunks)
        return data, offset + len(data), refs

    def _write_carry_forward(self, record: dict[str, Any]) -> None:
        job_id = _job_id(record.get("job_id"))
        input_tokens = int(record.get("input_tokens") or 0)
        cached_input_tokens = int(record.get("cached_input_tokens") or 0)
        output_tokens = int(record.get("output_tokens") or 0)
        carry = {
            "status": record.get("status"),
            "failure_class": record.get("failure_class"),
            "session_id": record.get("session_id"),
            "tokens": {
                "input": input_tokens,
                "cached_input": cached_input_tokens,
                "cached_input_in_total": True,
                "output": output_tokens,
                "total": input_tokens + output_tokens,
            },
            "last_valid_output": {
                "stdout": str(record.get("stdout") or ""),
                "stderr": str(record.get("stderr") or ""),
            },
            "output_segments": record.get("output_segments") or [],
            "output_bytes": record.get("output_bytes") or {
                "stdout": 0, "stderr": 0, "total": 0,
            },
            "generation_model_tokens": 0,
        }
        self._write_json(self._output_directory(job_id) / "carry-forward.json", carry)

    def _write_json(self, target: Path, payload: dict[str, Any]) -> None:
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8") as output:
            output.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            output.flush()
            os.fsync(output.fileno())
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
            timeout=PROBE_TIMEOUT_SECONDS, check=False, env=_safe_environment(),
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
            "failure_class": _provider_failure_class(
                completed.returncode, stdout, stderr
            ),
            "input_tokens": parsed["input_tokens"],
            "cached_input_tokens": parsed["cached_input_tokens"],
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
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "session_id": session_id,
        }


def verify(payload: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, object]:
    project_path = Path(str(payload["project_path"])).expanduser().resolve()
    if not project_path.is_dir() or not any(_is_within(project_path, root) for root in roots):
        raise ValueError("project_path is outside the configured project roots")
    execution_payload = payload.get("execution")
    specs = payload.get("verification")
    if not isinstance(execution_payload, dict):
        raise ValueError("execution must be an object")
    if not isinstance(specs, list) or not 1 <= len(specs) <= 32:
        raise ValueError("verification must contain 1-32 checks")
    execution = ExecutionResult(
        returncode=int(execution_payload.get("returncode", 1)),
        stdout=str(execution_payload.get("stdout") or ""),
        stderr=str(execution_payload.get("stderr") or ""),
        duration_ms=int(execution_payload.get("duration_ms", 0)),
        failure_class=execution_payload.get("failure_class"),
        input_tokens=int(execution_payload.get("input_tokens", 0)),
        cached_input_tokens=int(execution_payload.get("cached_input_tokens", 0)),
        output_tokens=int(execution_payload.get("output_tokens", 0)),
        external_session_id=execution_payload.get("external_session_id"),
    )
    acceptance = payload.get("acceptance", [])
    if not isinstance(acceptance, list) or any(
        not isinstance(item, str) for item in acceptance
    ):
        raise ValueError("acceptance must be a string list")
    result = VerificationEngine().verify(
        project_path,
        execution,
        specs,
        acceptance=acceptance,
        require_structured_verdict=bool(
            payload.get("require_structured_verdict", False)
        ),
    )
    return {
        "passed": result.passed,
        "verdict": result.verdict,
        "reason_codes": result.reason_codes,
        "failed_acceptance_ids": result.failed_acceptance_ids,
        "checks": result.checks,
        "evidence_hash": result.evidence_hash,
        "summary": result.summary,
    }


def inspect_artifacts(
    payload: dict[str, Any], roots: tuple[Path, ...]
) -> dict[str, object]:
    project_path = _project_path(payload, roots)
    paths = payload.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise ValueError("paths must contain 1-32 relative artifact paths")
    cursor = _resolve_executable("", "cursor")
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path in paths:
        relative_path = str(raw_path)
        if relative_path in seen:
            continue
        seen.add(relative_path)
        target = _artifact_path(project_path, relative_path)
        exists = target.is_file()
        stat = target.stat() if exists else None
        sha256 = None
        if stat is not None and stat.st_size <= MAX_ARTIFACT_HASH_BYTES:
            digest = hashlib.sha256()
            with target.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(1_048_576), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        artifacts.append({
            "relative_path": relative_path,
            "host_path": str(target),
            "exists": exists,
            "bytes": stat.st_size if stat is not None else None,
            "sha256": sha256,
            "modified_at": (
                datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
                if stat is not None else None
            ),
            "actions": ["finder", *(["cursor"] if cursor else [])] if exists else [],
        })
    return {"project_path": str(project_path), "artifacts": artifacts}


def evidence_snapshot(
    payload: dict[str, Any], roots: tuple[Path, ...]
) -> dict[str, Any]:
    project_path = _project_path(payload, roots)
    paths = payload.get("paths")
    if not isinstance(paths, list) or len(paths) > 64:
        raise ValueError("paths must contain at most 64 relative artifact paths")
    return snapshot_environment(project_path, [str(path) for path in paths])


def _workspace_progress(
    project_path: Path,
    *,
    excluded_roots: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Small content-bound workspace fingerprint; output volume is excluded."""
    try:
        status = subprocess.run(
            ["git", "-C", str(project_path), "status", "--porcelain=v1", "-z"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"kind": "workspace", "available": False}
    if status.returncode != 0:
        return {"kind": "workspace", "available": False}
    paths: list[str] = []
    for entry in status.stdout.decode("utf-8", errors="replace").split("\0"):
        if len(entry) < 4:
            continue
        path = entry[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and path not in paths:
            paths.append(path)
    digest = hashlib.sha256()
    hashed = 0
    records: list[dict[str, object]] = []
    for relative in sorted(paths)[:256]:
        target = (project_path / relative).resolve()
        if not target.is_relative_to(project_path.resolve()):
            continue
        if any(
            target == excluded.resolve()
            or target.is_relative_to(excluded.resolve())
            for excluded in excluded_roots
        ):
            continue
        exists = target.is_file()
        item: dict[str, object] = {"path": relative, "exists": exists}
        if exists:
            size = target.stat().st_size
            item["bytes"] = size
            if hashed + size <= 16 * 1024 * 1024:
                file_hash = _sha256_file(target)
                item["sha256"] = file_hash
                hashed += size
        encoded = json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest.update(encoded)
        records.append(item)
    return {
        "kind": "workspace",
        "available": True,
        "fingerprint": digest.hexdigest(),
        "changed_files": len(paths),
        "sample": records[:20],
        "truncated": len(paths) > len(records),
    }


def open_artifact(
    payload: dict[str, Any], roots: tuple[Path, ...]
) -> dict[str, object]:
    project_path = _project_path(payload, roots)
    relative_path = str(payload["relative_path"])
    target = _artifact_path(project_path, relative_path)
    if not target.is_file():
        raise ValueError("artifact does not exist")
    action = str(payload.get("action") or "")
    if action == "finder":
        argv = ["/usr/bin/open", "-R", str(target)]
    elif action == "cursor":
        cursor = _resolve_executable("", "cursor")
        if cursor is None:
            raise ValueError("Cursor CLI is not available")
        argv = [cursor, str(target)]
    else:
        raise ValueError("unsupported artifact action")
    process = subprocess.Popen(
        argv, cwd=project_path, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_safe_environment(), start_new_session=True,
    )
    return {
        "status": "opened",
        "action": action,
        "relative_path": relative_path,
        "host_path": str(target),
        "pid": process.pid,
    }


def _project_path(payload: dict[str, Any], roots: tuple[Path, ...]) -> Path:
    project_path = Path(str(payload["project_path"])).expanduser().resolve()
    if not project_path.is_dir() or not any(
        _is_within(project_path, root) for root in roots
    ):
        raise ValueError("project_path is outside the configured project roots")
    return project_path


def _artifact_path(project_path: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("artifact path must be relative")
    target = (project_path / relative_path).resolve()
    if not _is_within(target, project_path):
        raise ValueError("artifact path escapes project root")
    return target


def _execution_argv(
    adapter: str, executable: str, project: Path, session_id: str | None, prompt: str
) -> list[str]:
    if adapter == "codex":
        if session_id:
            return [
                executable, "exec", "resume", "--json",
                "--disable", "multi_agent", session_id, "-",
            ]
        return [
            executable, "exec", "--json", "--sandbox", "workspace-write",
            "--disable", "multi_agent",
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
    return [
        *_json_worker_argv(executable),
        "--project", str(project), "--session", session_id, "--json",
    ]


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
    cached_input_tokens = 0
    output_tokens = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or _find_string(event, {"thread_id", "threadId", "session_id", "sessionId", "chat_id", "chatId"})
        event_input_tokens = _find_int(event, {"input_tokens", "inputTokens"})
        cursor_cache_read_tokens = _find_int(event, {"cacheReadTokens"})
        input_tokens = max(
            input_tokens,
            event_input_tokens + cursor_cache_read_tokens,
        )
        cached_input_tokens = max(
            cached_input_tokens,
            cursor_cache_read_tokens,
            _find_int(
                event,
                {"cached_input_tokens", "cachedInputTokens", "cached_tokens"},
            ),
        )
        output_tokens = max(output_tokens, _find_int(event, {"output_tokens", "outputTokens"}))
    return {
        "session_id": session_id,
        "input_tokens": input_tokens,
        "cached_input_tokens": min(input_tokens, cached_input_tokens),
        "output_tokens": output_tokens,
    }


def _provider_failure_class(returncode: int, stdout: str, stderr: str) -> str | None:
    if returncode == 0:
        return None
    evidence = f"{stdout}\n{stderr}".lower()
    if any(marker in evidence for marker in (
        "selected model is at capacity",
        "model is at capacity",
        "rate limit exceeded",
        "rate_limit_exceeded",
        "too many requests",
        "http 429",
        "status 429",
        "server is overloaded",
        "overloaded_error",
    )):
        return "provider_capacity"
    return "command_failed"


def _output_kind(stream: str, text: str) -> str:
    compact = text.replace(" ", "").lower()
    if '"tool_call"' in compact or '"type":"tool' in compact:
        return "tool"
    return stream


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
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    sibling = Path(sys.executable).parent / candidate
    return str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else None


def _version_argv(adapter: str, executable: str) -> list[str]:
    if adapter == "cursor":
        return [executable, "agent", "--version"]
    if adapter == "json-worker":
        return [*_json_worker_argv(executable), "--probe"]
    return [executable, "--version"]


def _json_worker_argv(executable: str) -> list[str]:
    if Path(executable).name == "simple-worker":
        return [sys.executable, str(Path(__file__).with_name("simple_worker.py"))]
    return [executable]


def _safe_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CURSOR_API_KEY"}
    deepseek_key = re.compile(r"^DEEPSEEK_API_KEY(?:_\d+)?$")
    return {
        key: value for key, value in os.environ.items()
        if key in allowed
        or key in {
            "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
            "PLOW_WHIP_SIMPLE_WORKER_STATE_DIR",
        }
        or deepseek_key.fullmatch(key)
    }


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


def _append_bounded(current: str, value: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    combined = current + value
    encoded = combined.encode("utf-8")
    if len(encoded) <= limit:
        return combined
    tail = encoded[-limit:]
    while tail:
        try:
            return tail.decode("utf-8")
        except UnicodeDecodeError:
            tail = tail[1:]
    return ""


def _utf8_chunks(value: str, limit: int) -> list[str]:
    if not value:
        return []
    if len(value.encode("utf-8")) <= limit:
        return [value]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if current and size + encoded_size > limit:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(character)
        size += encoded_size
    if current:
        chunks.append("".join(current))
    return chunks


def _tail_from_files(paths: list[Path], limit: int) -> str:
    remaining = limit
    chunks: list[bytes] = []
    for path in reversed(paths):
        if remaining <= 0:
            break
        size = path.stat().st_size
        with path.open("rb") as output:
            output.seek(max(0, size - remaining))
            chunk = output.read(remaining)
        chunks.append(chunk)
        remaining -= len(chunk)
    tail = b"".join(reversed(chunks))
    while tail:
        try:
            return tail.decode("utf-8")
        except UnicodeDecodeError:
            tail = tail[1:]
    return ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as output:
        for chunk in iter(lambda: output.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    # A just-spawned process can briefly be visible to kill(0) before ps exposes
    # its start identity. Keep the PID-reuse guard and retry only that short race.
    for attempt in range(3):
        try:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = completed.stdout.strip()
        if value:
            return value
        if attempt < 2 and _process_alive(pid):
            time.sleep(0.01)
    return None


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
