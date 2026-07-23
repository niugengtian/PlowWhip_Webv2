from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from .execution import _write_atomic
from .intake import canonical_json
from .store import Store


def checkpoint_project(store: Store, project_id: str) -> None:
    """Refresh bounded Warm handoffs from canonical state without model calls."""
    with store.transaction() as connection:
        sessions = connection.execute(
            """
            SELECT session.id, session.task_id, session.role_key, session.settings_json,
                   task.spec_revision, task.public_status, task.phase, task.wait_reason,
                   task.fault_code, task.outcome,
                   generation.id AS generation_id, generation.generation
            FROM task_sessions session
            JOIN tasks task ON task.id = session.task_id
            JOIN session_generations generation ON generation.task_session_id = session.id
            WHERE task.project_id = ? AND generation.generation = (
                SELECT MAX(latest.generation) FROM session_generations latest
                WHERE latest.task_session_id = session.id
            )
            ORDER BY task.created_at, task.rowid, session.role_key
            """,
            (project_id,),
        ).fetchall()
        for session in sessions:
            _checkpoint_session(store, connection, project_id, session)


def _checkpoint_session(
    store: Store, connection: sqlite3.Connection, project_id: str, session: sqlite3.Row
) -> None:
    artifacts = connection.execute(
        """
        SELECT kind, path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind IN ('output', 'evidence')
        ORDER BY created_at DESC, rowid DESC LIMIT 8
        """,
        (session["task_id"],),
    ).fetchall()
    decisions = connection.execute(
        """
        SELECT id, action_json FROM messages
        WHERE project_id = ? AND processed_at IS NOT NULL AND action_json IS NOT NULL
        ORDER BY processed_at DESC, rowid DESC LIMIT 50
        """,
        (project_id,),
    ).fetchall()
    latest_decision = next(
        (
            row
            for row in decisions
            if json.loads(row["action_json"]).get("task_id") == session["task_id"]
        ),
        None,
    )
    evidence = [
        {
            "acceptance_id": row["acceptance_id"],
            "path": row["path"],
            "sha256": row["sha256"],
        }
        for row in artifacts
        if row["kind"] == "evidence"
    ]
    handoff = {
        "version": 1,
        "project_id": project_id,
        "task_id": session["task_id"],
        "task_session_id": session["id"],
        "role_key": session["role_key"],
        "session_generation": session["generation"],
        "task_spec_revision": session["spec_revision"],
        "state": {
            "public_status": session["public_status"],
            "phase": session["phase"],
            "outcome": session["outcome"],
            "fault_code": session["fault_code"],
            "blocker": session["wait_reason"],
        },
        "confirmed_evidence": evidence,
        "artifacts": [
            dict(row) for row in reversed(artifacts) if row["kind"] == "output"
        ],
        "latest_owner_decision": _decision_ref(latest_decision),
        "next_smallest_action": _next_action(session),
    }
    body = (canonical_json(handoff) + "\n").encode()
    settings = json.loads(session["settings_json"])
    values = settings.get("values", {})
    byte_cap = min(
        int(values.get("handoff_max_bytes", 8192)),
        int(values.get("checkpoint_max_bytes", 8192)),
    )
    if byte_cap < 512 or len(body) > byte_cap:
        raise ValueError(f"handoff exceeds configured {byte_cap}-byte cap")

    digest = hashlib.sha256(body).hexdigest()
    acceptance_id = f"handoff:{session['role_key']}"
    previous = connection.execute(
        """
        SELECT sha256 FROM artifacts
        WHERE task_id = ? AND kind = 'handoff' AND acceptance_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (session["task_id"], acceptance_id),
    ).fetchone()
    if previous and previous["sha256"] == digest:
        return
    revision = connection.execute(
        """
        SELECT COALESCE(MAX(revision), 0) + 1 AS value FROM artifacts
        WHERE task_id = ? AND kind = 'handoff' AND acceptance_id = ?
        """,
        (session["task_id"], acceptance_id),
    ).fetchone()["value"]
    base = (
        store.data_root
        / "projects"
        / project_id
        / "tasks"
        / session["task_id"]
        / "handoffs"
        / session["role_key"]
    )
    archived = base / "archive" / f"revision-{revision:06d}.json"
    _write_atomic(archived, body)
    _write_atomic(base / "current.json", body)
    relative = store.relative_data_path(archived)
    connection.execute(
        """
        INSERT INTO artifacts(
            id, project_id, task_id, kind, path, sha256, bytes,
            acceptance_id, revision, created_at
        ) VALUES (?, ?, ?, 'handoff', ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            project_id,
            session["task_id"],
            relative,
            digest,
            len(body),
            acceptance_id,
            revision,
            time.time(),
        ),
    )
    connection.execute(
        "UPDATE session_generations SET handoff_ref = ? WHERE id = ?",
        (relative, session["generation_id"]),
    )


def _decision_ref(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    action = json.loads(row["action_json"])
    return {"message_id": row["id"], "action_kind": action["kind"]}


def _next_action(session: sqlite3.Row) -> str | None:
    if session["outcome"]:
        return None
    if session["public_status"] == "needs_decision":
        return "await_owner_decision"
    return {
        "queued": "wait_for_dependencies",
        "execute": "execute_task",
        "verify": "verify_evidence",
    }.get(session["phase"], "reconcile_state")
