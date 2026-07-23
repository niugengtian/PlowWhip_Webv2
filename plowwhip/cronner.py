from __future__ import annotations

import logging
import threading
import time
from uuid import uuid4

from .lifecycle import advance_project
from .store import Store


LEASE_SECONDS = 30


def tick(store: Store, limit: int = 100) -> list[dict[str, str]]:
    """Wake due projects and advance each project by at most one action."""
    now = time.time()
    connection = store.connect()
    try:
        projects = connection.execute(
            """
            SELECT p.id FROM projects p
            WHERE (p.lease_until IS NULL OR p.lease_until < ?)
              AND (
                EXISTS (
                    SELECT 1 FROM tasks t
                    WHERE t.project_id = p.id
                      AND t.outcome IS NULL
                      AND t.public_status IN ('pending', 'in_progress')
                      AND t.next_action_at <= ?
                )
                OR EXISTS (
                    SELECT 1 FROM messages action
                    WHERE action.project_id = p.id
                      AND action.processed_at IS NULL
                      AND action.action_json IS NOT NULL
                )
                OR (
                    NOT EXISTS (
                        SELECT 1 FROM tasks active
                        WHERE active.project_id = p.id
                          AND active.outcome IS NULL
                          AND active.public_status IN ('pending', 'in_progress', 'needs_decision')
                    )
                    AND EXISTS (
                        SELECT 1 FROM messages m
                        WHERE m.project_id = p.id AND m.processed_at IS NULL
                    )
                )
              )
            ORDER BY p.created_at LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
    finally:
        connection.close()

    results = []
    for project in projects:
        lease = _claim(store, project["id"])
        if not lease:
            continue
        token, fence = lease
        try:
            action = advance_project(store, project["id"], token, fence)
            results.append(
                {
                    "project_id": project["id"],
                    "action": action,
                    "status": _latest_status(store, project["id"]),
                }
            )
        finally:
            _release(store, project["id"], token, fence)
    return results


def run_until_idle(store: Store, max_actions: int = 100) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    while len(results) < max_actions:
        batch = tick(store, limit=max_actions - len(results))
        if not batch:
            return results
        results.extend(batch)
    raise RuntimeError("cronner max_actions reached before idle")


def run(store: Store, stop: threading.Event, interval_seconds: float = 1.0) -> None:
    while not stop.is_set():
        try:
            tick(store)
        except Exception:
            logging.exception("cronner tick failed")
        stop.wait(interval_seconds)


def _latest_status(store: Store, project_id: str) -> str:
    connection = store.connect()
    try:
        row = connection.execute(
            """
            SELECT public_status, outcome FROM tasks
            WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        return row["outcome"] or row["public_status"] if row else "pending"
    finally:
        connection.close()


def _claim(store: Store, project_id: str) -> tuple[str, int] | None:
    token = uuid4().hex
    now = time.time()
    with store.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE projects
            SET lease_token = ?, lease_fence = lease_fence + 1, lease_until = ?
            WHERE id = ? AND (lease_until IS NULL OR lease_until < ?)
            """,
            (token, now + LEASE_SECONDS, project_id, now),
        )
        if cursor.rowcount != 1:
            return None
        fence = connection.execute(
            "SELECT lease_fence FROM projects WHERE id = ?", (project_id,)
        ).fetchone()["lease_fence"]
    return token, int(fence)


def _release(store: Store, project_id: str, token: str, fence: int) -> None:
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE projects SET lease_token = NULL, lease_until = NULL
            WHERE id = ? AND lease_token = ? AND lease_fence = ?
            """,
            (project_id, token, fence),
        )
