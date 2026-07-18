from __future__ import annotations

import json
import uuid
from typing import Any

from plow_whip_web.domain.model import TaskRecord
from plow_whip_web.store.database import Database


CALL_KINDS = {
    "executor",
    "butler_planner",
    "router",
    "verifier",
    "convention_refinement",
}


class ModelCallLedger:
    """Idempotent call receipts and normalized usage telemetry; never a gate."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def prepare(
        self,
        *,
        idempotency_key: str,
        call_kind: str,
        provider: str,
        model: str | None = None,
        task: TaskRecord | None = None,
        project_id: str | None = None,
        worker_id: str | None = None,
        session_id: str | None = None,
        session_generation: int | None = None,
        host_job_id: str | None = None,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        if call_kind not in CALL_KINDS:
            raise ValueError(f"unsupported model call kind: {call_kind}")
        resolved_call_id = call_id or str(uuid.uuid4())
        resolved_project_id = task.project_id if task else project_id
        resolved_worker_id = task.worker_id if task else worker_id
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM model_calls WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["call_kind"] != call_kind or existing["provider"] != provider:
                    raise ValueError("model call idempotency key metadata mismatch")
                return dict(existing)
            if call_id is not None:
                existing = connection.execute(
                    "SELECT * FROM model_calls WHERE call_id = ?",
                    (resolved_call_id,),
                ).fetchone()
                if existing is not None:
                    expected_task_id = task.id if task else None
                    if (
                        existing["call_kind"] != call_kind
                        or existing["provider"] != provider
                        or existing["task_id"] != expected_task_id
                    ):
                        raise ValueError("model call identity metadata mismatch")
                    return dict(existing)
            connection.execute(
                """
                INSERT INTO model_calls(
                    call_id, idempotency_key, project_id, task_id, worker_id,
                    host_job_id, provider, model, call_kind, session_id,
                    session_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_call_id,
                    idempotency_key,
                    resolved_project_id,
                    task.id if task else None,
                    resolved_worker_id,
                    host_job_id,
                    provider,
                    model or provider,
                    call_kind,
                    session_id,
                    session_generation,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM model_calls WHERE call_id = ?",
                    (resolved_call_id,),
                ).fetchone()
            )

    def dispatched(
        self,
        call_id: str,
        *,
        host_job_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            call_id,
            status="dispatched",
            host_job_id=host_job_id,
            session_id=session_id,
        )

    def unknown(self, call_id: str, *, error_class: str) -> dict[str, Any]:
        return self._transition(call_id, status="unknown", error_class=error_class)

    def settle(
        self,
        call_id: str,
        execution: dict[str, Any] | None = None,
        *,
        failed: bool = False,
        error_class: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        usage = execution or {}
        input_tokens = max(0, int(usage.get("input_tokens") or 0))
        cached_input_tokens = min(
            input_tokens, max(0, int(usage.get("cached_input_tokens") or 0))
        )
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"model call receipt not found: {call_id}")
            if row["status"] in {"completed", "failed"}:
                return dict(row)
            resolved_session_id = session_id or row["session_id"]
            previous = self._previous_snapshot(
                connection,
                row,
                session_id=resolved_session_id,
            )
            delta_input = _counter_delta(input_tokens, previous, "raw_input_tokens")
            delta_cached = min(
                delta_input,
                _counter_delta(
                    cached_input_tokens, previous, "raw_cached_input_tokens"
                ),
            )
            delta_output = _counter_delta(
                output_tokens, previous, "raw_output_tokens"
            )
            normalized = {
                "input_tokens": delta_input,
                "cached_input_tokens": delta_cached,
                "uncached_input_tokens": delta_input - delta_cached,
                "output_tokens": delta_output,
                "total_tokens": delta_input + delta_output,
                "raw_input_tokens": input_tokens,
                "raw_cached_input_tokens": cached_input_tokens,
                "raw_output_tokens": output_tokens,
                "source": "provider_cumulative_delta",
                "baseline_call_id": previous["call_id"] if previous else None,
            }
            connection.execute(
                """
                UPDATE model_calls
                SET status = ?, input_tokens = ?, cached_input_tokens = ?,
                    output_tokens = ?, raw_input_tokens = ?,
                    raw_cached_input_tokens = ?, raw_output_tokens = ?,
                    usage_semantics = 'delta', normalized_usage_json = ?,
                    error_class = ?, session_id = COALESCE(?, session_id),
                    dispatched_at = COALESCE(dispatched_at, CURRENT_TIMESTAMP),
                    settled_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE call_id = ?
                """,
                (
                    "failed" if failed else "completed",
                    delta_input,
                    delta_cached,
                    delta_output,
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                    error_class,
                    session_id,
                    call_id,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
                ).fetchone()
            )

    @staticmethod
    def _previous_snapshot(
        connection: Any,
        row: Any,
        *,
        session_id: str | None,
    ) -> Any | None:
        if session_id:
            return connection.execute(
                """
                SELECT call_id, raw_input_tokens, raw_cached_input_tokens,
                       raw_output_tokens
                FROM model_calls
                WHERE call_id != ? AND provider = ? AND session_id = ?
                  AND (? IS NULL OR task_id = ?)
                  AND (? IS NULL OR session_generation IS ?)
                  AND status IN ('completed', 'failed')
                  AND usage_semantics != 'unresolved_snapshot'
                ORDER BY settled_at DESC, created_at DESC, rowid DESC LIMIT 1
                """,
                (
                    row["call_id"],
                    row["provider"],
                    session_id,
                    row["task_id"],
                    row["task_id"],
                    row["session_generation"],
                    row["session_generation"],
                ),
            ).fetchone()
        if row["task_id"] and row["session_generation"] is not None:
            return connection.execute(
                """
                SELECT call_id, raw_input_tokens, raw_cached_input_tokens,
                       raw_output_tokens
                FROM model_calls
                WHERE call_id != ? AND provider = ? AND task_id = ?
                  AND session_generation IS ?
                  AND status IN ('completed', 'failed')
                  AND usage_semantics != 'unresolved_snapshot'
                ORDER BY settled_at DESC, created_at DESC, rowid DESC LIMIT 1
                """,
                (
                    row["call_id"],
                    row["provider"],
                    row["task_id"],
                    row["session_generation"],
                ),
            ).fetchone()
        return None

    def summary(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) cached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens
                FROM model_calls
                """
            ).fetchone()
            raw = connection.execute(
                """
                SELECT COALESCE(SUM(raw_input_tokens), 0) input_tokens,
                       COALESCE(SUM(raw_cached_input_tokens), 0) cached_input_tokens,
                       COALESCE(SUM(raw_output_tokens), 0) output_tokens
                FROM model_calls
                """
            ).fetchone()
            quality_rows = connection.execute(
                """
                SELECT usage_semantics, COUNT(*) calls,
                       COALESCE(SUM(input_tokens + output_tokens), 0) tokens
                FROM model_calls
                GROUP BY usage_semantics ORDER BY usage_semantics
                """
            ).fetchall()
            usage_quality = [dict(row) for row in quality_rows]
            has_legacy_inference = any(
                row["usage_semantics"] == "legacy_inferred_delta"
                for row in quality_rows
            )
            calls = [
                self._view(row)
                for row in connection.execute(
                    """
                    SELECT * FROM model_calls
                    ORDER BY created_at DESC, call_id DESC LIMIT 200
                    """
                ).fetchall()
            ]
            dimensions = {
                name: self._dimension(connection, column)
                for name, column in (
                    ("projects", "project_id"),
                    ("tasks", "task_id"),
                    ("workers", "worker_id"),
                    ("providers", "provider"),
                    ("models", "model"),
                    ("call_kinds", "call_kind"),
                    ("sessions", "session_id"),
                )
            }
            return {
                "input_tokens": int(total["input_tokens"]),
                "cached_input_tokens": int(total["cached_input_tokens"]),
                "cached_input_tokens_in_total": True,
                "output_tokens": int(total["output_tokens"]),
                "total_tokens": int(total["input_tokens"])
                + int(total["output_tokens"]),
                "total_formula": "input_tokens + output_tokens",
                "usage_semantics": (
                    "mixed_exact_and_legacy_inferred_delta"
                    if has_legacy_inference
                    else "physical_session_delta"
                ),
                "usage_quality": usage_quality,
                "raw_snapshot_totals": {
                    "input_tokens": int(raw["input_tokens"]),
                    "cached_input_tokens": int(raw["cached_input_tokens"]),
                    "output_tokens": int(raw["output_tokens"]),
                    "total_tokens": int(raw["input_tokens"])
                    + int(raw["output_tokens"]),
                },
                **dimensions,
                "calls": calls,
            }
        finally:
            connection.close()

    def _transition(
        self,
        call_id: str,
        *,
        status: str,
        host_job_id: str | None = None,
        session_id: str | None = None,
        error_class: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE model_calls
                SET status = ?, host_job_id = COALESCE(?, host_job_id),
                    session_id = COALESCE(?, session_id), error_class = ?,
                    dispatched_at = COALESCE(dispatched_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE call_id = ? AND status NOT IN ('completed', 'failed')
                """,
                (status, host_job_id, session_id, error_class, call_id),
            )
            row = connection.execute(
                "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"model call receipt not found: {call_id}")
            return dict(row)

    @staticmethod
    def _dimension(connection: Any, column: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"""
            SELECT {column}, SUM(input_tokens) input_tokens,
                   SUM(cached_input_tokens) cached_input_tokens,
                   SUM(input_tokens - cached_input_tokens) uncached_input_tokens,
                   SUM(output_tokens) output_tokens,
                   SUM(input_tokens + output_tokens) tokens,
                   COUNT(*) calls
            FROM model_calls GROUP BY {column}
            ORDER BY tokens DESC, calls DESC, {column}
            """
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["uncached_input_tokens"] = int(item["input_tokens"]) - int(
            item["cached_input_tokens"]
        )
        item["total_tokens"] = int(item["input_tokens"]) + int(
            item["output_tokens"]
        )
        item["normalized_usage"] = json.loads(item.pop("normalized_usage_json"))
        return item


def _counter_delta(current: int, previous: Any | None, field: str) -> int:
    if previous is None:
        return current
    baseline = max(0, int(previous[field] or 0))
    return current - baseline if current >= baseline else current
