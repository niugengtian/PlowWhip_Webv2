from __future__ import annotations

import json
from typing import Any

from plow_whip_web.store.database import Database


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, *, actor: str, method: str, path: str, status_code: int, detail: dict[str, Any]) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO audit_log(actor, method, path, status_code, detail_json) VALUES (?, ?, ?, ?, ?)",
                (actor, method, path, status_code, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
            )

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
            return [{
                "sequence": row["sequence"], "actor": row["actor"], "method": row["method"],
                "path": row["path"], "status_code": row["status_code"],
                "detail": json.loads(row["detail_json"]), "created_at": row["created_at"],
            } for row in rows]
        finally:
            connection.close()
