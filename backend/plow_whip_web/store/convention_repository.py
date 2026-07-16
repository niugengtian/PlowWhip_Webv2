from __future__ import annotations

import uuid
from typing import Any

from plow_whip_web.domain.model import RevisionConflictError
from plow_whip_web.store.database import Database


class ConventionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def put(self, *, scope: str, scope_id: str, content: str, expected_revision: int) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, revision FROM conventions WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            ).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise RevisionConflictError(f"expected convention revision {expected_revision}, current revision {current}")
            convention_id = row["id"] if row else str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO conventions(id, scope, scope_id, content, revision)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, scope_id) DO UPDATE SET content = excluded.content,
                    revision = excluded.revision, updated_at = CURRENT_TIMESTAMP
                """,
                (convention_id, scope, scope_id, content, current + 1),
            )
        return self.get(scope=scope, scope_id=scope_id)

    def get(self, *, scope: str, scope_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM conventions WHERE scope = ? AND scope_id = ?", (scope, scope_id)
            ).fetchone()
            if row is None:
                return {"scope": scope, "scope_id": scope_id, "content": "", "revision": 0, "updated_at": None}
            return dict(row)
        finally:
            connection.close()

    def resolve(self, *, project_id: str | None, task_id: str) -> list[dict[str, Any]]:
        scopes = [("global", "global")]
        if project_id:
            scopes.append(("project", project_id))
        scopes.append(("task", task_id))
        return [self.get(scope=scope, scope_id=scope_id) for scope, scope_id in scopes]
