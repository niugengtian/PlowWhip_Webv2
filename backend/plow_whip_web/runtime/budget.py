from __future__ import annotations

from typing import Any

from plow_whip_web.domain.model import BudgetExceededError, TaskRecord
from plow_whip_web.store.database import Database
from plow_whip_web.store.settings_repository import SettingsRepository

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
                "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM token_usage WHERE date(created_at) = date('now')"
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

    def record(
        self, task: TaskRecord, execution: dict[str, Any], *,
        provider: str, run_id: str | None = None, add_to_task: bool = False,
    ) -> None:
        input_tokens = int(execution.get("input_tokens", 0))
        output_tokens = int(execution.get("output_tokens", 0))
        with self.database.transaction(immediate=True) as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO token_usage(
                    task_id, project_id, worker_id, input_tokens, output_tokens, provider, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id, task.project_id, task.worker_id, input_tokens, output_tokens,
                    provider, run_id,
                ),
            )
            if add_to_task and inserted.rowcount:
                connection.execute(
                    """
                    UPDATE tasks SET tokens_used = tokens_used + ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (input_tokens + output_tokens, task.id),
                )
            if run_id:
                connection.execute(
                    """
                    UPDATE token_reservations
                    SET status = 'settled', actual_tokens = ?, settled_at = CURRENT_TIMESTAMP
                    WHERE run_id = ? AND status = 'active'
                    """,
                    (input_tokens + output_tokens, run_id),
                )

    def release(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE token_reservations
                SET status = 'released', settled_at = CURRENT_TIMESTAMP
                WHERE run_id = ? AND status = 'active'
                """,
                (run_id,),
            )

    def summary(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                "SELECT COALESCE(SUM(input_tokens),0) input, COALESCE(SUM(output_tokens),0) output FROM token_usage"
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
                FROM token_usage GROUP BY task_id ORDER BY tokens DESC LIMIT 100
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
                "control_tokens": 0,
                "reserved_tokens": reserved,
                "projects": [dict(row) for row in projects], "tasks": [dict(row) for row in tasks],
            }
        finally:
            connection.close()
