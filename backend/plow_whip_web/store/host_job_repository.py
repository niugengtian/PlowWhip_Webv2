from __future__ import annotations

import json
import uuid
from typing import Any

from plow_whip_web.security import Redactor
from plow_whip_web.store.database import Database


class HostJobRepository:
    """Container-side source of truth for durable Host Bridge execution."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def prepare(
        self, *, task_id: str, attempt_id: str, run_id: str, provider: str
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM host_jobs WHERE task_id = ? AND attempt_id = ?",
                (task_id, attempt_id),
            ).fetchone()
            if existing:
                return dict(existing)
            context = connection.execute(
                """
                SELECT t.worker_id, t.current_spec_revision, l.fencing_token,
                       w.session_generation, a.spec_revision AS attempt_spec_revision,
                       r.spec_revision AS run_spec_revision
                FROM tasks t
                LEFT JOIN task_leases l ON l.task_id = t.id
                LEFT JOIN workers w ON w.id = t.worker_id
                JOIN task_attempts a ON a.id = ? AND a.task_id = t.id
                JOIN task_runs r ON r.id = ? AND r.attempt_id = a.id
                WHERE t.id = ?
                """,
                (attempt_id, run_id, task_id),
            ).fetchone()
            if context is None:
                raise ValueError(f"task not found: {task_id}")
            revisions = {
                int(context["current_spec_revision"]),
                int(context["attempt_spec_revision"]),
                int(context["run_spec_revision"]),
            }
            if len(revisions) != 1:
                raise ValueError("Host Job TaskSpec revision mismatch")
            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO host_jobs(
                    job_id, task_id, attempt_id, run_id, worker_id, provider,
                    fencing_token, session_generation, spec_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, task_id, attempt_id, run_id, context["worker_id"], provider,
                    context["fencing_token"], context["session_generation"],
                    context["current_spec_revision"],
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM host_jobs WHERE job_id = ?", (job_id,)
            ).fetchone())

    def record(self, job_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        compact = _compact_snapshot(snapshot)
        result_json = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        session_id = snapshot.get("session_id") or snapshot.get("external_session_id")
        error_summary = compact.get("error_summary")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE host_jobs SET status = ?, host_pid = COALESCE(?, host_pid),
                    external_session_id = COALESCE(?, external_session_id),
                    heartbeat_at = COALESCE(?, CURRENT_TIMESTAMP),
                    finished_at = ?, result_json = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (
                    str(snapshot.get("status") or "unknown"), snapshot.get("pid"), session_id,
                    snapshot.get("heartbeat_at"), snapshot.get("finished_at"), result_json,
                    error_summary, job_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM host_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"host job not found: {job_id}")
            if row["worker_id"]:
                connection.execute(
                    """
                    UPDATE workers SET external_session_id = COALESCE(?, external_session_id),
                        last_seen_at = CURRENT_TIMESTAMP, last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND session_generation IS ?
                    """,
                    (
                        session_id, error_summary, row["worker_id"],
                        row["session_generation"],
                    ),
                )
            return dict(row)

    def active(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM host_jobs WHERE consumed_at IS NULL ORDER BY created_at"
            ).fetchall()]
        finally:
            connection.close()

    def renew(self, job_id: str, *, seconds: int = 300) -> None:
        modifier = f"+{max(60, seconds)} seconds"
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT task_id, worker_id FROM host_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE task_leases SET expires_at = datetime('now', ?) WHERE task_id = ?",
                (modifier, row["task_id"]),
            )
            connection.execute(
                "UPDATE resource_locks SET expires_at = datetime('now', ?) WHERE task_id = ?",
                (modifier, row["task_id"]),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE workers SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["worker_id"],),
                )

    def hold(self, job_id: str, error: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE host_jobs SET status = 'recovery_hold', last_error = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE job_id = ? AND consumed_at IS NULL
                """,
                (error[:1000], job_id),
            )
            row = connection.execute(
                "SELECT task_id, worker_id FROM host_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            if row["worker_id"]:
                connection.execute(
                    """
                    UPDATE workers SET last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (f"host_job_recovery_hold: {error}"[:1000], row["worker_id"]),
                )
            task = connection.execute(
                "SELECT revision FROM tasks WHERE id = ?", (row["task_id"],)
            ).fetchone()
            if task:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO task_events(
                        task_id, event_type, payload_json, state_revision, idempotency_key
                    ) VALUES (?, 'host_job.recovery_hold', ?, ?, ?)
                    """,
                    (
                        row["task_id"],
                        json.dumps(
                            {"host_job_id": job_id, "reason": error[:1000]},
                            ensure_ascii=False, sort_keys=True,
                        ),
                        task["revision"], f"host-job:{job_id}:recovery-hold",
                    ),
                )

    def consume(self, job_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE host_jobs SET consumed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (job_id,),
            )

    def consecutive_faults(
        self,
        worker_id: str | None,
        *,
        session_generation: int | None,
        reason: str,
        before_job_id: str,
        limit: int,
    ) -> int:
        if not worker_id or limit <= 0:
            return 0
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT last_error FROM host_jobs
                WHERE worker_id = ? AND session_generation IS ?
                  AND job_id != ? AND consumed_at IS NOT NULL
                ORDER BY updated_at DESC, created_at DESC LIMIT ?
                """,
                (worker_id, session_generation, before_job_id, limit),
            ).fetchall()
        finally:
            connection.close()
        count = 0
        for row in rows:
            if row["last_error"] != reason:
                break
            count += 1
        return count


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist refs/segments/hashes/bytes/status only — never stdout/stderr/prompt bodies."""
    scalar_fields = {
        "job_id", "status", "pid", "session_id", "external_session_id",
        "heartbeat_at", "finished_at", "returncode", "duration_ms",
        "failure_class", "input_tokens", "cached_input_tokens", "output_tokens",
        "cancel_requested",
        "output_ref", "carry_forward_ref",
    }
    compact = {key: snapshot[key] for key in scalar_fields if key in snapshot}
    raw_segments = snapshot.get("output_segments")
    compact["output_segments"] = [
        {
            key: segment[key]
            for key in ("stream", "index", "ref", "bytes", "sha256", "offset")
            if key in segment
        }
        for segment in (raw_segments if isinstance(raw_segments, list) else [])
        if isinstance(segment, dict)
    ]
    raw_bytes = snapshot.get("output_bytes")
    compact["output_bytes"] = {
        key: int(raw_bytes.get(key) or 0)
        for key in ("stdout", "stderr", "total")
    } if isinstance(raw_bytes, dict) else {"stdout": 0, "stderr": 0, "total": 0}
    error = snapshot.get("error_summary") or snapshot.get("failure_class")
    if not error:
        # Prefer failure_class over copying stderr body into SQLite.
        error = snapshot.get("failure_class")
    compact["error_summary"] = Redactor.redact(str(error))[:1000] if error else None
    compact["stdout_len"] = int(compact["output_bytes"].get("stdout") or 0)
    compact["stderr_len"] = int(compact["output_bytes"].get("stderr") or 0)
    compact["segment_count"] = len(compact["output_segments"])
    return compact
