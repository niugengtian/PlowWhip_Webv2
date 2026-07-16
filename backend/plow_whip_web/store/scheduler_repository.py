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
            }
        finally:
            connection.close()
