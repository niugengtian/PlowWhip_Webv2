from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from .intake import canonical_json
from .store import Store, write_atomic as _write_atomic


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
            _segment_session(store, connection, project_id, session)
            _checkpoint_session(store, connection, project_id, session)


def compile_hot_context(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    role_key: str,
) -> str:
    """Build one transient bounded Context Capsule; never persist a Hot copy."""
    session = connection.execute(
        """
        SELECT settings_json FROM task_sessions
        WHERE task_id = ? AND role_key = ?
        """,
        (task["id"], role_key),
    ).fetchone()
    if not session:
        raise ValueError(f"missing TaskSession settings for {role_key}")
    cap = int(json.loads(session["settings_json"])["values"]["context_max_bytes"])
    goal = connection.execute(
        "SELECT objective, boundary_json FROM goals WHERE id = ?", (task["goal_id"],)
    ).fetchone()
    artifacts = connection.execute(
        """
        SELECT kind, path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind IN ('output', 'evidence')
        ORDER BY created_at DESC, rowid DESC LIMIT 8
        """,
        (task["id"],),
    ).fetchall()
    handoff = connection.execute(
        """
        SELECT path, sha256 FROM artifacts
        WHERE task_id = ? AND kind = 'handoff'
          AND acceptance_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (task["id"], f"handoff:{role_key}"),
    ).fetchone()
    warm = None
    if handoff:
        try:
            warm = json.loads(store.resolve_data_path(handoff["path"]).read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            warm = {"path": handoff["path"], "sha256": handoff["sha256"]}
    capsule = {
        "version": 1,
        "project_id": task["project_id"],
        "task_id": task["id"],
        "role_key": role_key,
        "task_spec_revision": task["spec_revision"],
        "goal": {
            "objective": goal["objective"] if goal else None,
            "boundary": json.loads(goal["boundary_json"]) if goal else None,
        },
        "task_spec": json.loads(task["spec_json"]),
        "acceptance": json.loads(task["acceptance_json"]),
        "state": {
            "phase": task["phase"],
            "fault_code": task["fault_code"],
            "blocker": task["wait_reason"],
        },
        "warm_handoff": warm,
        "recent_evidence_and_artifacts": [dict(row) for row in reversed(artifacts)],
    }
    body = canonical_json(capsule)
    if len(body.encode()) > cap:
        capsule["warm_handoff"] = (
            {
                "path": handoff["path"],
                "sha256": handoff["sha256"],
            }
            if handoff
            else None
        )
        capsule["recent_evidence_and_artifacts"] = [
            {
                "kind": row["kind"],
                "path": row["path"],
                "sha256": row["sha256"],
                "acceptance_id": row["acceptance_id"],
            }
            for row in artifacts[:2]
        ]
        body = canonical_json(capsule)
    if len(body.encode()) > cap:
        raise ValueError(
            f"mandatory Context Capsule exceeds configured {cap}-byte cap; "
            "increase context_max_bytes or narrow the Task"
        )
    return body


def _segment_session(
    store: Store,
    connection: sqlite3.Connection,
    project_id: str,
    session: sqlite3.Row,
) -> None:
    """Append bounded Cold manifests for new HostJobs; logs remain separate files."""
    acceptance_id = (
        f"session_segment:{session['role_key']}:generation-{session['generation']:06d}"
    )
    previous = connection.execute(
        """
        SELECT path, revision FROM artifacts
        WHERE task_id = ? AND kind = 'log' AND acceptance_id = ?
        ORDER BY revision DESC LIMIT 1
        """,
        (session["task_id"], acceptance_id),
    ).fetchone()
    last_sequence = 0
    revision = 1
    if previous:
        revision = int(previous["revision"]) + 1
        try:
            last_sequence = int(
                json.loads(store.resolve_data_path(previous["path"]).read_text())[
                    "to_sequence"
                ]
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            last_sequence = 0
    jobs = connection.execute(
        """
        SELECT id, spec_revision, sequence, purpose, status, started_at, ended_at,
               returncode, output_ref, failure_code
        FROM host_jobs
        WHERE task_session_id = ? AND session_generation = ? AND sequence > ?
        ORDER BY sequence
        """,
        (session["id"], session["generation"], last_sequence),
    ).fetchall()
    if not jobs:
        return
    cap = int(
        json.loads(session["settings_json"])["values"]["session_segment_max_bytes"]
    )
    remaining = [dict(row) for row in jobs]
    while remaining:
        chunk = []
        while remaining:
            candidate = chunk + [remaining[0]]
            manifest = _segment_manifest(project_id, session, candidate)
            if len((canonical_json(manifest) + "\n").encode()) > cap:
                break
            chunk.append(remaining.pop(0))
        if not chunk:
            raise ValueError(
                f"one Cold session record exceeds configured {cap}-byte segment cap"
            )
        manifest = _segment_manifest(project_id, session, chunk)
        body = (canonical_json(manifest) + "\n").encode()
        path = (
            store.data_root
            / "projects"
            / project_id
            / "tasks"
            / session["task_id"]
            / "sessions"
            / session["role_key"]
            / f"generation-{session['generation']:06d}"
            / "segments"
            / f"segment-{revision:06d}.json"
        )
        _write_atomic(path, body)
        connection.execute(
            """
            INSERT INTO artifacts(
                id, project_id, task_id, kind, path, sha256, bytes,
                acceptance_id, revision, created_at
            ) VALUES (?, ?, ?, 'log', ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                project_id,
                session["task_id"],
                store.relative_data_path(path),
                hashlib.sha256(body).hexdigest(),
                len(body),
                acceptance_id,
                revision,
                time.time(),
            ),
        )
        revision += 1


def _segment_manifest(
    project_id: str, session: sqlite3.Row, jobs: list[dict]
) -> dict:
    return {
        "version": 1,
        "project_id": project_id,
        "task_id": session["task_id"],
        "task_session_id": session["id"],
        "role_key": session["role_key"],
        "session_generation": session["generation"],
        "from_sequence": jobs[0]["sequence"],
        "to_sequence": jobs[-1]["sequence"],
        "host_jobs": jobs,
    }


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
        "execute_snapshot": "snapshot_workspace",
        "execute_dispatch": "dispatch_host_job",
        "execute_wait": "reconcile_host_job",
        "verify": "verify_evidence",
        "repair": "repair_task",
        "stopping": "stop_host_job",
    }.get(session["phase"], "reconcile_state")
