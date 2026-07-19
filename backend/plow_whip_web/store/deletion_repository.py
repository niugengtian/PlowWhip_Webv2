from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from plow_whip_web.domain.model import (
    NotFoundError,
    RevisionConflictError,
    TaskStatus,
)
from plow_whip_web.runtime.aggregate_reducer import AggregateReducer
from plow_whip_web.store.database import Database


ACTIVE = {"running", "stopping", "verifying"}
TERMINAL = {"completed", "terminal_failed", "cancelled"}


class DeletionRepository:
    """Idempotent stop, reconcile, tombstone, then delete control data."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def eligibility(
        self, aggregate_type: str, aggregate_id: str, *, connection: Any | None = None
    ) -> dict[str, Any]:
        if aggregate_type not in {"task", "goal"}:
            raise NotFoundError(f"unsupported aggregate: {aggregate_type}")
        owns_connection = connection is None
        connection = connection or self.database.connect()
        try:
            tombstone = _tombstone(connection, aggregate_type, aggregate_id)
            if tombstone is not None and tombstone["status"] == "deleted":
                return {
                    "status": "deleted",
                    "eligible": False,
                    "next_action": "none",
                    "expected_revision": tombstone["final_revision"],
                    "active_task_ids": [],
                    "pending_host_jobs": [],
                    "cascade_task_count": 0,
                    "artifact_files_deleted": False,
                    "usage_retention": "anonymous",
                }
            table = "tasks" if aggregate_type == "task" else "goals"
            row = connection.execute(
                f"SELECT id, status, revision FROM {table} WHERE id = ?",
                (aggregate_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"{aggregate_type} not found: {aggregate_id}")
            tasks = (
                [row]
                if aggregate_type == "task"
                else connection.execute(
                    "SELECT id, status FROM tasks WHERE goal_id = ? ORDER BY ordinal, id",
                    (aggregate_id,),
                ).fetchall()
            )
            task_ids = [item["id"] for item in tasks]
            active = [item["id"] for item in tasks if item["status"] in ACTIVE]
            pending_jobs = _pending_jobs(connection, task_ids)
            waiting = bool(active or pending_jobs)
            stopping = bool(tombstone) or row["status"] == "stopping"
            return {
                "status": "stopping" if waiting and stopping else "stop_required" if waiting else "deletable",
                "eligible": not waiting,
                "next_action": "await_host_reconciliation" if waiting and stopping else "request_safe_stop" if waiting else "delete",
                "expected_revision": int(row["revision"]),
                "active_task_ids": active,
                "pending_host_jobs": pending_jobs,
                "cascade_task_count": len(task_ids) if aggregate_type == "goal" else 0,
                "artifact_files_deleted": False,
                "usage_retention": "anonymous",
            }
        finally:
            if owns_connection:
                connection.close()

    def task(
        self,
        task_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            tombstone = _tombstone(connection, "task", task_id)
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if tombstone is not None and tombstone["status"] == "deleted":
                return _view(tombstone, [])
            if row is None:
                if tombstone is not None:
                    return _view(tombstone, [])
                raise NotFoundError(f"task not found: {task_id}")
            if tombstone is None and int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {row['revision']}"
                )
            pending_jobs = _pending_jobs(connection, [task_id])
            if row["status"] in ACTIVE or pending_jobs:
                tombstone = self._request_task_stop(
                    connection,
                    row,
                    idempotency_key=idempotency_key,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason=reason,
                    tombstone=tombstone,
                )
                return _view(tombstone, pending_jobs)
            return self._delete_tasks(
                connection,
                aggregate_type="task",
                aggregate_id=task_id,
                rows=[row],
                idempotency_key=(
                    tombstone["idempotency_key"] if tombstone else idempotency_key
                ),
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                tombstone=tombstone,
            )

    def goal(
        self,
        goal_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            tombstone = _tombstone(connection, "goal", goal_id)
            goal = connection.execute(
                "SELECT * FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()
            if tombstone is not None and tombstone["status"] == "deleted":
                return _view(tombstone, [])
            if goal is None:
                if tombstone is not None:
                    return _view(tombstone, [])
                raise NotFoundError(f"goal not found: {goal_id}")
            if tombstone is None and int(goal["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {goal['revision']}"
                )
            tasks = connection.execute(
                "SELECT * FROM tasks WHERE goal_id = ? ORDER BY ordinal DESC, id",
                (goal_id,),
            ).fetchall()
            task_ids = [row["id"] for row in tasks]
            pending_jobs = _pending_jobs(connection, task_ids)
            pending_task_ids = _pending_task_ids(connection, task_ids)
            active = [row for row in tasks if row["status"] in ACTIVE]
            if active or pending_jobs:
                stable_key = (
                    tombstone["idempotency_key"] if tombstone else idempotency_key
                )
                for row in tasks:
                    task_key = f"{stable_key}:task:{row['id']}"
                    if row["status"] in ACTIVE:
                        self._request_task_stop(
                            connection,
                            row,
                            idempotency_key=task_key,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            reason=f"goal deletion: {reason}",
                            tombstone=None,
                            create_tombstone=False,
                        )
                    elif row["id"] in pending_task_ids:
                        if row["status"] in TERMINAL:
                            self._request_host_stop_without_state_rewrite(
                                connection, row, reason=f"goal deletion: {reason}"
                            )
                        else:
                            self._request_task_stop(
                                connection,
                                row,
                                idempotency_key=task_key,
                                actor_type=actor_type,
                                actor_id=actor_id,
                                reason=f"goal deletion: {reason}",
                                tombstone=None,
                                create_tombstone=False,
                            )
                    elif row["status"] not in TERMINAL:
                        self._cancel_undispatched_task(
                            connection,
                            row,
                            idempotency_key=task_key,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            reason=f"goal deletion: {reason}",
                        )
                if tombstone is None and goal["status"] not in TERMINAL:
                    next_revision = int(goal["revision"]) + 1
                    connection.execute(
                        """
                        UPDATE goals SET status = 'cancelled', revision = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND revision = ?
                        """,
                        (next_revision, goal_id, goal["revision"]),
                    )
                    AggregateReducer.record(
                        connection,
                        aggregate_type="goal",
                        aggregate_id=goal_id,
                        revision=next_revision,
                        idempotency_key=f"{stable_key}:stop",
                        actor_type=actor_type,
                        actor_id=actor_id,
                        reason=reason,
                        previous_state={
                            "status": goal["status"],
                            "revision": int(goal["revision"]),
                        },
                        new_state={"status": "cancelled", "revision": next_revision},
                        previous_evidence_hash=goal["last_evidence_hash"],
                        new_evidence_hash=goal["last_evidence_hash"],
                    )
                if tombstone is None:
                    connection.execute(
                        """
                        INSERT INTO deletion_tombstones(
                            aggregate_type, aggregate_id, command_id,
                            idempotency_key, requested_revision, status,
                            actor_type, actor_id, reason
                        ) VALUES ('goal', ?, ?, ?, ?, 'stopping', ?, ?, ?)
                        """,
                        (
                            goal_id,
                            str(uuid.uuid4()),
                            idempotency_key,
                            expected_revision,
                            actor_type,
                            actor_id,
                            reason,
                        ),
                    )
                return _view(
                    _tombstone(connection, "goal", goal_id),
                    pending_jobs,
                )
            return self._delete_tasks(
                connection,
                aggregate_type="goal",
                aggregate_id=goal_id,
                rows=list(tasks),
                idempotency_key=(
                    tombstone["idempotency_key"] if tombstone else idempotency_key
                ),
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                tombstone=tombstone,
                goal=goal,
            )

    @staticmethod
    def _request_task_stop(
        connection: Any,
        row: Any,
        *,
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
        tombstone: Any,
        create_tombstone: bool = True,
    ) -> Any:
        revision = int(row["revision"])
        if row["status"] != TaskStatus.STOPPING.value:
            next_revision = revision + 1
            connection.execute(
                """
                UPDATE tasks SET status = 'stopping', revision = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (next_revision, row["id"], revision),
            )
            connection.execute(
                """
                INSERT INTO task_controls(task_id, action, reason)
                VALUES (?, 'cancel', ?)
                """,
                (row["id"], reason),
            )
            connection.execute(
                """
                UPDATE provider_sessions SET state = 'terminating',
                    revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = 'bound'
                """,
                (row["id"],),
            )
            AggregateReducer.record(
                connection,
                aggregate_type="task",
                aggregate_id=row["id"],
                revision=next_revision,
                idempotency_key=f"{idempotency_key}:stop",
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                previous_state={"status": row["status"], "revision": revision},
                new_state={"status": "stopping", "revision": next_revision},
                previous_evidence_hash=row["last_evidence_hash"],
                new_evidence_hash=row["last_evidence_hash"],
            )
        if create_tombstone and tombstone is None:
            connection.execute(
                """
                INSERT INTO deletion_tombstones(
                    aggregate_type, aggregate_id, command_id, idempotency_key,
                    requested_revision, status, actor_type, actor_id, reason
                ) VALUES ('task', ?, ?, ?, ?, 'stopping', ?, ?, ?)
                """,
                (
                    row["id"],
                    str(uuid.uuid4()),
                    idempotency_key,
                    revision,
                    actor_type,
                    actor_id,
                    reason,
                ),
            )
        return _tombstone(connection, "task", row["id"]) if create_tombstone else None

    @staticmethod
    def _cancel_undispatched_task(
        connection: Any,
        row: Any,
        *,
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
    ) -> None:
        revision = int(row["revision"])
        next_revision = revision + 1
        updated = connection.execute(
            """
            UPDATE tasks SET status = 'cancelled', revision = ?, last_error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND revision = ? AND status NOT IN (
                'completed', 'terminal_failed', 'cancelled'
            )
            """,
            (next_revision, reason, row["id"], revision),
        )
        if updated.rowcount != 1:
            return
        connection.execute(
            """
            UPDATE provider_sessions SET state = 'archived',
                revision = revision + 1, unbound_at = CURRENT_TIMESTAMP,
                archived_at = CURRENT_TIMESTAMP, archive_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND state IN ('bound', 'idle', 'terminating')
            """,
            (reason, row["id"]),
        )
        AggregateReducer.record(
            connection,
            aggregate_type="task",
            aggregate_id=row["id"],
            revision=next_revision,
            idempotency_key=f"{idempotency_key}:cancelled",
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            previous_state={"status": row["status"], "revision": revision},
            new_state={"status": "cancelled", "revision": next_revision},
            previous_evidence_hash=row["last_evidence_hash"],
            new_evidence_hash=row["last_evidence_hash"],
        )

    @staticmethod
    def _request_host_stop_without_state_rewrite(
        connection: Any, row: Any, *, reason: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_controls(task_id, action, reason)
            SELECT ?, 'cancel', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM task_controls
                WHERE task_id = ? AND action = 'cancel' AND reason = ?
            )
            """,
            (row["id"], reason, row["id"], reason),
        )
        connection.execute(
            """
            UPDATE provider_sessions SET state = 'terminating',
                revision = revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND state = 'bound'
            """,
            (row["id"],),
        )

    @staticmethod
    def _delete_tasks(
        connection: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        rows: list[Any],
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
        tombstone: Any,
        goal: Any | None = None,
    ) -> dict[str, Any]:
        task_ids = [row["id"] for row in rows]
        artifacts = _retained_artifacts(connection, task_ids)
        usage_calls = 0
        for task_row in rows:
            task_id = task_row["id"]
            usage_calls += connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE model_calls
                SET task_id_hash = ?, task_id = NULL,
                    goal_id_hash = COALESCE(goal_id_hash, ?), goal_id = NULL,
                    project_id = NULL, role_id = NULL, worker_id = NULL,
                    host_job_id = NULL
                WHERE task_id = ?
                """,
                (
                    _anonymous_id(task_id),
                    _anonymous_id(task_row["goal_id"])
                    if task_row["goal_id"] else None,
                    task_id,
                ),
            )
        if goal is not None:
            goal_only_calls = connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE goal_id = ?",
                (aggregate_id,),
            ).fetchone()[0]
            usage_calls += goal_only_calls
            connection.execute(
                """
                UPDATE model_calls
                SET goal_id_hash = ?, goal_id = NULL, project_id = NULL,
                    role_id = NULL, worker_id = NULL, host_job_id = NULL
                WHERE goal_id = ?
                """,
                (_anonymous_id(aggregate_id), aggregate_id),
            )
        if goal is not None:
            for row in rows:
                task_revision = int(row["revision"]) + 1
                AggregateReducer.record(
                    connection,
                    aggregate_type="task",
                    aggregate_id=row["id"],
                    revision=task_revision,
                    idempotency_key=(
                        f"{idempotency_key}:task:{row['id']}:deleted"
                    ),
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason=f"goal cascade: {reason}",
                    previous_state={
                        "status": row["status"],
                        "revision": int(row["revision"]),
                    },
                    new_state={
                        "status": "deleted",
                        "revision": task_revision,
                    },
                    previous_evidence_hash=row["last_evidence_hash"],
                    new_evidence_hash=row["last_evidence_hash"],
                )
            final_revision = int(goal["revision"]) + 1
            AggregateReducer.record(
                connection,
                aggregate_type="goal",
                aggregate_id=aggregate_id,
                revision=final_revision,
                idempotency_key=f"{idempotency_key}:deleted",
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                previous_state={
                    "status": goal["status"],
                    "revision": int(goal["revision"]),
                },
                new_state={"status": "deleted", "revision": final_revision},
                previous_evidence_hash=goal["last_evidence_hash"],
                new_evidence_hash=goal["last_evidence_hash"],
            )
        else:
            row = rows[0]
            final_revision = int(row["revision"]) + 1
            AggregateReducer.record(
                connection,
                aggregate_type="task",
                aggregate_id=aggregate_id,
                revision=final_revision,
                idempotency_key=f"{idempotency_key}:deleted",
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                previous_state={
                    "status": row["status"],
                    "revision": int(row["revision"]),
                },
                new_state={"status": "deleted", "revision": final_revision},
                previous_evidence_hash=row["last_evidence_hash"],
                new_evidence_hash=row["last_evidence_hash"],
            )
        if tombstone is None:
            connection.execute(
                """
                INSERT INTO deletion_tombstones(
                    aggregate_type, aggregate_id, command_id, idempotency_key,
                    requested_revision, final_revision, status, actor_type,
                    actor_id, reason, anonymous_usage_calls,
                    retained_artifacts_json, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'deleted', ?, ?, ?, ?, ?,
                          CURRENT_TIMESTAMP)
                """,
                (
                    aggregate_type,
                    aggregate_id,
                    str(uuid.uuid4()),
                    idempotency_key,
                    int(goal["revision"] if goal is not None else rows[0]["revision"]),
                    final_revision,
                    actor_type,
                    actor_id,
                    reason,
                    usage_calls,
                    json.dumps(artifacts, sort_keys=True),
                ),
            )
        else:
            connection.execute(
                """
                UPDATE deletion_tombstones
                SET status = 'deleted', final_revision = ?,
                    anonymous_usage_calls = ?, retained_artifacts_json = ?,
                    deleted_at = CURRENT_TIMESTAMP
                WHERE aggregate_type = ? AND aggregate_id = ?
                """,
                (
                    final_revision,
                    usage_calls,
                    json.dumps(artifacts, sort_keys=True),
                    aggregate_type,
                    aggregate_id,
                ),
            )
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            connection.execute(
                f"DELETE FROM outbox_events WHERE aggregate_id IN ({placeholders})",
                tuple(task_ids),
            )
            connection.execute(
                f"UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id IN ({placeholders})",
                tuple(task_ids),
            )
            connection.execute(
                f"UPDATE goals SET parent_task_id = NULL WHERE parent_task_id IN ({placeholders})",
                tuple(task_ids),
            )
            connection.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",
                tuple(task_ids),
            )
        if goal is not None:
            connection.execute(
                "DELETE FROM outbox_events WHERE aggregate_id = ?", (aggregate_id,)
            )
            connection.execute("DELETE FROM goals WHERE id = ?", (aggregate_id,))
        for task_id in task_ids:
            connection.execute(
                """
                UPDATE aggregate_transitions SET aggregate_id = ?
                WHERE aggregate_type = 'task' AND aggregate_id = ?
                """,
                (_anonymous_id(task_id), task_id),
            )
        if goal is not None:
            connection.execute(
                """
                UPDATE aggregate_transitions SET aggregate_id = ?
                WHERE aggregate_type = 'goal' AND aggregate_id = ?
                """,
                (_anonymous_id(aggregate_id), aggregate_id),
            )
        connection.execute(
            """
            INSERT INTO audit_events(event_type, payload_json)
            VALUES ('aggregate.deleted', ?)
            """,
            (
                json.dumps(
                    {
                        "aggregate_type": aggregate_type,
                        "aggregate_id_hash": _anonymous_id(aggregate_id),
                        "usage_calls_retained": usage_calls,
                        "artifact_references_retained": len(artifacts),
                        "artifact_files_deleted": False,
                        "reason": reason,
                        "actor_type": actor_type,
                    },
                    sort_keys=True,
                ),
            ),
        )
        return _view(
            _tombstone(connection, aggregate_type, aggregate_id),
            [],
        )


def _pending_jobs(connection: Any, task_ids: list[str]) -> list[str]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    return [
        row["job_id"]
        for row in connection.execute(
            f"""
            SELECT job_id FROM host_jobs
            WHERE task_id IN ({placeholders}) AND consumed_at IS NULL
            ORDER BY created_at, job_id
            """,
            tuple(task_ids),
        ).fetchall()
    ]


def _pending_task_ids(connection: Any, task_ids: list[str]) -> set[str]:
    if not task_ids:
        return set()
    placeholders = ",".join("?" for _ in task_ids)
    return {
        row["task_id"]
        for row in connection.execute(
            f"""
            SELECT DISTINCT task_id FROM host_jobs
            WHERE task_id IN ({placeholders}) AND consumed_at IS NULL
            """,
            tuple(task_ids),
        ).fetchall()
    }


def _tombstone(connection: Any, aggregate_type: str, aggregate_id: str) -> Any:
    return connection.execute(
        """
        SELECT * FROM deletion_tombstones
        WHERE aggregate_type = ? AND aggregate_id = ?
        """,
        (aggregate_type, aggregate_id),
    ).fetchone()


def _retained_artifacts(connection: Any, task_ids: list[str]) -> list[str]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    values: set[str] = set()
    rows = connection.execute(
        f"""
        SELECT verification_json value FROM tasks WHERE id IN ({placeholders})
        UNION ALL
        SELECT result_json value FROM task_runs WHERE task_id IN ({placeholders})
        UNION ALL
        SELECT result_json value FROM host_jobs WHERE task_id IN ({placeholders})
        """,
        (*task_ids, *task_ids, *task_ids),
    ).fetchall()
    for row in rows:
        if not row["value"]:
            continue
        try:
            _collect_paths(json.loads(row["value"]), values)
        except (TypeError, ValueError):
            continue
    return sorted(values)


def _collect_paths(value: Any, found: set[str], key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_paths(child, found, child_key)
    elif isinstance(value, list):
        for child in value:
            _collect_paths(child, found, key)
    elif (
        isinstance(value, str)
        and key in {"path", "relative_path", "artifact", "output_ref"}
        and value
    ):
        found.add(value)


def _anonymous_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _view(row: Any, pending_jobs: list[str]) -> dict[str, Any]:
    item = dict(row)
    item["retained_artifacts"] = json.loads(item.pop("retained_artifacts_json"))
    item["pending_host_jobs"] = pending_jobs
    item["artifact_files_deleted"] = False
    return item
