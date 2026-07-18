from __future__ import annotations

from typing import Any

from plow_whip_web.domain.model import TaskRecord
from plow_whip_web.store.database import Database


class TokenLedger:
    """Idempotent Token accounting. Token usage is telemetry, never a control gate."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def record_in_transaction(
        connection: Any,
        *,
        call_id: str,
        execution: dict[str, Any],
        task: TaskRecord | None = None,
        provider: str | None = None,
        call_kind: str = "task_execution",
        project_id: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
        add_to_task: bool = False,
    ) -> bool:
        input_tokens = max(0, int(execution.get("input_tokens", 0)))
        cached_input_tokens = min(
            input_tokens, max(0, int(execution.get("cached_input_tokens", 0)))
        )
        output_tokens = max(0, int(execution.get("output_tokens", 0)))
        resolved_worker_id = worker_id or (task.worker_id if task else None)
        generation = None
        if resolved_worker_id:
            worker = connection.execute(
                "SELECT session_generation FROM workers WHERE id = ?",
                (resolved_worker_id,),
            ).fetchone()
            generation = worker["session_generation"] if worker else None
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO token_usage(
                task_id, project_id, worker_id, input_tokens, cached_input_tokens,
                output_tokens, provider, run_id, call_id, call_kind,
                session_generation, attribution_granularity,
                value_classification, rotation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id if task else None,
                task.project_id if task else project_id,
                resolved_worker_id,
                input_tokens,
                cached_input_tokens,
                output_tokens,
                provider or (task.provider if task else None),
                run_id,
                call_id,
                call_kind,
                generation,
                str(execution.get("attribution_granularity") or "turn"),
                str(execution.get("value_classification") or "unknown"),
                execution.get("rotation_reason"),
            ),
        )
        if add_to_task and task is not None and inserted.rowcount:
            connection.execute(
                """
                UPDATE tasks SET tokens_used = tokens_used + ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (input_tokens + output_tokens, task.id),
            )
        return bool(inserted.rowcount)

    def record(
        self,
        execution: dict[str, Any],
        *,
        call_id: str,
        task: TaskRecord | None = None,
        provider: str | None = None,
        call_kind: str = "task_execution",
        project_id: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
        add_to_task: bool = False,
    ) -> bool:
        with self.database.transaction(immediate=True) as connection:
            return self.record_in_transaction(
                connection,
                call_id=call_id,
                execution=execution,
                task=task,
                provider=provider,
                call_kind=call_kind,
                project_id=project_id,
                worker_id=worker_id,
                run_id=run_id,
                add_to_task=add_to_task,
            )

    def summary(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                "SELECT COALESCE(SUM(input_tokens),0) input, "
                "COALESCE(SUM(cached_input_tokens),0) cached_input, "
                "COALESCE(SUM(output_tokens),0) output FROM token_usage"
            ).fetchone()
            projects = connection.execute(
                """
                SELECT project_id, SUM(input_tokens) input_tokens,
                       SUM(cached_input_tokens) cached_input_tokens,
                       SUM(input_tokens - cached_input_tokens) uncached_input_tokens,
                       SUM(output_tokens) output_tokens,
                       SUM(input_tokens + output_tokens) tokens
                FROM token_usage GROUP BY project_id ORDER BY tokens DESC
                """
            ).fetchall()
            tasks = connection.execute(
                """
                SELECT task_id, SUM(input_tokens) input_tokens,
                       SUM(cached_input_tokens) cached_input_tokens,
                       SUM(input_tokens - cached_input_tokens) uncached_input_tokens,
                       SUM(output_tokens) output_tokens,
                       SUM(input_tokens + output_tokens) tokens
                FROM token_usage WHERE task_id IS NOT NULL
                GROUP BY task_id ORDER BY tokens DESC LIMIT 100
                """
            ).fetchall()
            calls = connection.execute(
                """
                SELECT call_id, call_kind, task_id, worker_id, provider,
                       session_generation, input_tokens, cached_input_tokens,
                       input_tokens - cached_input_tokens AS uncached_input_tokens,
                       output_tokens, attribution_granularity,
                       value_classification, rotation_reason, created_at
                FROM token_usage ORDER BY created_at DESC, id DESC LIMIT 100
                """
            ).fetchall()
            return {
                "input_tokens": total["input"],
                "cached_input_tokens": total["cached_input"],
                "cached_input_tokens_in_total": True,
                "output_tokens": total["output"],
                "total_tokens": total["input"] + total["output"],
                "total_formula": "input_tokens + output_tokens",
                "projects": [dict(row) for row in projects],
                "tasks": [dict(row) for row in tasks],
                "calls": [dict(row) for row in calls],
            }
        finally:
            connection.close()
