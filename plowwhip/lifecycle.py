from __future__ import annotations

import json
import sqlite3
import time
from uuid import uuid4

from .execution import execute_task
from .intake import canonical_json, normalize_instruction
from .store import Store
from .verification import verify_task


class LeaseLost(RuntimeError):
    pass


def advance_project(store: Store, project_id: str, lease_token: str, fence: int) -> str:
    """Perform exactly one lifecycle action. Cronner is the only caller with a lease."""
    with store.transaction() as connection:
        lease = connection.execute(
            """
            SELECT 1 FROM projects
            WHERE id = ? AND lease_token = ? AND lease_fence = ? AND lease_until >= ?
            """,
            (project_id, lease_token, fence, time.time()),
        ).fetchone()
        if not lease:
            raise LeaseLost(project_id)

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
            if task["phase"] == "verify":
                return verify_task(store, connection, task)
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
        if not message:
            return "idle"
        return _create_task(connection, message)


def _create_task(connection: sqlite3.Connection, message: sqlite3.Row) -> str:
    now = time.time()
    goal_id = uuid4().hex
    plan_id = uuid4().hex
    task_id = uuid4().hex
    spec, acceptance = normalize_instruction(message["content"])
    supported = spec["kind"] == "write_text"
    public_status = "pending" if supported else "needs_decision"
    phase = "execute" if supported else "intake"
    reason = None if supported else "supported instruction is: write <relative-path>: <content>"
    fault = None if supported else "scope"
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
            canonical_json({"writes": "task artifact directory only"}),
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
            plan_id, sprint, role_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'deterministic')
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
        connection.execute(
            """
            UPDATE tasks SET outcome = 'cancelled', phase = 'done',
                next_action_at = NULL, wait_reason = NULL, fault_code = NULL,
                updated_at = ? WHERE id = ?
            """,
            (now, task["id"]),
        )
        event, detail = "cancelled", {"message_id": message["id"]}
    elif kind == "rerun" and task["outcome"] == "cancelled":
        connection.execute(
            """
            UPDATE tasks SET public_status = 'pending', phase = 'execute',
                outcome = NULL, next_action_at = ?, updated_at = ? WHERE id = ?
            """,
            (now, now, task["id"]),
        )
        event, detail = "rerun", {"message_id": message["id"]}
    elif kind == "provide_decision" and task["public_status"] == "needs_decision":
        spec, acceptance = normalize_instruction(action["instruction"])
        if spec["kind"] == "write_text":
            revision = task["spec_revision"] + 1
            connection.execute(
                """
                UPDATE tasks SET spec_revision = ?, spec_json = ?, acceptance_json = ?,
                    public_status = 'pending', phase = 'execute', wait_reason = NULL,
                    fault_code = NULL, next_action_at = ?, outcome = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    revision,
                    canonical_json(spec),
                    canonical_json(acceptance),
                    now,
                    now,
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
                ("decision must use: write <relative-path>: <content>", now, task["id"]),
            )
            event = "decision_rejected"
            detail = {"message_id": message["id"]}
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
