from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import (
    DomainError,
    EvidenceBaselineMissingError,
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
from plow_whip_web.store.project_repository import rotate_worker_in_transaction
from plow_whip_web.store.settings_repository import resolve_effective_settings
from plow_whip_web.runtime.evidence import manifest_hash
from plow_whip_web.runtime.token_ledger import TokenLedger
from plow_whip_web.store.resilience_repository import checkpoint_hash

# XL bootstrap tier hard deadline; single safety cap for Host dispatch and leases.
MAX_HARD_DEADLINE_SECONDS = 4800
EXECUTION_DEADLINE_GRACE_SECONDS = 60
LEGACY_DEFAULT_TIMEOUT_SECONDS = 600


def task_sizing_status(task: TaskRecord) -> str:
    return str(task.sizing.get("status") or "legacy_fallback")


def task_hard_deadline_seconds(task: TaskRecord) -> int:
    deadline = int(
        task.spec.get("deadline", {}).get("hard_seconds")
        or task.command.get("timeout_seconds", LEGACY_DEFAULT_TIMEOUT_SECONDS)
    )
    return min(max(deadline, 10), MAX_HARD_DEADLINE_SECONDS)


def task_lease_seconds(task: TaskRecord) -> int:
    return max(300, task_hard_deadline_seconds(task) + EXECUTION_DEADLINE_GRACE_SECONDS)


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
        idempotency_key: str,
        project_id: str | None = None,
        role_id: str | None = None,
        resource_key: str | None = None,
        network_requirement: str = "none",
        provider: str = "generic-command",
        quality_profile: str = "deterministic",
        sizing: dict[str, Any] | None = None,
        execution_policy: dict[str, Any] | None = None,
        scope: list[str] | None = None,
        acceptance: list[str] | None = None,
        artifacts: list[str] | None = None,
        constraints: list[str] | None = None,
        deadline: dict[str, Any] | None = None,
        provider_policy: str = "auto",
        fallback_enabled: bool = True,
        provider_order: list[str] | None = None,
    ) -> TaskRecord:
        provider_order = _normalize_provider_routing(
            provider=provider,
            provider_policy=provider_policy,
            fallback_enabled=fallback_enabled,
            provider_order=provider_order,
        )
        if provider_policy == "pinned":
            fallback_enabled = False
        if sizing is None:
            if execution_policy is not None:
                raise DomainError("execution_policy requires an explicit sizing record")
            sizing = {"status": "legacy_fallback"}
        if execution_policy is not None and execution_policy.get("max_attempts") is not None:
            max_attempts = int(execution_policy["max_attempts"])
        spec = canonical_task_spec(
            objective=objective,
            scope=scope,
            acceptance=acceptance,
            verification=verification,
            artifacts=artifacts,
            constraints=constraints,
            deadline=deadline or {
                "hard_seconds": (
                    execution_policy.get("hard_deadline_seconds")
                    if execution_policy else command.get("timeout_seconds", LEGACY_DEFAULT_TIMEOUT_SECONDS)
                )
            },
        )
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
                    project_id, role_id, resource_key, network_requirement, provider, quality_profile,
                    sizing_json, execution_budget_json, provider_policy,
                    fallback_enabled, provider_order_json
                ) VALUES (
                    ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
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
                    project_id,
                    role_id,
                    resource_key,
                    network_requirement,
                    provider,
                    quality_profile,
                    _dump(sizing),
                    _dump(execution_policy) if execution_policy is not None else None,
                    provider_policy,
                    1 if fallback_enabled else 0,
                    _dump(provider_order),
                ),
            )
            spec_hash = insert_task_spec(connection, task_id, spec, revision=1)
            insert_task_provider_policy(
                connection,
                task_id=task_id,
                spec_revision=1,
                provider_policy=provider_policy,
                fallback_enabled=fallback_enabled,
                provider_order=provider_order,
                initial_provider=provider,
            )
            self._event(
                connection,
                task_id=task_id,
                event_type="task.created",
                payload={
                    "title": title,
                    "objective": objective,
                    "sizing_status": str(sizing.get("status") or "legacy_fallback"),
                    "size_class": sizing.get("size_class"),
                    "bootstrap_version": sizing.get("bootstrap_version"),
                    "hard_deadline_seconds": (
                        spec["deadline"]["hard_seconds"]
                    ),
                    "spec_revision": 1,
                    "spec_hash": spec_hash,
                },
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

    def deletion_eligibility(self, task_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            task = connection.execute(
                """
                SELECT id, title, status, revision, attempts_used,
                       last_evidence_hash, work_item_kind, goal_id
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            return self._deletion_eligibility(connection, task)
        finally:
            connection.close()

    def delete(
        self,
        task_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                """
                SELECT * FROM task_deletion_tombstones
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                if duplicate["task_id"] != task_id:
                    raise RevisionConflictError("idempotency key belongs to another task")
                return dict(duplicate)

            task = connection.execute(
                """
                SELECT id, title, status, revision, attempts_used, role_id,
                       last_evidence_hash, work_item_kind, goal_id
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if int(task["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {task['revision']}"
                )
            eligibility = self._deletion_eligibility(connection, task)
            if not eligibility["deletable"]:
                raise InvalidTransitionError(str(eligibility["reason"]))

            legacy_parent = task["work_item_kind"] == "coordination"
            if legacy_parent:
                connection.execute(
                    "UPDATE goals SET parent_task_id = NULL WHERE parent_task_id = ?",
                    (task_id,),
                )
                connection.execute(
                    "UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = ?",
                    (task_id,),
                )

            connection.execute(
                """
                INSERT INTO task_deletion_tombstones(
                    task_id, title, reason, deleted_revision, idempotency_key
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, task["title"], reason, task["revision"], idempotency_key),
            )
            connection.execute(
                "INSERT INTO task_deletion_permits(task_id) VALUES (?)", (task_id,)
            )
            connection.execute("DELETE FROM task_specs WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.execute(
                "DELETE FROM task_deletion_permits WHERE task_id = ?", (task_id,)
            )
            if task["role_id"]:
                connection.execute(
                    """
                    DELETE FROM roles WHERE id = ? AND status = 'ephemeral'
                      AND NOT EXISTS (SELECT 1 FROM tasks WHERE role_id = ?)
                      AND NOT EXISTS (SELECT 1 FROM workers WHERE role_id = ?)
                    """,
                    (task["role_id"], task["role_id"], task["role_id"]),
                )
            deleted = connection.execute(
                "SELECT * FROM task_deletion_tombstones WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert deleted is not None
            return dict(deleted)

    @staticmethod
    def _deletion_eligibility(connection: Any, task: Any) -> dict[str, Any]:
        if task["status"] in {"running", "verifying", "stopping"}:
            return {"deletable": False, "reason": "active task cannot be deleted"}
        if int(task["attempts_used"]) != 0 or task["last_evidence_hash"]:
            return {"deletable": False, "reason": "task has execution evidence"}
        if task["goal_id"] and task["work_item_kind"] != "coordination":
            return {"deletable": False, "reason": "goal work items belong to the Goal aggregate"}
        evidence = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM task_attempts WHERE task_id = ?) attempts,
              (SELECT COUNT(*) FROM task_runs WHERE task_id = ?) runs,
              (SELECT COUNT(*) FROM host_jobs WHERE task_id = ?) host_jobs,
              (SELECT COUNT(*) FROM context_packs WHERE task_id = ?) context_packs,
              (SELECT COUNT(*) FROM token_usage WHERE task_id = ?) usage
            """,
            (task["id"],) * 5,
        ).fetchone()
        if evidence is not None and any(int(evidence[key]) for key in evidence.keys()):
            return {"deletable": False, "reason": "task has persisted execution evidence"}

        dependent = connection.execute(
            """
            SELECT id FROM tasks
            WHERE id != ? AND (
                parent_task_id = ? OR EXISTS (
                    SELECT 1 FROM json_each(COALESCE(depends_on_json, '[]'))
                    WHERE json_each.value = ?
                )
            ) LIMIT 1
            """,
            (task["id"], task["id"], task["id"]),
        ).fetchone()
        if dependent and task["work_item_kind"] != "coordination":
            return {"deletable": False, "reason": "task has dependent work items"}
        return {"deletable": True, "reason": None}

    def list(self, *, limit: int = 100) -> list[TaskRecord]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""{_TASK_WITH_SPEC}
                WHERE COALESCE(t.work_item_kind, '') != 'coordination'
                ORDER BY t.created_at DESC, t.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [_task_from_row(row) for row in rows]
        finally:
            connection.close()

    def list_ready(self, *, limit: int = 100) -> list[TaskRecord]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT t.*, s.spec_json AS task_spec_json, s.spec_hash AS task_spec_hash
                FROM tasks t
                JOIN task_specs s ON s.task_id = t.id
                    AND s.spec_revision = t.current_spec_revision
                WHERE t.status = 'ready'
                AND (t.next_eligible_at IS NULL OR t.next_eligible_at <= CURRENT_TIMESTAMP)
                AND COALESCE(t.work_item_kind, '') != 'coordination'
                ORDER BY t.created_at, t.id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_task_from_row(row) for row in rows]
        finally:
            connection.close()

    def worker_execution_context(
        self, worker_id: str, *, task_id: str | None = None
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=task_id is not None) as connection:
            if task_id is not None:
                connection.execute(
                    """
                    INSERT INTO task_sessions(
                        task_id, project_id, role_id, worker_id, provider
                    )
                    SELECT id, project_id, role_id, ?, provider
                    FROM tasks WHERE id = ? AND worker_id = ?
                    ON CONFLICT(task_id) DO UPDATE SET
                        worker_id = excluded.worker_id,
                        role_id = excluded.role_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (worker_id, task_id, worker_id),
                )
            row = connection.execute(
                """
                SELECT
                       CASE WHEN ? IS NULL THEN w.external_session_id
                            ELSE ts.external_session_id END external_session_id,
                       w.provider,
                       CASE WHEN ? IS NULL THEN w.session_generation
                            ELSE ts.session_generation END session_generation,
                       p.path project_path,
                       COALESCE(p.host_path, p.path) host_path
                FROM workers w
                JOIN projects p ON p.id = w.project_id
                LEFT JOIN task_sessions ts ON ts.task_id = ?
                WHERE w.id = ?
                """,
                (task_id, task_id, task_id, worker_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"worker not found: {worker_id}")
            if task_id is not None and row["session_generation"] is None:
                raise NotFoundError(f"task session not found: {task_id}")
            return dict(row)

    def record_worker_result(
        self,
        worker_id: str,
        *,
        external_session_id: str | None,
        error: str | None,
        task_id: str | None = None,
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
            if task_id is not None:
                connection.execute(
                    """
                    UPDATE task_sessions
                    SET external_session_id = COALESCE(?, external_session_id),
                        worker_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = ?
                    """,
                    (external_session_id, worker_id, task_id),
                )

    def switch_provider(
        self,
        task_id: str,
        *,
        provider: str,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> TaskRecord:
        """Record one cross-provider replacement without creating a new attempt."""
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
                    f"expected revision {expected_revision}, current {task.revision}"
                )
            if task.status not in {
                TaskStatus.READY,
                TaskStatus.NETWORK_SUSPENDED,
                TaskStatus.PROVIDER_SUSPENDED,
                TaskStatus.NEEDS_HUMAN,
            }:
                raise InvalidTransitionError(
                    f"cannot switch provider in state {task.status}"
                )
            if task.provider_policy == "pinned" or not task.fallback_enabled:
                raise InvalidTransitionError("TaskSpec pins the Provider")
            if provider == task.provider:
                return task
            old_provider = task.provider
            revision = task.revision + 1
            connection.execute(
                """
                UPDATE tasks
                SET provider = ?, status = 'ready', revision = ?,
                    suspended_from_status = NULL, suspension_reason = NULL,
                    suspension_incident_id = NULL, next_eligible_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (provider, revision, task.id),
            )
            connection.execute(
                """
                UPDATE task_sessions
                SET provider = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (provider, task.id),
            )
            if task.worker_id:
                connection.execute(
                    """
                    UPDATE workers
                    SET provider = ?, external_session_id = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (provider, task.worker_id),
                )
            self._event(
                connection,
                task_id=task.id,
                event_type="task.provider_switched",
                payload={
                    "from": old_provider,
                    "to": provider,
                    "reason": reason,
                    "attempt_incremented": False,
                },
                revision=revision,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

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

    def latest_events(
        self, task_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            self._get_with_connection(connection, task_id)
            rows = connection.execute(
                """
                SELECT sequence, event_type, payload_json, state_revision, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (task_id, min(max(limit, 1), 100)),
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "state_revision": row["state_revision"],
                    "created_at": row["created_at"],
                }
                for row in reversed(rows)
            ]
        finally:
            connection.close()

    def claim(
        self, task_id: str, *, expected_revision: int, idempotency_key: str,
    ) -> ClaimResult:
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
            _assert_task_spec(task)
            recovery = connection.execute(
                """
                SELECT * FROM task_recovery_checkpoints
                WHERE task_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (task.id,),
            ).fetchone()
            if (
                recovery is None
                and task.attempts_used >= _authoritative_max_attempts(task)
            ):
                raise InvalidTransitionError("task attempt budget exhausted")
            limits, _, _ = resolve_effective_settings(
                connection,
                project_id=task.project_id,
                task_id=task.id,
                role_id=task.role_id,
            )
            if self._in_flight_count(connection) >= int(limits["max_parallel_workers"]):
                raise ResourceBusyError("global parallel worker limit reached")
            worker_id, lease_token, fencing_token = self._acquire_worker_and_lock(connection, task)
            attempt_id = (
                str(recovery["attempt_id"])
                if recovery is not None else str(uuid.uuid4())
            )
            run_id = str(uuid.uuid4())
            next_revision = task.revision + 1
            attempt_number = (
                int(
                    connection.execute(
                        """
                        SELECT attempt_number FROM task_attempts WHERE id = ?
                        """,
                        (attempt_id,),
                    ).fetchone()["attempt_number"]
                )
                if recovery is not None
                else int(connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM task_attempts WHERE task_id = ?",
                    (task.id,),
                ).fetchone()[0])
            )
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, attempts_used = ?, worker_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ? AND status = ?
                """,
                (
                    TaskStatus.RUNNING.value,
                    next_revision,
                    task.attempts_used + (0 if recovery is not None else 1),
                    worker_id,
                    task.id,
                    task.revision,
                    TaskStatus.READY.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError("task changed while claiming")
            connection.execute(
                """
                INSERT INTO task_sessions(
                    task_id, project_id, role_id, worker_id, provider
                )
                SELECT id, project_id, role_id, ?, provider
                FROM tasks WHERE id = ?
                ON CONFLICT(task_id) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    role_id = excluded.role_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (worker_id, task.id),
            )
            if recovery is None:
                connection.execute(
                    """
                    INSERT INTO task_attempts(
                        id, task_id, attempt_number, status, spec_revision
                    ) VALUES (?, ?, ?, 'running', ?)
                    """,
                    (attempt_id, task.id, attempt_number, task.spec_revision),
                )
            else:
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET status = 'running', finished_at = NULL
                    WHERE id = ? AND task_id = ?
                    """,
                    (attempt_id, task.id),
                )
                connection.execute(
                    """
                    UPDATE task_recovery_checkpoints
                    SET status = 'consumed', consumed_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'pending'
                    """,
                    (recovery["id"],),
                )
            connection.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt_id, run_type, provider, status, spec_revision
                ) VALUES (?, ?, ?, 'execute', ?, 'running', ?)
                """,
                (run_id, task.id, attempt_id, task.provider, task.spec_revision),
            )
            self._event(
                connection,
                task_id=task.id,
                event_type=(
                    "execution.resumed"
                    if recovery is not None else "attempt.started"
                ),
                payload={
                    "attempt_id": attempt_id, "run_id": run_id, "attempt_number": attempt_number,
                    "worker_id": worker_id, "lease_token": lease_token, "fencing_token": fencing_token,
                    "spec_revision": task.spec_revision,
                    "checkpoint_id": (
                        recovery["id"] if recovery is not None else None
                    ),
                    "attempt_incremented": recovery is None,
                },
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return ClaimResult(self._get_with_connection(connection, task.id), attempt_id, run_id, True)

    def in_flight_count(self) -> int:
        connection = self.database.connect()
        try:
            return self._in_flight_count(connection)
        finally:
            connection.close()

    @staticmethod
    def _in_flight_count(connection: Any) -> int:
        return int(connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT id AS task_id FROM tasks
                WHERE status IN ('running', 'verifying', 'stopping')
                UNION
                SELECT task_id FROM host_jobs WHERE consumed_at IS NULL
            )
            """
        ).fetchone()[0])

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

    def record_evidence_baseline(
        self,
        *,
        task_id: str,
        attempt_id: str,
        run_id: str,
        spec_revision: int,
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        baseline_json = _dump(baseline)
        baseline_hash = hashlib.sha256(baseline_json.encode("utf-8")).hexdigest()
        with self.database.transaction(immediate=True) as connection:
            context = connection.execute(
                """
                SELECT t.id, t.current_spec_revision, a.task_id AS attempt_task_id,
                       a.spec_revision AS attempt_spec_revision,
                       r.task_id AS run_task_id, r.attempt_id AS run_attempt_id,
                       r.spec_revision AS run_spec_revision
                FROM tasks t
                JOIN task_attempts a ON a.id = ?
                JOIN task_runs r ON r.id = ?
                WHERE t.id = ?
                """,
                (attempt_id, run_id, task_id),
            ).fetchone()
            if context is None or {
                context["id"],
                context["attempt_task_id"],
                context["run_task_id"],
            } != {task_id}:
                raise DomainError("evidence baseline task binding mismatch")
            if context["run_attempt_id"] != attempt_id or {
                int(context["current_spec_revision"]),
                int(context["attempt_spec_revision"]),
                int(context["run_spec_revision"]),
            } != {spec_revision}:
                raise DomainError("evidence baseline run/spec binding mismatch")
            connection.execute(
                """
                INSERT INTO run_evidence_baselines(
                    run_id, task_id, attempt_id, spec_revision,
                    baseline_json, baseline_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, task_id, attempt_id, spec_revision,
                    baseline_json, baseline_hash,
                ),
            )
        return baseline

    def evidence_baseline(self, run_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT baseline_json, baseline_hash
                FROM run_evidence_baselines WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise EvidenceBaselineMissingError("run evidence baseline is missing")
            if hashlib.sha256(row["baseline_json"].encode("utf-8")).hexdigest() != row[
                "baseline_hash"
            ]:
                raise DomainError("run evidence baseline is corrupt")
            return json.loads(row["baseline_json"])
        finally:
            connection.close()

    def evidence_execution_context(self, run_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT r.id run_id, r.started_at,
                       CURRENT_TIMESTAMP finished_at, t.project_path,
                       t.command_json, ts.session_generation,
                       COALESCE(h.external_session_id, ts.external_session_id)
                           external_session_id,
                       h.job_id host_job_id,
                       COALESCE(h.fencing_token, l.fencing_token) fencing_token
                FROM task_runs r
                JOIN tasks t ON t.id = r.task_id
                LEFT JOIN task_sessions ts ON ts.task_id = t.id
                LEFT JOIN host_jobs h ON h.run_id = r.id
                LEFT JOIN task_leases l ON l.task_id = t.id
                WHERE r.id = ?
                ORDER BY h.created_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise DomainError(f"execution context missing for run: {run_id}")
            command = json.loads(row["command_json"])
            return {
                "run_id": row["run_id"],
                "argv": list(command.get("argv") or []),
                "cwd": row["project_path"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "session_generation": row["session_generation"],
                "external_session_id": row["external_session_id"],
                "host_job_id": row["host_job_id"],
                "fencing_token": row["fencing_token"],
            }
        finally:
            connection.close()

    def inheritable_artifacts(
        self,
        task_id: str,
        *,
        session_generation: int | None,
        spec_revision: int | None = None,
        current_artifacts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded, hash-bound provenance for this Task generation only."""
        if session_generation is None:
            return []
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT manifest_json, manifest_hash, spec_revision, run_id
                FROM evidence_manifests
                WHERE task_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 20
                """,
                (task_id,),
            ).fetchall()
        finally:
            connection.close()
        inherited: dict[str, dict[str, Any]] = {}
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            environment = manifest.get("environment") or {}
            if (
                manifest.get("task_id") != task_id
                or environment.get("session_generation") != session_generation
            ):
                continue
            for artifact in manifest.get("artifacts") or []:
                relative_path = str(artifact.get("relative_path") or "")
                after = artifact.get("after") or {}
                if (
                    not relative_path
                    or not after.get("sha256")
                    or artifact.get("provenance", "current_run")
                    not in {"current_run", "same_task_session_generation"}
                ):
                    continue
                inherited.setdefault(relative_path, {
                    "relative_path": relative_path,
                    "sha256": after["sha256"],
                    "task_id": task_id,
                    "session_generation": session_generation,
                    "manifest_hash": row["manifest_hash"],
                    "spec_revision": row["spec_revision"],
                    "run_id": row["run_id"],
                })
        current_by_path = {
            str(item.get("relative_path") or ""): item
            for item in (current_artifacts or [])
            if item.get("relative_path") and item.get("sha256")
        }
        if current_by_path and spec_revision is not None:
            connection = self.database.connect()
            try:
                baselines = connection.execute(
                    """
                    SELECT b.run_id, b.spec_revision, b.baseline_json,
                           b.baseline_hash
                    FROM run_evidence_baselines b
                    WHERE b.task_id = ? AND b.spec_revision = ?
                    ORDER BY b.created_at, b.rowid LIMIT 20
                    """,
                    (task_id, spec_revision),
                ).fetchall()
            finally:
                connection.close()
            for row in baselines:
                baseline = json.loads(row["baseline_json"])
                if (
                    (baseline.get("environment") or {}).get("session_generation")
                    != session_generation
                ):
                    continue
                before_by_path = {
                    str(item.get("relative_path") or ""): item
                    for item in baseline.get("artifacts") or []
                }
                for relative_path, current in current_by_path.items():
                    before = before_by_path.get(relative_path) or {}
                    if before.get("sha256") == current.get("sha256"):
                        continue
                    inherited.setdefault(relative_path, {
                        "relative_path": relative_path,
                        "sha256": current["sha256"],
                        "task_id": task_id,
                        "session_generation": session_generation,
                        "baseline_hash": row["baseline_hash"],
                        "spec_revision": row["spec_revision"],
                        "run_id": row["run_id"],
                    })
        return list(inherited.values())

    def finish(
        self,
        task_id: str,
        *,
        expected_revision: int,
        attempt_id: str,
        run_id: str,
        execution: dict[str, Any],
        evidence_manifest: dict[str, Any],
        idempotency_key: str,
        max_same_failure: int = 2,
    ) -> TaskRecord:
        evidence_hash = manifest_hash(evidence_manifest)
        passed = bool(evidence_manifest["passed"])
        verification = {
            "passed": passed,
            "checks": [
                item["check"]
                for item in evidence_manifest["verification_commands"]
            ],
            "evidence_hash": evidence_hash,
            "failure_fingerprint": evidence_manifest["failure_fingerprint"],
            "summary": evidence_manifest["summary"],
        }
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
            _validate_evidence_manifest(task, evidence_manifest, execution)
            binding = connection.execute(
                """
                SELECT a.task_id AS attempt_task_id,
                       a.spec_revision AS attempt_spec_revision,
                       r.task_id AS run_task_id, r.attempt_id AS run_attempt_id,
                       r.spec_revision AS run_spec_revision,
                       b.run_id AS baseline_run_id
                FROM task_attempts a
                JOIN task_runs r ON r.id = ?
                LEFT JOIN run_evidence_baselines b ON b.run_id = r.id
                WHERE a.id = ?
                """,
                (run_id, attempt_id),
            ).fetchone()
            expected_binding = {
                "task_id": task.id,
                "attempt_id": attempt_id,
                "run_id": run_id,
                "spec_revision": task.spec_revision,
                "task_revision": task.revision,
            }
            if (
                binding is None
                or binding["attempt_task_id"] != task.id
                or binding["run_task_id"] != task.id
                or binding["run_attempt_id"] != attempt_id
                or binding["baseline_run_id"] != run_id
                or {
                    int(binding["attempt_spec_revision"]),
                    int(binding["run_spec_revision"]),
                }
                != {task.spec_revision}
                or any(
                    evidence_manifest.get(key) != value
                    for key, value in expected_binding.items()
                )
                or evidence_manifest.get("call_id") != run_id
            ):
                raise DomainError("EvidenceManifest call/run/spec/revision binding mismatch")
            token_delta = int(execution.get("input_tokens", 0)) + int(
                execution.get("output_tokens", 0)
            )
            actual_tokens = task.tokens_used + token_delta
            fingerprint = verification.get(
                "failure_fingerprint", verification["evidence_hash"]
            )
            same_failure_count = 0 if passed else (
                task.same_failure_count + 1 if task.last_failure_fingerprint == fingerprint else 1
            )
            # no_progress_count remains readable for legacy rows but is no longer
            # a second decision signal. The evidence fingerprint is authoritative.
            no_progress_count = 0
            can_retry = (
                not passed
                and task.attempts_used < _authoritative_max_attempts(task)
                and same_failure_count <= max_same_failure
            )
            target = (
                TaskStatus.CANDIDATE_READY
                if (
                    passed
                    and task.goal_id is not None
                    and task.work_item_kind == "implementation"
                )
                else TaskStatus.COMPLETED
            ) if passed else (
                TaskStatus.READY if can_retry else TaskStatus.TERMINAL_FAILED
            )
            next_revision = task.revision + 1
            TokenLedger.record_in_transaction(
                connection,
                call_id=run_id,
                execution=execution,
                task=task,
                provider=task.provider,
                run_id=run_id,
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, tokens_used = ?,
                    last_evidence_hash = ?, last_error = ?, same_failure_count = ?,
                    no_progress_count = ?, last_failure_fingerprint = ?,
                    next_eligible_at = CASE WHEN ? THEN datetime('now', ?) ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (
                    target.value,
                    next_revision,
                    actual_tokens,
                    evidence_hash,
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
            _record_worker_context_pressure(
                connection,
                task.worker_id,
                execution,
                reason=_context_pressure_reason(connection, execution),
            )
            run_status = "completed" if target in {
                TaskStatus.COMPLETED,
                TaskStatus.CANDIDATE_READY,
            } else (
                "failed"
            )
            connection.execute(
                "UPDATE task_attempts SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run_status, attempt_id),
            )
            result = {
                "execution": _execution_metadata(execution),
                "evidence_manifest": evidence_manifest,
            }
            connection.execute(
                """
                INSERT INTO evidence_manifests(
                    id, task_id, attempt_id, run_id, call_id, spec_revision,
                    task_revision, environment_hash, passed,
                    manifest_json, manifest_hash, verdict, reason_codes_json,
                    failed_acceptance_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), task.id, attempt_id, run_id,
                    evidence_manifest["call_id"], task.spec_revision, task.revision,
                    evidence_manifest["environment_hash"], 1 if passed else 0,
                    _dump(evidence_manifest), evidence_hash,
                    evidence_manifest["verdict"],
                    _dump(evidence_manifest["reason_codes"]),
                    _dump(evidence_manifest["failed_acceptance_ids"]),
                ),
            )
            connection.execute(
                """
                UPDATE task_runs SET status = ?, input_tokens = ?,
                    cached_input_tokens = ?, output_tokens = ?,
                    result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (
                    run_status,
                    execution.get("input_tokens", 0),
                    min(
                        int(execution.get("input_tokens", 0)),
                        int(execution.get("cached_input_tokens", 0)),
                    ),
                    execution.get("output_tokens", 0),
                    _dump(result),
                    run_id,
                ),
            )
            event_payload = {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "evidence_manifest_hash": evidence_hash,
                "passed": passed,
            }
            self._event(
                connection,
                task_id=task.id,
                event_type=(
                    "task.candidate_ready"
                    if target is TaskStatus.CANDIDATE_READY
                    else "task.completed"
                ) if target in {
                    TaskStatus.COMPLETED,
                    TaskStatus.CANDIDATE_READY,
                } else (
                    "task.retry_scheduled" if can_retry else "task.terminal_failed"
                ),
                payload=event_payload,
                revision=next_revision,
                idempotency_key=idempotency_key,
            )
            return self._get_with_connection(connection, task.id)

    def last_failure_delta(self, task_id: str) -> dict[str, Any] | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT result_json FROM task_runs
                WHERE task_id = ? AND run_type = 'execute' AND status = 'failed'
                  AND result_json IS NOT NULL
                ORDER BY finished_at DESC, rowid DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            result = json.loads(row["result_json"])
            manifest = result.get("evidence_manifest")
            verification = (
                {
                    "summary": manifest.get("summary"),
                    "evidence_hash": manifest.get("manifest_hash"),
                    "checks": [
                        item.get("check", {})
                        for item in manifest.get("verification_commands", [])
                    ],
                }
                if isinstance(manifest, dict)
                else result.get("verification", {})
            )
            return {
                "summary": str(verification.get("summary") or "verification failed")[:1000],
                "evidence_hash": str(verification.get("evidence_hash") or ""),
                "failed_checks": [
                    check for check in verification.get("checks", [])
                    if isinstance(check, dict) and not check.get("passed")
                ][:16],
            }
        finally:
            connection.close()

    def finalize_host_fault(
        self,
        task_id: str,
        *,
        job_id: str,
        attempt_id: str,
        run_id: str,
        action: str,
        failure_class: str,
        reason: str,
        execution: dict[str, Any],
        external_session_id: str | None,
        episode: dict[str, Any] | None = None,
        rotate_worker_reason: str | None = None,
    ) -> TaskRecord:
        if action not in {
            "defer",
            "resume",
            "needs_human",
            "network_suspended",
            "provider_suspended",
        }:
            raise ValueError(f"unsupported Host fault action: {action}")
        idempotency_key = f"host-job:{job_id}:fault"
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                connection.execute(
                    """
                    UPDATE host_jobs SET status = 'fault_finalized',
                        consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP WHERE job_id = ?
                    """,
                    (job_id,),
                )
                connection.execute(
                    """
                    UPDATE workers SET last_error = (
                        SELECT last_error FROM tasks WHERE id = ?
                    ), updated_at = CURRENT_TIMESTAMP
                    WHERE id = (SELECT worker_id FROM host_jobs WHERE job_id = ?)
                      AND session_generation IS (
                          SELECT session_generation FROM host_jobs WHERE job_id = ?
                      )
                    """,
                    (task_id, job_id, job_id),
                )
                return self._get_with_connection(connection, task_id)
            task = self._get_with_connection(connection, task_id)
            if task.status not in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                raise InvalidTransitionError(f"task has no active Host fault: {task.status}")
            token_total = int(execution.get("input_tokens", 0)) + int(
                execution.get("output_tokens", 0)
            )
            target = {
                "needs_human": TaskStatus.NEEDS_HUMAN,
                "network_suspended": TaskStatus.NETWORK_SUSPENDED,
                "provider_suspended": TaskStatus.PROVIDER_SUSPENDED,
            }.get(action, TaskStatus.READY)
            revision = task.revision + 1
            if token_total:
                TokenLedger.record_in_transaction(
                    connection,
                    call_id=run_id,
                    execution=execution,
                    task=task,
                    provider=task.provider,
                    run_id=run_id,
                    add_to_task=True,
                )
            backoff = f"+{2 ** min(8, max(1, task.attempts_used))} seconds"
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?,
                    worker_id = NULL,
                    last_error = ?,
                    suspended_from_status = CASE
                        WHEN ? IN ('network_suspended', 'provider_suspended')
                        THEN 'running' ELSE suspended_from_status END,
                    suspension_reason = CASE
                        WHEN ? IN ('network_suspended', 'provider_suspended')
                        THEN ? ELSE suspension_reason END,
                    next_eligible_at = CASE
                        WHEN ? = 'defer' THEN datetime('now', ?)
                        WHEN ? = 'resume' THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (
                    target.value, revision, reason[:1000],
                    action,
                    action,
                    reason[:1000],
                    action, backoff, action, task.id,
                ),
            )
            retained_session_id = external_session_id
            job = connection.execute(
                "SELECT external_session_id FROM host_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if retained_session_id is None and job is not None:
                retained_session_id = job["external_session_id"]
            if task.worker_id:
                connection.execute(
                    """
                    UPDATE workers SET external_session_id = COALESCE(?, external_session_id),
                        status = 'idle', active_task_id = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (retained_session_id, reason[:1000], task.worker_id),
                )
            connection.execute(
                """
                UPDATE task_sessions
                SET external_session_id = COALESCE(?, external_session_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (retained_session_id, task.id),
            )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
            if rotate_worker_reason and task.worker_id:
                _rotate_task_session(
                    connection,
                    task.id,
                    reason=rotate_worker_reason,
                    trigger_key=(
                        f"execution-episode:{episode['episode_id']}:task-session"
                        if episode and episode.get("episode_id")
                        else f"host-job:{job_id}:task-session"
                    ),
                )
                rotate_worker_in_transaction(
                    connection,
                    task.worker_id,
                    reason=rotate_worker_reason,
                    trigger_key=(
                        f"execution-episode:{episode['episode_id']}:replacement"
                        if episode and episode.get("episode_id")
                        else f"host-job:{job_id}:replacement"
                    ),
                )
            _record_worker_context_pressure(
                connection,
                task.worker_id,
                execution,
                reason=_context_pressure_reason(connection, execution),
            )
            run_status = {
                "defer": "deferred",
                "resume": "interrupted",
                "needs_human": "needs_human",
                "network_suspended": "network_suspended",
                "provider_suspended": "provider_suspended",
            }[action]
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = NULL
                WHERE id = ?
                """,
                ("suspended", attempt_id),
            )
            connection.execute(
                """
                UPDATE task_runs SET status = ?, input_tokens = ?,
                    cached_input_tokens = ?, output_tokens = ?,
                    result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (
                    run_status,
                    execution.get("input_tokens", 0),
                    min(
                        int(execution.get("input_tokens", 0)),
                        int(execution.get("cached_input_tokens", 0)),
                    ),
                    execution.get("output_tokens", 0),
                    _dump({
                        "fault": {
                            "action": action,
                            "failure_class": failure_class,
                            "reason": reason[:1000],
                        },
                        "execution_episode": episode,
                        "execution": _execution_metadata(execution),
                    }),
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE host_jobs SET status = 'fault_finalized', last_error = ?,
                    external_session_id = COALESCE(?, external_session_id),
                    consumed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (reason[:1000], retained_session_id, job_id),
            )
            checkpoint = {
                **dict(episode or {}),
                "task_id": task.id,
                "attempt_id": attempt_id,
                "spec_revision": task.spec_revision,
                "session_generation": (
                    connection.execute(
                        """
                        SELECT session_generation FROM task_sessions
                        WHERE task_id = ?
                        """,
                        (task.id,),
                    ).fetchone() or {"session_generation": 1}
                )["session_generation"],
                "failure_class": failure_class,
                "reason": reason,
                "completed_steps": list(
                    (episode or {}).get("completed_steps") or []
                ),
                "verified_artifacts": list(
                    (episode or {}).get("verified_artifacts") or []
                ),
                "invalidated_steps": list(
                    (episode or {}).get("invalidated_steps") or []
                ),
                "next_action": (
                    (episode or {}).get("next_action")
                    or "reconcile workspace and run the smallest deterministic probe"
                ),
            }
            checkpoint_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO task_recovery_checkpoints(
                    id, task_id, attempt_id, session_generation,
                    spec_revision, checkpoint_json, checkpoint_hash, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) WHERE status = 'pending' DO UPDATE SET
                    attempt_id = excluded.attempt_id,
                    session_generation = excluded.session_generation,
                    spec_revision = excluded.spec_revision,
                    checkpoint_json = excluded.checkpoint_json,
                    checkpoint_hash = excluded.checkpoint_hash,
                    reason = excluded.reason,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    checkpoint_id,
                    task.id,
                    attempt_id,
                    int(checkpoint["session_generation"]),
                    task.spec_revision,
                    _dump(checkpoint),
                    checkpoint_hash(checkpoint),
                    reason[:1000],
                ),
            )
            self._event(
                connection,
                task_id=task.id,
                event_type=(
                    "task.needs_human"
                    if target is TaskStatus.NEEDS_HUMAN
                    else f"task.{target.value}"
                    if target in {
                        TaskStatus.NETWORK_SUSPENDED,
                        TaskStatus.PROVIDER_SUSPENDED,
                    }
                    else "task.execution_resume_scheduled"
                ),
                payload={
                    "host_job_id": job_id,
                    "action": action,
                    "failure_class": failure_class,
                    "reason": reason,
                    "tokens": token_total,
                    "session_retained": bool(retained_session_id),
                    "execution_episode": episode,
                    "checkpoint_id": checkpoint_id,
                    "attempt_incremented": False,
                },
                revision=revision, idempotency_key=idempotency_key,
            )
            if target in {
                TaskStatus.NEEDS_HUMAN,
                TaskStatus.NETWORK_SUSPENDED,
                TaskStatus.PROVIDER_SUSPENDED,
            }:
                connection.execute(
                    """
                    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
                    VALUES ('task', ?, 'task.needs_human', ?)
                    """,
                    (task.id, _dump({
                        "task_id": task.id,
                        "reason": reason,
                        "failure_class": failure_class,
                    })),
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
            if action in {"retry", "restart"}:
                if task.status not in {
                    TaskStatus.TERMINAL_FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.NEEDS_HUMAN,
                    TaskStatus.PAUSED,
                }:
                    raise InvalidTransitionError(
                        f"cannot {action} task in state {task.status}"
                    )
                return self._restart_with_new_spec(
                    connection,
                    task,
                    action=action,
                    reason=reason,
                    idempotency_key=idempotency_key,
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
            if action == "resume" and not _dependencies_satisfied(connection, task):
                target = TaskStatus.PAUSED
            blocked_reason = (
                _dependency_blocked_reason(task)
                if target is TaskStatus.PAUSED and action == "resume"
                else None
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, next_eligible_at = NULL,
                    manual_override = ?, blocked_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    target.value,
                    revision,
                    1 if action == "pause" else 0,
                    blocked_reason,
                    task.id,
                ),
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

    def amend_spec(
        self,
        task_id: str,
        *,
        spec: dict[str, Any],
        reason: str,
        expected_revision: int,
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
            if task.status in {
                TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.STOPPING,
            }:
                raise InvalidTransitionError("active task cannot be amended")
            canonical = canonical_task_spec(
                objective=str(spec.get("objective") or ""),
                scope=list(spec.get("scope") or []),
                acceptance=list(spec.get("acceptance") or []),
                verification=list(spec.get("verification") or []),
                artifacts=list(spec.get("artifacts") or []),
                constraints=list(spec.get("constraints") or []),
                deadline=dict(spec.get("deadline") or {}),
            )
            return self._replace_spec(
                connection,
                task,
                spec=canonical,
                action="spec_amended",
                reason=reason,
                idempotency_key=idempotency_key,
            )

    def _restart_with_new_spec(
        self,
        connection: Any,
        task: TaskRecord,
        *,
        action: str,
        reason: str,
        idempotency_key: str,
    ) -> TaskRecord:
        return self._replace_spec(
            connection,
            task,
            spec=task.spec,
            action=action,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def _replace_spec(
        self,
        connection: Any,
        task: TaskRecord,
        *,
        spec: dict[str, Any],
        action: str,
        reason: str,
        idempotency_key: str,
    ) -> TaskRecord:
        active_dependent = connection.execute(
            """
            SELECT id FROM tasks
            WHERE status IN ('running', 'verifying', 'stopping')
              AND EXISTS (
                  SELECT 1 FROM json_each(COALESCE(depends_on_json, '[]'))
                  WHERE json_each.value = ?
              )
            LIMIT 1
            """,
            (task.id,),
        ).fetchone()
        if active_dependent:
            raise InvalidTransitionError("active dependent must stop before amendment")
        spec_revision = task.spec_revision + 1
        insert_task_spec(connection, task.id, spec, revision=spec_revision)
        insert_task_provider_policy(
            connection,
            task_id=task.id,
            spec_revision=spec_revision,
            provider_policy=task.provider_policy,
            fallback_enabled=task.fallback_enabled,
            provider_order=task.provider_order,
            initial_provider=task.provider,
        )
        connection.execute(
            """
            UPDATE task_recovery_checkpoints
            SET status = 'invalidated', consumed_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND status = 'pending'
            """,
            (task.id,),
        )
        ready = _dependencies_satisfied(connection, task)
        revision = task.revision + 1
        connection.execute(
            """
            UPDATE tasks SET objective = ?, verification_json = ?,
                current_spec_revision = ?, status = ?, revision = ?,
                attempts_used = 0, same_failure_count = 0, no_progress_count = 0,
                last_failure_fingerprint = NULL, last_evidence_hash = NULL,
                last_error = NULL, next_eligible_at = NULL, manual_override = 0,
                blocked_reason = ?, handoff_json = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                spec["objective"],
                _dump(spec["verification"]),
                spec_revision,
                TaskStatus.READY.value if ready else TaskStatus.PAUSED.value,
                revision,
                None if ready else _dependency_blocked_reason(task),
                task.id,
            ),
        )
        connection.execute(
            "INSERT INTO task_controls(task_id, action, reason) VALUES (?, ?, ?)",
            (task.id, action, reason),
        )
        self._event(
            connection,
            task_id=task.id,
            event_type=f"task.{action}",
            payload={"reason": reason, "spec_revision": spec_revision},
            revision=revision,
            idempotency_key=idempotency_key,
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

    def _acquire_worker_and_lock(self, connection: Any, task: TaskRecord) -> tuple[str | None, str | None, int | None]:
        if task.project_id is None or task.role_id is None:
            return None, None, None
        connection.execute("DELETE FROM resource_locks WHERE expires_at <= CURRENT_TIMESTAMP")
        connection.execute("DELETE FROM task_leases WHERE expires_at <= CURRENT_TIMESTAMP")
        role = connection.execute(
            "SELECT kind, status FROM roles WHERE id = ?", (task.role_id,)
        ).fetchone()
        role_id = task.role_id
        if role is None or role["status"] == "released" or (
            role["status"] == "draining" and task.worker_id is None
        ):
            role_id = str(uuid.uuid4())
            role_kind = (
                str(role["kind"]).split(":replacement:", 1)[0]
                if role is not None
                else str(task.work_item_kind or "worker")
            )
            connection.execute(
                """
                INSERT INTO roles(id, project_id, kind, status)
                VALUES (?, ?, ?, 'ephemeral')
                """,
                (
                    role_id,
                    task.project_id,
                    f"{role_kind}:replacement:{role_id}",
                ),
            )
            connection.execute(
                "UPDATE tasks SET role_id = ?, worker_id = NULL WHERE id = ?",
                (role_id, task.id),
            )
        worker = connection.execute(
            """
            SELECT w.*, r.status role_status FROM workers w
            JOIN roles r ON r.id = w.role_id
            WHERE w.project_id = ? AND w.role_id = ? AND w.released_at IS NULL
            """,
            (task.project_id, role_id),
        ).fetchone()
        if worker is None:
            worker_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workers(id, project_id, role_id, provider, session_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (worker_id, task.project_id, role_id, task.provider, str(uuid.uuid4())),
            )
        else:
            worker_id = worker["id"]
            if worker["role_status"] == "released" or (
                worker["role_status"] == "draining" and task.worker_id != worker_id
            ):
                raise ProviderUnavailableError("legacy role worker is retired")
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
        lease_seconds = task_lease_seconds(task)
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
            lifecycle = connection.execute(
                """
                SELECT t.status task_status, r.status role_status, w.project_id,
                       w.role_id, w.session_id, w.session_generation
                FROM tasks t
                JOIN workers w ON w.id = ?
                JOIN roles r ON r.id = w.role_id
                WHERE t.id = ?
                """,
                (worker_id, task_id),
            ).fetchone()
            if lifecycle and lifecycle["role_status"] in {"ephemeral", "draining"} and lifecycle[
                "task_status"
            ] in {"candidate_ready", "completed", "terminal_failed", "cancelled"}:
                connection.execute(
                    """
                    INSERT INTO worker_session_archives(
                        worker_id, project_id, role_id, session_id,
                        session_generation, reason
                    ) VALUES (?, ?, ?, ?, ?, 'task_terminal')
                    """,
                    (
                        worker_id, lifecycle["project_id"], lifecycle["role_id"],
                        lifecycle["session_id"], lifecycle["session_generation"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE workers SET status = 'released', active_task_id = NULL,
                        active_fencing_token = NULL, released_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND active_task_id = ?
                    """,
                    (worker_id, task_id),
                )
                connection.execute(
                    "UPDATE roles SET status = 'released' WHERE id = ?",
                    (lifecycle["role_id"],),
                )
                connection.execute(
                    """
                    UPDATE session_bindings
                    SET status = 'archived', updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = ? AND status = 'bound'
                    """,
                    (task_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE workers SET status = 'idle', active_task_id = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND active_task_id = ?
                    """,
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
        row = connection.execute(
            f"{_TASK_WITH_SPEC} WHERE t.id = ?", (task_id,)
        ).fetchone()
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
    spec_json = _optional(row, "task_spec_json")
    spec_hash = _optional(row, "task_spec_hash")
    if not spec_json or hashlib.sha256(spec_json.encode("utf-8")).hexdigest() != spec_hash:
        raise DomainError("immutable task spec is missing or corrupt")
    spec = json.loads(spec_json)
    if set(spec) != TASK_SPEC_FIELDS:
        raise DomainError("immutable task spec has an invalid shape")
    execution_policy = (
        json.loads(row["execution_budget_json"])
        if row["execution_budget_json"] else None
    )
    if execution_policy is not None:
        execution_policy = {
            key: value
            for key, value in execution_policy.items()
            if key not in {
                "reserved_tokens",
                "total_token_hard_cap",
                "estimated_total_token_hard_cap",
            }
        }
    max_attempts = int(row["max_attempts"])
    if execution_policy is not None and execution_policy.get("max_attempts") is not None:
        max_attempts = int(execution_policy["max_attempts"])
    return TaskRecord(
        id=row["id"],
        title=row["title"],
        objective=str(spec["objective"]),
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
        verification=list(spec["verification"]),
        max_attempts=max_attempts,
        attempts_used=row["attempts_used"],
        tokens_used=row["tokens_used"],
        last_evidence_hash=row["last_evidence_hash"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=_optional(row, "completed_at"),
        sizing=(
            json.loads(row["sizing_json"]) if row["sizing_json"]
            else {"status": "legacy_fallback"}
        ),
        execution_policy=execution_policy,
        provider_policy=_optional(row, "provider_policy") or "auto",
        fallback_enabled=bool(
            1 if _optional(row, "fallback_enabled") is None
            else _optional(row, "fallback_enabled")
        ),
        provider_order=json.loads(
            _optional(row, "provider_order_json")
            or '["codex","cursor","deepseek","kimi"]'
        ),
        suspended_from_status=_optional(row, "suspended_from_status"),
        suspension_reason=_optional(row, "suspension_reason"),
        suspension_incident_id=_optional(row, "suspension_incident_id"),
        goal_id=_optional(row, "goal_id"),
        parent_task_id=_optional(row, "parent_task_id"),
        depends_on=json.loads(_optional(row, "depends_on_json") or "[]"),
        work_item_kind=_optional(row, "work_item_kind"),
        ordinal=_optional(row, "ordinal"),
        blocked_reason=_optional(row, "blocked_reason"),
        handoff=_parse_json_object(_optional(row, "handoff_json")),
        spec_revision=int(row["current_spec_revision"]),
        spec=spec,
        evidence_manifest=_parse_json_object(_optional(row, "evidence_manifest_json")),
        execution_episode=_parse_json_object(
            _optional(row, "execution_episode_json")
        ),
    )


def _authoritative_max_attempts(task: TaskRecord) -> int:
    if task.execution_policy and task.execution_policy.get("max_attempts") is not None:
        return int(task.execution_policy["max_attempts"])
    return int(task.max_attempts)


def _optional(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    return json.loads(raw)


def _dependencies_satisfied(connection: Any, task: TaskRecord) -> bool:
    depends = list(task.depends_on or [])
    if not depends:
        return True
    placeholders = ",".join("?" for _ in depends)
    rows = connection.execute(
        f"""
        SELECT t.id, t.status, t.current_spec_revision, t.last_evidence_hash,
               em.manifest_hash
        FROM tasks t
        LEFT JOIN evidence_manifests em
          ON em.task_id = t.id
         AND em.spec_revision = t.current_spec_revision
         AND em.manifest_hash = t.last_evidence_hash
         AND em.passed = 1
        WHERE t.id IN ({placeholders})
        """,
        tuple(depends),
    ).fetchall()
    return len(rows) == len(depends) and all(
        row["status"] == TaskStatus.COMPLETED.value
        and row["manifest_hash"] is not None
        for row in rows
    )


def _dependency_blocked_reason(task: TaskRecord) -> str | None:
    depends = list(task.depends_on or [])
    return f"waiting_on:{','.join(depends)}" if depends else None


TASK_SPEC_FIELDS = {
    "objective", "scope", "acceptance", "verification", "artifacts",
    "constraints", "deadline",
}
_TASK_WITH_SPEC = """
SELECT t.*, s.spec_json AS task_spec_json, s.spec_hash AS task_spec_hash,
       (
           SELECT em.manifest_json FROM evidence_manifests em
           WHERE em.task_id = t.id
           ORDER BY em.created_at DESC, em.rowid DESC LIMIT 1
       ) AS evidence_manifest_json,
       (
           SELECT json_object(
               'id', e.id,
               'spec_revision', e.spec_revision,
               'ordinal', e.ordinal,
               'recovery_count', e.recovery_count,
               'recovery_stage', e.recovery_stage,
               'status', e.status,
               'deadline_at', e.deadline_at,
               'wall_deadline_at', e.wall_deadline_at,
               'host_process_count', e.host_process_count,
               'max_host_processes', e.max_host_processes,
               'same_fault_count', e.same_fault_count,
               'zero_progress_rounds', e.zero_progress_rounds,
               'progress_bytes', e.progress_bytes,
               'observed_tokens', e.observed_tokens,
               'burn_rate_tokens_per_minute', e.burn_rate_tokens_per_minute,
               'burn_rate_alert', json(e.burn_rate_alert),
               'checkpoint', CASE
                   WHEN e.checkpoint_json IS NULL THEN NULL
                   ELSE json(e.checkpoint_json)
               END,
               'end_reason', e.end_reason
           )
           FROM execution_episodes e
           WHERE e.task_id = t.id
           ORDER BY e.ordinal DESC LIMIT 1
       ) AS execution_episode_json
FROM tasks t
JOIN task_specs s ON s.task_id = t.id
    AND s.spec_revision = t.current_spec_revision
"""


def canonical_task_spec(
    *,
    objective: str,
    verification: list[dict[str, Any]],
    deadline: dict[str, Any],
    scope: list[str] | None = None,
    acceptance: list[str] | None = None,
    artifacts: list[str] | None = None,
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    try:
        hard_seconds = int(deadline["hard_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("TaskSpec deadline requires hard_seconds") from error
    if not 1 <= hard_seconds <= MAX_HARD_DEADLINE_SECONDS:
        raise DomainError("TaskSpec deadline is outside the supported range")
    declared_artifacts = artifacts or []
    return {
        "objective": objective,
        "scope": list(scope or []),
        "acceptance": list(acceptance or []),
        "verification": verification,
        "artifacts": list(dict.fromkeys(declared_artifacts)),
        "constraints": list(constraints or []),
        "deadline": {"hard_seconds": hard_seconds},
    }


def insert_task_spec(
    connection: Any,
    task_id: str,
    spec: dict[str, Any],
    *,
    revision: int,
) -> str:
    spec_json = _dump(spec)
    digest = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO task_specs(task_id, spec_revision, spec_json, spec_hash)
        VALUES (?, ?, ?, ?)
        """,
        (task_id, revision, spec_json, digest),
    )
    return digest


def insert_task_provider_policy(
    connection: Any,
    *,
    task_id: str,
    spec_revision: int,
    provider_policy: str,
    fallback_enabled: bool,
    provider_order: list[str],
    initial_provider: str,
) -> None:
    connection.execute(
        """
        INSERT INTO task_provider_policies(
            task_id, spec_revision, provider_policy, fallback_enabled,
            provider_order_json, initial_provider
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            spec_revision,
            provider_policy,
            1 if fallback_enabled else 0,
            _dump(provider_order),
            initial_provider,
        ),
    )


def _normalize_provider_routing(
    *,
    provider: str,
    provider_policy: str,
    fallback_enabled: bool,
    provider_order: list[str] | None,
) -> list[str]:
    if provider_policy not in {"auto", "preferred", "pinned"}:
        raise DomainError("invalid provider policy")
    order = list(
        dict.fromkeys(
            provider_order
            or [provider, "codex", "cursor", "deepseek", "kimi"]
        )
    )
    if provider not in order:
        order.insert(0, provider)
    if provider_policy in {"preferred", "pinned"} and order[0] != provider:
        raise DomainError("preferred or pinned provider must be first in provider order")
    if provider_policy == "pinned" and fallback_enabled:
        fallback_enabled = False
    if not order or any(not item for item in order):
        raise DomainError("provider order must contain non-empty provider names")
    return order


def _assert_task_spec(task: TaskRecord) -> None:
    if set(task.spec) != TASK_SPEC_FIELDS:
        raise DomainError("claim requires one complete immutable task spec")


def _validate_evidence_manifest(
    task: TaskRecord,
    evidence_manifest: dict[str, Any],
    execution: dict[str, Any],
) -> None:
    commands = evidence_manifest.get("verification_commands")
    artifacts = evidence_manifest.get("artifacts")
    report = evidence_manifest.get("test_report")
    if not isinstance(commands, list) or not isinstance(artifacts, list):
        raise DomainError("EvidenceManifest verification/artifact records are required")
    if [item.get("spec") for item in commands] != task.verification:
        raise DomainError("EvidenceManifest verification contract mismatch")
    required_gate_fields = {
        "acceptance_id", "argv", "cwd", "started_at", "finished_at",
        "exit_code", "host_job_id", "run_id", "session", "summary",
        "artifact_evidence", "check",
    }
    for item in commands:
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        command_gate = spec.get("kind") == "command"
        exact_argv = list(
            spec.get("argv") if command_gate else task.command.get("argv") or []
        )
        exact_cwd = (
            str((Path(task.project_path) / str(spec.get("cwd") or "")).resolve())
            if command_gate
            else task.project_path
        )
        exact_exit_code = (
            int(item.get("check", {}).get("actual", -1))
            if command_gate
            else int(execution.get("returncode", -1))
        )
        if not required_gate_fields.issubset(item):
            raise DomainError("EvidenceManifest command evidence is incomplete")
        if (
            not item.get("acceptance_id")
            or not exact_argv
            or item.get("argv") != exact_argv
            or str(item.get("cwd") or "") != exact_cwd
            or not item.get("started_at")
            or not item.get("finished_at")
            or not isinstance(item.get("exit_code"), int)
            or item.get("exit_code") != exact_exit_code
            or item.get("run_id") != evidence_manifest.get("run_id")
            or not isinstance(item.get("session"), dict)
            or not {"external_session_id", "session_generation", "fencing_token"}.issubset(
                item["session"]
            )
            or not isinstance(item.get("artifact_evidence"), list)
        ):
            raise DomainError("EvidenceManifest exact command evidence mismatch")
        for artifact in item["artifact_evidence"]:
            if (
                not artifact.get("path")
                or (
                    not artifact.get("sha256")
                    and evidence_manifest.get("verdict") == "PASS"
                )
            ):
                raise DomainError("EvidenceManifest command artifact hash is missing")

    checks_passed = bool(commands) and all(
        isinstance(item.get("check"), dict)
        and item["check"].get("passed") is True
        for item in commands
    )
    artifact_paths = [item.get("relative_path") for item in artifacts]
    if evidence_manifest.get("verdict") == "PASS" and any(
        not item.get("relative_path")
        or not item.get("after", {}).get("sha256")
        for item in artifacts
    ):
        raise DomainError("EvidenceManifest artifact path/hash is missing")
    artifact_passed = artifact_paths == task.spec["artifacts"] and all(
        item.get("provenance") in {
            "current_run", "same_task_session_generation",
        }
        and item.get("after", {}).get("sha256")
        and (
            item.get("provenance") != "same_task_session_generation"
            or (
                isinstance(item.get("inherited_from"), dict)
                and item["inherited_from"].get("task_id") == task.id
                and item["inherited_from"].get("session_generation")
                == evidence_manifest.get("environment", {}).get("session_generation")
                and item["inherited_from"].get("sha256")
                == item.get("after", {}).get("sha256")
            )
        )
        for item in artifacts
    )
    verdict = evidence_manifest.get("verdict")
    reason_codes = evidence_manifest.get("reason_codes")
    failed_acceptance_ids = evidence_manifest.get("failed_acceptance_ids")
    required_acceptance_ids = evidence_manifest.get("required_acceptance_ids")
    if (
        verdict not in {"PASS", "CHANGES_REQUIRED"}
        or not isinstance(reason_codes, list)
        or not isinstance(failed_acceptance_ids, list)
        or not isinstance(required_acceptance_ids, list)
        or any(not isinstance(item, str) or not item for item in required_acceptance_ids)
        or any(not isinstance(item, str) or not item for item in failed_acceptance_ids)
    ):
        raise DomainError("EvidenceManifest canonical verdict fields are invalid")
    gate_acceptance_ids = {item["acceptance_id"] for item in commands}
    if not set(required_acceptance_ids).issubset(gate_acceptance_ids):
        if verdict == "PASS":
            raise DomainError("EvidenceManifest required acceptance id is missing")
    browser_required = any(
        marker in str(value).lower()
        for value in [
            *(task.spec.get("acceptance") or []),
            *(task.spec.get("constraints") or []),
        ]
        for marker in ("browser", "浏览器", "e2e")
    )
    if browser_required and not (
        isinstance(evidence_manifest.get("browser_evidence"), list)
        and evidence_manifest["browser_evidence"]
        and all(item.get("passed") is True for item in evidence_manifest["browser_evidence"])
    ):
        if verdict == "PASS":
            raise DomainError("EvidenceManifest required browser evidence is missing")
    verifier_passed = bool(report and report.get("passed"))
    expected_passed = (
        checks_passed
        and verifier_passed
        and artifact_passed
        and verdict == "PASS"
        and not reason_codes
        and not failed_acceptance_ids
    )
    if (
        not isinstance(report, dict)
        or bool(evidence_manifest.get("artifact_contract_passed")) != artifact_passed
        or bool(evidence_manifest.get("passed")) != expected_passed
        or (bool(evidence_manifest.get("passed")) != (verdict == "PASS"))
    ):
        raise DomainError("EvidenceManifest completion verdict is inconsistent")


def _execution_metadata(execution: dict[str, Any]) -> dict[str, Any]:
    """Persist only metadata in SQLite; full stdout/stderr/prompt stay in files."""
    blocked = {"stdout", "stderr", "prompt", "prompt_text"}
    meta = {key: value for key, value in execution.items() if key not in blocked}
    if "output_bytes" not in meta:
        meta["output_bytes"] = {
            "stdout": len(str(execution.get("stdout") or "").encode("utf-8")),
            "stderr": len(str(execution.get("stderr") or "").encode("utf-8")),
        }
        meta["output_bytes"]["total"] = (
            int(meta["output_bytes"]["stdout"]) + int(meta["output_bytes"]["stderr"])
        )
    return meta


def _context_pressure_reason(_connection: Any, _execution: dict[str, Any]) -> str:
    return "turn_usage_observed"


def _record_worker_context_pressure(
    connection: Any,
    worker_id: str | None,
    execution: dict[str, Any],
    *,
    reason: str,
) -> None:
    if not worker_id:
        return
    input_tokens = max(0, int(execution.get("input_tokens", 0)))
    cached_input_tokens = min(
        input_tokens, max(0, int(execution.get("cached_input_tokens", 0)))
    )
    output_tokens = max(0, int(execution.get("output_tokens", 0)))
    connection.execute(
        """
        UPDATE workers SET last_input_tokens = ?,
            last_cached_input_tokens = ?, last_output_tokens = ?,
            last_uncached_input_tokens = ?,
            last_context_pressure_tokens = ?,
            last_context_pressure_reason = ?,
            last_context_session_generation = session_generation,
            last_attribution_granularity = ?,
            last_value_classification = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            input_tokens, cached_input_tokens, output_tokens,
            input_tokens - cached_input_tokens, input_tokens, reason,
            str(execution.get("attribution_granularity") or "turn"),
            str(execution.get("value_classification") or "unknown"),
            worker_id,
        ),
    )


def _rotate_task_session(
    connection: Any,
    task_id: str,
    *,
    reason: str,
    trigger_key: str,
) -> None:
    current = connection.execute(
        "SELECT * FROM task_sessions WHERE task_id = ?", (task_id,)
    ).fetchone()
    if current is None or connection.execute(
        "SELECT 1 FROM task_session_archives WHERE trigger_key = ?",
        (trigger_key,),
    ).fetchone():
        return
    connection.execute(
        """
        INSERT INTO task_session_archives(
            task_id, project_id, role_id, worker_id, provider,
            external_session_id, session_generation, reason, trigger_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            current["project_id"],
            current["role_id"],
            current["worker_id"],
            current["provider"],
            current["external_session_id"],
            current["session_generation"],
            reason,
            trigger_key,
        ),
    )
    connection.execute(
        """
        UPDATE task_sessions
        SET external_session_id = NULL,
            session_generation = session_generation + 1,
            replacement_reason = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE task_id = ?
        """,
        (reason, task_id),
    )
    next_generation = int(current["session_generation"]) + 1
    if current["session_binding_id"]:
        from plow_whip_web.store.role_instance_repository import _binding_hash

        binding = connection.execute(
            "SELECT * FROM session_bindings WHERE id = ?",
            (current["session_binding_id"],),
        ).fetchone()
        if binding and binding["status"] == "bound":
            connection.execute(
                """
                UPDATE session_bindings
                SET session_generation = ?, external_session_id = NULL,
                    fencing_token = 0, binding_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND session_generation = ? AND status = 'bound'
                """,
                (
                    next_generation,
                    _binding_hash(
                        project_id=binding["project_id"],
                        role_instance_id=binding["role_instance_id"],
                        task_id=binding["task_id"],
                        provider=binding["provider"],
                        session_generation=next_generation,
                    ),
                    binding["id"],
                    binding["session_generation"],
                ),
            )
    if current["worker_id"]:
        connection.execute(
            """
            UPDATE workers
            SET external_session_id = NULL, session_generation = ?,
                active_fencing_token = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND active_task_id = ?
            """,
            (next_generation, current["worker_id"], task_id),
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
