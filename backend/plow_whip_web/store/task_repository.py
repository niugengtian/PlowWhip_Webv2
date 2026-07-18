from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
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
from plow_whip_web.store.settings_repository import DEFAULT_SETTINGS
from plow_whip_web.runtime.evidence import manifest_hash
from plow_whip_web.runtime.token_ledger import TokenLedger

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
    ) -> TaskRecord:
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
                    sizing_json, execution_budget_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            spec_hash = insert_task_spec(connection, task_id, spec, revision=1)
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

    def worker_execution_context(self, worker_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT w.external_session_id, w.provider, w.session_generation,
                       p.path project_path,
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
                """
                INSERT INTO task_attempts(
                    id, task_id, attempt_number, status, spec_revision
                ) VALUES (?, ?, ?, 'running', ?)
                """,
                (attempt_id, task.id, attempt_number, task.spec_revision),
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
                event_type="attempt.started",
                payload={
                    "attempt_id": attempt_id, "run_id": run_id, "attempt_number": attempt_number,
                    "worker_id": worker_id, "lease_token": lease_token, "fencing_token": fencing_token,
                    "spec_revision": task.spec_revision,
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
            _validate_evidence_manifest(task, evidence_manifest)
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
            target = TaskStatus.COMPLETED if passed else (
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
            run_status = "completed" if target is TaskStatus.COMPLETED else (
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
                    manifest_json, manifest_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), task.id, attempt_id, run_id,
                    evidence_manifest["call_id"], task.spec_revision, task.revision,
                    evidence_manifest["environment_hash"], 1 if passed else 0,
                    _dump(evidence_manifest), evidence_hash,
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
            target = (
                TaskStatus.NEEDS_HUMAN
                if action == "needs_human" else TaskStatus.READY
            )
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
                    UPDATE workers SET external_session_id = COALESCE(?, external_session_id),
                        status = 'idle', active_task_id = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (retained_session_id, reason[:1000], task.worker_id),
                )
            self._release_worker_and_lock(connection, task.id, task.worker_id)
            if rotate_worker_reason and task.worker_id:
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
                    "execution_episode": episode,
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
            "SELECT status FROM roles WHERE id = ?", (task.role_id,)
        ).fetchone()
        if role is None or role["status"] == "released" or (
            role["status"] == "draining" and task.worker_id is None
        ):
            raise ProviderUnavailableError("legacy role worker is retired")
        worker = connection.execute(
            """
            SELECT w.*, r.status role_status FROM workers w
            JOIN roles r ON r.id = w.role_id
            WHERE w.project_id = ? AND w.role_id = ? AND w.released_at IS NULL
            """,
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
            ] in {"completed", "terminal_failed", "cancelled"}:
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
                        released_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND active_task_id = ?
                    """,
                    (worker_id, task_id),
                )
                connection.execute(
                    "UPDATE roles SET status = 'released' WHERE id = ?",
                    (lifecycle["role_id"],),
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
        sizing=(
            json.loads(row["sizing_json"]) if row["sizing_json"]
            else {"status": "legacy_fallback"}
        ),
        execution_policy=execution_policy,
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


def _assert_task_spec(task: TaskRecord) -> None:
    if set(task.spec) != TASK_SPEC_FIELDS:
        raise DomainError("claim requires one complete immutable task spec")


def _validate_evidence_manifest(
    task: TaskRecord, evidence_manifest: dict[str, Any]
) -> None:
    commands = evidence_manifest.get("verification_commands")
    artifacts = evidence_manifest.get("artifacts")
    report = evidence_manifest.get("test_report")
    if not isinstance(commands, list) or not isinstance(artifacts, list):
        raise DomainError("EvidenceManifest verification/artifact records are required")
    if [item.get("spec") for item in commands] != task.verification:
        raise DomainError("EvidenceManifest verification contract mismatch")
    checks_passed = bool(commands) and all(
        item.get("exit_code") == 0
        and isinstance(item.get("check"), dict)
        and item["check"].get("passed") is True
        for item in commands
    )
    artifact_paths = [item.get("relative_path") for item in artifacts]
    artifact_passed = artifact_paths == task.spec["artifacts"] and all(
        item.get("produced_by_run") is True for item in artifacts
    )
    if (
        not isinstance(report, dict)
        or bool(report.get("passed")) != checks_passed
        or bool(evidence_manifest.get("artifact_contract_passed")) != artifact_passed
        or bool(evidence_manifest.get("passed")) != (checks_passed and artifact_passed)
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


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
