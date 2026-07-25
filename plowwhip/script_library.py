"""Project script-library helpers: search, summary, and auto-promote enqueue."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import time
from pathlib import PurePosixPath
from uuid import uuid4

from .intake import LIBRARY_KEY, canonical_json
from .store import Store


SUMMARY_LINE = re.compile(
    r"^\s*(?:summary|功能|说明)\s*[:=：]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_script_summary(body: str | bytes) -> str:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        module = ast.parse(text)
    except SyntaxError:
        module = None
    docstring = ast.get_docstring(module) if module is not None else None
    if docstring:
        match = SUMMARY_LINE.search(docstring)
        if match:
            return match.group(1).strip()[:240]
        first = next(
            (line.strip() for line in docstring.splitlines() if line.strip()),
            "",
        )
        if first:
            return first[:240]
    return ""


def search_script_library(
    connection: sqlite3.Connection,
    store: Store,
    query: str,
    *,
    project_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    query = query.strip()
    if not query or len(query) > 128:
        return []
    lowered = query.lower()
    if project_id:
        rows = connection.execute(
            """
            SELECT current.id, current.project_id, current.item_key, current.revision,
                   current.path, current.sha256, current.source_task_id
            FROM library_items current
            WHERE current.scope = 'project'
              AND current.project_id = ?
              AND current.kind = 'script'
              AND current.revision = (
                  SELECT MAX(latest.revision) FROM library_items latest
                  WHERE latest.scope = current.scope
                    AND latest.project_id IS current.project_id
                    AND latest.kind = current.kind
                    AND latest.item_key = current.item_key
              )
            ORDER BY current.item_key
            """,
            (project_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT current.id, current.project_id, current.item_key, current.revision,
                   current.path, current.sha256, current.source_task_id
            FROM library_items current
            WHERE current.scope = 'project'
              AND current.kind = 'script'
              AND current.revision = (
                  SELECT MAX(latest.revision) FROM library_items latest
                  WHERE latest.scope = current.scope
                    AND latest.project_id IS current.project_id
                    AND latest.kind = current.kind
                    AND latest.item_key = current.item_key
              )
            ORDER BY current.project_id, current.item_key
            """
        ).fetchall()
    hits: list[dict[str, object]] = []
    for row in rows:
        path = store.resolve_data_path(row["path"])
        try:
            body = path.read_bytes() if path.is_file() else b""
        except OSError:
            body = b""
        summary = extract_script_summary(body)
        haystack = f"{row['item_key']} {row['path']} {summary}".lower()
        if lowered not in haystack:
            continue
        hits.append(
            {
                "kind": "script",
                "project_id": row["project_id"],
                "ref": row["id"],
                "status": f"revision-{row['revision']}",
                "detail": summary or row["item_key"],
                "item_key": row["item_key"],
                "revision": row["revision"],
                "path": row["path"],
                "sha256": row["sha256"],
                "summary": summary,
                "source_task_id": row["source_task_id"],
            }
        )
        if len(hits) >= max(1, min(int(limit), 50)):
            break
    return hits


def list_project_scripts(
    connection: sqlite3.Connection,
    store: Store,
    project_id: str,
    *,
    limit: int = 30,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT current.id, current.item_key, current.revision, current.path,
               current.sha256, current.source_task_id
        FROM library_items current
        WHERE current.scope = 'project'
          AND current.project_id = ?
          AND current.kind = 'script'
          AND current.revision = (
              SELECT MAX(latest.revision) FROM library_items latest
              WHERE latest.scope = current.scope
                AND latest.project_id IS current.project_id
                AND latest.kind = current.kind
                AND latest.item_key = current.item_key
          )
        ORDER BY current.item_key
        LIMIT ?
        """,
        (project_id, max(1, min(int(limit), 50))),
    ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        path = store.resolve_data_path(row["path"])
        try:
            body = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            body = ""
        items.append(
            {
                "item_key": row["item_key"],
                "revision": row["revision"],
                "path": row["path"],
                "sha256": row["sha256"],
                "summary": extract_script_summary(body),
                "source_task_id": row["source_task_id"],
                "library_item_id": row["id"],
            }
        )
    return items


def resolve_library_script(
    connection: sqlite3.Connection,
    store: Store,
    project_id: str,
    item_key: str,
    revision: int | None = None,
) -> dict[str, object]:
    if not LIBRARY_KEY.fullmatch(item_key):
        raise ValueError("library script item_key is invalid")
    if revision is None:
        row = connection.execute(
            """
            SELECT * FROM library_items
            WHERE scope = 'project' AND project_id = ?
              AND kind = 'script' AND item_key = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (project_id, item_key),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT * FROM library_items
            WHERE scope = 'project' AND project_id = ?
              AND kind = 'script' AND item_key = ? AND revision = ?
            """,
            (project_id, item_key, revision),
        ).fetchone()
    if not row:
        raise ValueError(f"library script not found: {item_key}")
    path = store.resolve_data_path(row["path"])
    body = path.read_bytes()
    return {
        "item_key": row["item_key"],
        "revision": row["revision"],
        "path": row["path"],
        "sha256": row["sha256"],
        "body": body.decode("utf-8"),
        "summary": extract_script_summary(body),
    }


def suggested_item_key(artifact_path: str) -> str | None:
    name = PurePosixPath(artifact_path).stem
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-_")
    if not candidate:
        return None
    if candidate[0].isdigit():
        candidate = f"script-{candidate}"
    candidate = candidate[:64]
    return candidate if LIBRARY_KEY.fullmatch(candidate) else None


def enqueue_auto_promote_script(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    task_id: str,
    spec_revision: int,
    artifact_id: str,
    artifact_sha256: str,
    item_key: str,
    now: float | None = None,
) -> str | None:
    """Queue promote_script after Checker PASS; skip if already promoted or queued."""
    if not LIBRARY_KEY.fullmatch(item_key):
        return None
    existing = connection.execute(
        """
        SELECT 1 FROM library_items
        WHERE scope = 'project' AND project_id = ?
          AND kind = 'script' AND item_key = ? AND sha256 = ?
        LIMIT 1
        """,
        (project_id, item_key, artifact_sha256),
    ).fetchone()
    if existing:
        return None
    pending = connection.execute(
        """
        SELECT id FROM messages
        WHERE project_id = ?
          AND entry_kind = 'action'
          AND processed_at IS NULL
          AND action_json LIKE ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (project_id, f'%"task_id":"{task_id}"%'),
    ).fetchone()
    if pending:
        try:
            # Narrow check in Python for promote_script on this task.
            row = connection.execute(
                "SELECT action_json FROM messages WHERE id = ?",
                (pending["id"],),
            ).fetchone()
            action = json.loads(row["action_json"]) if row else {}
            if (
                action.get("kind") == "promote_script"
                and action.get("task_id") == task_id
                and action.get("artifact_id") == artifact_id
            ):
                return str(pending["id"])
        except (TypeError, json.JSONDecodeError):
            pass
    message_id = uuid4().hex
    stamped = time.time() if now is None else now
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO messages(
            id, project_id, role, entry_kind, content, action_json,
            idempotency_key, created_at
        ) VALUES (?, ?, 'owner', 'action', ?, ?, ?, ?)
        """,
        (
            message_id,
            project_id,
            f"auto promote_script {item_key}",
            canonical_json(
                {
                    "kind": "promote_script",
                    "task_id": task_id,
                    "spec_revision": spec_revision,
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact_sha256,
                    "item_key": item_key,
                    "auto": True,
                }
            ),
            f"auto-promote:{task_id}:{artifact_id}:{spec_revision}",
            stamped,
        ),
    )
    if cursor.rowcount != 1:
        existing_id = connection.execute(
            """
            SELECT id FROM messages
            WHERE project_id = ? AND idempotency_key = ?
            """,
            (project_id, f"auto-promote:{task_id}:{artifact_id}:{spec_revision}"),
        ).fetchone()
        return str(existing_id["id"]) if existing_id else None
    return message_id
