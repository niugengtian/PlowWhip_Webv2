from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
    ResourceBusyError,
    TaskRecord,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
)
from plow_whip_web.store.database import Database


@dataclass(frozen=True, slots=True)
class ClaimResult:
    task: TaskRecord
    attempt_id: str | None
    run_id: str | None
    claimed: bool


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        title: str,
        objective: str,
        project_path: str,
        command: dict[str, Any],
        verification: list[dict[str, Any]],
        max_attempts: int,
        token_budget: int,
        idempotency_key: str,
        project_id: str | None = None,
        role_id: str | None = None,
        resource_key: str | None = None,
    ) -> TaskRecord:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, duplicate["task_id"])
            task_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, revision,
                    command_json, verification_json, max_attempts, token_budget,
                    project_id, role_id, resource_key
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    objective,
                    project_path,
                    TaskStatus.READY.value,
                    _dump(command),
                    _dump(verification),
                    max_attempts,
                    token_budget,
                    project_id,
                    role_id,
                    resource_key,
                ),
            )
            self._event(
                connection,
                task_id=task_id,
                event_type="task.created",
                payload={"title": title, "objective": objective},
                revision=0,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task_id)

    def get(self, task_id: str) -> TaskRecord:
        connection = self.database.connect()
        try:
            return self._get_with_connection(connection, task_id)
        finally:
            connection.close()

    def list(self, *, limit: int = 100) -> list[TaskRecord]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_task_from_row(row) for row in rows]
        finally:
            connection.close()

    def events(self, task_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            self._get_with_connection(connection, task_id)
            rows = connection.execute(
                """
                SELECT sequence, event_type, payload_json, state_revision, created_at
                FROM task_events WHERE task_id = ? AND sequence > ? ORDER BY sequence
                """,
                (task_id, after),
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "state_revision": row["state_revision"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            connection.close()

    def claim(self, task_id: str, *, expected_revision: int, idempotency_key: str) -> ClaimResult:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return ClaimResult(self._get_with_connection(connection, duplicate["task_id"]), None, None, False)
            task = self._get_with_connection(connection, task_id)
            if task.revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task.revision}"
                )
            if task.status in TERMINAL_TASK_STATUSES:
                raise InvalidTransitionError(f"terminal task cannot run: {task.status}")
            if task.status is not TaskStatus.READY:
                raise InvalidTransitionError(f"task is not ready: {task.status}")
            if task.attempts_used >= task.max_attempts:
                raise InvalidTransitionError("task attempt budget exhausted")
            worker_id, lease_token, fencing_token = self._acquire_worker_and_lock(connection, task)
            attempt_id = str(uuid.uuid4())
            run_id = str(uuid.uuid4())
            next_revision = task.revision + 1
            attempt_number = task.attempts_used + 1
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, attempts_used = ?, worker_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ? AND status = ?
                """,
                (
                    TaskStatus.RUNNING.value,
                    next_revision,
                    attempt_number,
                    worker_id,
                    task.id,
                    task.revision,
                    TaskStatus.READY.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError("task changed while claiming")
            connection.execute(
                "INSERT INTO task_attempts(id, task_id, attempt_number, status) VALUES (?, ?, ?, 'running')",
                (attempt_id, task.id, attempt_number),
            )
            connection.execute(
                """
                INSERT INTO task_runs(id, task_id, attempt_id, run_type, provider, status)
                VALUES (?, ?, ?, 'execute', 'generic-command', 'running')
                """,
                (run_id, task.id, attempt_id),
            )
            self._event(
                connection,
                task_id=task.id,
                event_type="attempt.started",
                payload={
                    "attempt_id": attempt_id, "run_id": run_id, "attempt_number": attempt_number,
                    "worker_id": worker_id, "lease_token": lease_token, "fencing_token": fencing_token,
                },
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return ClaimResult(self._get_with_connection(connection, task.id), attempt_id, run_id, True)

    def mark_verifying(
        self,
        task_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> TaskRecord:
        return self._transition(
            task_id,
            expected_revision=expected_revision,
            from_status=TaskStatus.RUNNING,
            to_status=TaskStatus.VERIFYING,
            event_type="verification.started",
            payload={},
            idempotency_key=idempotency_key,
        )

    def finish(
        self,
        task_id: str,
        *,
        expected_revision: int,
        attempt_id: str,
        run_id: str,
        execution: dict[str, Any],
        verification: dict[str, Any],
        idempotency_key: str,
    ) -> TaskRecord:
        passed = bool(verification["passed"])
        target = TaskStatus.COMPLETED if passed else TaskStatus.TERMINAL_FAILED
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, duplicate["task_id"])
            task = self._get_with_connection(connection, task_id)
            if task.revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task.revision}"
                )
            if task.status is not TaskStatus.VERIFYING:
                raise InvalidTransitionError(f"task is not verifying: {task.status}")
            next_revision = task.revision + 1
            token_delta = int(execution.get("input_tokens", 0)) + int(execution.get("output_tokens", 0))
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, tokens_used = tokens_used + ?,
                    last_evidence_hash = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (
                    target.value,
                    next_revision,
                    token_delta,
                    verification["evidence_hash"],
                    None if passed else verification["summary"],
                    task.id,
                    task.revision,
                ),
            )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
            connection.execute(
                "UPDATE task_attempts SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("completed" if passed else "failed", attempt_id),
            )
            connection.execute(
                """
                UPDATE task_runs SET status = ?, input_tokens = ?, output_tokens = ?,
                    result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (
                    "completed" if passed else "failed",
                    execution.get("input_tokens", 0),
                    execution.get("output_tokens", 0),
                    _dump({"execution": execution, "verification": verification}),
                    run_id,
                ),
            )
            self._event(
                connection,
                task_id=task.id,
                event_type="task.completed" if passed else "task.terminal_failed",
                payload={"attempt_id": attempt_id, "run_id": run_id, "verification": verification},
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

    def _acquire_worker_and_lock(self, connection: Any, task: TaskRecord) -> tuple[str | None, str | None, int | None]:
        if task.project_id is None or task.role_id is None:
            return None, None, None
        connection.execute("DELETE FROM resource_locks WHERE expires_at <= CURRENT_TIMESTAMP")
        connection.execute("DELETE FROM task_leases WHERE expires_at <= CURRENT_TIMESTAMP")
        worker = connection.execute(
            "SELECT * FROM workers WHERE project_id = ? AND role_id = ?",
            (task.project_id, task.role_id),
        ).fetchone()
        if worker is None:
            worker_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workers(id, project_id, role_id, provider, session_id)
                VALUES (?, ?, ?, 'generic-command', ?)
                """,
                (worker_id, task.project_id, task.role_id, str(uuid.uuid4())),
            )
        else:
            worker_id = worker["id"]
            if worker["status"] != "idle":
                raise ResourceBusyError(f"role worker is busy: {worker_id}")
        resource_key = task.resource_key or f"project:{task.project_id}"
        collision = connection.execute(
            "SELECT task_id FROM resource_locks WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        if collision:
            raise ResourceBusyError(f"resource is busy: {resource_key}")
        connection.execute(
            "UPDATE projects SET next_fencing_token = next_fencing_token + 1 WHERE id = ?",
            (task.project_id,),
        )
        fencing_token = connection.execute(
            "SELECT next_fencing_token FROM projects WHERE id = ?", (task.project_id,)
        ).fetchone()[0]
        lease_token = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO task_leases(task_id, worker_id, lease_token, fencing_token, expires_at)
            VALUES (?, ?, ?, ?, datetime('now', '+5 minutes'))
            """,
            (task.id, worker_id, lease_token, fencing_token),
        )
        connection.execute(
            """
            INSERT INTO resource_locks(resource_key, project_id, task_id, worker_id, lease_token, expires_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+5 minutes'))
            """,
            (resource_key, task.project_id, task.id, worker_id, lease_token),
        )
        connection.execute(
            "UPDATE workers SET status = 'busy', active_task_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (task.id, worker_id),
        )
        return worker_id, lease_token, fencing_token

    @staticmethod
    def _release_worker_and_lock(connection: Any, task_id: str, worker_id: str | None) -> None:
        connection.execute("DELETE FROM resource_locks WHERE task_id = ?", (task_id,))
        connection.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))
        if worker_id:
            connection.execute(
                "UPDATE workers SET status = 'idle', active_task_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND active_task_id = ?",
                (worker_id, task_id),
            )

    def _transition(
        self,
        task_id: str,
        *,
        expected_revision: int,
        from_status: TaskStatus,
        to_status: TaskStatus,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> TaskRecord:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, duplicate["task_id"])
            task = self._get_with_connection(connection, task_id)
            if task.revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task.revision}"
                )
            if task.status is not from_status:
                raise InvalidTransitionError(f"expected {from_status}, current {task.status}")
            next_revision = task.revision + 1
            connection.execute(
                "UPDATE tasks SET status = ?, revision = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND revision = ?",
                (to_status.value, next_revision, task.id, task.revision),
            )
            self._event(
                connection,
                task_id=task.id,
                event_type=event_type,
                payload=payload,
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

    def _get_with_connection(self, connection: Any, task_id: str) -> TaskRecord:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id}")
        return _task_from_row(row)

    @staticmethod
    def _event(
        connection: Any,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        revision: int,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events(task_id, event_type, payload_json, state_revision, idempotency_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, event_type, _dump(payload), revision, idempotency_key),
        )


def _task_from_row(row: Any) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        title=row["title"],
        objective=row["objective"],
        project_path=row["project_path"],
        project_id=row["project_id"],
        role_id=row["role_id"],
        worker_id=row["worker_id"],
        resource_key=row["resource_key"],
        status=TaskStatus(row["status"]),
        revision=row["revision"],
        command=json.loads(row["command_json"]),
        verification=json.loads(row["verification_json"]),
        max_attempts=row["max_attempts"],
        attempts_used=row["attempts_used"],
        token_budget=row["token_budget"],
        tokens_used=row["tokens_used"],
        last_evidence_hash=row["last_evidence_hash"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
