from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from plow_whip_web.domain.model import DomainError, NotFoundError, TaskStatus
from plow_whip_web.runtime.goal_reducer import reduce_goal_status
from plow_whip_web.runtime.orchestration import (
    GoalPlan,
    PlannedWorkItem,
    child_sizing_inputs,
    plan_to_dict,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.store.database import Database
from plow_whip_web.store.role_instance_repository import RoleInstanceRepository
from plow_whip_web.store.task_repository import canonical_task_spec, insert_task_spec


def _preview_to_persistence(preview: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    sizing = {
        "status": preview["status"],
        "missing_gates": list(preview["missing_gates"]),
        "size_class": preview["size_class"],
        "rationale": list(preview["rationale"]),
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
        "model_invoked": False,
        "bootstrap_version": preview["bootstrap_version"],
    }
    execution_policy = {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
    }
    return sizing, execution_policy


def resolve_max_attempts(execution_policy: dict[str, Any] | None, fallback: int) -> int:
    if execution_policy is not None and execution_policy.get("max_attempts") is not None:
        return int(execution_policy["max_attempts"])
    return int(fallback)


class GoalRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_with_plan(
        self,
        *,
        title: str,
        objective: str,
        project_id: str,
        project_path: str,
        provider: str,
        plan: GoalPlan,
        sizing_inputs: dict[str, Any],
        verification: list[dict[str, Any]],
        scope: list[str],
        acceptance: list[str],
        artifacts: list[str],
        constraints: list[str],
        deadline: dict[str, Any] | None,
        idempotency_key: str,
        network_requirement: str = "none",
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if plan.status != "planned" or not plan.items:
            raise DomainError("goal plan is not dispatchable")
        command = command or {"argv": None, "timeout_seconds": 60, "output_limit_bytes": 131_072}
        base_inputs = TaskSizingInputs(**sizing_inputs)
        goal_preview = estimate_task_sizing(base_inputs)
        goal_deadline = deadline or {
            "hard_seconds": int(goal_preview["hard_deadline_seconds"] or 600)
        }
        goal_spec = canonical_task_spec(
            objective=objective,
            scope=scope,
            acceptance=acceptance,
            verification=verification,
            artifacts=artifacts,
            constraints=constraints,
            deadline=goal_deadline,
        )

        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                """
                SELECT payload_json FROM task_events
                WHERE idempotency_key = ? AND event_type = 'goal.created'
                """,
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                payload = json.loads(duplicate["payload_json"])
                return self._get_with_connection(connection, str(payload["goal_id"]))

            goal_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO goals(
                    id, title, objective, project_id, provider, status,
                    plan_json, sizing_inputs_json, parent_task_id
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, NULL)
                """,
                (
                    goal_id,
                    title,
                    objective,
                    project_id,
                    provider,
                    json.dumps(plan_to_dict(plan), ensure_ascii=False, sort_keys=True),
                    json.dumps(sizing_inputs, ensure_ascii=False, sort_keys=True),
                ),
            )
            _insert_goal_spec(connection, goal_id, goal_spec, revision=1)

            ordinal_to_task: dict[int, str] = {}
            first_task_id: str | None = None
            for item in plan.items:
                task_id = str(uuid.uuid4())
                first_task_id = first_task_id or task_id
                ordinal_to_task[item.ordinal] = task_id
                depends_ids = [
                    ordinal_to_task[dep]
                    for dep in item.depends_on_ordinals
                    if dep in ordinal_to_task
                ]
                ready = not depends_ids
                child_inputs = child_sizing_inputs(
                    base=base_inputs,
                    item=item,
                    total_items=max(1, len(plan.items)),
                )
                preview = estimate_task_sizing(child_inputs)
                if preview["status"] != "estimated":
                    raise DomainError(
                        f"child work item {item.ordinal} sizing needs_planning: "
                        f"{preview['missing_gates']}"
                    )
                sizing, execution_policy = _preview_to_persistence(preview)
                max_attempts = resolve_max_attempts(execution_policy, 1)
                # Prefer the project's named capability role so Plan/Task/Role/
                # Worker/Context all expose the same identity (backend, not a
                # compound alias or route label).
                existing_role = connection.execute(
                    """
                    SELECT id FROM roles
                    WHERE project_id = ? AND kind = ?
                    ORDER BY created_at ASC, id ASC LIMIT 1
                    """,
                    (project_id, item.role),
                ).fetchone()
                if existing_role is not None:
                    role_id = str(existing_role["id"])
                else:
                    role_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO roles(id, project_id, kind, status)
                        VALUES (?, ?, ?, 'ephemeral')
                        """,
                        (role_id, project_id, item.role),
                    )
                child_command, child_verification = _child_command_and_verification(
                    item=item,
                    shared_command=command,
                    shared_verification=verification,
                )
                timeout = int(execution_policy["hard_deadline_seconds"])
                child_command = {
                    **child_command,
                    "timeout_seconds": min(
                        int(child_command.get("timeout_seconds") or timeout),
                        timeout,
                    ),
                }
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id, title, objective, project_path, status, revision,
                        command_json, verification_json, max_attempts, token_budget,
                        project_id, role_id, resource_key, network_requirement, provider,
                        quality_profile, sizing_json, execution_budget_json,
                        manual_override, goal_id, parent_task_id, depends_on_json,
                        work_item_kind, ordinal, blocked_reason
                    ) VALUES (
                        ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?,
                        'deterministic', ?, ?, 0, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        task_id,
                        item.title,
                        item.objective,
                        project_path,
                        TaskStatus.READY.value if ready else TaskStatus.PAUSED.value,
                        json.dumps(child_command, ensure_ascii=False, sort_keys=True),
                        json.dumps(child_verification, ensure_ascii=False, sort_keys=True),
                        max_attempts,
                        project_id,
                        role_id,
                        f"goal:{goal_id}:item:{item.ordinal}",
                        network_requirement,
                        item.provider or provider,
                        json.dumps(sizing, ensure_ascii=False, sort_keys=True),
                        json.dumps(execution_policy, ensure_ascii=False, sort_keys=True),
                        goal_id,
                        None,
                        json.dumps(depends_ids, ensure_ascii=False),
                        item.kind,
                        item.ordinal,
                        None if ready else f"waiting_on:{','.join(depends_ids)}",
                    ),
                )
                insert_task_spec(
                    connection,
                    task_id,
                    canonical_task_spec(
                        objective=item.objective,
                        scope=list(dict.fromkeys([*scope, item.role, item.kind])),
                        acceptance=list(dict.fromkeys([*acceptance, *item.acceptance])),
                        verification=child_verification,
                        artifacts=list(dict.fromkeys([*artifacts, *item.artifacts])),
                        constraints=list(dict.fromkeys([
                            *constraints,
                            f"network:{network_requirement}",
                            f"provider:{provider}",
                            "worker_lifecycle:ephemeral",
                        ])),
                        deadline={
                            "hard_seconds": execution_policy["hard_deadline_seconds"]
                        },
                    ),
                    revision=1,
                )
                # Immutable RoleInstance + SessionBinding after confirmed WorkItem.
                RoleInstanceRepository(self.database).create_for_task(
                    connection,
                    project_id=project_id,
                    goal_id=goal_id,
                    task_id=task_id,
                    role_kind=item.role,
                    role_id=role_id,
                    provider=item.provider or provider,
                    task_spec_revision=1,
                    work_item={
                        "boundaries": list(scope),
                        "deliverables": list(item.artifacts),
                        "verification": [
                            str(check.get("kind") or check)
                            for check in child_verification
                        ],
                        "tools": [],
                    },
                )

            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, payload_json, state_revision, idempotency_key
                ) VALUES (?, 'goal.created', ?, 0, ?)
                """,
                (
                    first_task_id,
                    json.dumps(
                        {
                            "goal_id": goal_id,
                            "work_items": len(plan.items),
                            "rationale": list(plan.rationale),
                            "model_invoked": False,
                            "model_pm_implemented": False,
                            "route": plan.route,
                            "provider": provider,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    idempotency_key,
                ),
            )
            return self._get_with_connection(connection, goal_id)

    def get(self, goal_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            return self._get_with_connection(connection, goal_id)
        finally:
            connection.close()

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT id FROM goals ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._get_with_connection(connection, row["id"]) for row in rows]
        finally:
            connection.close()

    def advance(self) -> dict[str, Any]:
        """Normalize task gates and recompute every Goal from immutable facts."""
        unblocked: list[str] = []
        replanned: list[str] = []
        completed_goals: list[str] = []
        blocked_goals: list[dict[str, str]] = []
        with self.database.transaction(immediate=True) as connection:
            # Legacy parent ids are compatibility columns, never Goal truth.
            connection.execute(
                "UPDATE goals SET parent_task_id = NULL WHERE parent_task_id IS NOT NULL"
            )
            connection.execute(
                """
                UPDATE tasks SET parent_task_id = NULL
                WHERE goal_id IS NOT NULL AND parent_task_id IS NOT NULL
                """
            )
            goals = connection.execute(
                "SELECT id, status, current_spec_revision FROM goals ORDER BY created_at, id"
            ).fetchall()
            for goal in goals:
                goal_spec = connection.execute(
                    """
                    SELECT spec_json, spec_hash FROM goal_specs
                    WHERE goal_id = ? AND spec_revision = ?
                    """,
                    (goal["id"], goal["current_spec_revision"]),
                ).fetchone()
                children = connection.execute(
                    """
                    SELECT t.id, t.status, t.revision, t.last_evidence_hash,
                           t.last_error, t.depends_on_json, t.manual_override,
                           t.current_spec_revision, t.work_item_kind,
                           s.spec_json, s.spec_hash,
                           em.manifest_hash, em.rowid AS evidence_sequence
                    FROM tasks t
                    LEFT JOIN task_specs s ON s.task_id = t.id
                        AND s.spec_revision = t.current_spec_revision
                    LEFT JOIN evidence_manifests em ON em.task_id = t.id
                        AND em.spec_revision = t.current_spec_revision
                        AND em.manifest_hash = t.last_evidence_hash
                        AND em.passed = 1
                    WHERE t.goal_id = ?
                      AND work_item_kind IN ('implementation', 'verification')
                    ORDER BY t.ordinal, t.created_at, t.id
                    """,
                    (goal["id"],),
                ).fetchall()
                safety_reason = None
                if not _valid_spec_row(goal_spec):
                    safety_reason = "goal_spec_missing_or_corrupt"
                elif not children:
                    safety_reason = "task_spec_missing"

                facts: dict[str, dict[str, Any]] = {}
                status_overrides: dict[str, str] = {}
                for child in children:
                    depends = json.loads(child["depends_on_json"] or "[]")
                    if not _valid_spec_row(child):
                        safety_reason = safety_reason or "task_spec_missing_or_corrupt"
                    if any(dependency not in facts for dependency in depends):
                        safety_reason = safety_reason or "dependency_missing"
                    dependencies_valid = all(
                        facts[dependency]["valid_completion"] for dependency in depends
                        if dependency in facts
                    ) and len(depends) == sum(dependency in facts for dependency in depends)
                    dependencies_state_completed = all(
                        facts[dependency]["status"] == TaskStatus.COMPLETED.value
                        for dependency in depends if dependency in facts
                    ) and len(depends) == sum(dependency in facts for dependency in depends)
                    dependencies_complete = (
                        dependencies_state_completed
                        if child["work_item_kind"] == "verification"
                        else dependencies_valid
                    )
                    dependency_evidence_sequence = max(
                        (
                            int(facts[dependency]["evidence_sequence"] or 0)
                            for dependency in depends if dependency in facts
                        ),
                        default=0,
                    )
                    own_evidence = child["manifest_hash"] is not None
                    valid_completion = (
                        child["status"] == TaskStatus.COMPLETED.value
                        and own_evidence
                        and dependencies_complete
                        and int(child["evidence_sequence"] or 0)
                        >= dependency_evidence_sequence
                    )
                    facts[child["id"]] = {
                        "valid_completion": valid_completion,
                        "evidence_sequence": child["evidence_sequence"],
                        "status": child["status"],
                        "work_item_kind": child["work_item_kind"],
                        "own_evidence": own_evidence,
                        "dependency_closure": {
                            ancestor
                            for dependency in depends if dependency in facts
                            for ancestor in (
                                dependency,
                                *facts[dependency]["dependency_closure"],
                            )
                        },
                    }

                    if (
                        child["status"] == TaskStatus.NEEDS_HUMAN.value
                        and not _autonomy_blocker(child["last_error"])
                        and _valid_spec_row(child)
                    ):
                        prior_replan = connection.execute(
                            """
                            SELECT 1 FROM task_events
                            WHERE task_id = ?
                              AND event_type = 'goal.work_item_replanned'
                              AND json_extract(
                                  payload_json, '$.to_spec_revision'
                              ) = ?
                            LIMIT 1
                            """,
                            (child["id"], child["current_spec_revision"]),
                        ).fetchone()
                        next_revision = int(child["revision"]) + 1
                        if prior_replan is None:
                            spec_revision = int(child["current_spec_revision"]) + 1
                            insert_task_spec(
                                connection,
                                child["id"],
                                json.loads(child["spec_json"]),
                                revision=spec_revision,
                            )
                            target = (
                                TaskStatus.READY.value
                                if dependencies_complete else TaskStatus.PAUSED.value
                            )
                            connection.execute(
                                """
                                UPDATE tasks SET current_spec_revision = ?, status = ?,
                                    revision = ?, attempts_used = 0,
                                    same_failure_count = 0, no_progress_count = 0,
                                    last_failure_fingerprint = NULL,
                                    last_evidence_hash = NULL, last_error = NULL,
                                    next_eligible_at = NULL, manual_override = 0,
                                    blocked_reason = ?, handoff_json = NULL,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = ? AND revision = ?
                                """,
                                (
                                    spec_revision, target, next_revision,
                                    None if dependencies_complete
                                    else f"waiting_on:{','.join(depends)}",
                                    child["id"], child["revision"],
                                ),
                            )
                            connection.execute(
                                """
                                INSERT INTO task_events(
                                    task_id, event_type, payload_json,
                                    state_revision, idempotency_key
                                ) VALUES (?, 'goal.work_item_replanned', ?, ?, ?)
                                """,
                                (
                                    child["id"],
                                    json.dumps(
                                        {
                                            "from_spec_revision": child[
                                                "current_spec_revision"
                                            ],
                                            "to_spec_revision": spec_revision,
                                            "reason": child["last_error"],
                                        },
                                        ensure_ascii=False, sort_keys=True,
                                    ),
                                    next_revision,
                                    f"goal-replan:{child['id']}:{spec_revision}",
                                ),
                            )
                            status_overrides[child["id"]] = target
                            replanned.append(child["id"])
                        else:
                            connection.execute(
                                """
                                UPDATE tasks SET status = 'terminal_failed', revision = ?,
                                    last_error = 'goal_replan_exhausted',
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = ? AND revision = ?
                                """,
                                (next_revision, child["id"], child["revision"]),
                            )
                            connection.execute(
                                """
                                INSERT INTO task_events(
                                    task_id, event_type, payload_json,
                                    state_revision, idempotency_key
                                ) VALUES (?, 'task.terminal_failed', ?, ?, ?)
                                """,
                                (
                                    child["id"],
                                    json.dumps(
                                        {"reason": "goal_replan_exhausted"},
                                        sort_keys=True,
                                    ),
                                    next_revision,
                                    f"goal-replan-exhausted:{child['id']}",
                                ),
                            )
                            status_overrides[child["id"]] = (
                                TaskStatus.TERMINAL_FAILED.value
                            )

                    if (
                        child["status"] == TaskStatus.COMPLETED.value
                        and own_evidence
                        and not valid_completion
                    ):
                        next_revision = int(child["revision"]) + 1
                        connection.execute(
                            """
                            UPDATE tasks SET status = 'paused', revision = ?,
                                attempts_used = 0, last_evidence_hash = NULL,
                                blocked_reason = ?, handoff_json = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ? AND revision = ?
                            """,
                            (
                                next_revision,
                                f"waiting_on:{','.join(depends)}",
                                child["id"], child["revision"],
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO task_events(
                                task_id, event_type, payload_json,
                                state_revision, idempotency_key
                            ) VALUES (?, 'goal.work_item_invalidated', ?, ?, ?)
                            """,
                            (
                                child["id"],
                                json.dumps(
                                    {"depends_on": depends},
                                    ensure_ascii=False, sort_keys=True,
                                ),
                                next_revision,
                                f"goal-invalidate:{child['id']}:{next_revision}",
                            ),
                        )
                    if child["status"] in {
                        TaskStatus.READY.value,
                        TaskStatus.PAUSED.value,
                    }:
                        desired = (
                            TaskStatus.READY.value
                            if dependencies_complete and not child["manual_override"]
                            else TaskStatus.PAUSED.value
                        )
                        blocked_reason = (
                            None if desired == TaskStatus.READY.value
                            else f"waiting_on:{','.join(depends)}" if depends else "manual_pause"
                        )
                        if child["status"] != desired:
                            next_revision = int(child["revision"]) + 1
                            handoff = (
                                _handoff_from_completed(connection, depends[-1])
                                if (
                                    desired == TaskStatus.READY.value
                                    and depends
                                    and dependencies_valid
                                )
                                else None
                            )
                            connection.execute(
                                """
                                UPDATE tasks SET status = ?, revision = ?, blocked_reason = ?,
                                    handoff_json = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE id = ? AND revision = ?
                                """,
                                (
                                    desired, next_revision, blocked_reason,
                                    json.dumps(handoff, ensure_ascii=False, sort_keys=True)
                                    if handoff else None,
                                    child["id"], child["revision"],
                                ),
                            )
                            event_type = (
                                "goal.work_item_unblocked"
                                if desired == TaskStatus.READY.value
                                else "goal.work_item_blocked"
                            )
                            connection.execute(
                                """
                                INSERT INTO task_events(
                                    task_id, event_type, payload_json,
                                    state_revision, idempotency_key
                                ) VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    child["id"], event_type,
                                    json.dumps(
                                        {"depends_on": depends, "handoff": handoff},
                                        ensure_ascii=False, sort_keys=True,
                                    ),
                                    next_revision,
                                    f"goal-gate:{child['id']}:{next_revision}:{desired}",
                                ),
                            )
                            if desired == TaskStatus.READY.value:
                                unblocked.append(child["id"])

                verification_candidates = [
                    fact for fact in facts.values()
                    if fact["work_item_kind"] == "verification"
                    and fact["status"] not in {
                        TaskStatus.CANCELLED.value,
                        TaskStatus.TERMINAL_FAILED.value,
                    }
                ]
                candidate_coverage = {
                    task_id
                    for fact in verification_candidates
                    for task_id in fact["dependency_closure"]
                }
                valid_verification_coverage = {
                    task_id
                    for fact in verification_candidates if fact["valid_completion"]
                    for task_id in fact["dependency_closure"]
                }
                uncovered_missing_evidence = [
                    task_id for task_id, fact in facts.items()
                    if fact["status"] == TaskStatus.COMPLETED.value
                    and not fact["own_evidence"]
                    and not (
                        fact["work_item_kind"] == "implementation"
                        and task_id in candidate_coverage
                    )
                ]
                if uncovered_missing_evidence:
                    safety_reason = safety_reason or "evidence_manifest_missing"

                statuses = {
                    status_overrides.get(row["id"], row["status"])
                    for row in children
                }
                human_child = next(
                    (
                        row for row in children
                        if row["status"] == TaskStatus.NEEDS_HUMAN.value
                        and _autonomy_blocker(row["last_error"])
                    ),
                    None,
                )
                desired, reason = reduce_goal_status(
                    safety_reason=safety_reason,
                    child_statuses=statuses,
                    all_completed=bool(children) and all(
                        facts[row["id"]]["valid_completion"]
                        or (
                            facts[row["id"]]["work_item_kind"] == "implementation"
                            and row["id"] in valid_verification_coverage
                        )
                        for row in children
                    ),
                    has_autonomy_blocker=human_child is not None,
                )
                changed = self._settle_goal(
                    connection,
                    goal["id"],
                    children[-1] if children else None,
                    current_status=goal["status"],
                    status=desired,
                    reason=reason,
                    fact_hash=_goal_fact_hash(goal, children, desired, reason),
                )
                if desired == "completed" and changed:
                    completed_goals.append(goal["id"])
                elif desired in {"needs_human", "terminal_failed", "cancelled"}:
                    blocked_goals.append({"goal_id": goal["id"], "reason": reason})
        return {
            "unblocked": unblocked,
            "replanned": replanned,
            "completed_goals": completed_goals,
            "blocked_goals": blocked_goals,
            "model_invoked": False,
        }

    @staticmethod
    def _settle_goal(
        connection: Any,
        goal_id: str,
        event_task: Any,
        *,
        current_status: str,
        status: str,
        reason: str,
        fact_hash: str,
    ) -> bool:
        if status == current_status:
            return False
        connection.execute(
            "UPDATE goals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, goal_id),
        )
        if event_task is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO task_events(
                    task_id, event_type, payload_json, state_revision, idempotency_key
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_task["id"],
                    f"goal.{status}",
                    json.dumps(
                        {"goal_id": goal_id, "reason": reason},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    event_task["revision"],
                    f"goal-recompute:{goal_id}:{status}:{fact_hash}",
                ),
            )
        return True

    def _get_with_connection(self, connection: Any, goal_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        tasks = connection.execute(
            """
            SELECT t.id, t.title, t.objective, t.status, t.role_id, r.kind AS role,
                   t.worker_id,
                   t.work_item_kind, t.ordinal, t.depends_on_json, t.blocked_reason,
                   t.parent_task_id, t.revision, t.provider, t.created_at, t.updated_at,
                   t.last_error, t.last_evidence_hash, t.attempts_used, t.max_attempts,
                   t.tokens_used, t.sizing_json, t.execution_budget_json,
                   t.handoff_json, t.command_json, t.verification_json,
                   t.current_spec_revision, s.spec_json AS task_spec_json,
                   (
                       SELECT em.manifest_json FROM evidence_manifests em
                       WHERE em.task_id = t.id
                       ORDER BY em.created_at DESC, em.rowid DESC LIMIT 1
                   ) AS evidence_manifest_json,
                   w.session_id, w.external_session_id, w.session_generation, w.last_error
                     AS worker_last_error, w.last_input_tokens,
                   w.last_cached_input_tokens, w.last_output_tokens,
                   w.last_uncached_input_tokens, w.last_context_pressure_tokens,
                   w.last_context_pressure_reason, w.last_attribution_granularity,
                   w.last_value_classification,
                   ts.external_session_id AS task_external_session_id,
                   ts.session_generation AS task_session_generation,
                   ts.replacement_reason AS task_session_replacement_reason
            FROM tasks t
            JOIN roles r ON r.id = t.role_id
            JOIN task_specs s ON s.task_id = t.id
                AND s.spec_revision = t.current_spec_revision
            LEFT JOIN workers w ON w.id = t.worker_id
            LEFT JOIN task_sessions ts ON ts.task_id = t.id
            WHERE t.goal_id = ? AND COALESCE(t.work_item_kind, '') != 'coordination'
            ORDER BY t.ordinal, t.created_at, t.id
            """,
            (goal_id,),
        ).fetchall()
        # Also attach idle role workers for items not yet claimed.
        role_workers = {
            item["role_id"]: item
            for item in connection.execute(
                """
                SELECT role_id, id, session_id, external_session_id, session_generation,
                       provider, last_error, last_input_tokens,
                       last_cached_input_tokens, last_output_tokens,
                       last_uncached_input_tokens, last_context_pressure_tokens,
                       last_context_pressure_reason, last_attribution_granularity,
                       last_value_classification
                FROM workers
                WHERE project_id = ? AND released_at IS NULL
                """,
                (row["project_id"],),
            ).fetchall()
        }
        work_items = []
        for task in tasks:
            task_spec = json.loads(task["task_spec_json"])
            worker = None
            if task["worker_id"]:
                worker = {
                    "id": task["worker_id"],
                    "session_id": task["session_id"],
                    "external_session_id": task["external_session_id"],
                    "session_generation": task["session_generation"],
                    "last_error": task["worker_last_error"],
                    "last_input_tokens": task["last_input_tokens"],
                    "last_cached_input_tokens": task["last_cached_input_tokens"],
                    "last_output_tokens": task["last_output_tokens"],
                    "last_uncached_input_tokens": task["last_uncached_input_tokens"],
                    "last_context_pressure_tokens": task["last_context_pressure_tokens"],
                    "last_context_pressure_reason": task["last_context_pressure_reason"],
                    "last_attribution_granularity": task["last_attribution_granularity"],
                    "last_value_classification": task["last_value_classification"],
                }
            elif task["role_id"] in role_workers:
                bound = role_workers[task["role_id"]]
                worker = {
                    "id": bound["id"],
                    "session_id": bound["session_id"],
                    "external_session_id": bound["external_session_id"],
                    "session_generation": bound["session_generation"],
                    "last_error": bound["last_error"],
                    "last_input_tokens": bound["last_input_tokens"],
                    "last_cached_input_tokens": bound["last_cached_input_tokens"],
                    "last_output_tokens": bound["last_output_tokens"],
                    "last_uncached_input_tokens": bound["last_uncached_input_tokens"],
                    "last_context_pressure_tokens": bound["last_context_pressure_tokens"],
                    "last_context_pressure_reason": bound["last_context_pressure_reason"],
                    "last_attribution_granularity": bound["last_attribution_granularity"],
                    "last_value_classification": bound["last_value_classification"],
                }
            rotation = connection.execute(
                """
                SELECT reason, session_generation, archived_at
                FROM worker_session_archives
                WHERE worker_id = ?
                ORDER BY archived_at DESC, id DESC LIMIT 1
                """,
                (worker["id"],),
            ).fetchone() if worker else None
            work_items.append(
                {
                    "id": task["id"],
                    "title": task["title"],
                    "objective": task_spec["objective"],
                    "status": task["status"],
                    "role_id": task["role_id"],
                    "role": str(task["role"]).split(":", 1)[0],
                    "worker_id": task["worker_id"] or (worker["id"] if worker else None),
                    "work_item_kind": task["work_item_kind"],
                    "ordinal": task["ordinal"],
                    "depends_on": json.loads(task["depends_on_json"] or "[]"),
                    "blocked_reason": task["blocked_reason"],
                    "parent_task_id": task["parent_task_id"],
                    "revision": task["revision"],
                    "provider": task["provider"],
                    "created_at": task["created_at"],
                    "updated_at": task["updated_at"],
                    "last_error": task["last_error"],
                    "last_evidence_hash": task["last_evidence_hash"],
                    "attempts_used": task["attempts_used"],
                    "max_attempts": task["max_attempts"],
                    "tokens_used": task["tokens_used"],
                    "sizing": json.loads(task["sizing_json"]) if task["sizing_json"] else {},
                    "execution_policy": _execution_policy(task["execution_budget_json"]),
                    "handoff": json.loads(task["handoff_json"]) if task["handoff_json"] else None,
                    "command": json.loads(task["command_json"]) if task["command_json"] else {},
                    "verification": task_spec["verification"],
                    "spec_revision": int(task["current_spec_revision"]),
                    "spec": task_spec,
                    "evidence_manifest": (
                        json.loads(task["evidence_manifest_json"])
                        if task["evidence_manifest_json"] else None
                    ),
                    "session_id": worker["session_id"] if worker else None,
                    "external_session_id": (
                        task["task_external_session_id"]
                        if task["task_session_generation"] is not None
                        else worker["external_session_id"] if worker else None
                    ),
                    "session_generation": (
                        task["task_session_generation"]
                        if task["task_session_generation"] is not None
                        else worker["session_generation"] if worker else None
                    ),
                    "session_scope": (
                        "task_role"
                        if task["task_session_generation"] is not None
                        else "worker_legacy"
                    ),
                    "rotation_reason": (
                        task["task_session_replacement_reason"]
                        or (rotation["reason"] if rotation else None)
                    ),
                    "input_tokens": worker["last_input_tokens"] if worker else 0,
                    "cached_input_tokens": (
                        worker["last_cached_input_tokens"] if worker else 0
                    ),
                    "cached_input_tokens_in_total": True,
                    "output_tokens": worker["last_output_tokens"] if worker else 0,
                    "uncached_input_tokens": (
                        worker["last_uncached_input_tokens"] if worker else 0
                    ),
                    "attribution_granularity": (
                        worker["last_attribution_granularity"] if worker else "turn"
                    ),
                    "value_classification": (
                        worker["last_value_classification"] if worker else "unknown"
                    ),
                    "last_context_pressure": (
                        worker["last_context_pressure_tokens"] if worker else 0
                    ),
                    "last_context_pressure_reason": (
                        worker["last_context_pressure_reason"] if worker else None
                    ),
                    "progress": {
                        "attempts_used": task["attempts_used"],
                        "max_attempts": task["max_attempts"],
                        "tokens_used": task["tokens_used"],
                        "status": task["status"],
                    },
                }
            )
        spec_row = connection.execute(
            """
            SELECT spec_revision, spec_json, spec_hash
            FROM goal_specs
            WHERE goal_id = ? AND spec_revision = ?
            """,
            (goal_id, row["current_spec_revision"]),
        ).fetchone()
        if spec_row is None or hashlib.sha256(
            spec_row["spec_json"].encode("utf-8")
        ).hexdigest() != spec_row["spec_hash"]:
            raise DomainError("immutable GoalSpec is missing or corrupt")
        return {
            "id": row["id"],
            "title": row["title"],
            "objective": row["objective"],
            "project_id": row["project_id"],
            "provider": row["provider"],
            "status": row["status"],
            "plan": json.loads(row["plan_json"]),
            "sizing_inputs": (
                json.loads(row["sizing_inputs_json"]) if row["sizing_inputs_json"] else None
            ),
            "parent_task_id": row["parent_task_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "work_items": work_items,
            "spec_revision": int(spec_row["spec_revision"]),
            "spec": json.loads(spec_row["spec_json"]),
        }


def _execution_policy(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        key: value
        for key, value in json.loads(raw).items()
        if key not in {
            "reserved_tokens",
            "total_token_hard_cap",
            "estimated_total_token_hard_cap",
        }
    }


def _valid_spec_row(row: Any) -> bool:
    if row is None or not row["spec_json"] or not row["spec_hash"]:
        return False
    return hashlib.sha256(row["spec_json"].encode("utf-8")).hexdigest() == row[
        "spec_hash"
    ]


def _autonomy_blocker(reason: str | None) -> bool:
    value = str(reason or "").lower()
    return any(marker in value for marker in (
        "provider_auth", "permission_denied", "credential", "secret",
        "policy_violation", "security_boundary", "spec_missing",
    ))


def _goal_fact_hash(
    goal: Any,
    children: list[Any],
    status: str,
    reason: str,
) -> str:
    payload = {
        "goal_spec_revision": int(goal["current_spec_revision"]),
        "status": status,
        "reason": reason,
        "tasks": [
            {
                "id": row["id"],
                "status": row["status"],
                "revision": int(row["revision"]),
                "spec_revision": int(row["current_spec_revision"]),
                "evidence": row["last_evidence_hash"],
                "depends_on": json.loads(row["depends_on_json"] or "[]"),
            }
            for row in children
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _child_command_and_verification(
    *,
    item: PlannedWorkItem,
    shared_command: dict[str, Any],
    shared_verification: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Every routed task carries the caller's deterministic verification Gate."""
    return dict(shared_command), list(shared_verification)


def _handoff_from_completed(connection: Any, task_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT t.id, t.title, t.last_evidence_hash, t.work_item_kind, t.role_id,
               s.spec_json
        FROM tasks t
        JOIN task_specs s ON s.task_id = t.id
            AND s.spec_revision = t.current_spec_revision
        WHERE t.id = ? AND t.status = 'completed'
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    artifact_paths = list(json.loads(row["spec_json"])["artifacts"])
    return {
        "from_task_id": row["id"],
        "from_title": row["title"],
        "from_kind": row["work_item_kind"],
        "evidence_hash": row["last_evidence_hash"],
        "artifact_paths": artifact_paths[:32],
    }


def _insert_goal_spec(
    connection: Any,
    goal_id: str,
    spec: dict[str, Any],
    *,
    revision: int,
) -> str:
    spec_json = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO goal_specs(goal_id, spec_revision, spec_json, spec_hash)
        VALUES (?, ?, ?, ?)
        """,
        (goal_id, revision, spec_json, digest),
    )
    return digest
