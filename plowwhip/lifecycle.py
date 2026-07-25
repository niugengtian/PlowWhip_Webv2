from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from .artifact_contract import (
    register_artifact,
    task_data_scope,
    verified_artifact,
)
from .butler import sync_conversation_files
from .execution import (
    ProbeStep,
    ProviderStep,
    _context_policy,
    _fallback_provider_generation,
    _provider_output_streams,
    activate_task_session_roles,
    apply_probe_step,
    apply_provider_step,
    archive_task_sessions,
    create_task_session,
    create_task_sessions,
    current_session,
    effective_settings,
    ensure_task_sessions,
    execute_task,
    pending_probe_step,
    pending_provider_step,
    perform_probe_step,
    perform_provider_step,
    rotate_task_sessions,
)
from .intake import (
    _artifact_matches_declared_path,
    canonical_json,
    declared_step_count,
    extract_git_publish_spec,
    normalize_instruction,
)
from .lifecycle_state import (
    apply_model_budget_fact,
    finalize_task_terminal,
    lifecycle_write_scope,
    write_task_fields,
)
from .planner import (
    PlannerStep,
    instruction_facts,
    normalize_plan,
    parse_planner_result,
    perform_planner_step,
    planner_prompt,
)
from .provider import (
    ACTIVE_HOST_JOB_STATUSES,
    CHECKER_RESULT_PREFIX,
    model_budget_fact,
    record_model_call,
    selected_model,
)
from .store import Store, write_atomic as _write_atomic
from .verification import (
    CheckerStep,
    apply_checker_step,
    pending_checker_step,
    perform_checker_step,
    verify_task,
)

CONTINUE_DECISIONS = {"继续", "继续啊", "继续执行", "重试", "再试", "重新执行"}


class LeaseLost(RuntimeError):
    pass


def advance_project(store: Store, project_id: str, lease_token: str, fence: int) -> str:
    with lifecycle_write_scope("advance_project"):
        return _advance_project_owned(store, project_id, lease_token, fence)


def _advance_project_owned(
    store: Store, project_id: str, lease_token: str, fence: int
) -> str:
    result = _advance_project_transaction(store, project_id, lease_token, fence)
    if isinstance(result, (ProviderStep, ProbeStep, CheckerStep, PlannerStep)):
        with store.transaction() as connection:
            _assert_lease(connection, project_id, lease_token, fence)
            connection.execute(
                """
                UPDATE projects SET lease_until = ?
                WHERE id = ? AND lease_token = ? AND lease_fence = ?
                """,
                (
                    time.time()
                    + (
                        result.timeout_seconds + 30
                        if isinstance(result, (CheckerStep, PlannerStep, ProbeStep))
                        else 90
                    ),
                    project_id,
                    lease_token,
                    fence,
                ),
            )
        if isinstance(result, ProbeStep):
            facts = perform_probe_step(result)
        elif isinstance(result, CheckerStep):
            facts = perform_checker_step(result)
        elif isinstance(result, PlannerStep):
            facts = perform_planner_step(result)
        else:
            facts = perform_provider_step(result)
        with store.transaction() as connection:
            _assert_lease(connection, project_id, lease_token, fence)
            if isinstance(result, ProbeStep):
                outcome = apply_probe_step(store, connection, result, facts)
            elif isinstance(result, CheckerStep):
                outcome = apply_checker_step(store, connection, result, facts)
                if outcome == "planner_checker_pass":
                    outcome = _consume_checked_planner(
                        store, connection, result, time.time()
                    )
                elif outcome == "probe_checker_pass":
                    outcome = _complete_checked_model_probe(
                        store, connection, result, time.time()
                    )
            elif isinstance(result, PlannerStep):
                outcome = _apply_planner_step(store, connection, result, facts)
            else:
                outcome = apply_provider_step(store, connection, result, facts)
    else:
        outcome = result
    with store.transaction() as connection:
        _assert_lease(connection, project_id, lease_token, fence)
        _ensure_project_question(connection, project_id)
    sync_conversation_files(store, project_id)
    return outcome


def record_checkpoint_failure(
    store: Store, project_id: str, error: ValueError
) -> None:
    now = time.time()
    with lifecycle_write_scope("advance_project"):
        with store.transaction() as connection:
            task = connection.execute(
                """
                SELECT id FROM tasks
                WHERE project_id = ? AND outcome IS NULL
                ORDER BY created_at, rowid LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if not task:
                return
            detail = str(error)[:500]
            write_task_fields(
                connection,
                task["id"],
                {
                    "public_status": "needs_decision",
                    "phase": "provider_recovery",
                    "fault_code": "scope",
                    "wait_reason": detail,
                    "next_action_at": None,
                    "next_action_kind": None,
                    "updated_at": now,
                },
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    project_id, task_id, kind, detail_json, created_at
                ) VALUES (?, ?, 'checkpoint_failed', ?, ?)
                """,
                (
                    project_id,
                    task["id"],
                    canonical_json({"error": detail}),
                    now,
                ),
            )


def _assert_lease(
    connection: sqlite3.Connection, project_id: str, lease_token: str, fence: int
) -> None:
    lease = connection.execute(
        """
        SELECT 1 FROM projects
        WHERE id = ? AND lease_token = ? AND lease_fence = ? AND lease_until >= ?
        """,
        (project_id, lease_token, fence, time.time()),
    ).fetchone()
    if not lease:
        raise LeaseLost(project_id)


def _advance_project_transaction(
    store: Store, project_id: str, lease_token: str, fence: int
) -> str | ProviderStep | ProbeStep | CheckerStep | PlannerStep:
    """Perform exactly one lifecycle action. Cronner is the only caller with a lease."""
    with store.transaction() as connection:
        _assert_lease(connection, project_id, lease_token, fence)

        action = connection.execute(
            """
            SELECT * FROM messages
            WHERE project_id = ? AND processed_at IS NULL AND action_json IS NOT NULL
            ORDER BY created_at, rowid LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if action:
            action_kind = json.loads(action["action_json"]).get("kind")
            if action_kind in {
                "create_project",
                "restore_project",
                "bind_project_workspace",
                "archive_project",
                "set_project_setting",
                "set_project_rule",
                "promote_script",
                "global_route",
            }:
                return _apply_project_action(store, connection, action)
            return _apply_action(connection, action)

        task = connection.execute(
            """
            SELECT * FROM tasks
            WHERE project_id = ? AND outcome IS NULL
              AND public_status IN ('pending', 'in_progress')
              AND (
                next_action_at <= ?
                OR (deadline_at IS NOT NULL AND deadline_at <= ?)
              )
            ORDER BY created_at, rowid LIMIT 1
            """,
            (project_id, time.time(), time.time()),
        ).fetchone()
        if task:
            if (
                task["deadline_at"] is not None
                and task["deadline_at"] <= time.time()
                and task["phase"] not in {"timeout_reconcile", "stopping"}
            ):
                return _handle_task_deadline(connection, task)
            if task["phase"] == "plan":
                return _prepare_planner_step(connection, task)
            if task["phase"] == "execute":
                return execute_task(store, connection, task)
            if task["phase"] == "repair":
                return execute_task(store, connection, task, "repair")
            if task["phase"] == "verify":
                return verify_task(store, connection, task)
            if task["phase"] in {
                "execute_snapshot",
                "execute_dispatch",
                "execute_wait",
            }:
                return pending_provider_step(connection, task)
            if task["phase"] in {
                "probe_call",
                "probe_dispatch",
                "probe_wait",
            }:
                return pending_probe_step(connection, task)
            if task["phase"] == "timeout_reconcile":
                spec = json.loads(task["spec_json"])
                return (
                    pending_probe_step(connection, task)
                    if spec["kind"] == "provider_probe"
                    else pending_provider_step(connection, task)
                )
            if task["phase"] == "stopping":
                spec = json.loads(task["spec_json"])
                return (
                    pending_probe_step(connection, task)
                    if spec["kind"] == "provider_probe"
                    else pending_provider_step(connection, task)
                )
            if task["phase"] in {"check_call", "check_wait"}:
                return pending_checker_step(store, connection, task)
            if task["phase"] in {"plan_call", "plan_wait"}:
                return _pending_planner_step(connection, task)
            return _stop_unknown_phase(connection, task)

        blocking = connection.execute(
            """
            SELECT 1 FROM tasks
            WHERE project_id = ? AND outcome IS NULL
              AND public_status = 'needs_decision' LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if blocking:
            return "blocked"

        message = connection.execute(
            """
            SELECT * FROM messages
            WHERE project_id = ? AND processed_at IS NULL AND action_json IS NULL
            ORDER BY created_at, rowid LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if message:
            return _create_task(store, connection, message)

        broken = connection.execute(
            """
            SELECT queued.* FROM tasks queued
            WHERE queued.project_id = ? AND queued.outcome IS NULL
              AND queued.phase = 'queued'
              AND EXISTS (
                  SELECT 1 FROM task_dependencies edge
                  JOIN tasks dependency ON dependency.id = edge.depends_on_task_id
                  WHERE edge.task_id = queued.id AND dependency.outcome = 'cancelled'
              )
            ORDER BY queued.created_at, queued.rowid LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if broken:
            now = time.time()
            connection.execute(
                """
                UPDATE tasks SET public_status = 'needs_decision', phase = 'plan',
                    wait_reason = 'a required dependency was cancelled',
                    fault_code = 'scope', next_action_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, broken["id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
                VALUES (?, ?, 'dependency_blocked', '{}', ?)
                """,
                (project_id, broken["id"], now),
            )
            return "dependency_blocked"

        ready = connection.execute(
            """
            SELECT queued.* FROM tasks queued
            WHERE queued.project_id = ? AND queued.outcome IS NULL
              AND queued.phase = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM task_dependencies edge
                  JOIN tasks dependency ON dependency.id = edge.depends_on_task_id
                  WHERE edge.task_id = queued.id AND dependency.outcome IS NOT 'done'
              )
            ORDER BY queued.created_at, queued.rowid LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if ready:
            now = time.time()
            connection.execute(
                """
                UPDATE tasks SET phase = 'execute',
                    next_action_at = ? + COALESCE(
                        json_extract(spec_json, '$.earliest_start_delay_seconds'), 0
                    ),
                    next_action_kind = 'execute', updated_at = ? WHERE id = ?
                """,
                (now, now, ready["id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
                VALUES (?, ?, 'task_ready', '{}', ?)
                """,
                (project_id, ready["id"], now),
            )
            return "ready"

        return "idle"


def _create_task(
    store: Store, connection: sqlite3.Connection, message: sqlite3.Row
) -> str:
    now = time.time()
    connection.execute(
        "UPDATE projects SET archived_at = NULL WHERE id = ?",
        (message["project_id"],),
    )
    goal_id = uuid4().hex
    plan_id = uuid4().hex
    task_id = uuid4().hex
    normalized_candidate, acceptance = normalize_instruction(message["content"])
    facts = instruction_facts(
        message["content"], str(normalized_candidate["kind"])
    )
    project = connection.execute(
        "SELECT host_path FROM projects WHERE id = ?", (message["project_id"],)
    ).fetchone()
    project_path = str((project["host_path"] if project else "") or "")
    spec = {
        "kind": "planner_intake",
        "instruction": message["content"],
        "normalized_candidate": normalized_candidate,
        "input_facts": facts,
        "planner_workspace_key": message["project_id"],
        "project_path": project_path,
    }
    role_key = "planner"
    provider_key = _first_provider(connection, message["project_id"], role_key)
    checker_role = "independent_checker"

    connection.execute(
        """
        INSERT INTO goals(
            id, project_id, source_message_id, objective,
            boundary_json, acceptance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            goal_id,
            message["project_id"],
            message["id"],
            message["content"],
            canonical_json(
                {
                    "writes": (
                        project_path
                        if normalized_candidate["kind"]
                        in {"provider_task", "git_publish"}
                        else "task artifact directory only"
                    )
                }
            ),
            canonical_json(acceptance),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO plans(id, goal_id, revision, selected, summary_json, created_at)
        VALUES (?, ?, 1, ?, ?, ?)
        """,
        (
            plan_id,
            goal_id,
            0,
            canonical_json(
                {
                    "input_facts": facts,
                    "status": "planner_pending",
                }
            ),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO tasks(
            id, project_id, goal_id, spec_json, acceptance_json, public_status,
            phase, wait_reason, fault_code, next_action_at, outcome, created_at, updated_at,
            plan_id, sprint, role_key, checker_role_key, next_action_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                  ?, ?, ?)
        """,
        (
            task_id,
            message["project_id"],
            goal_id,
            canonical_json(spec),
            canonical_json(acceptance),
            "pending",
            "plan",
            None,
            None,
            now,
            None,
            now,
            now,
            plan_id,
            role_key,
            checker_role,
            "plan",
        ),
    )
    create_task_session(
        connection,
        message["project_id"],
        task_id,
        now,
        role_key,
        provider_key,
        False,
    )
    create_task_session(
        connection,
        message["project_id"],
        task_id,
        now,
        checker_role,
        _first_provider(connection, message["project_id"], checker_role),
        True,
    )
    connection.execute(
        "UPDATE messages SET action_json = ?, processed_at = ? WHERE id = ?",
        (canonical_json(spec), now, message["id"]),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            message["project_id"],
            task_id,
            "task_created",
            canonical_json(
                {
                    "source_message_id": message["id"],
                    "spec_revision": 1,
                    "input_facts": facts,
                }
            ),
            now,
        ),
    )
    return "intake"


def _apply_action(connection: sqlite3.Connection, message: sqlite3.Row) -> str:
    now = time.time()
    action = json.loads(message["action_json"])
    task = connection.execute(
        """
        SELECT * FROM tasks
        WHERE id = ? AND project_id = ?
        """,
        (action["task_id"], message["project_id"]),
    ).fetchone()
    connection.execute(
        "UPDATE messages SET processed_at = ? WHERE id = ?", (now, message["id"])
    )
    if not task:
        return "decision_rejected"

    kind = action["kind"]
    if kind == "confirm_not_executed" and task["outcome"] is None:
        job = connection.execute(
            """
            SELECT id FROM host_jobs
            WHERE id = ? AND task_id = ? AND status = 'dispatching'
            """,
            (action.get("host_job_id"), task["id"]),
        ).fetchone()
        if not (
            job
            and task["public_status"] == "needs_decision"
            and task["fault_code"] == "unsafe_unknown"
        ):
            event = "decision_rejected"
            detail = {
                "message_id": message["id"],
                "reason": "host_job_is_not_confirmable_as_unexecuted",
            }
        else:
            connection.execute(
                """
                UPDATE host_jobs SET status = 'failed', ended_at = ?,
                    returncode = 125, failure_code = 'not_accepted'
                WHERE id = ?
                """,
                (now, job["id"]),
            )
            finalize_task_terminal(
                connection, task["id"], "cancelled", now
            )
            event = "host_job_confirmed_not_executed"
            detail = {
                "message_id": message["id"],
                "host_job_id": job["id"],
                "next": "submit a corrected TaskSpec",
            }
    elif kind == "cancel" and task["outcome"] is None:
        active_jobs = connection.execute(
            """
            SELECT id, dispatch_json FROM host_jobs
            WHERE task_id = ?
              AND status IN ('dispatching', 'running', 'cancelling')
            ORDER BY sequence DESC
            """,
            (task["id"],),
        ).fetchall()
        if active_jobs:
            for active_job in active_jobs:
                dispatch = json.loads(active_job["dispatch_json"])
                dispatch.setdefault("stop_requested_at", now)
                dispatch["stop_reason"] = "owner_cancel"
                connection.execute(
                    """
                    UPDATE host_jobs SET status = 'cancelling',
                        dispatch_json = ? WHERE id = ?
                    """,
                    (canonical_json(dispatch), active_job["id"]),
                )
            connection.execute(
                """
                UPDATE tasks SET public_status = 'in_progress', phase = 'stopping',
                    next_action_at = ?, next_action_kind = 'cancel',
                    wait_reason = 'cancellation requested',
                    fault_code = NULL, updated_at = ? WHERE id = ?
                """,
                (now, now, task["id"]),
            )
            event, detail = "cancel_requested", {
                "message_id": message["id"],
                "host_job_ids": [job["id"] for job in active_jobs],
            }
        else:
            finalize_task_terminal(
                connection, task["id"], "cancelled", now
            )
            event, detail = "cancelled", {"message_id": message["id"]}
    elif kind == "rerun" and task["outcome"] == "cancelled":
        spec = json.loads(task["spec_json"])
        acceptance = json.loads(task["acceptance_json"])
        spec_changed = False
        if (
            task["plan_id"]
            and str(spec.get("instruction") or "").strip() in CONTINUE_DECISIONS
        ):
            created = connection.execute(
                """
                SELECT detail_json FROM task_events
                WHERE task_id = ? AND kind = 'task_created'
                ORDER BY created_at LIMIT 1
                """,
                (task["id"],),
            ).fetchone()
            planned = connection.execute(
                "SELECT summary_json FROM plans WHERE id = ?", (task["plan_id"],)
            ).fetchone()
            task_key = (
                str(json.loads(created["detail_json"]).get("task_key") or "")
                if created
                else ""
            )
            plan = json.loads(planned["summary_json"]) if planned else {}
            original = next(
                (
                    item
                    for item in plan.get("tasks", [])
                    if item.get("key") == task_key
                ),
                None,
            )
            if original:
                spec = dict(original["spec"])
                acceptance = list(original["acceptance"])
                spec_changed = True
        for revoked_key in (
            "authorization",
            "secret_ref",
            "secret_refs",
            "credential_ref",
            "credential_refs",
        ):
            if revoked_key in spec:
                spec.pop(revoked_key, None)
                spec_changed = True
        normalized_spec, _normalized_acceptance = normalize_instruction(
            str(spec.get("instruction") or "")
        )
        if (
            spec.get("kind") == "provider_task"
            and normalized_spec.get("kind") == "provider_task"
            and spec.get("workspace_change_required", True)
            and not normalized_spec.get("workspace_change_required", True)
        ):
            spec = {**spec, "workspace_change_required": False}
            spec_changed = True
        spec_revision = task["spec_revision"] + (1 if spec_changed else 0)
        planner_rerun = bool(
            task["role_key"] == "planner"
            and spec.get("kind") == "planner_intake"
        )
        if planner_rerun:
            supported = True
            reason = None
            role_key = "planner"
            provider_key = _first_provider(
                connection, task["project_id"], role_key
            )
            checker_role = task["checker_role_key"] or "independent_checker"
            checker_provider = _first_provider(
                connection, task["project_id"], checker_role
            )
        else:
            (
                supported,
                spec,
                role_key,
                provider_key,
                checker_role,
                checker_provider,
                reason,
            ) = _runtime_contract(connection, task["project_id"], spec)
        connection.execute(
            """
            UPDATE tasks SET public_status = 'pending', phase = 'queued',
                wait_reason = NULL, fault_code = NULL,
                next_action_at = NULL, next_action_kind = 'dependency',
                updated_at = ?
            WHERE project_id = ? AND outcome IS NULL
              AND public_status = 'needs_decision' AND phase = 'plan'
              AND wait_reason = 'a required dependency was cancelled'
              AND EXISTS (
                  SELECT 1 FROM task_dependencies edge
                  WHERE edge.task_id = tasks.id
                    AND edge.depends_on_task_id = ?
              )
            """,
            (now, task["project_id"], task["id"]),
        )
        if supported:
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role=role_key,
                checker_role=checker_role,
                executor_provider=provider_key,
                checker_provider=checker_provider,
            )
            if planner_rerun:
                activate_task_session_roles(
                    connection,
                    task["id"],
                    {role_key, checker_role},
                    now,
                )
            else:
                rotate_task_sessions(connection, task["id"], now)
        connection.execute(
            """
            UPDATE tasks SET public_status = ?, phase = ?, outcome = NULL,
                spec_json = ?, acceptance_json = ?, spec_revision = ?,
                retry_count = 0, next_retry_at = NULL,
                next_action_at = ?, next_action_kind = ?,
                wait_reason = ?, fault_code = ?,
                terminal_capabilities_revoked_at = NULL,
                updated_at = ? WHERE id = ?
            """,
            (
                "pending" if supported else "needs_decision",
                (
                    "plan"
                    if supported and planner_rerun
                    else "execute"
                    if supported
                    else "intake"
                ),
                canonical_json(spec),
                canonical_json(acceptance),
                spec_revision,
                now if supported else None,
                (
                    "plan"
                    if supported and planner_rerun
                    else "execute"
                    if supported
                    else None
                ),
                None if supported else reason,
                None if supported else "scope",
                now,
                task["id"],
            ),
        )
        event, detail = "rerun", {"message_id": message["id"]}
    elif (
        kind == "refresh_git_publish_context"
        and task["public_status"] == "needs_decision"
        and task["outcome"] is None
    ):
        spec = json.loads(task["spec_json"])
        revision = int(action.get("next_spec_revision") or 0)
        active_job = connection.execute(
            """
            SELECT 1 FROM host_jobs
            WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
            LIMIT 1
            """,
            (task["id"],),
        ).fetchone()
        valid = bool(
            spec.get("kind") == "git_publish"
            and not active_job
            and action.get("previous_spec_revision") == task["spec_revision"]
            and revision == task["spec_revision"] + 1
        )
        revised = {
            **spec,
            "operation": "inspect",
            "workspace_change_required": False,
        }
        revised.pop("authorization", None)
        revised.pop("expected_remote_head", None)
        revised.pop("publish_mode", None)
        (
            supported,
            revised,
            role_key,
            provider_key,
            checker_role,
            checker_provider,
            reason,
        ) = _runtime_contract(connection, task["project_id"], revised)
        if valid and supported:
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role=role_key,
                checker_role=checker_role,
                executor_provider=provider_key,
                checker_provider=checker_provider,
                settings_overrides={role_key: {"retry_count": 0}},
            )
            connection.execute(
                """
                UPDATE tasks SET spec_revision = ?, spec_json = ?,
                    public_status = 'pending', phase = 'execute',
                    wait_reason = NULL, fault_code = NULL, retry_count = 0,
                    next_retry_at = NULL, next_action_at = ?,
                    next_action_kind = 'execute', role_key = ?,
                    checker_role_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    revision,
                    canonical_json(revised),
                    now,
                    role_key,
                    checker_role,
                    now,
                    task["id"],
                ),
            )
            event = "git_publish_context_refresh_requested"
            detail = {
                "message_id": message["id"],
                "spec_revision": revision,
                "external_write": False,
            }
        else:
            event = "decision_rejected"
            detail = {
                "message_id": message["id"],
                "reason": reason or "stale_git_publish_context_refresh",
            }
    elif (
        kind in {"publish_new_branch", "force_publish_with_lease"}
        and task["public_status"] == "needs_decision"
        and task["outcome"] is None
    ):
        spec = json.loads(task["spec_json"])
        authorization = action.get("authorization")
        revised = {
            **spec,
            "operation": "publish",
            "workspace_change_required": True,
            "branch": action.get("branch"),
            "publish_mode": action.get("publish_mode"),
            "authorization": authorization,
        }
        if action.get("expected_remote_head"):
            revised["expected_remote_head"] = action["expected_remote_head"]
        else:
            revised.pop("expected_remote_head", None)
        (
            supported,
            revised,
            role_key,
            provider_key,
            checker_role,
            checker_provider,
            reason,
        ) = _runtime_contract(connection, task["project_id"], revised)
        revision = int(action.get("next_spec_revision") or 0)
        context_row = connection.execute(
            """
            SELECT detail_json FROM task_events
            WHERE id = ? AND task_id = ? AND kind = 'git_publish_needs_decision'
            """,
            (action.get("decision_context_event_id"), task["id"]),
        ).fetchone()
        context = json.loads(context_row["detail_json"]) if context_row else {}
        valid = bool(
            spec.get("kind") == "git_publish"
            and action.get("previous_spec_revision") == task["spec_revision"]
            and revision == task["spec_revision"] + 1
            and isinstance(authorization, dict)
            and authorization.get("spec_revision") == revision
            and authorization.get("source_decision_event_id")
            == action.get("decision_context_event_id")
            and authorization.get("expected_head") == context.get("local_head")
            and context.get("complete") is True
            and context.get("spec_revision") == task["spec_revision"]
            and kind in context.get("allowed_decisions", [])
            and (
                kind != "force_publish_with_lease"
                or action.get("expected_remote_head") == context.get("remote_head")
            )
        )
        if supported and valid:
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role=role_key,
                checker_role=checker_role,
                executor_provider=provider_key,
                checker_provider=checker_provider,
                settings_overrides={role_key: {"retry_count": 0}},
            )
            rotate_task_sessions(connection, task["id"], now)
            connection.execute(
                """
                UPDATE tasks SET spec_revision = ?, spec_json = ?,
                    public_status = 'pending', phase = 'execute',
                    wait_reason = NULL, fault_code = NULL, retry_count = 0,
                    next_retry_at = NULL, next_action_at = ?,
                    next_action_kind = 'execute', role_key = ?,
                    checker_role_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    revision,
                    canonical_json(revised),
                    now,
                    role_key,
                    checker_role,
                    now,
                    task["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    project_id, task_id, kind, detail_json, created_at
                ) VALUES (?, ?, 'authorization_granted', ?, ?)
                """,
                (
                    task["project_id"],
                    task["id"],
                    canonical_json(authorization),
                    now,
                ),
            )
            event = "git_publish_recovery_authorized"
            detail = {
                "message_id": message["id"],
                "spec_revision": revision,
                "publish_mode": action.get("publish_mode"),
                "branch": action.get("branch"),
                "expected_remote_head": action.get("expected_remote_head"),
            }
        else:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'scope',
                    updated_at = ? WHERE id = ?
                """,
                (
                    reason or "Git publish recovery authorization is stale",
                    now,
                    task["id"],
                ),
            )
            event = "decision_rejected"
            detail = {
                "message_id": message["id"],
                "reason": "stale_or_invalid_git_publish_recovery",
            }
    elif (
        kind == "provide_decision"
        and task["public_status"] == "needs_decision"
        and task["phase"] == "plan"
        and connection.execute(
            """
            SELECT 1 FROM plans
            WHERE goal_id = ? AND selected = 0 AND revision > 1 LIMIT 1
            """,
            (task["goal_id"],),
        ).fetchone()
    ):
        connection.execute(
            """
            UPDATE tasks SET wait_reason = ?,
                fault_code = 'scope', updated_at = ? WHERE id = ?
            """,
            (
                "Planner proposal is awaiting an explicit plan authorization or a replacement plan",
                now,
                task["id"],
            ),
        )
        event = "decision_rejected"
        detail = {"message_id": message["id"], "reason": "plan_authorization_required"}
    elif (
        kind == "provide_decision"
        and task["public_status"] == "needs_decision"
        and connection.execute(
            """
            SELECT 1 FROM host_jobs
            WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
            LIMIT 1
            """,
            (task["id"],),
        ).fetchone()
    ):
        connection.execute(
            """
            UPDATE tasks SET wait_reason = ?, fault_code = 'unsafe_unknown',
                updated_at = ? WHERE id = ?
            """,
            (
                "active HostJob outcome must be reconciled or cancelled before changing TaskSpec",
                now,
                task["id"],
            ),
        )
        event = "decision_rejected"
        detail = {"message_id": message["id"], "reason": "active_host_job"}
    elif (
        kind == "provide_decision"
        and task["public_status"] == "needs_decision"
        and task["phase"] == "provider_recovery"
        and action["instruction"].strip() in CONTINUE_DECISIONS
    ):
        spec = json.loads(task["spec_json"])
        (
            supported,
            spec,
            role_key,
            provider_key,
            checker_role,
            checker_provider,
            reason,
        ) = _runtime_contract(connection, task["project_id"], spec)
        if supported:
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role=role_key,
                checker_role=checker_role,
                executor_provider=provider_key,
                checker_provider=checker_provider,
            )
            rotate_task_sessions(connection, task["id"], now)
            connection.execute(
                """
                UPDATE tasks SET public_status = 'pending', phase = 'execute',
                    wait_reason = NULL, fault_code = NULL, retry_count = 0,
                    next_retry_at = NULL, next_action_at = ?,
                    next_action_kind = 'execute', updated_at = ?
                WHERE id = ?
                """,
                (now, now, task["id"]),
            )
            event = "decision_retry"
        else:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'scope',
                    updated_at = ? WHERE id = ?
                """,
                (reason, now, task["id"]),
            )
            event = "decision_rejected"
        detail = {"message_id": message["id"]}
    elif kind == "provide_decision" and task["public_status"] == "needs_decision":
        spec, acceptance = normalize_instruction(action["instruction"])
        (
            supported,
            spec,
            role_key,
            provider_key,
            checker_role,
            checker_provider,
            reason,
        ) = _runtime_contract(connection, task["project_id"], spec)
        if supported:
            revision = task["spec_revision"] + 1
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role=role_key,
                checker_role=checker_role,
                executor_provider=provider_key,
                checker_provider=checker_provider,
                settings_overrides=(
                    {role_key: {"retry_count": 0}}
                    if spec.get("mode") == "minimal"
                    else None
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET spec_revision = ?, spec_json = ?, acceptance_json = ?,
                    public_status = 'pending', phase = 'execute', wait_reason = NULL,
                    fault_code = NULL, retry_count = 0, next_retry_at = NULL,
                    next_action_at = ?, next_action_kind = 'execute',
                    outcome = NULL, role_key = ?, updated_at = ?
                    , checker_role_key = ?
                WHERE id = ?
                """,
                (
                    revision,
                    canonical_json(spec),
                    canonical_json(acceptance),
                    now,
                    role_key,
                    now,
                    checker_role,
                    task["id"],
                ),
            )
            event = "decision_applied"
            detail = {"message_id": message["id"], "spec_revision": revision}
        else:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'scope', updated_at = ?
                WHERE id = ?
                """,
                (
                    reason,
                    now,
                    task["id"],
                ),
            )
            event = "decision_rejected"
            detail = {"message_id": message["id"]}
    elif (
        kind == "wake"
        and task["outcome"] is None
        and task["public_status"] in {"pending", "in_progress"}
    ):
        connection.execute(
            """
            UPDATE tasks SET next_action_at = ?, next_action_kind = 'wake',
                updated_at = ? WHERE id = ?
            """,
            (now, now, task["id"]),
        )
        event, detail = "wake_requested", {"message_id": message["id"]}
    elif (
        kind == "authorize"
        and task["public_status"] == "needs_decision"
        and action.get("action_kind") == "git_publish"
    ):
        spec = json.loads(task["spec_json"])
        project = connection.execute(
            "SELECT host_path FROM projects WHERE id = ?", (task["project_id"],)
        ).fetchone()
        target_scope = (
            f"{spec.get('remote_ssh')}#refs/heads/{spec.get('branch')}"
        )
        valid = bool(
            spec.get("kind") == "git_publish"
            and project
            and project["host_path"]
            and action.get("spec_revision") == task["spec_revision"]
            and action.get("target_scope") == target_scope
            and float(action.get("expires_at") or 0) >= now
        )
        if valid:
            revision = task["spec_revision"] + 1
            authorization = {
                **action,
                "source_message_id": message["id"],
                "project_id": task["project_id"],
                "spec_revision": revision,
            }
            revised = {
                **spec,
                "operation": "publish",
                "workspace_change_required": True,
                "authorization": authorization,
            }
            revised.pop("expected_remote_head", None)
            revised.pop("publish_mode", None)
            ensure_task_sessions(
                connection,
                task["project_id"],
                task["id"],
                now,
                executor_role="git_publisher",
                checker_role="deterministic_checker",
                executor_provider="git_publish",
                checker_provider="local",
                settings_overrides={"git_publisher": {"retry_count": 0}},
            )
            rotate_task_sessions(connection, task["id"], now)
            connection.execute(
                """
                UPDATE tasks SET spec_revision = ?, spec_json = ?,
                    public_status = 'pending', phase = 'execute', outcome = NULL,
                    wait_reason = NULL, fault_code = NULL, retry_count = 0,
                    next_retry_at = NULL, next_action_at = ?,
                    next_action_kind = 'execute', role_key = 'git_publisher',
                    checker_role_key = 'deterministic_checker', updated_at = ?
                WHERE id = ?
                """,
                (
                    revision,
                    canonical_json(revised),
                    now,
                    now,
                    task["id"],
                ),
            )
            event = "authorization_granted"
            detail = authorization
        else:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'scope',
                    updated_at = ? WHERE id = ?
                """,
                ("Git publish authorization is stale or out of scope", now, task["id"]),
            )
            event = "authorization_rejected"
            detail = {"message_id": message["id"]}
    elif kind == "authorize" and task["public_status"] == "needs_decision":
        project = connection.execute(
            "SELECT host_path FROM projects WHERE id = ?", (task["project_id"],)
        ).fetchone()
        expected_scope = (
            project["host_path"] if project and project["host_path"]
            else f"project:{task['project_id']}"
        )
        valid = bool(
            action.get("action_kind") == "select_plan"
            and action.get("spec_revision") == task["spec_revision"]
            and action.get("target_scope") == expected_scope
            and float(action.get("expires_at") or 0) >= now
        )
        proposal = (
            connection.execute(
                """
                SELECT summary_json FROM plans
                WHERE id = ? AND goal_id = ? AND revision = ? AND selected = 0
                """,
                (
                    action.get("plan_id"),
                    task["goal_id"],
                    action.get("plan_revision"),
                ),
            ).fetchone()
            if valid
            else None
        )
        try:
            proposed = json.loads(proposal["summary_json"]) if proposal else None
            proposed_plan = proposed["plan"]
            normalized_plan = normalize_plan(
                proposed_plan,
                size=str(proposed_plan.get("size") or "large"),
                requires_owner_choice=bool(
                    proposed["classification"]["requires_owner_choice"]
                ),
            )
            normalized_plan["classification"] = proposed["classification"]
            plan = _materialize_plan(
                connection,
                task["project_id"],
                task["goal_id"],
                normalized_plan,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'scope',
                    updated_at = ? WHERE id = ?
                """,
                (f"authorization rejected: {error}", now, task["id"]),
            )
            event = "authorization_rejected"
            detail = {"message_id": message["id"]}
        else:
            _record_event(
                connection,
                task,
                "authorization_granted",
                {
                    "message_id": message["id"],
                    "spec_revision": action["spec_revision"],
                    "action_kind": action["action_kind"],
                    "target_scope": action["target_scope"],
                    "expires_at": action["expires_at"],
                },
                now,
            )
            _install_plan(connection, task, message["id"], plan, now)
            return kind
    elif kind == "provide_plan" and task["public_status"] == "needs_decision":
        try:
            provided_plan = action["plan"]
            plan = _materialize_plan(
                connection,
                task["project_id"],
                task["goal_id"],
                normalize_plan(
                    provided_plan,
                    size=str(provided_plan.get("size") or "large"),
                    requires_owner_choice=bool(
                        isinstance(provided_plan.get("selection"), dict)
                        and provided_plan["selection"].get("mode")
                        == "owner_required"
                    ),
                ),
            )
        except ValueError as error:
            connection.execute(
                "UPDATE tasks SET wait_reason = ?, fault_code = 'scope', updated_at = ? WHERE id = ?",
                (str(error), now, task["id"]),
            )
            event = "plan_rejected"
            detail = {"message_id": message["id"], "error": str(error)}
        else:
            _install_plan(connection, task, message["id"], plan, now)
            return kind
    else:
        event, detail = "action_rejected", {"message_id": message["id"], "kind": kind}
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (message["project_id"], task["id"], event, canonical_json(detail), now),
    )
    return kind


def _apply_project_action(
    store: Store, connection: sqlite3.Connection, message: sqlite3.Row
) -> str:
    now = time.time()
    action = json.loads(message["action_json"])
    kind = action["kind"]
    if kind in {"create_project", "restore_project", "bind_project_workspace"}:
        connection.execute(
            """
            UPDATE projects SET archived_at = NULL,
                display_name = COALESCE(?, display_name),
                host_path = COALESCE(?, host_path) WHERE id = ?
            """,
            (
                action.get("display_name"),
                action.get("host_path"),
                message["project_id"],
            ),
        )
    elif kind == "archive_project":
        if action.get("confirmation") != message["project_id"]:
            raise ValueError("archive confirmation no longer matches project")
        if connection.execute(
            "SELECT 1 FROM tasks WHERE project_id = ? AND outcome IS NULL LIMIT 1",
            (message["project_id"],),
        ).fetchone():
            raise ValueError("project acquired an active task before archive")
        connection.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?",
            (now, message["project_id"]),
        )
    elif kind == "set_project_setting":
        value = action["value"]
        if action["setting_key"] in {"provider_order", "provider_models"}:
            current = connection.execute(
                """
                SELECT value_json FROM settings
                WHERE setting_key = ?
                  AND (
                    (scope = 'project' AND project_id = ?)
                    OR (scope = 'global' AND project_id IS NULL)
                  )
                ORDER BY CASE scope WHEN 'project' THEN 0 ELSE 1 END LIMIT 1
                """,
                (action["setting_key"], message["project_id"]),
            ).fetchone()
            value = {
                **(json.loads(current["value_json"]) if current else {}),
                **value,
            }
        connection.execute(
            """
            INSERT INTO settings(
                id, scope, project_id, setting_key, value_json, source, updated_at
            ) VALUES (?, 'project', ?, ?, ?, ?, ?)
            ON CONFLICT(scope, project_id, setting_key) DO UPDATE SET
                value_json = excluded.value_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                f"project:{message['project_id']}:{action['setting_key']}",
                message["project_id"],
                action["setting_key"],
                canonical_json(value),
                f"owner_message:{message['id']}",
                now,
            ),
        )
    elif kind == "set_project_rule":
        revision = connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM library_items
            WHERE scope = 'project' AND project_id = ?
              AND kind = 'rule' AND item_key = ?
            """,
            (message["project_id"], action["rule_key"]),
        ).fetchone()["value"]
        body = str(action["content"]).encode()
        path = (
            store.data_root
            / "projects"
            / message["project_id"]
            / "library"
            / "rules"
            / f"{action['rule_key']}.revision-{revision:06d}.md"
        )
        _write_atomic(path, body)
        connection.execute(
            """
            INSERT INTO library_items(
                id, scope, project_id, kind, item_key, revision,
                path, sha256, created_at
            ) VALUES (?, 'project', ?, 'rule', ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                message["project_id"],
                action["rule_key"],
                revision,
                store.relative_data_path(path),
                hashlib.sha256(body).hexdigest(),
                now,
            ),
        )
    elif kind == "promote_script":
        task = connection.execute(
            """
            SELECT * FROM tasks
            WHERE id = ? AND project_id = ?
            """,
            (action["task_id"], message["project_id"]),
        ).fetchone()
        if (
            not task
            or task["outcome"] != "done"
            or task["public_status"] != "done"
            or task["spec_revision"] != action["spec_revision"]
        ):
            raise ValueError("script source Task is no longer a verified terminal revision")
        spec = json.loads(task["spec_json"])
        result = spec.get("task_contract", {}).get("result", {})
        script_contract = result.get("script_contract")
        artifact_contract = result.get("artifact")
        artifact = connection.execute(
            """
            SELECT * FROM artifacts
            WHERE id = ? AND task_id = ? AND project_id = ?
              AND kind = 'output' AND revision = ?
            """,
            (
                action["artifact_id"],
                task["id"],
                task["project_id"],
                task["spec_revision"],
            ),
        ).fetchone()
        if (
            not isinstance(script_contract, dict)
            or not isinstance(artifact_contract, dict)
            or not artifact
            or not _artifact_matches_declared_path(
                artifact["path"], artifact_contract.get("path")
            )
            or artifact["sha256"] != action["artifact_sha256"]
        ):
            raise ValueError("script Artifact no longer matches its frozen contract")
        artifact_ref, body = verified_artifact(
            store,
            artifact,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
        if not _has_passing_evidence(
            store,
            connection,
            task,
            set(script_contract["acceptance_ids"].values()),
        ):
            raise ValueError("script promotion requires complete current-revision Evidence")
        _validate_promotable_python(body, script_contract)
        revision = connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM library_items
            WHERE scope = 'project' AND project_id = ?
              AND kind = 'script' AND item_key = ?
            """,
            (message["project_id"], action["item_key"]),
        ).fetchone()["value"]
        path = (
            store.data_root
            / "projects"
            / message["project_id"]
            / "library"
            / "scripts"
            / f"{action['item_key']}.revision-{revision:06d}.py"
        )
        _write_atomic(path, body)
        library_id = uuid4().hex
        connection.execute(
            """
            INSERT INTO library_items(
                id, scope, project_id, kind, item_key, revision,
                path, sha256, source_task_id, source_artifact_id,
                source_revision, created_at
            ) VALUES (?, 'project', ?, 'script', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                library_id,
                message["project_id"],
                action["item_key"],
                revision,
                store.relative_data_path(path),
                artifact_ref["sha256"],
                task["id"],
                artifact["id"],
                task["spec_revision"],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events(
                project_id, task_id, kind, detail_json, created_at
            ) VALUES (?, ?, 'script_promoted', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "library_item_id": library_id,
                        "item_key": action["item_key"],
                        "revision": revision,
                        "path": store.relative_data_path(path),
                        "sha256": artifact_ref["sha256"],
                        "source_artifact_id": artifact["id"],
                        "source_revision": task["spec_revision"],
                    }
                ),
                now,
            ),
        )
    elif kind == "global_route":
        pass
    connection.execute(
        "UPDATE messages SET processed_at = ? WHERE id = ?", (now, message["id"])
    )
    return str(kind)


def _has_passing_evidence(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    expected_acceptance_ids: set[str],
) -> bool:
    rows = connection.execute(
        """
        SELECT * FROM artifacts
        WHERE task_id = ? AND kind = 'evidence' AND revision = ?
        ORDER BY created_at, rowid
        """,
        (task["id"], task["spec_revision"]),
    ).fetchall()
    if not rows:
        return False
    passed_ids: set[str] = set()
    for row in rows:
        _, body = verified_artifact(
            store,
            row,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
        try:
            evidence = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError("script Evidence is not valid JSON") from error
        if not isinstance(evidence, dict):
            continue
        if evidence.get("passed") is True and isinstance(
            evidence.get("acceptance_id"), str
        ):
            passed_ids.add(evidence["acceptance_id"])
        if evidence.get("verdict") == "PASS":
            for acceptance in evidence.get("acceptances", []):
                if (
                    isinstance(acceptance, dict)
                    and acceptance.get("passed") is True
                    and isinstance(acceptance.get("acceptance_id"), str)
                ):
                    passed_ids.add(acceptance["acceptance_id"])
    return expected_acceptance_ids <= passed_ids


def _validate_promotable_python(body: bytes, contract: dict) -> None:
    if len(body) > 1_048_576:
        raise ValueError("promotable script exceeds 1 MiB")
    try:
        source = body.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ValueError("promotable script must be valid UTF-8 Python") from error
    callable_name = contract.get("callable")
    cli_entry = contract.get("cli_entry")
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if (
        contract.get("language") != "python"
        or not isinstance(callable_name, str)
        or callable_name not in functions
        or cli_entry != "main"
        or cli_entry not in functions
        or callable_name == cli_entry
    ):
        raise ValueError("script callable and CLI entry do not match TaskSpec")
    main_guard = False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            continue
        for descendant in ast.walk(node):
            if (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Name)
                and descendant.func.id == "SystemExit"
                and descendant.args
                and isinstance(descendant.args[0], ast.Call)
                and isinstance(descendant.args[0].func, ast.Name)
                and descendant.args[0].func.id == cli_entry
            ):
                main_guard = True
                break
    if not main_guard:
        raise ValueError("script CLI must exit through SystemExit(main())")


def _prepare_planner_step(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> PlannerStep:
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    goal = connection.execute(
        "SELECT objective FROM goals WHERE id = ?", (task["goal_id"],)
    ).fetchone()
    task_session_id, session_generation = current_session(
        connection, task["id"], "planner"
    )
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?", (task_session_id,)
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    job_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'command', 'dispatching', ?, ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            started_at,
            canonical_json(
                {
                    "prompt": planner_prompt(
                        str(
                            goal["objective"]
                            if goal
                            else spec.get("instruction") or ""
                        ),
                        task["project_id"],
                        dict(spec.get("input_facts") or {}),
                    ),
                    "access": "read",
                    "workspace_kind": "planner",
                    "workspace_key": str(spec["planner_workspace_key"]),
                }
            ),
        ),
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'in_progress', phase = 'plan_call',
            wait_reason = NULL, fault_code = NULL, next_action_at = ?,
            next_action_kind = 'plan', updated_at = ?
        WHERE id = ?
        """,
        (started_at, started_at, task["id"]),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'planner_prepared', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json({"host_job_id": job_id, "sequence": sequence}),
            started_at,
        ),
    )
    input_facts = dict(spec.get("input_facts") or {})
    prompt = planner_prompt(
        str(goal["objective"] if goal else spec.get("instruction") or ""),
        task["project_id"],
        input_facts,
    )
    return PlannerStep(
        "start",
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        str(spec.get("project_path") or ""),
        str(spec["planner_workspace_key"]),
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        input_facts,
        _context_policy(settings),
        selected_model(settings, generation["provider_key"]),
    )


def _pending_planner_step(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> PlannerStep:
    job = connection.execute(
        """
        SELECT * FROM host_jobs
        WHERE task_id = ? AND purpose = 'command'
          AND status IN ('dispatching', 'running')
        ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    if not job:
        raise RuntimeError(f"Task {task['id']} has no active Planner HostJob")
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (job["task_session_id"], job["session_generation"]),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (job["task_session_id"],),
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    spec = json.loads(task["spec_json"])
    dispatch = json.loads(job["dispatch_json"])
    input_facts = dict(spec.get("input_facts") or {})
    return PlannerStep(
        "start" if job["status"] == "dispatching" else "poll",
        task["project_id"],
        task["id"],
        job["id"],
        generation["provider_key"],
        str(spec.get("project_path") or ""),
        str(spec["planner_workspace_key"]),
        str(dispatch["prompt"]),
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        input_facts,
        _context_policy(settings),
        selected_model(settings, generation["provider_key"]),
    )


def _apply_planner_step(
    store: Store,
    connection: sqlite3.Connection,
    step: PlannerStep,
    facts: dict[str, object],
) -> str:
    task = connection.execute(
        "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
        (step.task_id, step.project_id),
    ).fetchone()
    job = connection.execute(
        "SELECT * FROM host_jobs WHERE id = ? AND task_id = ?",
        (step.job_id, step.task_id),
    ).fetchone()
    if not task or not job or task["outcome"] is not None:
        return "stale_planner_fact"
    now = time.time()
    if not facts.get("ok"):
        if step.kind == "start" and facts.get("failure_kind") == "rejected":
            return _reject_planner_start(
                store, connection, task, job, step, facts, now
            )
        dispatch = json.loads(job["dispatch_json"])
        failures = int(dispatch.get("reconcile_failures") or 0) + 1
        dispatch["reconcile_failures"] = failures
        session = connection.execute(
            "SELECT settings_json FROM task_sessions WHERE id = ?",
            (job["task_session_id"],),
        ).fetchone()
        values = json.loads(session["settings_json"]).get("values", {})
        exhausted = failures > int(values.get("retry_count", 0))
        connection.execute(
            "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
            (canonical_json(dispatch), job["id"]),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = ?, phase = ?,
                wait_reason = ?, fault_code = ?, next_action_at = ?,
                next_action_kind = ?, updated_at = ? WHERE id = ?
            """,
            (
                "needs_decision" if exhausted else "in_progress",
                "provider_recovery" if exhausted else task["phase"],
                (
                    "Planner outcome is unknown; reconcile or cancel the HostJob"
                    if exhausted
                    else "Planner HostJob unavailable; idempotent reconcile scheduled"
                ),
                "unsafe_unknown" if exhausted else "transport",
                None if exhausted else now + max(
                    1, int(values.get("retry_backoff_seconds", 0))
                ),
                None if exhausted else (
                    "plan" if job["status"] == "dispatching" else "plan_poll"
                ),
                now,
                task["id"],
            ),
        )
        _record_event(
            connection,
            task,
            "host_job_reconcile_deferred",
            {
                "host_job_id": job["id"],
                "purpose": "command",
                "failures": failures,
            },
            now,
        )
        return "needs_decision" if exhausted else "plan_reconcile"
    state = facts.get("state")
    state = state if isinstance(state, dict) else {}
    if str(state.get("status")) in ACTIVE_HOST_JOB_STATUSES:
        connection.execute(
            "UPDATE host_jobs SET status = 'running' WHERE id = ?",
            (job["id"],),
        )
        if state.get("session_id"):
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (
                    state["session_id"],
                    job["task_session_id"],
                    job["session_generation"],
                ),
            )
        connection.execute(
            """
            UPDATE tasks SET phase = 'plan_wait', next_action_at = ?,
                next_action_kind = 'plan_poll', updated_at = ? WHERE id = ?
            """,
            (now + 1, now, task["id"]),
        )
        return "plan_wait"
    stdout, stderr = _provider_output_streams(facts.get("output"))
    input_tokens = max(0, int(state.get("input_tokens") or 0))
    result = {
        "returncode": (
            int(state["returncode"])
            if isinstance(state.get("returncode"), int)
            else 1
        ),
        "stdout": stdout,
        "stderr": stderr,
        "session_id": state.get("session_id"),
        "input_tokens": input_tokens,
        "cached_input_tokens": min(
            input_tokens, max(0, int(state.get("cached_input_tokens") or 0))
        ),
        "output_tokens": max(0, int(state.get("output_tokens") or 0)),
        "usage_kind": str(state.get("usage_kind") or "cumulative"),
        "model": state.get("model") or step.provider_key,
    }
    if isinstance(result, dict):
        if result.get("session_id"):
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (
                    result["session_id"],
                    job["task_session_id"],
                    job["session_generation"],
                ),
            )
        input_tokens = max(0, int(result.get("input_tokens") or 0))
        cached_tokens = min(
            input_tokens, max(0, int(result.get("cached_input_tokens") or 0))
        )
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            step.provider_key,
            str(result.get("usage_kind") or "cumulative"),
            input_tokens,
            cached_tokens,
            max(0, int(result.get("output_tokens") or 0)),
            str(result.get("model") or step.provider_key),
        )
        budget_fact = model_budget_fact(connection, task["id"])
        if budget_fact:
            apply_model_budget_fact(connection, task["id"], budget_fact)
            connection.execute(
                """
                UPDATE host_jobs SET status = 'succeeded', ended_at = ?,
                    returncode = ? WHERE id = ?
                """,
                (now, int(result.get("returncode") or 0), job["id"]),
            )
            return "needs_decision"
    returncode = int(result.get("returncode") or 0) if isinstance(result, dict) else 1
    stdout = str(result.get("stdout") or "") if isinstance(result, dict) else ""
    if str(state.get("status")) != "completed" or returncode != 0:
        connection.execute(
            """
            UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = ?,
                failure_code = ? WHERE id = ?
            """,
            (
                now,
                returncode,
                "provider",
                job["id"],
            ),
        )
        fallback = _fallback_provider_generation(
            store, connection, task, job, step.provider_key, now
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = ?, phase = ?, wait_reason = ?,
                fault_code = 'provider', next_action_at = ?,
                next_action_kind = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "in_progress" if fallback else "needs_decision",
                "plan" if fallback else "provider_recovery",
                (
                    f"Planner Provider failed; falling back to {fallback}"
                    if fallback
                    else "Planner did not return a usable result; all frozen candidates are exhausted"
                ),
                now if fallback else None,
                "plan" if fallback else None,
                now,
                task["id"],
            ),
        )
        if not fallback:
            _archive_role_generation(connection, task["id"], "planner", now)
        _record_event(
            connection,
            task,
            "planner_failed",
            {"host_job_id": job["id"], "error": state.get("failure_class")},
            now,
        )
        return "provider_fallback" if fallback else "needs_decision"

    try:
        proposal = parse_planner_result(stdout)
        if proposal["plan"] is not None:
            proposal["plan"] = _materialize_plan(
                connection, task["project_id"], task["goal_id"], proposal["plan"]
            )
    except ValueError as error:
        connection.execute(
            """
            UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = 1,
                failure_code = 'verification' WHERE id = ?
            """,
            (now, job["id"]),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'plan',
                wait_reason = ?, fault_code = 'verification',
                next_action_at = NULL, next_action_kind = NULL,
                updated_at = ? WHERE id = ?
            """,
            (f"Planner output is invalid: {error}", now, task["id"]),
        )
        _archive_role_generation(connection, task["id"], "planner", now)
        _record_event(
            connection,
            task,
            "planner_rejected",
            {"host_job_id": job["id"], "error": str(error)},
            now,
        )
        return "needs_decision"

    artifact_body = canonical_json(proposal).encode()
    artifact_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"plan-{job['sequence']:06d}"
        / "output"
        / "planner.json"
    )
    _write_atomic(artifact_path, artifact_body)
    artifact_ref = store.relative_data_path(artifact_path)
    register_artifact(
        store,
        connection,
        project_id=task["project_id"],
        task_id=task["id"],
        kind="output",
        path=artifact_path,
        stored_path=artifact_ref,
        acceptance_id="planner_contract",
        revision=task["spec_revision"],
        scope=task_data_scope(["planner_classification", "plan_contract"]),
        source_task_id=task["id"],
        created_at=now,
    )
    connection.execute(
        """
        UPDATE host_jobs SET status = 'succeeded', ended_at = ?, returncode = 0,
            output_ref = ?, failure_code = NULL WHERE id = ?
        """,
        (now, artifact_ref, job["id"]),
    )
    _archive_role_generation(connection, task["id"], "planner", now)
    return _prepare_planner_checker(
        connection,
        task,
        proposal,
        artifact_ref,
        hashlib.sha256(artifact_body).hexdigest(),
        job["id"],
        step.input_facts,
        now,
    )


def _prepare_planner_checker(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    proposal: dict,
    artifact_ref: str,
    artifact_sha256: str,
    planner_job_id: str,
    input_facts: dict[str, object],
    now: float,
) -> str:
    task_session_id, session_generation = current_session(
        connection, task["id"], "independent_checker"
    )
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id
        FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (task_session_id,),
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    sequence = connection.execute(
        """
        SELECT COALESCE(MAX(sequence), 0) + 1 AS value
        FROM host_jobs WHERE task_id = ?
        """,
        (task["id"],),
    ).fetchone()["value"]
    checker_job_id = uuid4().hex
    execution = {
        "subject": "planner",
        "artifact_ref": artifact_ref,
        "artifact_sha256": artifact_sha256,
        "artifact_revision": task["spec_revision"],
        "source_task_id": task["id"],
        "planner_job_id": planner_job_id,
        "input_facts": input_facts,
    }
    prompt = (
        "Independently check this parsed Planner Artifact. Do not read or trust "
        "Planner chat. Verify semantic sizing reasons, workflow gate, complete "
        "Plan alternatives, objective selection, atomic DAG, TaskSpec result/"
        "acceptance/dependency/runtime/authorization contracts, and that no "
        "scope was invented.\n"
        f"Task ID: {task['id']} · revision {task['spec_revision']}\n"
        f"Artifact: {artifact_ref} sha256={artifact_sha256}\n"
        f"Complete parsed Artifact:\n{canonical_json(proposal)}\n"
        f"Finish with {CHECKER_RESULT_PREFIX!r} followed by "
        '{"verdict":"PASS|CHANGES_REQUIRED|NEEDS_DECISION",'
        '"acceptances":[{"acceptance_id":"planner_contract",'
        '"passed":true,"actual_evidence":"bounded independent fact",'
        '"recheck_command":"bounded recheck"}],"decision_reason":null}. '
        "Do not modify files or create external effects."
    )
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'dispatching', ?, ?)
        """,
        (
            checker_job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            now,
            canonical_json(
                {
                    "prompt": prompt,
                    "access": "read",
                    "workspace_kind": "planner",
                    "workspace_key": json.loads(task["spec_json"])[
                        "planner_workspace_key"
                    ],
                    "check_subject": "planner",
                    "execution": execution,
                }
            ),
        ),
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress",
            "phase": "check_call",
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": now,
            "next_action_kind": "plan_check",
            "updated_at": now,
        },
    )
    _record_event(
        connection,
        task,
        "planner_checker_prepared",
        {
            "host_job_id": checker_job_id,
            "planner_job_id": planner_job_id,
            "artifact_ref": artifact_ref,
            "artifact_sha256": artifact_sha256,
        },
        now,
    )
    return "planner_check"


def _apply_checked_planner_proposal(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    proposal: dict,
    planner_job_id: str,
    input_facts: dict[str, object],
    now: float,
) -> str:
    if proposal["plan"] is None:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "plan",
                "wait_reason": proposal["blocking_reason"],
                "fault_code": "scope",
                "next_action_at": None,
                "next_action_kind": None,
                "updated_at": now,
            },
        )
        _record_event(
            connection,
            task,
            "planner_needs_information",
            {
                "host_job_id": planner_job_id,
                "classification": proposal["classification"],
                "blocking_reason": proposal["blocking_reason"],
            },
            now,
        )
        return "needs_decision"
    auto_select = bool(
        proposal["confidence"] >= 0.95
        and not proposal["classification"]["requires_owner_choice"]
        and not input_facts.get("possible_high_risk_terms")
    )
    if auto_select:
        _install_plan(
            connection,
            task,
            f"planner:{planner_job_id}",
            proposal["plan"],
            now,
        )
        return "plan_applied"

    revision = connection.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM plans WHERE goal_id = ?",
        (task["goal_id"],),
    ).fetchone()["value"]
    connection.execute(
        """
        INSERT INTO plans(id, goal_id, revision, selected, summary_json, created_at)
        VALUES (?, ?, ?, 0, ?, ?)
        """,
        (
            uuid4().hex,
            task["goal_id"],
            revision,
            canonical_json(proposal),
            now,
        ),
    )
    selected = proposal["plan"]["alternatives"][proposal["plan"]["selected"]]
    question = (
        f"Planner 建议“{selected['name']}”，置信度 {proposal['confidence']:.2f}；"
        "这是高风险操作，是否批准该方案？"
        if (
            proposal["classification"]["requires_owner_choice"]
            or input_facts.get("possible_high_risk_terms")
        )
        else (
            f"Planner 建议“{selected['name']}”，但置信度只有 "
            f"{proposal['confidence']:.2f}；是否按该方案继续？"
        )
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'needs_decision', phase = 'plan',
            wait_reason = ?, fault_code = 'scope', next_action_at = NULL,
            next_action_kind = NULL,
            updated_at = ? WHERE id = ?
        """,
        (question, now, task["id"]),
    )
    _record_event(
        connection,
        task,
        "planner_needs_decision",
        {
            "host_job_id": planner_job_id,
            "plan_revision": revision,
            "confidence": proposal["confidence"],
        },
        now,
    )
    return "needs_decision"


def _complete_checked_model_probe(
    store: Store,
    connection: sqlite3.Connection,
    step: CheckerStep,
    now: float,
) -> str:
    task = connection.execute(
        "SELECT * FROM tasks WHERE id = ?",
        (step.task_id,),
    ).fetchone()
    if not task:
        raise RuntimeError("checked model probe Task disappeared")
    spec = json.loads(task["spec_json"])
    source = step.execution
    output = connection.execute(
        """
        SELECT * FROM artifacts
        WHERE task_id = ? AND kind = 'output'
          AND acceptance_id = 'provider_minimal_probe'
          AND revision = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task["id"], task["spec_revision"]),
    ).fetchone()
    checker_evidence = connection.execute(
        """
        SELECT * FROM artifacts
        WHERE task_id = ? AND kind = 'evidence'
          AND acceptance_id = 'provider_minimal_probe'
          AND revision = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task["id"], task["spec_revision"]),
    ).fetchone()
    try:
        if (
            spec.get("kind") != "provider_probe"
            or spec.get("mode") != "minimal"
            or not output
            or not checker_evidence
        ):
            raise ValueError("minimal probe result or Checker Evidence is missing")
        output_ref, output_body = verified_artifact(
            store,
            output,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
        evidence_ref, evidence_body = verified_artifact(
            store,
            checker_evidence,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
        probe_result = json.loads(output_body)
        evidence = json.loads(evidence_body)
        if (
            source.get("subject") != "model_probe"
            or source.get("source_task_id") != task["id"]
            or int(source.get("source_revision") or -1)
            != task["spec_revision"]
            or source.get("artifact_ref") != output_ref
            or source.get("probe_result") != probe_result
            or evidence.get("subject") != "model_probe"
            or evidence.get("source_artifact") != source
            or evidence.get("passed") is not True
            or evidence.get("status") != "PASS"
        ):
            raise ValueError(
                "minimal probe and independent Checker lineage do not match"
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "verify",
                "wait_reason": str(error),
                "fault_code": "verification",
                "next_action_at": None,
                "next_action_kind": None,
                "updated_at": now,
            },
        )
        _record_event(
            connection,
            task,
            "model_probe_checker_lineage_failed",
            {"reason": str(error)},
            now,
        )
        return "needs_decision"
    finalize_task_terminal(connection, task["id"], "done", now)
    _record_event(
        connection,
        task,
        "verified",
        {
            "acceptance_id": "provider_minimal_probe",
            "source_artifact": output_ref,
            "checker_evidence": evidence_ref,
            "passed": True,
        },
        now,
    )
    return "verify"


def _consume_checked_planner(
    store: Store,
    connection: sqlite3.Connection,
    step: CheckerStep,
    now: float,
) -> str:
    execution = step.execution
    artifact = connection.execute(
        """
        SELECT kind, path, sha256, bytes, acceptance_id, revision,
               scope_json, source_task_id
        FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND path = ? AND revision = ?
          AND acceptance_id = 'planner_contract'
        ORDER BY rowid DESC LIMIT 1
        """,
        (
            step.task_id,
            execution.get("artifact_ref"),
            execution.get("artifact_revision"),
        ),
    ).fetchone()
    task = connection.execute(
        "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
        (step.task_id, step.project_id),
    ).fetchone()
    if not task or not artifact:
        raise RuntimeError("checked Planner Artifact is missing")
    entry, body = verified_artifact(
        store,
        artifact,
        expected_source_task_id=task["id"],
        expected_revision=task["spec_revision"],
    )
    if (
        entry["sha256"] != execution.get("artifact_sha256")
        or entry["revision"] != execution.get("artifact_revision")
    ):
        raise ValueError("checked Planner Artifact lineage changed")
    try:
        proposal = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError("checked Planner Artifact is invalid JSON") from error
    if not isinstance(proposal, dict):
        raise ValueError("checked Planner Artifact is not an object")
    return _apply_checked_planner_proposal(
        connection,
        task,
        proposal,
        str(execution["planner_job_id"]),
        dict(execution.get("input_facts") or {}),
        now,
    )


def _reject_planner_start(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    step: PlannerStep,
    facts: dict[str, object],
    now: float,
) -> str:
    dispatch = json.loads(job["dispatch_json"])
    dispatch["rejection"] = {
        "status": facts.get("error_status"),
        "detail": str(facts.get("error_detail") or "")[:500],
    }
    connection.execute(
        """
        UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = 125,
            failure_code = 'rejected', dispatch_json = ? WHERE id = ?
        """,
        (now, canonical_json(dispatch), job["id"]),
    )
    fallback = _fallback_provider_generation(
        store, connection, task, job, step.provider_key, now
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = ?, phase = ?, wait_reason = ?,
            fault_code = 'provider', next_action_at = ?, next_action_kind = ?,
            updated_at = ? WHERE id = ?
        """,
        (
            "in_progress" if fallback else "needs_decision",
            "plan" if fallback else "provider_recovery",
            (
                f"Planner start was rejected; falling back to {fallback}"
                if fallback
                else "Host Bridge rejected Planner before acceptance"
            ),
            now if fallback else None,
            "plan" if fallback else None,
            now,
            task["id"],
        ),
    )
    _record_event(
        connection,
        task,
        "host_job_rejected",
        {
            "host_job_id": job["id"],
            "provider_key": step.provider_key,
            "http_status": facts.get("error_status"),
            "fallback": fallback,
        },
        now,
    )
    return "provider_fallback" if fallback else "needs_decision"


def _materialize_plan(
    connection: sqlite3.Connection, project_id: str, goal_id: str, plan: dict
) -> dict:
    source = connection.execute(
        """
        SELECT message.id, message.content
        FROM goals goal JOIN messages message ON message.id = goal.source_message_id
        WHERE goal.id = ?
        """,
        (goal_id,),
    ).fetchone()
    explicit_git = (
        extract_git_publish_spec(str(source["content"])) if source else None
    )
    source_content = str(source["content"]) if source else ""
    tasks = []
    for item in plan["tasks"]:
        if item["spec"]["kind"] == "git_publish":
            spec = item["spec"]
            if not (
                explicit_git
                and spec.get("remote_ssh") == explicit_git["remote_ssh"]
                and spec.get("branch") == explicit_git["branch"]
            ):
                raise ValueError(
                    f"task {item['key']} Git target was not explicitly authorized"
                )
            project = connection.execute(
                "SELECT host_path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            project_path = str((project["host_path"] if project else "") or "")
            supported = bool(project_path)
            spec = {**spec, "project_path": project_path}
            role_key, provider_key = "git_publisher", "git_publish"
            checker_role, checker_provider = "deterministic_checker", "local"
            reason = (
                None
                if supported
                else "project workspace is not bound; set an absolute Host Bridge path"
            )
        else:
            (
                supported,
                spec,
                role_key,
                provider_key,
                checker_role,
                checker_provider,
                reason,
            ) = _runtime_contract(connection, project_id, item["spec"])
        if not supported:
            raise ValueError(f"task {item['key']} cannot run: {reason}")
        settings = item["settings"]
        if spec.get("butler_semantic_query"):
            settings = {
                role: dict(values) for role, values in settings.items()
            }
            settings.setdefault(role_key, {})["provider_order"] = (
                _read_model_order(connection, project_id, role_key)
            )
            settings.setdefault(checker_role, {})["provider_order"] = (
                _read_model_order(connection, project_id, checker_role)
            )
        executor_order = settings.get(role_key, {}).get("provider_order")
        checker_order = settings.get(checker_role, {}).get("provider_order")
        tasks.append(
            {
                **item,
                "spec": {
                    **spec,
                    "earliest_start_delay_seconds": item[
                        "earliest_start_delay_seconds"
                    ],
                },
                "role_key": role_key,
                "checker_role": checker_role,
                "executor_provider": (
                    str(executor_order[0]) if executor_order else provider_key
                ),
                "checker_provider": (
                    str(checker_order[0]) if checker_order else checker_provider
                ),
            }
        )
    explicit_three_provider_flow = bool(
        explicit_git
        and declared_step_count(source_content) == 3
        and "cursor" in source_content.lower()
        and "codex" in source_content.lower()
    )
    if explicit_three_provider_flow:
        expected_providers = ["git_publish", "cursor_cli", "codex_cli"]
        if (
            len(tasks) != 3
            or [item["executor_provider"] for item in tasks]
            != expected_providers
            or tasks[0]["depends_on"]
            or tasks[1]["depends_on"] != [tasks[0]["key"]]
            or tasks[2]["depends_on"] != [tasks[1]["key"]]
        ):
            raise ValueError(
                "explicit Git/Cursor/Codex workflow requires exactly three serial tasks"
            )
        repair_acceptance = tasks[2]["acceptance"]
        if not any(
            item["id"] == "review-findings-disposition"
            for item in repair_acceptance
        ):
            if len(repair_acceptance) >= 20:
                raise ValueError(
                    "Codex repair task has no room for findings disposition acceptance"
                )
            repair_acceptance.append(
                {
                    "id": "review-findings-disposition",
                    "kind": "planner_acceptance",
                    "expected": (
                        "逐项列出 Cursor 审查的每个编号发现，并给出已修复、"
                        "不适用或明确延期的结论及代码、测试或事实依据；"
                        "不得遗漏 Critical 或 High 发现"
                    ),
                }
            )
    return {**plan, "tasks": tasks}


def _archive_role_generation(
    connection: sqlite3.Connection, task_id: str, role_key: str, now: float
) -> None:
    connection.execute(
        """
        UPDATE session_generations SET status = 'archived', ended_at = ?
        WHERE status = 'active' AND task_session_id IN (
            SELECT id FROM task_sessions WHERE task_id = ? AND role_key = ?
        )
        """,
        (now, task_id, role_key),
    )


def _record_event(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    kind: str,
    detail: dict,
    now: float,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            kind,
            canonical_json(detail),
            now,
        ),
    )


def _install_plan(
    connection: sqlite3.Connection,
    placeholder: sqlite3.Row,
    message_id: str,
    plan: dict,
    now: float,
) -> None:
    revision = connection.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM plans WHERE goal_id = ?",
        (placeholder["goal_id"],),
    ).fetchone()["value"]
    plan_id = uuid4().hex
    connection.execute("UPDATE plans SET selected = 0 WHERE goal_id = ?", (placeholder["goal_id"],))
    connection.execute(
        """
        INSERT INTO plans(id, goal_id, revision, selected, summary_json, created_at)
        VALUES (?, ?, ?, 1, ?, ?)
        """,
        (plan_id, placeholder["goal_id"], revision, canonical_json(plan), now),
    )
    connection.execute(
        "UPDATE goals SET spec_revision = spec_revision + 1 WHERE id = ?",
        (placeholder["goal_id"],),
    )

    source = connection.execute(
        "SELECT source_message_id FROM goals WHERE id = ?",
        (placeholder["goal_id"],),
    ).fetchone()
    source_message_id = str(source["source_message_id"]) if source else ""
    task_ids = {item["key"]: uuid4().hex for item in plan["tasks"]}
    first = plan["tasks"][0]
    task_ids[first["key"]] = placeholder["id"]
    first_spec = _bind_planned_git_authorization(
        first["spec"],
        source_message_id,
        placeholder["project_id"],
        placeholder["id"],
        int(placeholder["spec_revision"]) + 1,
        now,
    )
    connection.execute(
        """
        UPDATE tasks SET plan_id = ?, spec_revision = spec_revision + 1,
            spec_json = ?, acceptance_json = ?, public_status = 'pending',
            phase = 'execute', wait_reason = NULL, fault_code = NULL,
            retry_count = 0, next_retry_at = NULL, next_action_at = ?,
            next_action_kind = 'execute', deadline_at = ?,
            outcome = NULL, sprint = ?, role_key = ?,
            checker_role_key = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            plan_id,
            canonical_json(first_spec),
            canonical_json(first["acceptance"]),
            now + first["earliest_start_delay_seconds"],
            (
                now + first["deadline_seconds"]
                if first["deadline_seconds"] is not None
                else None
            ),
            first["sprint"],
            first["role_key"],
            first["checker_role"],
            now,
            placeholder["id"],
        ),
    )
    ensure_task_sessions(
        connection,
        placeholder["project_id"],
        placeholder["id"],
        now,
        executor_role=first["role_key"],
        checker_role=first["checker_role"],
        executor_provider=first["executor_provider"],
        checker_provider=first["checker_provider"],
        settings_overrides=first["settings"],
    )
    activate_task_session_roles(
        connection,
        placeholder["id"],
        {first["role_key"], first["checker_role"]},
        now,
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'plan_applied', ?, ?)
        """,
        (
            placeholder["project_id"],
            placeholder["id"],
            canonical_json({"message_id": message_id, "plan_revision": revision}),
            now,
        ),
    )
    if first_spec["kind"] == "git_publish":
        _record_event(
            connection,
            placeholder,
            "authorization_granted",
            first_spec["authorization"],
            now,
        )
    for item in plan["tasks"][1:]:
        task_id = task_ids[item["key"]]
        item_spec = _bind_planned_git_authorization(
            item["spec"],
            source_message_id,
            placeholder["project_id"],
            task_id,
            1,
            now,
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, project_id, goal_id, spec_json, acceptance_json, public_status,
                phase, next_action_at, next_action_kind, deadline_at,
                created_at, updated_at, plan_id, sprint,
                role_key, checker_role_key
            ) VALUES (?, ?, ?, ?, ?, 'pending', 'queued', NULL, 'dependency',
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                placeholder["project_id"],
                placeholder["goal_id"],
                canonical_json(item_spec),
                canonical_json(item["acceptance"]),
                (
                    now + item["deadline_seconds"]
                    if item["deadline_seconds"] is not None
                    else None
                ),
                now,
                now,
                plan_id,
                item["sprint"],
                item["role_key"],
                item["checker_role"],
            ),
        )
        create_task_sessions(
            connection,
            placeholder["project_id"],
            task_id,
            now,
            executor_role=item["role_key"],
            checker_role=item["checker_role"],
            executor_provider=item["executor_provider"],
            checker_provider=item["checker_provider"],
            settings_overrides=item["settings"],
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'task_created', ?, ?)
            """,
            (
                placeholder["project_id"],
                task_id,
                canonical_json({"plan_revision": revision, "task_key": item["key"]}),
                now,
            ),
        )
        if item_spec["kind"] == "git_publish":
            connection.execute(
                """
                INSERT INTO task_events(
                    project_id, task_id, kind, detail_json, created_at
                ) VALUES (?, ?, 'authorization_granted', ?, ?)
                """,
                (
                    placeholder["project_id"],
                    task_id,
                    canonical_json(item_spec["authorization"]),
                    now,
                ),
            )
    for item in plan["tasks"]:
        for dependency in item["depends_on"]:
            connection.execute(
                "INSERT INTO task_dependencies(task_id, depends_on_task_id) VALUES (?, ?)",
                (task_ids[item["key"]], task_ids[dependency]),
            )


def _bind_planned_git_authorization(
    spec: dict,
    source_message_id: str,
    project_id: str,
    task_id: str,
    spec_revision: int,
    now: float,
) -> dict:
    if spec["kind"] != "git_publish":
        return spec
    target_scope = f"{spec['remote_ssh']}#refs/heads/{spec['branch']}"
    return {
        **spec,
        "authorization": {
            "source_message_id": source_message_id,
            "project_id": project_id,
            "task_id": task_id,
            "spec_revision": spec_revision,
            "action_kind": "git_publish",
            "target_scope": target_scope,
            "expires_at": now + 900,
        },
    }


def _handle_task_deadline(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> str:
    now = time.time()
    active = connection.execute(
        """
        SELECT * FROM host_jobs
        WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
        ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    if active:
        dispatch = json.loads(active["dispatch_json"])
        dispatch.setdefault("deadline_detected_at", now)
        dispatch["timeout_stage"] = "reconcile"
        connection.execute(
            """
            UPDATE host_jobs SET dispatch_json = ?
            WHERE id = ?
            """,
            (canonical_json(dispatch), active["id"]),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'in_progress',
                phase = 'timeout_reconcile',
                wait_reason = '[deadline] reconcile and checkpoint required before stop',
                fault_code = 'process', next_action_at = ?,
                next_action_kind = 'reconcile_stop', updated_at = ? WHERE id = ?
            """,
            (now, now, task["id"]),
        )
        event = "deadline_reconcile_requested"
        detail = {"host_job_id": active["id"], "deadline_at": task["deadline_at"]}
        result = "deadline_reconcile"
    else:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision',
                phase = 'provider_recovery',
                wait_reason = '[deadline] reached with no active HostJob to reconcile',
                fault_code = 'process', next_action_at = NULL,
                next_action_kind = NULL, updated_at = ? WHERE id = ?
            """,
            (now, task["id"]),
        )
        event = "deadline_reached"
        detail = {"deadline_at": task["deadline_at"]}
        result = "needs_decision"
    _record_event(connection, task, event, detail, now)
    return result


def _stop_unknown_phase(connection: sqlite3.Connection, task: sqlite3.Row) -> str:
    now = time.time()
    connection.execute(
        """
        UPDATE tasks SET public_status = 'needs_decision', wait_reason = ?,
            fault_code = 'unsafe_unknown', next_action_at = NULL,
            outcome = NULL, updated_at = ? WHERE id = ?
        """,
        (f"unknown lifecycle phase: {task['phase']}", now, task["id"]),
    )
    return "needs_decision"


def _ensure_project_question(
    connection: sqlite3.Connection, project_id: str
) -> None:
    del project_id
    now = time.time()
    question_rows = connection.execute(
        """
        SELECT message.id, message.action_json, message.created_at,
               task.id AS task_id, task.outcome AS task_outcome,
               task.public_status AS task_public_status
        FROM messages message
        JOIN tasks task
          ON task.id = json_extract(message.action_json, '$.task_id')
        WHERE message.role = 'butler'
          AND json_extract(message.action_json, '$.kind') = 'question'
        ORDER BY message.created_at, message.rowid
        """
    ).fetchall()
    active_questions = []
    for row in question_rows:
        action = json.loads(row["action_json"])
        if (
            row["task_outcome"] is None
            and row["task_public_status"] == "needs_decision"
        ):
            active_questions.append(row)
        elif action.get("waiting", True) is not False:
            action["waiting"] = False
            action["resolved_at"] = now
            connection.execute(
                "UPDATE messages SET action_json = ? WHERE id = ?",
                (canonical_json(action), row["id"]),
            )
    waiting = []
    queued = []
    for row in active_questions:
        action = json.loads(row["action_json"])
        (waiting if action.get("waiting", True) is not False else queued).append(
            (row, action)
        )
    if waiting:
        for row, action in waiting[1:]:
            action["waiting"] = False
            action["queued_behind_task_id"] = waiting[0][0]["task_id"]
            connection.execute(
                "UPDATE messages SET action_json = ? WHERE id = ?",
                (canonical_json(action), row["id"]),
            )
        return
    if queued:
        row, action = queued[0]
        action["waiting"] = True
        action.pop("queued_behind_task_id", None)
        connection.execute(
            "UPDATE messages SET action_json = ? WHERE id = ?",
            (canonical_json(action), row["id"]),
        )
        return
    task = connection.execute(
        """
        SELECT id, project_id, spec_revision, phase, fault_code, wait_reason
        FROM tasks
        WHERE outcome IS NULL AND public_status = 'needs_decision'
        ORDER BY updated_at, rowid LIMIT 1
        """
    ).fetchone()
    if not task:
        return
    reason = str(task["wait_reason"] or "请确认下一步")
    fingerprint = hashlib.sha256(
        f"{task['spec_revision']}:{task['phase']}:{task['fault_code']}:{reason}".encode()
    ).hexdigest()[:16]
    connection.execute(
        """
        INSERT OR IGNORE INTO messages(
            id, project_id, role, content, action_json,
            idempotency_key, created_at, processed_at
        ) VALUES (?, ?, 'butler', ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task["project_id"],
            f"我现在只需要你决定一件事：{reason}",
            canonical_json(
                {
                    "kind": "question",
                    "task_id": task["id"],
                    "spec_revision": task["spec_revision"],
                    "waiting": True,
                }
            ),
            f"question:{task['id']}:{fingerprint}",
            now,
            now,
        ),
    )


def _runtime_contract(
    connection: sqlite3.Connection,
    project_id: str,
    spec: dict,
) -> tuple[bool, dict, str, str, str, str, str | None]:
    kind = spec["kind"]
    if kind == "write_text":
        return (
            True,
            spec,
            "deterministic",
            "local",
            "deterministic_checker",
            "local",
            None,
        )
    if kind == "provider_probe":
        provider = str(spec["provider_key"])
        checker_role = (
            "independent_checker"
            if spec["mode"] == "minimal"
            else "deterministic_checker"
        )
        checker_provider = (
            _first_provider(connection, project_id, checker_role)
            if checker_role == "independent_checker"
            else "local"
        )
        return (
            True,
            spec,
            "provider_probe",
            provider,
            checker_role,
            checker_provider,
            None,
        )
    if kind == "provider_task":
        if spec.get("butler_semantic_query"):
            worker_order = _read_model_order(
                connection, project_id, "fullstack"
            )
            requested = spec.get("provider_key")
            provider = (
                str(requested)
                if requested in worker_order
                else worker_order[0]
            )
            checker_provider = _read_model_order(
                connection, project_id, "independent_checker"
            )[0]
            workspace_key = "butler-" + hashlib.sha256(
                canonical_json(spec["butler_semantic_query"]).encode()
            ).hexdigest()[:48]
            return (
                True,
                {
                    **spec,
                    "project_path": "",
                    "provider_key": provider,
                    "butler_workspace_key": workspace_key,
                },
                "fullstack",
                provider,
                "independent_checker",
                checker_provider,
                None,
            )
        project = connection.execute(
            "SELECT host_path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_path = str(spec.get("project_path") or (project["host_path"] if project else "") or "")
        if project_path:
            provider = _first_provider(
                connection, project_id, "fullstack", spec.get("provider_key")
            )
            checker_provider = _first_provider(
                connection, project_id, "independent_checker"
            )
            return (
                True,
                {**spec, "project_path": project_path, "provider_key": provider},
                "fullstack",
                provider,
                "independent_checker",
                checker_provider,
                None,
            )
        return (
            False,
            spec,
            "fullstack",
            "codex_cli",
            "independent_checker",
            "codex_cli",
            "project workspace is not bound; set an absolute Host Bridge path",
        )
    if kind == "git_publish":
        project = connection.execute(
            "SELECT host_path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        project_path = str(
            spec.get("project_path")
            or (project["host_path"] if project else "")
            or ""
        )
        if spec.get("operation") == "inspect":
            return (
                bool(project_path),
                {**spec, "project_path": project_path},
                "git_publisher",
                "git_publish",
                "deterministic_checker",
                "local",
                (
                    None
                    if project_path
                    else "project workspace is not bound; set an absolute Host Bridge path"
                ),
            )
        authorization = spec.get("authorization")
        publish_mode = str(spec.get("publish_mode") or "fast_forward")
        expected_scope = (
            f"{spec.get('remote_ssh')}#refs/heads/{spec.get('branch')}"
        )
        expected_action = (
            "git_publish_force_with_lease"
            if publish_mode == "force_with_lease"
            else "git_publish"
        )
        expected_remote_head = spec.get("expected_remote_head")
        authorization_task = (
            connection.execute(
                """
                SELECT terminal_capabilities_revoked_at FROM tasks
                WHERE id = ? AND project_id = ?
                """,
                (
                    authorization.get("task_id"),
                    project_id,
                ),
            ).fetchone()
            if isinstance(authorization, dict)
            and isinstance(authorization.get("task_id"), str)
            else None
        )
        authorized = bool(
            isinstance(authorization, dict)
            and authorization_task
            and authorization_task[
                "terminal_capabilities_revoked_at"
            ]
            is None
            and authorization.get("project_id") == project_id
            and authorization.get("action_kind") == expected_action
            and authorization.get("target_scope") == expected_scope
            and (
                publish_mode != "force_with_lease"
                or (
                    isinstance(expected_remote_head, str)
                    and len(expected_remote_head) == 40
                    and authorization.get("expected_remote_head")
                    == expected_remote_head
                )
            )
            and float(authorization.get("expires_at") or 0) >= time.time()
            and (
                int(authorization.get("spec_revision") or 0) <= 1
                or (
                    isinstance(authorization.get("expected_head"), str)
                    and len(authorization["expected_head"]) == 40
                )
            )
        )
        if project_path and authorized:
            return (
                True,
                {**spec, "project_path": project_path},
                "git_publisher",
                "git_publish",
                "deterministic_checker",
                "local",
                None,
            )
        return (
            False,
            {**spec, "project_path": project_path},
            "git_publisher",
            "git_publish",
            "deterministic_checker",
            "local",
            (
                "Git publish authorization is missing or expired"
                if project_path
                else "project workspace is not bound; set an absolute Host Bridge path"
            ),
        )
    return (
        False,
        spec,
        "deterministic",
        "local",
        "deterministic_checker",
        "local",
        str(
            spec.get(
                "wait_reason",
                "instruction is outside the current safe execution boundary",
            )
        ),
    )


def _first_provider(
    connection: sqlite3.Connection,
    project_id: str,
    role_key: str,
    requested: object = None,
) -> str:
    order = (
        effective_settings(connection, project_id, {})["values"]
        .get("provider_order", {})
        .get(role_key, [])
    )
    if requested and str(requested) in order:
        return str(requested)
    if not order:
        raise ValueError(f"Provider order is empty for role {role_key}")
    return str(order[0])


def _read_model_order(
    connection: sqlite3.Connection, project_id: str, role_key: str
) -> list[str]:
    configured = (
        effective_settings(connection, project_id, {})["values"]
        .get("provider_order", {})
        .get(role_key, [])
    )
    order = [
        str(provider)
        for provider in configured
        if provider in {"codex_cli", "cursor_cli"}
    ]
    if not order:
        raise ValueError(
            f"read-only semantic summary has no eligible Provider for {role_key}"
        )
    return order
