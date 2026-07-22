from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote


def snapshot(db_path: str | Path, data_root: str | Path, project_id: str) -> dict:
    """Read canonical state and a bounded log tail without opening a write path."""
    db_path = Path(db_path).resolve()
    data_root = Path(data_root).resolve()
    connection = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        task = connection.execute(
            """
            SELECT * FROM tasks WHERE project_id = ?
            ORDER BY created_at DESC LIMIT 1
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
            ORDER BY CASE kind WHEN 'output' THEN 0 ELSE 1 END
            """,
            (task["id"],),
        ).fetchall()
        last_output = _tail(data_root, job["output_ref"]) if job and job["output_ref"] else []
        return {
            "project_id": project_id,
            "task": dict(task),
            "events": [dict(event) for event in events],
            "artifacts": [
                {
                    **dict(artifact),
                    "kind": "artifact" if artifact["kind"] == "output" else "evidence",
                    "path": str((data_root / artifact["path"]).resolve()),
                }
                for artifact in artifacts
            ],
            "last_output": last_output,
        }
    finally:
        connection.close()


def _tail(data_root: Path, relative: str, lines: int = 20, byte_cap: int = 8192) -> list[str]:
    path = (data_root / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - byte_cap))
        return handle.read(byte_cap).decode(errors="replace").splitlines()[-lines:]
