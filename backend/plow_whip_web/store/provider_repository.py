from __future__ import annotations

import json
from typing import Any

from plow_whip_web.store.database import Database


class ProviderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute("SELECT * FROM provider_configs ORDER BY name").fetchall()
            return [{
                "name": row["name"], "status": row["status"],
                "model_invoked": bool(row["model_invoked"]),
                "capabilities": json.loads(row["capabilities_json"]), "reason": row["reason"],
            } for row in rows]
        finally:
            connection.close()

    def get(self, name: str) -> dict[str, Any] | None:
        return next((item for item in self.list() if item["name"] == name), None)
