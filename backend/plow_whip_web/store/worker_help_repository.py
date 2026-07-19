from __future__ import annotations

import json
import uuid
from typing import Any

from plow_whip_web.domain.model import DomainError, InvalidTransitionError, NotFoundError
from plow_whip_web.runtime.worker_help import (
    is_extreme_escalation,
    validate_worker_help_request,
)
from plow_whip_web.store.database import Database


class WorkerHelpRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_help_request(
        self,
        *,
        project_id: str,
        task_id: str,
        worker_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        clean = validate_worker_help_request(payload)
        request_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT id, project_id, status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None or task["project_id"] != project_id:
                raise NotFoundError(f"task not found in project: {task_id}")
            connection.execute(
                """
                INSERT INTO worker_help_requests(
                    id, project_id, task_id, worker_id, blocker, evidence_json,
                    attempted_actions_json, minimal_question, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    request_id,
                    project_id,
                    task_id,
                    worker_id,
                    clean["blocker"],
                    json.dumps(clean["evidence"], ensure_ascii=False, sort_keys=True),
                    json.dumps(clean["attempted_actions"], ensure_ascii=False),
                    clean["minimal_question"],
                ),
            )
        return self.get_help_request(request_id)

    def resolve_help_request(
        self,
        request_id: str,
        *,
        resolution: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if resolution not in {"answered", "replanned", "replaced", "closed"}:
            raise DomainError(f"unsupported help resolution: {resolution}")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM worker_help_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"help request not found: {request_id}")
            if row["status"] not in {"open", "answered", "replanned", "replaced"}:
                raise InvalidTransitionError("help request is no longer open")
            connection.execute(
                """
                UPDATE worker_help_requests
                SET status = ?, resolution_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    resolution,
                    json.dumps(
                        {"resolution": resolution, **(detail or {})},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    request_id,
                ),
            )
        return self.get_help_request(request_id)

    def escalate(
        self,
        *,
        project_id: str,
        task_id: str,
        reason_class: str,
        detail: str,
        help_request_id: str | None = None,
    ) -> dict[str, Any]:
        if not is_extreme_escalation(reason_class):
            raise DomainError(
                "only extreme credential/permission, safety, conflicting owner "
                "directives, or unresolvable ambiguity may escalate to the owner"
            )
        escalation_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT id, project_id, status, revision FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None or task["project_id"] != project_id:
                raise NotFoundError(f"task not found in project: {task_id}")
            if task["status"] in {"completed", "failed", "cancelled"}:
                raise InvalidTransitionError("terminal tasks cannot escalate")
            connection.execute(
                """
                UPDATE tasks
                SET status = 'paused', revision = revision + 1,
                    blocked_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (f"escalated:{reason_class}", task_id),
            )
            connection.execute(
                """
                INSERT INTO task_escalations(
                    id, project_id, task_id, help_request_id, reason_class, detail, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    escalation_id,
                    project_id,
                    task_id,
                    help_request_id,
                    reason_class,
                    detail.strip(),
                ),
            )
            if help_request_id:
                connection.execute(
                    """
                    UPDATE worker_help_requests
                    SET status = 'escalated', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND project_id = ?
                    """,
                    (help_request_id, project_id),
                )
        return self.get_escalation(escalation_id)

    def get_help_request(self, request_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM worker_help_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"help request not found: {request_id}")
            return _help_row(row)
        finally:
            connection.close()

    def list_help_requests(self, project_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            if task_id:
                rows = connection.execute(
                    """
                    SELECT * FROM worker_help_requests
                    WHERE project_id = ? AND task_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (project_id, task_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM worker_help_requests
                    WHERE project_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (project_id,),
                ).fetchall()
            return [_help_row(row) for row in rows]
        finally:
            connection.close()

    def get_escalation(self, escalation_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM task_escalations WHERE id = ?",
                (escalation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"escalation not found: {escalation_id}")
            return dict(row)
        finally:
            connection.close()


def _help_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json"))
    item["attempted_actions"] = json.loads(item.pop("attempted_actions_json"))
    resolution = item.pop("resolution_json")
    item["resolution"] = json.loads(resolution) if resolution else None
    return item
