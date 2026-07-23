from __future__ import annotations

from pathlib import Path

from .store import Store


def search(db_path: str | Path, data_root: str | Path, query: str) -> dict:
    """Search canonical indexes across projects without invoking a model."""
    query = query.strip()
    if not query or len(query) > 128:
        raise ValueError("search query must contain 1-128 characters")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT 'task' AS kind, project_id, id AS ref,
                   public_status AS status, phase AS detail
            FROM tasks
            WHERE id LIKE ? ESCAPE '\\' OR spec_json LIKE ? ESCAPE '\\'
            UNION ALL
            SELECT 'goal', project_id, id, NULL, objective
            FROM goals WHERE objective LIKE ? ESCAPE '\\'
            UNION ALL
            SELECT 'message', project_id, id, role, substr(content, 1, 256)
            FROM messages WHERE content LIKE ? ESCAPE '\\'
            UNION ALL
            SELECT 'artifact', project_id, id, kind, path
            FROM artifacts
            WHERE path LIKE ? ESCAPE '\\' OR acceptance_id LIKE ? ESCAPE '\\'
            ORDER BY project_id, kind, ref LIMIT 50
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        return {"query": query, "results": [dict(row) for row in rows]}
    finally:
        connection.close()


def conversation(
    db_path: str | Path, data_root: str | Path, project_id: str
) -> dict:
    """Read the bounded project Butler history without invoking a Provider."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        project = connection.execute(
            "SELECT id, created_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            return {"project": None, "messages": []}
        rows = connection.execute(
            """
            SELECT id, role, content, action_json, created_at, processed_at
            FROM messages WHERE project_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 50
            """,
            (project_id,),
        ).fetchall()
        return {
            "project": dict(project),
            "messages": [dict(row) for row in reversed(rows)],
        }
    finally:
        connection.close()
