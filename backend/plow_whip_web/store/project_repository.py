from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import InvalidTransitionError, NotFoundError
from plow_whip_web.store.database import Database


DEFAULT_ROLES = ("coordination", "fullstack", "web3", "devops_sre", "verification")


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, *, name: str, path: str, host_path: str | None = None) -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        resolved = str(Path(path).expanduser().resolve())
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO projects(id, name, path, host_path) VALUES (?, ?, ?, ?)",
                (project_id, name, resolved, host_path),
            )
            for kind in DEFAULT_ROLES:
                connection.execute(
                    "INSERT INTO roles(id, project_id, kind) VALUES (?, ?, ?)",
                    (str(uuid.uuid4()), project_id, kind),
                )
        return self.get(project_id)

    def get(self, project_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            roles = connection.execute(
                "SELECT id, kind, status FROM roles WHERE project_id = ? ORDER BY kind",
                (project_id,),
            ).fetchall()
            workers = connection.execute(
                """
                SELECT w.id, w.role_id, r.kind role, w.provider, w.session_id,
                       w.external_session_id, w.session_generation, w.status,
                       w.active_task_id, w.last_seen_at, w.last_error, w.released_at
                FROM workers w JOIN roles r ON r.id = w.role_id
                WHERE w.project_id = ? ORDER BY r.kind
                """,
                (project_id,),
            ).fetchall()
            return {
                "id": row["id"], "name": row["name"], "path": row["path"],
                "host_path": row["host_path"],
                "status": row["status"], "created_at": row["created_at"],
                "roles": [dict(item) for item in roles],
                "workers": [dict(item) for item in workers],
            }
        finally:
            connection.close()

    def list(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM projects ORDER BY created_at DESC, id DESC"
            )]
        finally:
            connection.close()
        return [self.get(project_id) for project_id in ids]

    def resolve_role(self, project_id: str, kind: str) -> dict[str, str]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT p.path project_path, p.host_path, p.status project_status, r.id role_id
                FROM projects p JOIN roles r ON r.project_id = p.id
                WHERE p.id = ? AND r.kind = ?
                """,
                (project_id, kind),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"role not found: {project_id}/{kind}")
            if row["project_status"] != "active":
                raise InvalidTransitionError("project is not active")
            return dict(row)
        finally:
            connection.close()

    def release(self, project_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            project = connection.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise NotFoundError(f"project not found: {project_id}")
            unfinished = connection.execute(
                """
                SELECT COUNT(*) FROM tasks WHERE project_id = ?
                AND status NOT IN ('completed', 'terminal_failed', 'cancelled')
                """,
                (project_id,),
            ).fetchone()[0]
            if unfinished:
                raise InvalidTransitionError("project has unfinished tasks")
            workers = connection.execute(
                "SELECT * FROM workers WHERE project_id = ? AND released_at IS NULL",
                (project_id,),
            ).fetchall()
            for worker in workers:
                connection.execute(
                    """
                    INSERT INTO worker_session_archives(
                        worker_id, project_id, role_id, session_id, session_generation, reason
                    ) VALUES (?, ?, ?, ?, ?, 'project_completed')
                    """,
                    (worker["id"], project_id, worker["role_id"], worker["session_id"], worker["session_generation"]),
                )
            connection.execute("DELETE FROM task_leases WHERE worker_id IN (SELECT id FROM workers WHERE project_id = ?)", (project_id,))
            connection.execute("DELETE FROM resource_locks WHERE project_id = ?", (project_id,))
            connection.execute(
                "UPDATE workers SET status = 'released', active_task_id = NULL, released_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project_id,),
            )
            connection.execute(
                "UPDATE projects SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (project_id,),
            )
        return self.get(project_id)

    def rotate_worker(self, worker_id: str, *, reason: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            worker = connection.execute(
                """
                SELECT w.*, r.kind role FROM workers w JOIN roles r ON r.id = w.role_id
                WHERE w.id = ?
                """,
                (worker_id,),
            ).fetchone()
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            if worker["status"] != "idle":
                raise InvalidTransitionError("only an idle worker session can rotate")
            connection.execute(
                """
                INSERT INTO worker_session_archives(
                    worker_id, project_id, role_id, session_id, session_generation, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (worker_id, worker["project_id"], worker["role_id"], worker["session_id"], worker["session_generation"], reason),
            )
            session_id = str(uuid.uuid4())
            connection.execute(
                """
                UPDATE workers SET session_id = ?, external_session_id = NULL,
                    last_error = NULL, session_generation = session_generation + 1,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (session_id, worker_id),
            )
            updated = connection.execute(
                """
                SELECT w.id, w.role_id, r.kind role, w.provider, w.session_id,
                       w.external_session_id, w.session_generation, w.status,
                       w.active_task_id, w.last_seen_at, w.last_error, w.released_at
                FROM workers w JOIN roles r ON r.id = w.role_id WHERE w.id = ?
                """,
                (worker_id,),
            ).fetchone()
            return dict(updated)

    def rebind_worker(self, worker_id: str, *, provider: str, reason: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            worker = connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            if worker["status"] != "idle":
                raise InvalidTransitionError("only an idle worker can be rebound")
            configured = connection.execute(
                "SELECT enabled FROM provider_configs WHERE name = ?", (provider,)
            ).fetchone()
            if configured is None or not configured["enabled"]:
                raise InvalidTransitionError("target provider is not enabled")
            connection.execute(
                """
                INSERT INTO worker_session_archives(
                    worker_id, project_id, role_id, session_id, session_generation, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    worker_id, worker["project_id"], worker["role_id"], worker["session_id"],
                    worker["session_generation"], reason,
                ),
            )
            connection.execute(
                """
                UPDATE workers SET provider = ?, session_id = ?, external_session_id = NULL,
                    last_error = NULL, session_generation = session_generation + 1,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (provider, str(uuid.uuid4()), worker_id),
            )
        project = self.get(worker["project_id"])
        return next(item for item in project["workers"] if item["id"] == worker_id)
