from __future__ import annotations

import json
import uuid
from typing import Any

from plow_whip_web.domain.model import DomainError, NotFoundError, TaskStatus
from plow_whip_web.runtime.orchestration import (
    GoalPlan,
    PlannedWorkItem,
    child_sizing_inputs,
    plan_to_dict,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.store.database import Database
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
        role_providers: dict[str, str],
        plan: GoalPlan,
        sizing_inputs: dict[str, Any],
        verification: list[dict[str, Any]],
        role_ids: dict[str, str],
        idempotency_key: str,
        network_requirement: str = "none",
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if plan.status != "planned" or not plan.items:
            raise DomainError("goal plan is not dispatchable")
        missing_providers = sorted(
            ({"coordination"} | {item.role for item in plan.items}) - set(role_providers)
        )
        if missing_providers:
            raise DomainError(
                f"missing role provider decision: {', '.join(missing_providers)}"
            )
        command = command or {"argv": None, "timeout_seconds": 60, "output_limit_bytes": 131_072}
        base_inputs = TaskSizingInputs(**sizing_inputs)

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
            parent_task_id = str(uuid.uuid4())
            # Coordination parent is bookkeeping-only; sizing supplies execution timing.
            parent_preview = estimate_task_sizing(
                TaskSizingInputs(
                    layers_touched=1,
                    components_touched=1,
                    estimated_files_changed=1,
                    has_migration=False,
                    has_deploy=False,
                    verification_commands_count=1,
                    estimated_verification_seconds=60,
                    external_dependencies_count=0,
                    risk_level="low",
                    independent_review_required=False,
                    gate_artifact=True,
                    gate_boundary=True,
                    gate_verification=True,
                    gate_dependency=True,
                )
            )
            parent_sizing, parent_policy = _preview_to_persistence(parent_preview)
            parent_attempts = resolve_max_attempts(parent_policy, 1)
            parent_provider = role_providers["coordination"]

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
                    parent_provider,
                    json.dumps(plan_to_dict(plan), ensure_ascii=False, sort_keys=True),
                    json.dumps(sizing_inputs, ensure_ascii=False, sort_keys=True),
                ),
            )
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
                    ?, ?, ?, ?, 'paused', 0, ?, ?, ?, 0, ?, ?, ?, ?, ?,
                    'deterministic', ?, ?, 0, ?, NULL, '[]',
                    'coordination', 0, 'waiting_children'
                )
                """,
                (
                    parent_task_id,
                    title,
                    objective,
                    project_path,
                    json.dumps(
                        {"argv": None, "timeout_seconds": 60, "output_limit_bytes": 131_072},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps([], ensure_ascii=False),
                    parent_attempts,
                    project_id,
                    role_ids["coordination"],
                    f"goal:{goal_id}",
                    network_requirement,
                    parent_provider,
                    json.dumps(parent_sizing, ensure_ascii=False, sort_keys=True),
                    json.dumps(parent_policy, ensure_ascii=False, sort_keys=True),
                    goal_id,
                ),
            )
            connection.execute(
                "UPDATE goals SET parent_task_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (parent_task_id, goal_id),
            )
            insert_task_spec(
                connection,
                parent_task_id,
                canonical_task_spec(
                    objective=objective,
                    scope=["coordination"],
                    acceptance=["all_work_items_verified"],
                    verification=[],
                    artifacts=[],
                    constraints=[
                        f"network:{network_requirement}",
                        f"provider:{parent_provider}",
                    ],
                    deadline={"hard_seconds": parent_policy["hard_deadline_seconds"]},
                ),
                revision=1,
            )

            ordinal_to_task: dict[int, str] = {}
            impl_count = sum(1 for item in plan.items if item.kind == "implementation")
            for item in plan.items:
                task_id = str(uuid.uuid4())
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
                item_provider = role_providers[item.role]
                child_command, child_verification = _child_command_and_verification(
                    item=item,
                    shared_command=command,
                    shared_verification=verification,
                    impl_count=impl_count,
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
                        role_ids[item.role],
                        f"goal:{goal_id}:item:{item.ordinal}",
                        network_requirement,
                        item_provider,
                        json.dumps(sizing, ensure_ascii=False, sort_keys=True),
                        json.dumps(execution_policy, ensure_ascii=False, sort_keys=True),
                        goal_id,
                        parent_task_id,
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
                        scope=[item.role, item.kind],
                        acceptance=list(item.acceptance),
                        verification=child_verification,
                        artifacts=list(item.artifacts) or None,
                        constraints=[
                            f"network:{network_requirement}",
                            f"provider:{item_provider}",
                        ],
                        deadline={
                            "hard_seconds": execution_policy["hard_deadline_seconds"]
                        },
                    ),
                    revision=1,
                )

            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, payload_json, state_revision, idempotency_key
                ) VALUES (?, 'goal.created', ?, 0, ?)
                """,
                (
                    parent_task_id,
                    json.dumps(
                        {
                            "goal_id": goal_id,
                            "work_items": len(plan.items),
                            "rationale": list(plan.rationale),
                            "model_invoked": False,
                            "model_pm_implemented": False,
                            "role_providers": role_providers,
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
        """Unblock ready children and settle parents. Idempotent and 0 Token."""
        unblocked: list[str] = []
        completed_goals: list[str] = []
        blocked_goals: list[dict[str, str]] = []
        with self.database.transaction(immediate=True) as connection:
            paused = connection.execute(
                """
                SELECT id, depends_on_json, parent_task_id, goal_id, revision
                FROM tasks
                WHERE status = 'paused'
                  AND work_item_kind IN ('implementation', 'verification')
                  AND goal_id IS NOT NULL
                ORDER BY ordinal, created_at, id
                """
            ).fetchall()
            for row in paused:
                depends = json.loads(row["depends_on_json"] or "[]")
                if not depends:
                    continue
                pending = connection.execute(
                    f"""
                    SELECT id, status FROM tasks
                    WHERE id IN ({",".join("?" for _ in depends)})
                    """,
                    tuple(depends),
                ).fetchall()
                if len(pending) != len(depends):
                    continue
                if any(item["status"] != TaskStatus.COMPLETED.value for item in pending):
                    if any(
                        item["status"] in {
                            TaskStatus.TERMINAL_FAILED.value,
                            TaskStatus.NEEDS_HUMAN.value,
                            TaskStatus.CANCELLED.value,
                        }
                        for item in pending
                    ):
                        continue
                    continue
                handoff = _handoff_from_completed(connection, depends[-1])
                next_revision = int(row["revision"]) + 1
                connection.execute(
                    """
                    UPDATE tasks SET status = 'ready', revision = ?, blocked_reason = NULL,
                        handoff_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'paused' AND revision = ?
                    """,
                    (
                        next_revision,
                        json.dumps(handoff, ensure_ascii=False, sort_keys=True)
                        if handoff else None,
                        row["id"],
                        row["revision"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_events(
                        task_id, event_type, payload_json, state_revision, idempotency_key
                    ) VALUES (?, 'goal.work_item_unblocked', ?, ?, ?)
                    """,
                    (
                        row["id"],
                        json.dumps(
                            {"depends_on": depends, "handoff": handoff},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        next_revision,
                        f"goal-unblock:{row['id']}:{next_revision}",
                    ),
                )
                unblocked.append(row["id"])

            parents = connection.execute(
                """
                SELECT id, goal_id, revision FROM tasks
                WHERE work_item_kind = 'coordination' AND status = 'paused'
                  AND goal_id IS NOT NULL
                """
            ).fetchall()
            for parent in parents:
                children = connection.execute(
                    """
                    SELECT id, status, work_item_kind FROM tasks
                    WHERE parent_task_id = ? AND work_item_kind IN (
                        'implementation', 'verification'
                    )
                    """,
                    (parent["id"],),
                ).fetchall()
                if not children:
                    continue
                statuses = {row["status"] for row in children}
                if TaskStatus.NEEDS_HUMAN.value in statuses:
                    self._settle_parent(
                        connection, parent, goal_status="needs_human",
                        task_status=TaskStatus.NEEDS_HUMAN,
                        reason="child_needs_human",
                    )
                    blocked_goals.append(
                        {"goal_id": parent["goal_id"], "reason": "child_needs_human"}
                    )
                    continue
                if TaskStatus.TERMINAL_FAILED.value in statuses or TaskStatus.CANCELLED.value in statuses:
                    self._settle_parent(
                        connection, parent, goal_status="terminal_failed",
                        task_status=TaskStatus.TERMINAL_FAILED,
                        reason="child_terminal_failed",
                    )
                    blocked_goals.append(
                        {"goal_id": parent["goal_id"], "reason": "child_terminal_failed"}
                    )
                    continue
                has_verification = any(
                    row["work_item_kind"] == "verification" for row in children
                )
                all_done = all(
                    row["status"] == TaskStatus.COMPLETED.value for row in children
                )
                if all_done and has_verification:
                    self._settle_parent(
                        connection, parent, goal_status="completed",
                        task_status=TaskStatus.COMPLETED,
                        reason="children_verified",
                    )
                    completed_goals.append(parent["goal_id"])
        return {
            "unblocked": unblocked,
            "completed_goals": completed_goals,
            "blocked_goals": blocked_goals,
            "model_invoked": False,
        }

    @staticmethod
    def _settle_parent(
        connection: Any,
        parent: Any,
        *,
        goal_status: str,
        task_status: TaskStatus,
        reason: str,
    ) -> None:
        next_revision = int(parent["revision"]) + 1
        connection.execute(
            """
            UPDATE tasks SET status = ?, revision = ?, blocked_reason = NULL,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND revision = ?
            """,
            (task_status.value, next_revision, reason, parent["id"], parent["revision"]),
        )
        connection.execute(
            """
            UPDATE goals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (goal_status, parent["goal_id"]),
        )
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, payload_json, state_revision, idempotency_key
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                parent["id"],
                f"goal.{goal_status}",
                json.dumps(
                    {"goal_id": parent["goal_id"], "reason": reason},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                next_revision,
                f"goal-settle:{parent['goal_id']}:{goal_status}:{next_revision}",
            ),
        )

    def _get_with_connection(self, connection: Any, goal_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        tasks = connection.execute(
            """
            SELECT t.id, t.title, t.objective, t.status, t.role_id, t.worker_id,
                   t.work_item_kind, t.ordinal, t.depends_on_json, t.blocked_reason,
                   t.parent_task_id, t.revision, t.provider, t.created_at, t.updated_at,
                   t.last_error, t.last_evidence_hash, t.attempts_used, t.max_attempts,
                   t.tokens_used, t.sizing_json, t.execution_budget_json,
                   t.handoff_json, t.command_json, t.verification_json,
                   w.session_id, w.external_session_id, w.session_generation, w.last_error
                     AS worker_last_error, w.last_input_tokens,
                   w.last_cached_input_tokens, w.last_output_tokens,
                   w.last_uncached_input_tokens, w.last_context_pressure_tokens,
                   w.last_context_pressure_reason, w.last_attribution_granularity,
                   w.last_value_classification
            FROM tasks t
            LEFT JOIN workers w ON w.id = t.worker_id
            WHERE t.goal_id = ?
            ORDER BY CASE WHEN t.work_item_kind = 'coordination' THEN -1 ELSE t.ordinal END,
                     t.ordinal, t.created_at, t.id
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
                    "objective": task["objective"],
                    "status": task["status"],
                    "role_id": task["role_id"],
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
                    "verification": (
                        json.loads(task["verification_json"])
                        if task["verification_json"] else []
                    ),
                    "session_id": worker["session_id"] if worker else None,
                    "external_session_id": worker["external_session_id"] if worker else None,
                    "session_generation": worker["session_generation"] if worker else None,
                    "rotation_reason": rotation["reason"] if rotation else None,
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


def _child_command_and_verification(
    *,
    item: PlannedWorkItem,
    shared_command: dict[str, Any],
    shared_verification: list[dict[str, Any]],
    impl_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Each child gets its own acceptance surface; verification aggregates priors."""
    if item.kind == "verification":
        return dict(shared_command), list(shared_verification)

    # Implementation: prefer artifact acceptance when declared; else exit_code.
    if item.artifacts:
        artifact = item.artifacts[0]
        verification: list[dict[str, Any]] = [
            {"kind": "exit_code", "expected": 0},
            {"kind": "file_exists", "path": artifact},
        ]
    else:
        verification = [{"kind": "exit_code", "expected": 0}]

    # When a shared diagnostic argv is provided (tests / operator override), reuse it
    # for the first implementation slice only; later slices stay argv-null for CLI roles.
    if shared_command.get("argv") and (impl_count <= 1 or item.ordinal == 1):
        return dict(shared_command), (
            list(shared_verification)
            if shared_verification and impl_count <= 1
            else verification
        )
    return {
        "argv": None,
        "timeout_seconds": int(shared_command.get("timeout_seconds") or 60),
        "output_limit_bytes": int(shared_command.get("output_limit_bytes") or 131_072),
    }, verification


def _handoff_from_completed(connection: Any, task_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, title, last_evidence_hash, verification_json, work_item_kind, role_id
        FROM tasks WHERE id = ? AND status = 'completed'
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    verification = json.loads(row["verification_json"] or "[]")
    artifact_paths = [
        str(spec["path"])
        for spec in verification
        if isinstance(spec, dict)
        and spec.get("kind") in {"file_exists", "file_contains"}
        and spec.get("path")
    ]
    return {
        "from_task_id": row["id"],
        "from_title": row["title"],
        "from_kind": row["work_item_kind"],
        "evidence_hash": row["last_evidence_hash"],
        "artifact_paths": artifact_paths[:32],
    }
