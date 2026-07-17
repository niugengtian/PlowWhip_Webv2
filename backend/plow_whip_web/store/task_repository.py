from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    ProviderUnavailableError,
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
        network_requirement: str = "none",
        provider: str = "generic-command",
        quality_profile: str = "balanced",
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
                    project_id, role_id, resource_key, network_requirement, provider, quality_profile
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    network_requirement,
                    provider,
                    quality_profile,
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

    def list_ready(self, *, limit: int = 100) -> list[TaskRecord]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE status = 'ready'
                AND (next_eligible_at IS NULL OR next_eligible_at <= CURRENT_TIMESTAMP)
                ORDER BY created_at, id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_task_from_row(row) for row in rows]
        finally:
            connection.close()

    def worker_execution_context(self, worker_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT w.external_session_id, w.provider, p.path project_path,
                       COALESCE(p.host_path, p.path) host_path
                FROM workers w JOIN projects p ON p.id = w.project_id
                WHERE w.id = ?
                """,
                (worker_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            return dict(row)
        finally:
            connection.close()

    def record_worker_result(
        self, worker_id: str, *, external_session_id: str | None, error: str | None
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workers SET external_session_id = COALESCE(?, external_session_id),
                    last_seen_at = CURRENT_TIMESTAMP, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (external_session_id, error, worker_id),
            )

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
            attempt_number = int(connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM task_attempts WHERE task_id = ?",
                (task.id,),
            ).fetchone()[0])
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
                VALUES (?, ?, ?, 'execute', ?, 'running')
                """,
                (run_id, task.id, attempt_id, task.provider),
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

    def record_quality_run(
        self, *, task_id: str, attempt_id: str, run_type: str, result: dict[str, Any]
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO task_runs(id, task_id, attempt_id, run_type, provider, status, result_json, finished_at)
                VALUES (?, ?, ?, ?, 'deterministic-quality-gate', 'completed', ?, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()), task_id, attempt_id, run_type, _dump(result)),
            )

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
        max_same_failure: int = 3,
        max_no_progress: int = 3,
    ) -> TaskRecord:
        passed = bool(verification["passed"])
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
            fingerprint = verification["evidence_hash"]
            same_failure_count = 0 if passed else (
                task.same_failure_count + 1 if task.last_failure_fingerprint == fingerprint else 1
            )
            no_progress_count = 0 if passed else same_failure_count
            can_retry = (
                not passed and task.attempts_used < task.max_attempts
                and same_failure_count < max_same_failure and no_progress_count < max_no_progress
            )
            target = TaskStatus.COMPLETED if passed else (TaskStatus.READY if can_retry else TaskStatus.TERMINAL_FAILED)
            next_revision = task.revision + 1
            token_delta = int(execution.get("input_tokens", 0)) + int(execution.get("output_tokens", 0))
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, tokens_used = tokens_used + ?,
                    last_evidence_hash = ?, last_error = ?, same_failure_count = ?,
                    no_progress_count = ?, last_failure_fingerprint = ?,
                    next_eligible_at = CASE WHEN ? THEN datetime('now', ?) ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (
                    target.value,
                    next_revision,
                    token_delta,
                    verification["evidence_hash"],
                    None if passed else verification["summary"],
                    same_failure_count,
                    no_progress_count,
                    None if passed else fingerprint,
                    1 if can_retry else 0,
                    f"+{min(300, 2 ** task.attempts_used)} seconds",
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
                event_type="task.completed" if passed else ("task.retry_scheduled" if can_retry else "task.terminal_failed"),
                payload={"attempt_id": attempt_id, "run_id": run_id, "verification": verification},
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

    def control(
        self, task_id: str, *, action: str, reason: str, expected_revision: int,
        idempotency_key: str,
    ) -> TaskRecord:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, duplicate["task_id"])
            task = self._get_with_connection(connection, task_id)
            if task.revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task.revision}"
                )
            if task.status in TERMINAL_TASK_STATUSES:
                raise InvalidTransitionError(f"terminal task cannot be controlled: {task.status}")
            transitions = {
                "pause": ({TaskStatus.READY, TaskStatus.NEEDS_HUMAN}, TaskStatus.PAUSED),
                "resume": ({TaskStatus.PAUSED, TaskStatus.NEEDS_HUMAN}, TaskStatus.READY),
                "needs_human": ({TaskStatus.READY, TaskStatus.PAUSED}, TaskStatus.NEEDS_HUMAN),
                "cancel": ({TaskStatus.READY, TaskStatus.PAUSED, TaskStatus.NEEDS_HUMAN}, TaskStatus.CANCELLED),
            }
            if action not in transitions:
                raise InvalidTransitionError(f"unsupported control action: {action}")
            allowed, target = transitions[action]
            if task.status not in allowed:
                raise InvalidTransitionError(f"cannot {action} task in state {task.status}")
            revision = task.revision + 1
            connection.execute(
                "UPDATE tasks SET status = ?, revision = ?, next_eligible_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target.value, revision, task.id),
            )
            connection.execute(
                "INSERT INTO task_controls(task_id, action, reason) VALUES (?, ?, ?)",
                (task.id, action, reason),
            )
            self._event(
                connection, task_id=task.id, event_type=f"task.{action}", payload={"reason": reason},
                revision=revision, idempotency_key=idempotency_key,
            )
            if target is TaskStatus.NEEDS_HUMAN:
                connection.execute(
                    """
                    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
                    VALUES ('task', ?, 'task.needs_human', ?)
                    """,
                    (task.id, _dump({"task_id": task.id, "reason": reason})),
                )
            return self._get_with_connection(connection, task.id)

    def request_running_cancel(
        self, task_id: str, *, reason: str, expected_revision: int, idempotency_key: str
    ) -> TaskRecord:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, task_id)
            task = self._get_with_connection(connection, task_id)
            if task.revision != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task.revision}"
                )
            if task.status not in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                raise InvalidTransitionError(f"cannot cancel task in state {task.status}")
            revision = task.revision + 1
            connection.execute(
                "UPDATE tasks SET status = ?, revision = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (TaskStatus.STOPPING.value, revision, task.id),
            )
            connection.execute(
                "INSERT INTO task_controls(task_id, action, reason) VALUES (?, 'cancel', ?)",
                (task.id, reason),
            )
            self._event(
                connection, task_id=task.id, event_type="task.cancel_requested",
                payload={"reason": reason}, revision=revision, idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

    def finalize_running_cancel(self, task_id: str, *, job_id: str) -> TaskRecord:
        with self.database.transaction(immediate=True) as connection:
            task = self._get_with_connection(connection, task_id)
            if task.status is TaskStatus.CANCELLED:
                return task
            if task.status is not TaskStatus.STOPPING:
                raise InvalidTransitionError(f"task is not stopping: {task.status}")
            revision = task.revision + 1
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, last_error = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (TaskStatus.CANCELLED.value, revision, task.id),
            )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
            connection.execute(
                """
                UPDATE task_attempts SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND status = 'running'
                """,
                (task.id,),
            )
            connection.execute(
                """
                UPDATE task_runs SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND status = 'running'
                """,
                (task.id,),
            )
            self._event(
                connection, task_id=task.id, event_type="task.cancelled",
                payload={"host_job_id": job_id}, revision=revision,
                idempotency_key=f"host-job:{job_id}:cancelled",
            )
            return self._get_with_connection(connection, task.id)

    def resume_after_external_interruption(
        self, task_id: str, *, job_id: str, external_session_id: str | None
    ) -> TaskRecord:
        """Release only after the Host Bridge has confirmed the old PID is gone."""
        with self.database.transaction(immediate=True) as connection:
            task = self._get_with_connection(connection, task_id)
            if task.status not in {TaskStatus.RUNNING, TaskStatus.STOPPING}:
                return task
            target = (
                TaskStatus.READY
                if task.attempts_used <= task.max_attempts
                else TaskStatus.NEEDS_HUMAN
            )
            revision = task.revision + 1
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?,
                    attempts_used = MAX(0, attempts_used - 1), worker_id = NULL,
                    last_error = 'external_execution_interrupted',
                    next_eligible_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target.value, revision, task.id),
            )
            if task.worker_id:
                connection.execute(
                    """
                    UPDATE workers SET external_session_id = COALESCE(?, external_session_id),
                        status = 'idle', active_task_id = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (external_session_id, "external_execution_interrupted", task.worker_id),
                )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
            connection.execute(
                """
                UPDATE task_attempts SET status = 'interrupted', finished_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND status = 'running'
                """,
                (task.id,),
            )
            connection.execute(
                """
                UPDATE task_runs SET status = 'interrupted', finished_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND status = 'running'
                """,
                (task.id,),
            )
            self._event(
                connection, task_id=task.id, event_type="task.execution_interrupted",
                payload={
                    "host_job_id": job_id, "target": target.value,
                    "session_retained": bool(external_session_id),
                },
                revision=revision, idempotency_key=f"host-job:{job_id}:interrupted",
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
                VALUES (?, ?, ?, ?, ?)
                """,
                (worker_id, task.project_id, task.role_id, task.provider, str(uuid.uuid4())),
            )
        else:
            worker_id = worker["id"]
            if worker["provider"] != task.provider:
                raise ProviderUnavailableError(
                    f"角色已绑定 {worker['provider']}，请轮转会话后再切换到 {task.provider}"
                )
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
        lease_seconds = max(300, int(task.command.get("timeout_seconds", 60)) + 60)
        lease_modifier = f"+{lease_seconds} seconds"
        connection.execute(
            """
            INSERT INTO task_leases(task_id, worker_id, lease_token, fencing_token, expires_at)
            VALUES (?, ?, ?, ?, datetime('now', ?))
            """,
            (task.id, worker_id, lease_token, fencing_token, lease_modifier),
        )
        connection.execute(
            """
            INSERT INTO resource_locks(resource_key, project_id, task_id, worker_id, lease_token, expires_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', ?))
            """,
            (resource_key, task.project_id, task.id, worker_id, lease_token, lease_modifier),
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
        network_requirement=row["network_requirement"],
        same_failure_count=row["same_failure_count"],
        no_progress_count=row["no_progress_count"],
        last_failure_fingerprint=row["last_failure_fingerprint"],
        next_eligible_at=row["next_eligible_at"],
        provider=row["provider"],
        quality_profile=row["quality_profile"],
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
