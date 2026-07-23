from __future__ import annotations

import json
import re
import time
from pathlib import Path
from uuid import uuid4

from .intake import PROJECT_ID, canonical_json, submit_message
from .store import Store, write_atomic as _write_atomic


GLOBAL_ROUTE_PREFIX = re.compile(
    r"^\s*@([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s+([\s\S]+)$"
)
GLOBAL_SEARCH = re.compile(r"^\s*(?:找|查找|定位)\s*(.+?)(?:任务)?\s*$")


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


def route_global_message(
    store: Store,
    content: str,
    idempotency_key: str,
    explicit_project_id: str | None = None,
) -> dict[str, object]:
    if not content or len(content.encode()) > 65_536:
        raise ValueError("message must contain 1-65536 UTF-8 bytes")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")
    connection = store.connect_readonly()
    try:
        duplicate = connection.execute(
            """
            SELECT id, project_id, action_json FROM messages
            WHERE idempotency_key = ? ORDER BY created_at LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        projects = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM projects WHERE archived_at IS NULL ORDER BY id"
            )
        ]
    finally:
        connection.close()
    if duplicate:
        return {
            "message_id": duplicate["id"],
            "project_id": duplicate["project_id"],
            "routed_only": bool(duplicate["action_json"]),
        }

    routed_content = content
    search_match = GLOBAL_SEARCH.fullmatch(content)
    search_result = (
        search(store.db_path, store.data_root, search_match.group(1))
        if search_match
        else None
    )
    explicit_route = bool(explicit_project_id)
    if explicit_project_id:
        if not PROJECT_ID.fullmatch(explicit_project_id):
            raise ValueError("invalid project_id")
        project_id = explicit_project_id
    else:
        prefixed = GLOBAL_ROUTE_PREFIX.fullmatch(content)
        if prefixed:
            project_id, routed_content = prefixed.groups()
            explicit_route = True
        else:
            named = [
                project_id
                for project_id in projects
                if re.search(
                    rf"(?<![A-Za-z0-9._-]){re.escape(project_id)}(?![A-Za-z0-9._-])",
                    content,
                )
            ]
            project_id = named[0] if len(named) == 1 else ""
        if not project_id and search_result:
            matched_projects = {
                item["project_id"] for item in search_result["results"]
            }
            if len(matched_projects) == 1:
                project_id = matched_projects.pop()
        if not project_id and len(projects) == 1:
            project_id = projects[0]
        if not project_id:
            raise ValueError(
                "无法唯一确定项目；请在指令开头写 @project_id，或先明确创建项目"
            )
    if project_id not in projects and not explicit_route:
        raise ValueError("global Butler can route only to an active project")

    search_match = GLOBAL_SEARCH.fullmatch(routed_content)
    if search_match:
        result = search_result or search(
            store.db_path, store.data_root, search_match.group(1)
        )
        matched_projects = {
            item["project_id"] for item in result["results"]
        }
        if matched_projects and project_id not in matched_projects:
            if len(matched_projects) != 1:
                raise ValueError(
                    "查询命中多个项目；请在开头使用 @project_id 明确范围"
                )
            project_id = matched_projects.pop()
        message_id = _submit_global_route_reference(
            store, project_id, routed_content, idempotency_key
        )
        return {
            "message_id": message_id,
            "project_id": project_id,
            "routed_only": True,
            "results": result["results"],
        }
    return {
        "message_id": submit_message(
            store, project_id, routed_content, idempotency_key
        ),
        "project_id": project_id,
        "routed_only": False,
    }


def _submit_global_route_reference(
    store: Store, project_id: str, content: str, idempotency_key: str
) -> str:
    now = time.time()
    message_id = uuid4().hex
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                message_id,
                project_id,
                content,
                canonical_json({"kind": "global_route"}),
                idempotency_key,
                now,
            ),
        )
    return message_id


def sync_conversation_files(store: Store, project_id: str) -> None:
    """Refresh bounded, rebuildable conversation projections from SQLite."""
    connection = store.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT id, project_id, role, content, action_json, created_at, processed_at
            FROM messages WHERE project_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 50
            """,
            (project_id,),
        ).fetchall()
    finally:
        connection.close()
    project_root = store.data_root / "projects" / project_id / "conversations"
    global_root = store.data_root / "global" / "conversations"
    for row in reversed(rows):
        payload = dict(row)
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        project_path = project_root / f"{row['id']}.json"
        if not project_path.exists():
            _write_atomic(project_path, body)
        if row["role"] == "owner":
            global_body = json.dumps(
                {
                    "message_id": row["id"],
                    "routed_project_id": project_id,
                    "created_at": row["created_at"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            global_path = global_root / f"{row['id']}.json"
            if not global_path.exists():
                _write_atomic(global_path, global_body)
