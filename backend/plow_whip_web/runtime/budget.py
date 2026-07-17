from __future__ import annotations

import json
from typing import Any

from plow_whip_web.domain.model import BudgetExceededError, TaskRecord
from plow_whip_web.store.database import Database
from plow_whip_web.store.settings_repository import DEFAULT_SETTINGS, SettingsRepository


class BudgetManager:
    def __init__(self, database: Database, settings: SettingsRepository) -> None:
        self.database = database
        self.settings = settings

    def ensure(self, task: TaskRecord, estimated_tokens: int) -> None:
        if estimated_tokens <= 0:
            return
        if task.tokens_used + estimated_tokens > task.token_budget:
            raise BudgetExceededError("task token budget would be exceeded")
        daily_limit = self.settings.get()["values"]["global_daily_token_budget"]
        connection = self.database.connect()
        try:
            spent = connection.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
                "FROM token_usage WHERE date(created_at) = date('now')"
            ).fetchone()[0]
        finally:
            connection.close()
        if spent + estimated_tokens > daily_limit:
            raise BudgetExceededError("global daily token budget would be exceeded")

    def host_reservation(self, task: TaskRecord) -> int:
        remaining = task.token_budget - task.tokens_used
        if remaining <= 0:
            raise BudgetExceededError("task token budget has no reservable tokens")
        return remaining

    @staticmethod
    def reserve_in_transaction(
        connection: Any,
        *,
        call_id: str,
        call_kind: str,
        idempotency_key: str,
        provider: str,
        reserved_tokens: int,
        task: TaskRecord | None = None,
        project_id: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if reserved_tokens <= 0:
            raise BudgetExceededError("model call has no reservable tokens")
        limits = dict(DEFAULT_SETTINGS)
        settings = connection.execute(
            "SELECT settings_json FROM system_settings WHERE id = 1"
        ).fetchone()
        if settings:
            limits.update(json.loads(settings["settings_json"]))
        if task is not None:
            task_reserved = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0)
                FROM token_reservations
                WHERE task_id = ? AND status = 'active'
                """,
                (task.id,),
            ).fetchone()[0]
            if task.tokens_used + task_reserved + reserved_tokens > task.token_budget:
                raise BudgetExceededError("task token budget would be exceeded")
        daily_allocated = connection.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
                 FROM token_usage WHERE date(created_at) = date('now'))
              + (SELECT COALESCE(SUM(reserved_tokens), 0)
                 FROM token_reservations
                 WHERE status = 'active' AND date(created_at) = date('now'))
            """
        ).fetchone()[0]
        if daily_allocated + reserved_tokens > int(limits["global_daily_token_budget"]):
            raise BudgetExceededError("global daily token budget would be exceeded")
        connection.execute(
            """
            INSERT INTO token_reservations(
                call_id, call_kind, idempotency_key, run_id, task_id,
                project_id, worker_id, provider, reserved_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id, call_kind, idempotency_key, run_id,
                task.id if task else None,
                task.project_id if task else project_id,
                task.worker_id if task else worker_id,
                provider, reserved_tokens,
            ),
        )

    @staticmethod
    def settle_in_transaction(
        connection: Any,
        *,
        call_id: str,
        execution: dict[str, Any],
        task: TaskRecord | None = None,
        provider: str | None = None,
        add_to_task: bool = False,
    ) -> bool:
        reservation = connection.execute(
            "SELECT * FROM token_reservations WHERE call_id = ?", (call_id,)
        ).fetchone()
        input_tokens = int(execution.get("input_tokens", 0))
        output_tokens = int(execution.get("output_tokens", 0))
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO token_usage(
                task_id, project_id, worker_id, input_tokens, output_tokens,
                provider, run_id, call_id, call_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation["task_id"] if reservation else (task.id if task else None),
                reservation["project_id"] if reservation else (task.project_id if task else None),
                reservation["worker_id"] if reservation else (task.worker_id if task else None),
                input_tokens, output_tokens,
                reservation["provider"] if reservation else provider,
                reservation["run_id"] if reservation else call_id,
                call_id,
                reservation["call_kind"] if reservation else "task_execution",
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
        if reservation:
            connection.execute(
                """
                UPDATE token_reservations
                SET status = 'settled', actual_tokens = ?, settled_at = CURRENT_TIMESTAMP
                WHERE call_id = ? AND status = 'active'
                """,
                (input_tokens + output_tokens, call_id),
            )
        return bool(inserted.rowcount)

    def settle(
        self,
        task: TaskRecord | None,
        execution: dict[str, Any],
        *,
        call_id: str,
        provider: str | None = None,
        add_to_task: bool = False,
    ) -> bool:
        with self.database.transaction(immediate=True) as connection:
            return self.settle_in_transaction(
                connection, call_id=call_id, execution=execution, task=task,
                provider=provider, add_to_task=add_to_task,
            )

    def record(
        self, task: TaskRecord, execution: dict[str, Any], *,
        provider: str, run_id: str | None = None, add_to_task: bool = False,
    ) -> None:
        self.settle(
            task, execution, call_id=run_id or f"task-usage:{task.id}",
            provider=provider, add_to_task=add_to_task,
        )

    @staticmethod
    def release_in_transaction(connection: Any, call_id: str) -> None:
        connection.execute(
            """
            UPDATE token_reservations
            SET status = 'released', settled_at = CURRENT_TIMESTAMP
            WHERE call_id = ? AND status = 'active'
            """,
            (call_id,),
        )

    def release(self, call_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            self.release_in_transaction(connection, call_id)

    def summary(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                "SELECT COALESCE(SUM(input_tokens),0) input, "
                "COALESCE(SUM(output_tokens),0) output FROM token_usage"
            ).fetchone()
            projects = connection.execute(
                """
                SELECT project_id, SUM(input_tokens + output_tokens) tokens
                FROM token_usage GROUP BY project_id ORDER BY tokens DESC
                """
            ).fetchall()
            tasks = connection.execute(
                """
                SELECT task_id, SUM(input_tokens + output_tokens) tokens
                FROM token_usage WHERE task_id IS NOT NULL
                GROUP BY task_id ORDER BY tokens DESC LIMIT 100
                """
            ).fetchall()
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0)
                FROM token_reservations WHERE status = 'active'
                """
            ).fetchone()[0]
            return {
                "input_tokens": total["input"], "output_tokens": total["output"],
                "total_tokens": total["input"] + total["output"],
                "control_tokens": 0, "reserved_tokens": reserved,
                "projects": [dict(row) for row in projects],
                "tasks": [dict(row) for row in tasks],
            }
        finally:
            connection.close()
