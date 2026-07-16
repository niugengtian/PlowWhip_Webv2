from __future__ import annotations

import json
from typing import Any

from plow_whip_web.store.database import Database


class OutboxRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM outbox_events WHERE sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (after, limit),
            ).fetchall()
            return [{
                "sequence": row["sequence"], "topic": row["topic"],
                "aggregate_id": row["aggregate_id"], "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]), "created_at": row["created_at"],
                "delivered_at": row["delivered_at"],
            } for row in rows]
        finally:
            connection.close()

    def acknowledge(self, sequence: int) -> bool:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE outbox_events SET delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP) WHERE sequence = ?",
                (sequence,),
            )
            return cursor.rowcount == 1
