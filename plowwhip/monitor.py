from __future__ import annotations

from pathlib import Path

from .store import Store


def snapshot(db_path: str | Path, data_root: str | Path, project_id: str) -> dict:
    """Read canonical state and a bounded log tail without opening a write path."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        task = connection.execute(
            """
            SELECT * FROM tasks WHERE project_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
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
                ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1
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
    last_output = (
        _tail(store.data_root, job["output_ref"]) if job and job["output_ref"] else []
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
        "last_output": last_output,
    }


def _tail(data_root: Path, relative: str, lines: int = 20, byte_cap: int = 8192) -> list[str]:
    path = (data_root / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - byte_cap))
        return handle.read(byte_cap).decode(errors="replace").splitlines()[-lines:]
