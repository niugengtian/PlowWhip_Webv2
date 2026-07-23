from __future__ import annotations

import json
import sqlite3
import time
from uuid import uuid4

from .execution import (
    ProviderStep,
    apply_provider_step,
    archive_task_sessions,
    create_task_sessions,
    effective_settings,
    ensure_task_sessions,
    execute_task,
    pending_provider_step,
    perform_provider_step,
    rotate_task_sessions,
)
from .intake import canonical_json, normalize_instruction
from .planner import normalize_plan
from .store import Store
from .verification import CheckerStep, apply_checker_step, perform_checker_step, verify_task


class LeaseLost(RuntimeError):
    pass


def advance_project(store: Store, project_id: str, lease_token: str, fence: int) -> str:
    result = _advance_project_transaction(store, project_id, lease_token, fence)
    if not isinstance(result, (ProviderStep, CheckerStep)):
        return result
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
                    if isinstance(result, CheckerStep)
                    else 90
                ),
                project_id,
                lease_token,
                fence,
            ),
        )
    facts = (
        perform_checker_step(result)
        if isinstance(result, CheckerStep)
        else perform_provider_step(result)
    )
    with store.transaction() as connection:
        _assert_lease(connection, project_id, lease_token, fence)
        return (
            apply_checker_step(store, connection, result, facts)
            if isinstance(result, CheckerStep)
            else apply_provider_step(store, connection, result, facts)
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
) -> str | ProviderStep:
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
            return _apply_action(connection, action)

        task = connection.execute(
            """
            SELECT * FROM tasks
            WHERE project_id = ? AND outcome IS NULL
              AND public_status IN ('pending', 'in_progress')
              AND next_action_at <= ?
            ORDER BY created_at, rowid LIMIT 1
            """,
            (project_id, time.time()),
        ).fetchone()
        if task:
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
                "stopping",
            }:
                return pending_provider_step(connection, task)
            if task["phase"] == "check_call":
                return _stop_interrupted_checker(connection, task)
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
                "UPDATE tasks SET phase = 'execute', next_action_at = ?, updated_at = ? WHERE id = ?",
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

        message = connection.execute(
            """
            SELECT * FROM messages
            WHERE project_id = ? AND processed_at IS NULL AND action_json IS NULL
            ORDER BY created_at, rowid LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if not message:
            return "idle"
        return _create_task(connection, message)


def _create_task(connection: sqlite3.Connection, message: sqlite3.Row) -> str:
    now = time.time()
    goal_id = uuid4().hex
    plan_id = uuid4().hex
    task_id = uuid4().hex
    spec, acceptance = normalize_instruction(message["content"])
    supported, spec, role_key, provider_key, checker_role, checker_provider, reason = (
        _runtime_contract(connection, message["project_id"], spec)
    )
    public_status = "pending" if supported else "needs_decision"
    phase = "execute" if supported else "intake"
    fault = None if supported else (
        "credential" if spec["kind"] == "authorization_required" else "scope"
    )
    outcome = None

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
                        spec.get("project_path")
                        if spec["kind"] == "provider_task"
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
        VALUES (?, ?, 1, 1, ?, ?)
        """,
        (
            plan_id,
            goal_id,
            canonical_json({"classification": "simple" if supported else "undetermined"}),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO tasks(
            id, project_id, goal_id, spec_json, acceptance_json, public_status,
            phase, wait_reason, fault_code, next_action_at, outcome, created_at, updated_at,
            plan_id, sprint, role_key, checker_role_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                  ?, ?)
        """,
        (
            task_id,
            message["project_id"],
            goal_id,
            canonical_json(spec),
            canonical_json(acceptance),
            public_status,
            phase,
            reason,
            fault,
            now if supported else None,
            outcome,
            now,
            now,
            plan_id,
            role_key,
            checker_role,
        ),
    )
    if supported:
        create_task_sessions(
            connection,
            message["project_id"],
            task_id,
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
            "task_created" if supported else "needs_decision",
            canonical_json({"source_message_id": message["id"], "spec_revision": 1}),
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
    if kind == "cancel" and task["outcome"] is None:
        spec = json.loads(task["spec_json"])
        active_job = (
            connection.execute(
                """
                SELECT id FROM host_jobs
                WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
                ORDER BY sequence DESC LIMIT 1
                """,
                (task["id"],),
            ).fetchone()
            if spec.get("kind") == "provider_task"
            else None
        )
        if active_job and task["phase"] != "execute_snapshot":
            connection.execute(
                """
                UPDATE host_jobs SET status = 'cancelling' WHERE id = ?
                """,
                (active_job["id"],),
            )
            connection.execute(
                """
                UPDATE tasks SET public_status = 'in_progress', phase = 'stopping',
                    next_action_at = ?, wait_reason = 'cancellation requested',
                    fault_code = NULL, updated_at = ? WHERE id = ?
                """,
                (now, now, task["id"]),
            )
            event, detail = "cancel_requested", {
                "message_id": message["id"],
                "host_job_id": active_job["id"],
            }
        else:
            if active_job:
                connection.execute(
                    """
                    UPDATE host_jobs SET status = 'cancelled', ended_at = ?,
                        returncode = -15, failure_code = 'process'
                    WHERE id = ?
                    """,
                    (now, active_job["id"]),
                )
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', outcome = 'cancelled', phase = 'done',
                    next_action_at = NULL, wait_reason = NULL, fault_code = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (now, task["id"]),
            )
            archive_task_sessions(connection, task["id"], now)
            event, detail = "cancelled", {"message_id": message["id"]}
    elif kind == "rerun" and task["outcome"] == "cancelled":
        spec = json.loads(task["spec_json"])
        (
            supported,
            spec,
            role_key,
            provider_key,
            checker_role,
            checker_provider,
            _reason,
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
            UPDATE tasks SET public_status = ?, phase = ?, outcome = NULL,
                retry_count = 0, next_retry_at = NULL,
                next_action_at = ?, updated_at = ? WHERE id = ?
            """,
            (
                "pending" if supported else "needs_decision",
                "execute" if supported else "intake",
                now if supported else None,
                now,
                task["id"],
            ),
        )
        event, detail = "rerun", {"message_id": message["id"]}
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
                    next_action_at = ?, outcome = NULL, role_key = ?, updated_at = ?
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
            "UPDATE tasks SET next_action_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task["id"]),
        )
        event, detail = "wake_requested", {"message_id": message["id"]}
    elif kind == "provide_plan" and task["public_status"] == "needs_decision":
        try:
            plan = normalize_plan(action["plan"])
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

    task_ids = {item["key"]: uuid4().hex for item in plan["tasks"]}
    first = plan["tasks"][0]
    task_ids[first["key"]] = placeholder["id"]
    connection.execute(
        """
        UPDATE tasks SET plan_id = ?, spec_revision = spec_revision + 1,
            spec_json = ?, acceptance_json = ?, public_status = 'pending',
            phase = 'execute', wait_reason = NULL, fault_code = NULL,
            retry_count = 0, next_retry_at = NULL, next_action_at = ?,
            outcome = NULL, sprint = ?, role_key = ?,
            checker_role_key = 'deterministic_checker', updated_at = ?
        WHERE id = ?
        """,
        (
            plan_id,
            canonical_json(first["spec"]),
            canonical_json(first["acceptance"]),
            now,
            first["sprint"],
            first["role_key"],
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
        checker_role="deterministic_checker",
        settings_overrides=first["settings"],
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
    for item in plan["tasks"][1:]:
        task_id = task_ids[item["key"]]
        connection.execute(
            """
            INSERT INTO tasks(
                id, project_id, goal_id, spec_json, acceptance_json, public_status,
                phase, next_action_at, created_at, updated_at, plan_id, sprint,
                role_key, checker_role_key
            ) VALUES (?, ?, ?, ?, ?, 'pending', 'queued', NULL, ?, ?, ?, ?, ?,
                      'deterministic_checker')
            """,
            (
                task_id,
                placeholder["project_id"],
                placeholder["goal_id"],
                canonical_json(item["spec"]),
                canonical_json(item["acceptance"]),
                now,
                now,
                plan_id,
                item["sprint"],
                item["role_key"],
            ),
        )
        create_task_sessions(
            connection,
            placeholder["project_id"],
            task_id,
            now,
            executor_role=item["role_key"],
            checker_role="deterministic_checker",
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
    for item in plan["tasks"]:
        for dependency in item["depends_on"]:
            connection.execute(
                "INSERT INTO task_dependencies(task_id, depends_on_task_id) VALUES (?, ?)",
                (task_ids[item["key"]], task_ids[dependency]),
            )


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


def _stop_interrupted_checker(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> str:
    now = time.time()
    connection.execute(
        """
        UPDATE host_jobs SET status = 'interrupted', ended_at = ?,
            returncode = 1, failure_code = 'unsafe_unknown'
        WHERE task_id = ? AND purpose = 'check' AND status = 'dispatching'
        """,
        (now, task["id"]),
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'needs_decision', phase = 'provider_recovery',
            wait_reason = 'Checker call was interrupted; automatic paid replay is unsafe',
            fault_code = 'unsafe_unknown', next_action_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now, task["id"]),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'checker_interrupted', '{}', ?)
        """,
        (task["project_id"], task["id"], now),
    )
    return "needs_decision"


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
        return (
            True,
            spec,
            "provider_probe",
            provider,
            "deterministic_checker",
            "local",
            None,
        )
    if kind == "provider_task":
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
