from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from plow_whip_web.domain.model import (
    DomainError,
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
from plow_whip_web.store.settings_repository import DEFAULT_SETTINGS
from plow_whip_web.runtime.budget import BudgetManager
from plow_whip_web.runtime.aggregate_reducer import AggregateReducer

# XL bootstrap tier hard deadline; single safety cap for Host dispatch and leases.
MAX_HARD_DEADLINE_SECONDS = 4800
EXECUTION_DEADLINE_GRACE_SECONDS = 60
LEGACY_DEFAULT_TIMEOUT_SECONDS = 600


def task_sizing_status(task: TaskRecord) -> str:
    return str(task.sizing.get("status") or "legacy_fallback")


def task_hard_deadline_seconds(task: TaskRecord) -> int:
    if task_sizing_status(task) == "estimated":
        if task.execution_budget is None:
            raise DomainError("estimated task is missing execution_budget")
        deadline = int(task.execution_budget["hard_deadline_seconds"])
    else:
        deadline = int(task.command.get("timeout_seconds", LEGACY_DEFAULT_TIMEOUT_SECONDS))
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
        token_budget: int,
        idempotency_key: str,
        project_id: str | None = None,
        role_id: str | None = None,
        resource_key: str | None = None,
        network_requirement: str = "none",
        provider: str = "generic-command",
        quality_profile: str = "deterministic",
        sizing: dict[str, Any] | None = None,
        execution_budget: dict[str, Any] | None = None,
        manual_override: bool = False,
        override_reason: str | None = None,
        budget_overrun_evidence: dict[str, Any] | None = None,
    ) -> TaskRecord:
        if manual_override and not (override_reason or "").strip():
            raise DomainError("manual_override requires a non-empty override_reason")
        if not manual_override and override_reason is not None:
            raise DomainError("override_reason is only allowed with manual_override")
        if sizing is None:
            if execution_budget is not None:
                raise DomainError("execution_budget requires an explicit sizing record")
            sizing = {"status": "legacy_fallback"}
        # Single source of truth: execution_budget.max_attempts overrides column input.
        if execution_budget is not None and execution_budget.get("max_attempts") is not None:
            max_attempts = int(execution_budget["max_attempts"])
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
                    sizing_json, execution_budget_json, manual_override, override_reason,
                    budget_overrun_evidence_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _dump(sizing),
                    _dump(execution_budget) if execution_budget is not None else None,
                    1 if manual_override else 0,
                    override_reason,
                    _dump(budget_overrun_evidence) if budget_overrun_evidence is not None else None,
                ),
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
                    "total_token_hard_cap": (
                        execution_budget.get("total_token_hard_cap")
                        if execution_budget else None
                    ),
                    "hard_deadline_seconds": (
                        execution_budget.get("hard_deadline_seconds")
                        if execution_budget else None
                    ),
                    "manual_override": manual_override,
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
                AND COALESCE(work_item_kind, '') != 'coordination'
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
                SELECT ps.external_session_id, w.provider, p.path project_path,
                       COALESCE(p.host_path, p.path) host_path,
                       ps.task_id, ps.session_generation
                FROM workers w JOIN projects p ON p.id = w.project_id
                LEFT JOIN provider_sessions ps
                  ON ps.worker_id = w.id
                 AND ps.task_id = w.active_task_id
                 AND ps.state IN ('bound', 'terminating')
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
                UPDATE workers SET external_session_id = NULL,
                    last_seen_at = CURRENT_TIMESTAMP, last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error, worker_id),
            )
            connection.execute(
                """
                UPDATE provider_sessions
                SET external_session_id = COALESCE(?, external_session_id),
                    revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                WHERE worker_id = ?
                  AND task_id = (
                      SELECT active_task_id FROM workers WHERE id = ?
                  )
                  AND state IN ('bound', 'terminating')
                """,
                (external_session_id, worker_id, worker_id),
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

    def claim(
        self, task_id: str, *, expected_revision: int, idempotency_key: str,
        reserved_tokens: int = 0,
    ) -> ClaimResult:
        # Kept in the signature for old clients only. Token estimates do not
        # participate in claim admission or execution control.
        del reserved_tokens
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
            if task.attempts_used >= _authoritative_max_attempts(task):
                raise InvalidTransitionError("task attempt budget exhausted")
            limits = dict(DEFAULT_SETTINGS)
            settings = connection.execute(
                "SELECT settings_json FROM system_settings WHERE id = 1"
            ).fetchone()
            if settings:
                limits.update(json.loads(settings["settings_json"]))
            if self._in_flight_count(connection) >= int(limits["max_parallel_workers"]):
                raise ResourceBusyError("global parallel worker limit reached")
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
                    task.attempts_used + 1,
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
            provider = connection.execute(
                "SELECT model_invoked FROM provider_configs WHERE name = ?",
                (task.provider,),
            ).fetchone()
            if provider is not None and bool(provider["model_invoked"]):
                BudgetManager.prepare_in_transaction(
                    connection, call_id=run_id, call_kind="task_execution",
                    idempotency_key=f"task-run:{run_id}", provider=task.provider,
                    task=task, worker_id=worker_id, run_id=run_id,
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
        max_same_failure: int = 2,
        budget_overrun_evidence: dict[str, Any] | None = None,
        host_job_id: str | None = None,
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
            # Token usage is observable accounting only. It cannot change the
            # verification result, retry decision, or terminal state.
            del budget_overrun_evidence
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
                TaskStatus.COMPLETED
                if passed
                else TaskStatus.READY
                if can_retry
                else TaskStatus.TERMINAL_FAILED
            )
            next_revision = task.revision + 1
            prepared_call = connection.execute(
                "SELECT 1 FROM model_calls WHERE call_id = ?", (run_id,)
            ).fetchone()
            normalized = 0
            if prepared_call is not None:
                BudgetManager.settle_in_transaction(
                    connection,
                    call_id=run_id,
                    execution=execution,
                    task=task,
                    provider=task.provider,
                    attempt_id=attempt_id,
                    host_job_id=host_job_id,
                )
                normalized = int(connection.execute(
                    """
                    SELECT normalized_input_tokens + normalized_output_tokens
                    FROM model_calls WHERE call_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0])
            actual_tokens = task.tokens_used + normalized
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, tokens_used = ?,
                    last_evidence_hash = ?, last_error = ?, same_failure_count = ?,
                    no_progress_count = ?, last_failure_fingerprint = ?,
                    budget_overrun_evidence_json = NULL,
                    next_eligible_at = CASE WHEN ? THEN datetime('now', ?) ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (
                    target.value,
                    next_revision,
                    actual_tokens,
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
            _record_worker_context_pressure(
                connection,
                task.worker_id,
                execution,
                reason=_context_pressure_reason(connection, execution),
            )
            run_status = "completed" if target is TaskStatus.COMPLETED else (
                "retry" if target is TaskStatus.READY else "failed"
            )
            connection.execute(
                "UPDATE task_attempts SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run_status, attempt_id),
            )
            result = {
                "execution": _execution_metadata(execution),
                "verification": verification,
            }
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
                "verification": verification,
            }
            self._event(
                connection,
                task_id=task.id,
                event_type="task.completed" if target is TaskStatus.COMPLETED else (
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
            verification = json.loads(row["result_json"]).get("verification", {})
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
    ) -> TaskRecord:
        if action not in {"defer", "resume", "needs_human"}:
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
                    """,
                    (task_id, job_id),
                )
                return self._get_with_connection(connection, task_id)
            task = self._get_with_connection(connection, task_id)
            if task.status not in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                raise InvalidTransitionError(f"task has no active Host fault: {task.status}")
            token_total = int(execution.get("input_tokens", 0)) + int(
                execution.get("output_tokens", 0)
            )
            target = (
                TaskStatus.NEEDS_HUMAN
                if action == "needs_human"
                else TaskStatus.READY
            )
            revision = task.revision + 1
            BudgetManager.settle_in_transaction(
                connection,
                call_id=run_id,
                execution=execution,
                task=task,
                provider=task.provider,
                add_to_task=True,
                attempt_id=attempt_id,
                host_job_id=job_id,
            )
            backoff = f"+{2 ** min(8, max(1, task.attempts_used))} seconds"
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?,
                    attempts_used = MAX(0, attempts_used - 1),
                    worker_id = NULL,
                    last_error = ?,
                    next_eligible_at = CASE
                        WHEN ? = 'defer' THEN datetime('now', ?)
                        WHEN ? = 'resume' THEN CURRENT_TIMESTAMP
                        ELSE NULL
                    END,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (
                    target.value, revision, reason[:1000],
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
                    UPDATE workers SET external_session_id = NULL,
                        status = 'idle', active_task_id = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (reason[:1000], task.worker_id),
                )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
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
            }[action]
            connection.execute(
                "UPDATE task_attempts SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (run_status, attempt_id),
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
            self._event(
                connection,
                task_id=task.id,
                event_type=(
                    "task.terminal_failed"
                    if target is TaskStatus.TERMINAL_FAILED else (
                        "task.needs_human"
                        if target is TaskStatus.NEEDS_HUMAN else "task.retry_scheduled"
                    )
                ),
                payload={
                    "host_job_id": job_id,
                    "action": action,
                    "failure_class": failure_class,
                    "reason": reason,
                    "tokens": token_total,
                    "session_retained": bool(retained_session_id),
                },
                revision=revision, idempotency_key=idempotency_key,
            )
            if target in {TaskStatus.NEEDS_HUMAN, TaskStatus.TERMINAL_FAILED}:
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
        physical = connection.execute(
            """
            SELECT * FROM provider_sessions
            WHERE project_id = ? AND role_id = ? AND task_id = ?
              AND state IN ('bound', 'idle', 'terminating')
            ORDER BY session_generation DESC LIMIT 1
            """,
            (task.project_id, task.role_id, task.id),
        ).fetchone()
        if physical is None:
            generation = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(session_generation), 0) + 1
                    FROM provider_sessions
                    WHERE project_id = ? AND role_id = ? AND task_id = ?
                    """,
                    (task.project_id, task.role_id, task.id),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO provider_sessions(
                    id, project_id, role_id, task_id, worker_id, provider,
                    session_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    task.project_id,
                    task.role_id,
                    task.id,
                    worker_id,
                    task.provider,
                    generation,
                ),
            )
        else:
            if physical["provider"] != task.provider:
                raise ProviderUnavailableError(
                    "task provider session is bound to another provider"
                )
            connection.execute(
                """
                UPDATE provider_sessions SET state = 'bound', worker_id = ?,
                    revision = revision + 1, bound_at = CURRENT_TIMESTAMP,
                    unbound_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (worker_id, physical["id"]),
            )
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
            """
            UPDATE workers SET status = 'busy', active_task_id = ?,
                external_session_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
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
        connection.execute(
            """
            UPDATE provider_sessions SET state = 'idle', unbound_at = CURRENT_TIMESTAMP,
                revision = revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND state = 'bound'
            """,
            (task_id,),
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
        current = connection.execute(
            """
            SELECT status, revision, last_evidence_hash
            FROM tasks WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        if current is None:
            return
        previous = connection.execute(
            """
            SELECT new_state_json, new_evidence_hash
            FROM aggregate_transitions
            WHERE aggregate_type = 'task' AND aggregate_id = ? AND revision < ?
            ORDER BY revision DESC LIMIT 1
            """,
            (task_id, revision),
        ).fetchone()
        previous_state = (
            json.loads(previous["new_state_json"])
            if previous is not None
            else {"status": "unknown", "revision": max(-1, revision - 1)}
        )
        AggregateReducer.record(
            connection,
            aggregate_type="task",
            aggregate_id=task_id,
            revision=revision,
            idempotency_key=f"task-transition:{idempotency_key}",
            actor_type="runtime",
            actor_id=None,
            reason=event_type,
            previous_state=previous_state,
            new_state={
                "status": current["status"],
                "revision": int(current["revision"]),
            },
            previous_evidence_hash=(
                previous["new_evidence_hash"] if previous is not None else None
            ),
            new_evidence_hash=current["last_evidence_hash"],
        )


def _task_from_row(row: Any) -> TaskRecord:
    execution_budget = (
        json.loads(row["execution_budget_json"])
        if row["execution_budget_json"] else None
    )
    max_attempts = int(row["max_attempts"])
    if execution_budget is not None and execution_budget.get("max_attempts") is not None:
        max_attempts = int(execution_budget["max_attempts"])
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
        max_attempts=max_attempts,
        attempts_used=row["attempts_used"],
        token_budget=row["token_budget"],
        tokens_used=row["tokens_used"],
        last_evidence_hash=row["last_evidence_hash"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        sizing=(
            json.loads(row["sizing_json"]) if row["sizing_json"]
            else {"status": "legacy_fallback"}
        ),
        execution_budget=execution_budget,
        manual_override=bool(row["manual_override"]),
        override_reason=row["override_reason"],
        budget_overrun_evidence=(
            json.loads(row["budget_overrun_evidence_json"])
            if row["budget_overrun_evidence_json"] else None
        ),
        goal_id=_optional(row, "goal_id"),
        parent_task_id=_optional(row, "parent_task_id"),
        depends_on=json.loads(_optional(row, "depends_on_json") or "[]"),
        work_item_kind=_optional(row, "work_item_kind"),
        ordinal=_optional(row, "ordinal"),
        blocked_reason=_optional(row, "blocked_reason"),
        handoff=_parse_json_object(_optional(row, "handoff_json")),
    )


def _authoritative_max_attempts(task: TaskRecord) -> int:
    if task.execution_budget and task.execution_budget.get("max_attempts") is not None:
        return int(task.execution_budget["max_attempts"])
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


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
