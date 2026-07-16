from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from plow_whip_web.store.database import Database


@dataclass(frozen=True, slots=True)
class SchedulerLease:
    acquired: bool
    owner: str
    lease_token: str | None
    fencing_token: int


class SchedulerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def acquire(self, owner: str, *, lease_seconds: int) -> SchedulerLease:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM scheduler_state WHERE id = 'global'").fetchone()
            busy = connection.execute(
                """
                SELECT 1 FROM scheduler_state
                WHERE id = 'global' AND lease_until > CURRENT_TIMESTAMP AND lease_owner != ?
                """,
                (owner,),
            ).fetchone()
            if busy:
                return SchedulerLease(False, owner, None, row["fencing_token"])
            lease_token = str(uuid.uuid4())
            fencing_token = row["fencing_token"] + 1
            connection.execute(
                """
                UPDATE scheduler_state SET lease_owner = ?, lease_token = ?, fencing_token = ?,
                    lease_until = datetime('now', ?), updated_at = CURRENT_TIMESTAMP WHERE id = 'global'
                """,
                (owner, lease_token, fencing_token, f"+{lease_seconds} seconds"),
            )
            return SchedulerLease(True, owner, lease_token, fencing_token)

    def finish(self, lease: SchedulerLease, result: dict[str, Any], error: str | None = None) -> bool:
        if not lease.acquired or not lease.lease_token:
            return False
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_state SET lease_until = CURRENT_TIMESTAMP, last_tick_at = CURRENT_TIMESTAMP,
                    last_result_json = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 'global' AND lease_token = ? AND fencing_token = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    error,
                    lease.lease_token,
                    lease.fencing_token,
                ),
            )
            return cursor.rowcount == 1

    def status(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT * FROM scheduler_state WHERE id = 'global'").fetchone()
            result = json.loads(row["last_result_json"]) if row["last_result_json"] else None
            return {
                "lease_owner": row["lease_owner"], "lease_until": row["lease_until"],
                "fencing_token": row["fencing_token"], "last_tick_at": row["last_tick_at"],
                "last_result": result, "last_error": row["last_error"],
                "runner_id": row["runner_id"], "runner_started_at": row["runner_started_at"],
                "runner_heartbeat_at": row["runner_heartbeat_at"],
                "runner_stopped_at": row["runner_stopped_at"], "runner_error": row["runner_error"],
                "runner_active": bool(connection.execute(
                    """
                    SELECT runner_heartbeat_at > datetime('now', '-45 seconds')
                        AND (runner_stopped_at IS NULL OR runner_stopped_at < runner_started_at)
                    FROM scheduler_state WHERE id = 'global'
                    """
                ).fetchone()[0]),
                "last_cron_slot": row["last_cron_slot"],
            }
        finally:
            connection.close()

    def runner_started(self, runner_id: str) -> None:
        self._runner_update(
            "runner_id = ?, runner_started_at = CURRENT_TIMESTAMP, runner_heartbeat_at = CURRENT_TIMESTAMP, "
            "runner_stopped_at = NULL, runner_error = NULL",
            (runner_id,),
        )

    def runner_heartbeat(self, runner_id: str) -> None:
        self._runner_update(
            "runner_heartbeat_at = CURRENT_TIMESTAMP",
            (),
            runner_id=runner_id,
        )

    def runner_stopped(self, runner_id: str) -> None:
        self._runner_update(
            "runner_stopped_at = CURRENT_TIMESTAMP",
            (),
            runner_id=runner_id,
        )

    def runner_error(self, runner_id: str, error: str) -> None:
        self._runner_update(
            "runner_error = ?, runner_heartbeat_at = CURRENT_TIMESTAMP",
            (error[:2000],),
            runner_id=runner_id,
        )

    def claim_cron_slot(self, slot: str) -> bool:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_state SET last_cron_slot = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = 'global' AND (last_cron_slot IS NULL OR last_cron_slot != ?)
                """,
                (slot, slot),
            )
            return cursor.rowcount == 1

    def _runner_update(
        self,
        assignments: str,
        parameters: tuple[object, ...],
        *,
        runner_id: str | None = None,
    ) -> None:
        where = "id = 'global'"
        values = parameters
        if runner_id is not None:
            where += " AND runner_id = ?"
            values += (runner_id,)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                f"UPDATE scheduler_state SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE {where}",
                values,
            )
