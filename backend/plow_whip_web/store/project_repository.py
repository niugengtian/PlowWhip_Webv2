from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import InvalidTransitionError, NotFoundError
from plow_whip_web.roles import DEFAULT_PROJECT_ROLES, ROLE_KINDS
from plow_whip_web.runtime.butler import project_execution_policy
from plow_whip_web.store.database import Database


DEFAULT_ROLES = DEFAULT_PROJECT_ROLES


def rotate_worker_in_transaction(
    connection: Any,
    worker_id: str,
    *,
    reason: str,
    trigger_key: str | None = None,
) -> dict[str, Any]:
    worker = connection.execute(
        """
        SELECT w.*, r.kind role FROM workers w JOIN roles r ON r.id = w.role_id
        WHERE w.id = ?
        """,
        (worker_id,),
    ).fetchone()
    if worker is None:
        raise NotFoundError(f"worker not found: {worker_id}")
    if trigger_key and connection.execute(
        "SELECT 1 FROM worker_session_archives WHERE trigger_key = ?",
        (trigger_key,),
    ).fetchone():
        return _worker_view(connection, worker_id)
    if worker["status"] != "idle":
        raise InvalidTransitionError("only an idle worker session can rotate")
    connection.execute(
        """
        INSERT INTO worker_session_archives(
            worker_id, project_id, role_id, session_id, session_generation,
            reason, trigger_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            worker_id, worker["project_id"], worker["role_id"], worker["session_id"],
            worker["session_generation"], reason, trigger_key,
        ),
    )
    connection.execute(
        """
        UPDATE workers SET session_id = ?, external_session_id = NULL,
            last_error = NULL, session_generation = session_generation + 1,
            updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """,
        (str(uuid.uuid4()), worker_id),
    )
    return _worker_view(connection, worker_id)


def _worker_view(connection: Any, worker_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT w.id, w.role_id, r.kind role, w.provider, w.session_id,
               w.external_session_id, w.session_generation, w.status,
               w.active_task_id, w.last_seen_at, w.last_error, w.released_at,
               w.last_input_tokens, w.last_cached_input_tokens,
               w.last_output_tokens, w.last_uncached_input_tokens,
               w.last_context_pressure_tokens,
               w.last_context_pressure_reason, w.last_context_session_generation,
               w.last_attribution_granularity, w.last_value_classification,
               (
                   SELECT a.reason FROM worker_session_archives a
                   WHERE a.worker_id = w.id
                   ORDER BY a.archived_at DESC, a.id DESC LIMIT 1
               ) rotation_reason
        FROM workers w JOIN roles r ON r.id = w.role_id WHERE w.id = ?
        """,
        (worker_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self, *, name: str, path: str, host_path: str | None = None,
        execution_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        resolved = str(Path(path).expanduser().resolve())
        policy = project_execution_policy(execution_policy)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO projects(id, name, path, host_path, execution_policy_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id, name, resolved, host_path,
                    json.dumps(policy, ensure_ascii=False, sort_keys=True),
                ),
            )
            for kind in DEFAULT_ROLES:
                connection.execute(
                    """
                    INSERT INTO roles(id, project_id, kind, legacy)
                    VALUES (?, ?, ?, 0)
                    """,
                    (str(uuid.uuid4()), project_id, kind),
                )
            # Exactly one ProjectButler; never pre-create fixed development roles.
            butler_count = int(connection.execute(
                """
                SELECT COUNT(*) FROM roles
                WHERE project_id = ? AND kind = 'butler' AND legacy = 0
                """,
                (project_id,),
            ).fetchone()[0])
            if butler_count != 1:
                raise InvalidTransitionError(
                    "project create must materialize exactly one ProjectButler"
                )
        return self.get(project_id)

    def role_provider_bindings(self, project_id: str) -> dict[str, str]:
        """Return stable project+role → provider bindings from existing workers."""
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT r.kind role, w.provider
                FROM workers w
                JOIN roles r ON r.id = w.role_id
                WHERE w.project_id = ? AND w.released_at IS NULL
                """,
                (project_id,),
            ).fetchall()
            return {row["role"]: row["provider"] for row in rows}
        finally:
            connection.close()

    def get(self, project_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"project not found: {project_id}")
            roles = connection.execute(
                "SELECT id, kind, status, legacy FROM roles WHERE project_id = ? ORDER BY kind",
                (project_id,),
            ).fetchall()
            workers = connection.execute(
                """
                SELECT w.id, w.role_id, r.kind role, w.provider, w.session_id,
                       w.external_session_id, w.session_generation, w.status,
                       w.active_task_id, w.last_seen_at, w.last_error, w.released_at,
                       w.last_input_tokens, w.last_cached_input_tokens,
                       w.last_output_tokens, w.last_uncached_input_tokens,
                       w.last_context_pressure_tokens,
                       w.last_context_pressure_reason, w.last_context_session_generation,
                       w.last_attribution_granularity, w.last_value_classification,
                       (
                           SELECT a.reason FROM worker_session_archives a
                           WHERE a.worker_id = w.id
                           ORDER BY a.archived_at DESC, a.id DESC LIMIT 1
                       ) rotation_reason
                FROM workers w JOIN roles r ON r.id = w.role_id
                WHERE w.project_id = ? ORDER BY r.kind
                """,
                (project_id,),
            ).fetchall()
            return {
                "id": row["id"], "name": row["name"], "path": row["path"],
                "host_path": row["host_path"],
                "execution_policy": json.loads(row["execution_policy_json"]),
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

    def worker_detail(self, worker_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            worker = connection.execute(
                """
                SELECT w.*, r.kind role, r.status role_status,
                       p.name project_name, p.path project_path,
                       p.host_path project_host_path
                FROM workers w
                JOIN roles r ON r.id = w.role_id
                JOIN projects p ON p.id = w.project_id
                WHERE w.id = ?
                """,
                (worker_id,),
            ).fetchone()
            if worker is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            job = connection.execute(
                """
                SELECT * FROM host_jobs WHERE worker_id = ?
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            task_id = worker["active_task_id"] or (job["task_id"] if job else None)
            task = connection.execute(
                """
                SELECT t.id, t.title, t.objective, t.status, t.revision,
                       t.current_spec_revision spec_revision, t.provider,
                       t.goal_id, t.worker_id, s.spec_json
                FROM tasks t
                JOIN task_specs s ON s.task_id = t.id
                    AND s.spec_revision = t.current_spec_revision
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone() if task_id else None
            episode = connection.execute(
                "SELECT * FROM execution_episodes WHERE id = ?",
                (job["episode_id"],),
            ).fetchone() if job and job["episode_id"] else None
            task_session = connection.execute(
                "SELECT * FROM task_sessions WHERE task_id = ?",
                (task_id,),
            ).fetchone() if task_id else None
            worker_view = dict(worker)
            task_view = dict(task) if task else None
            if task_view:
                task_view["spec"] = json.loads(task_view.pop("spec_json"))
            job_view = dict(job) if job else None
            if job_view and job_view.get("result_json"):
                job_view["result"] = json.loads(job_view.pop("result_json"))
            episode_view = dict(episode) if episode else None
            if episode_view and episode_view.get("checkpoint_json"):
                episode_view["checkpoint"] = json.loads(
                    episode_view.pop("checkpoint_json")
                )
            return {
                "worker": worker_view,
                "task": task_view,
                "host_job": job_view,
                "episode": episode_view,
                "task_session": dict(task_session) if task_session else None,
                "ownership": {
                    "project_id": worker["project_id"],
                    "project_name": worker["project_name"],
                    "role_id": worker["role_id"],
                    "role": worker["role"],
                    "session_id": worker["session_id"],
                    "external_session_id": (
                        task_session["external_session_id"]
                        if task_session else worker["external_session_id"]
                    ),
                    "session_generation": (
                        task_session["session_generation"]
                        if task_session else worker["session_generation"]
                    ),
                    "session_scope": "task_role" if task_session else "worker_legacy",
                    "task_id": task_id,
                },
            }
        finally:
            connection.close()

    def resolve_role(self, project_id: str, kind: str) -> dict[str, str]:
        if kind not in ROLE_KINDS and kind != "butler":
            raise NotFoundError(f"role not found: {project_id}/{kind}")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT p.path project_path, p.host_path, p.status project_status,
                       r.id role_id, r.status role_status
                FROM projects p JOIN roles r ON r.project_id = p.id
                WHERE p.id = ? AND r.kind = ?
                """,
                (project_id, kind),
            ).fetchone()
            if row is None or row["role_status"] != "available":
                project = connection.execute(
                    "SELECT path, host_path, status FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if project is None:
                    raise NotFoundError(f"project not found: {project_id}")
                if project["status"] != "active":
                    raise InvalidTransitionError("project is not active")
                role_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO roles(id, project_id, kind, status)
                    VALUES (?, ?, ?, 'ephemeral')
                    """,
                    (role_id, project_id, f"{kind}:manual:{role_id}"),
                )
                return {
                    "project_path": project["path"],
                    "host_path": project["host_path"],
                    "project_status": project["status"],
                    "role_id": role_id,
                }
            if row["project_status"] != "active":
                raise InvalidTransitionError("project is not active")
            return dict(row)

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

    def rotate_worker(
        self, worker_id: str, *, reason: str, trigger_key: str | None = None
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            return rotate_worker_in_transaction(
                connection, worker_id, reason=reason, trigger_key=trigger_key
            )

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
