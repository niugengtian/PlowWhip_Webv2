from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .provider import provider_facts
from .store import Store


def snapshot(db_path: str | Path, data_root: str | Path, project_id: str) -> dict:
    """Read canonical state and a bounded log tail without opening a write path."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        task = connection.execute(
            """
            SELECT * FROM tasks WHERE project_id = ?
            ORDER BY
              CASE
                WHEN outcome IS NULL AND phase <> 'queued' THEN 0
                WHEN outcome IS NULL THEN 1
                ELSE 2
              END,
              created_at DESC, rowid DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if not task:
            return {
                "project_id": project_id,
                "task": None,
                "events": [],
                "artifacts": [],
                "last_output": [],
            }
        return _task_view(connection, store, task)
    finally:
        connection.close()


def projects_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT p.id AS project_id, p.created_at,
                   t.id AS task_id, t.public_status, t.phase,
                   t.spec_revision, t.outcome, t.updated_at
            FROM projects p
            LEFT JOIN tasks t ON t.rowid = (
                SELECT latest.rowid FROM tasks latest
                WHERE latest.project_id = p.id
                ORDER BY
                  CASE
                    WHEN latest.outcome IS NULL AND latest.phase <> 'queued' THEN 0
                    WHEN latest.outcome IS NULL THEN 1
                    ELSE 2
                  END,
                  latest.created_at DESC, latest.rowid DESC LIMIT 1
            )
            ORDER BY p.created_at, p.rowid
            """
        ).fetchall()
        return {"projects": [dict(row) for row in rows]}
    finally:
        connection.close()


def task_snapshot(db_path: str | Path, data_root: str | Path, task_id: str) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return {"task": None, "events": [], "artifacts": [], "last_output": []}
        return _task_view(connection, store, task)
    finally:
        connection.close()


def settings_library_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        settings = connection.execute(
            """
            SELECT scope, project_id, setting_key, value_json, source, updated_at
            FROM settings ORDER BY scope, project_id, setting_key
            """
        ).fetchall()
        items = connection.execute(
            """
            SELECT scope, project_id, kind, item_key, revision, path, sha256, created_at
            FROM library_items current
            WHERE revision = (
                SELECT MAX(latest.revision) FROM library_items latest
                WHERE latest.scope = current.scope
                  AND latest.project_id IS current.project_id
                  AND latest.kind = current.kind
                  AND latest.item_key = current.item_key
            )
            ORDER BY scope, project_id, kind, item_key
            """
        ).fetchall()
        library = []
        for item in items:
            path = store.resolve_data_path(item["path"])
            body = path.read_bytes() if path.is_file() else b""
            library.append(
                {
                    **dict(item),
                    "content": body[:65_536].decode(errors="replace"),
                    "truncated": len(body) > 65_536,
                    "sha256_matches": hashlib.sha256(body).hexdigest() == item["sha256"],
                }
            )
        return {
            "settings": [
                {**dict(row), "value": json.loads(row["value_json"])}
                for row in settings
            ],
            "library": library,
        }
    finally:
        connection.close()


def _task_view(connection, store: Store, task) -> dict:
    events = connection.execute(
        """
        SELECT kind, detail_json, created_at FROM task_events
        WHERE task_id = ? ORDER BY id DESC LIMIT 20
        """,
        (task["id"],),
    ).fetchall()
    job = connection.execute(
        """
        SELECT output_ref FROM host_jobs
        WHERE task_id = ? ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    artifacts = connection.execute(
        """
        SELECT kind, path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind IN ('output', 'evidence')
        ORDER BY revision, CASE kind WHEN 'output' THEN 0 ELSE 1 END
        """,
        (task["id"],),
    ).fetchall()
    handoffs = connection.execute(
        """
        SELECT path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind = 'handoff'
        ORDER BY created_at DESC, rowid DESC LIMIT 20
        """,
        (task["id"],),
    ).fetchall()
    sessions = connection.execute(
        """
        SELECT session.id AS task_session_id, session.role_key,
               session.role_snapshot_json, session.settings_json,
               generation.generation, generation.provider_key,
               generation.status, generation.handoff_ref
        FROM task_sessions session
        JOIN session_generations generation ON generation.task_session_id = session.id
        WHERE session.task_id = ?
        ORDER BY session.role_key, generation.generation
        """,
        (task["id"],),
    ).fetchall()
    model_usage = connection.execute(
        """
        SELECT task_session_id, SUM(normalized_total) AS normalized_total
        FROM model_calls WHERE task_id = ? GROUP BY task_session_id
        """,
        (task["id"],),
    ).fetchall()
    tail_values = {}
    for session in sessions:
        if session["role_key"] == (task["role_key"] or "deterministic"):
            tail_values = json.loads(session["settings_json"]).get("values", {})
            break
    last_output = (
        _tail(
            store.data_root,
            job["output_ref"],
            int(tail_values.get("monitor_tail_lines", 20)),
            int(tail_values.get("monitor_tail_bytes", 8192)),
        )
        if job and job["output_ref"]
        else []
    )
    return {
        "project_id": task["project_id"],
        "task": dict(task),
        "events": [dict(event) for event in events],
        "artifacts": [
            {
                **dict(artifact),
                "kind": "artifact" if artifact["kind"] == "output" else "evidence",
                "path": str(store.resolve_data_path(artifact["path"])),
            }
            for artifact in artifacts
        ],
        "handoffs": [
            {**dict(handoff), "path": str(store.resolve_data_path(handoff["path"]))}
            for handoff in handoffs
        ],
        "sessions": [
            {
                **dict(session),
                "model": "deterministic" if session["provider_key"] == "local" else None,
                "role_snapshot": json.loads(session["role_snapshot_json"]),
                "settings": json.loads(session["settings_json"]),
                "provider_candidates": provider_facts(session["role_key"]),
            }
            for session in sessions
        ],
        "model_usage": [dict(row) for row in model_usage],
        "session_files": _session_files(store, task),
        "last_output": last_output,
    }


def _session_files(store: Store, task) -> list[dict]:
    root = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "sessions"
    )
    if not root.is_dir():
        return []
    files = sorted((path for path in root.rglob("*") if path.is_file()), reverse=True)[:100]
    return [
        {
            "path": str(path),
            "bytes": path.stat().st_size,
        }
        for path in files
    ]


def _tail(data_root: Path, relative: str, lines: int = 20, byte_cap: int = 8192) -> list[str]:
    path = (data_root / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - byte_cap))
        return handle.read(byte_cap).decode(errors="replace").splitlines()[-lines:]
