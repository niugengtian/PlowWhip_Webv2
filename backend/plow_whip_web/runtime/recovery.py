from __future__ import annotations

import json
from typing import Any

from plow_whip_web.runtime.aggregate_reducer import AggregateReducer
from plow_whip_web.store.database import Database


class RecoveryService:
    model_invoked = False

    def __init__(self, database: Database) -> None:
        self.database = database

    def reconcile(self) -> dict[str, Any]:
        recovered: list[str] = []
        with self.database.transaction(immediate=True) as connection:
            stale = connection.execute(
                """
                SELECT t.* FROM tasks t LEFT JOIN task_leases l ON l.task_id = t.id
                WHERE t.status IN ('running', 'stopping', 'verifying')
                AND (l.task_id IS NULL OR l.expires_at <= CURRENT_TIMESTAMP)
                AND NOT EXISTS (
                    SELECT 1 FROM host_jobs h
                    WHERE h.task_id = t.id AND h.consumed_at IS NULL
                )
                """
            ).fetchall()
            for task in stale:
                target = "ready" if task["attempts_used"] < task["max_attempts"] else "needs_human"
                revision = task["revision"] + 1
                event_key = f"recovery:{task['id']}:{revision}"
                connection.execute(
                    "UPDATE tasks SET status = ?, revision = ?, worker_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (target, revision, task["id"]),
                )
                connection.execute("DELETE FROM task_leases WHERE task_id = ?", (task["id"],))
                connection.execute("DELETE FROM resource_locks WHERE task_id = ?", (task["id"],))
                connection.execute(
                    "UPDATE workers SET status = 'idle', active_task_id = NULL WHERE active_task_id = ?",
                    (task["id"],),
                )
                connection.execute(
                    "UPDATE task_attempts SET status = 'interrupted', finished_at = CURRENT_TIMESTAMP WHERE task_id = ? AND status = 'running'",
                    (task["id"],),
                )
                connection.execute(
                    "UPDATE task_runs SET status = 'interrupted', finished_at = CURRENT_TIMESTAMP WHERE task_id = ? AND status = 'running'",
                    (task["id"],),
                )
                connection.execute(
                    """
                    UPDATE model_calls
                    SET status = 'failed', error_class = 'recovery_released',
                        settled_at = CURRENT_TIMESTAMP
                    WHERE task_id = ? AND status = 'prepared'
                    """,
                    (task["id"],),
                )
                connection.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, payload_json, state_revision, idempotency_key)
                    VALUES (?, 'task.recovered', ?, ?, ?)
                    """,
                    (
                        task["id"], json.dumps({"target": target}, sort_keys=True), revision,
                        event_key,
                    ),
                )
                AggregateReducer.record(
                    connection,
                    aggregate_type="task",
                    aggregate_id=task["id"],
                    revision=revision,
                    idempotency_key=f"task-transition:{event_key}",
                    actor_type="runtime",
                    actor_id=None,
                    reason="task.recovered",
                    previous_state={
                        "status": task["status"],
                        "revision": int(task["revision"]),
                    },
                    new_state={"status": target, "revision": revision},
                    previous_evidence_hash=task["last_evidence_hash"],
                    new_evidence_hash=task["last_evidence_hash"],
                )
                if target == "needs_human":
                    connection.execute(
                        """
                        INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
                        VALUES ('task', ?, 'task.needs_human', ?)
                        """,
                        (task["id"], json.dumps({"reason": "recovery_attempt_budget_exhausted"})),
                    )
                recovered.append(task["id"])
            reset = connection.execute(
                """
                UPDATE workers SET status = 'idle', active_task_id = NULL
                WHERE status = 'busy' AND active_task_id NOT IN (
                    SELECT id FROM tasks WHERE status IN ('running', 'stopping', 'verifying')
                )
                """
            ).rowcount
        return {"recovered_tasks": recovered, "reset_workers": reset, "model_invoked": False}
