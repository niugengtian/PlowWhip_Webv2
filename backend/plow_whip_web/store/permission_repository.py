from __future__ import annotations

import uuid
from typing import Any

from plow_whip_web.store.database import Database


class PermissionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def grant(
        self, *, project_id: str | None, capability: str, resource: str,
        decision: str, reason: str,
    ) -> dict[str, Any]:
        grant_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO permission_grants(id, project_id, capability, resource, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (grant_id, project_id, capability, resource, decision, reason),
            )
        return next(item for item in self.list() if item["id"] == grant_id)

    def list(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM permission_grants ORDER BY created_at DESC, id DESC"
            )]
        finally:
            connection.close()

    def revoke(self, grant_id: str) -> bool:
        with self.database.transaction(immediate=True) as connection:
            return connection.execute(
                "UPDATE permission_grants SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) WHERE id = ?",
                (grant_id,),
            ).rowcount == 1

    def is_allowed(self, *, project_id: str | None, capability: str, resource: str) -> bool:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT decision FROM permission_grants
                WHERE project_id IS ? AND capability = ? AND resource = ? AND revoked_at IS NULL
                ORDER BY rowid DESC LIMIT 1
                """,
                (project_id, capability, resource),
            ).fetchone()
            return bool(row and row["decision"] == "allow")
        finally:
            connection.close()
