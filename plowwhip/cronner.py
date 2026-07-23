from __future__ import annotations

import fcntl
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from .continuity import checkpoint_project
from .lifecycle import advance_project
from .store import Store


LEASE_SECONDS = 30


def acquire_scheduler_lock(data_root) -> object:
    path = data_root / ".cronner.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            "another image instance already owns the scheduler lock"
        ) from None
    return handle


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
                      AND (
                        t.next_action_at <= ?
                        OR (t.deadline_at IS NOT NULL AND t.deadline_at <= ?)
                      )
                )
                OR EXISTS (
                    SELECT 1 FROM tasks queued
                    WHERE queued.project_id = p.id AND queued.outcome IS NULL
                      AND queued.phase = 'queued'
                      AND (
                        NOT EXISTS (
                            SELECT 1 FROM task_dependencies edge
                            JOIN tasks dependency ON dependency.id = edge.depends_on_task_id
                            WHERE edge.task_id = queued.id AND dependency.outcome IS NOT 'done'
                        )
                        OR EXISTS (
                            SELECT 1 FROM task_dependencies edge
                            JOIN tasks dependency ON dependency.id = edge.depends_on_task_id
                            WHERE edge.task_id = queued.id AND dependency.outcome = 'cancelled'
                        )
                      )
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
            (now, now, now, limit),
        ).fetchall()
    finally:
        connection.close()

    project_ids = [project["id"] for project in projects]
    if len(project_ids) == 1:
        result = _advance_due_project(store, project_ids[0])
        return [result] if result else []
    if not project_ids:
        return []
    # ponytail: stdlib worker default bounds global project concurrency; same-project
    # serialization remains enforced by its lease and unique active Task constraint.
    with ThreadPoolExecutor(thread_name_prefix="plowwhip-project") as pool:
        return [
            result
            for result in pool.map(lambda project_id: _advance_due_project(store, project_id), project_ids)
            if result
        ]


def _advance_due_project(store: Store, project_id: str) -> dict[str, str] | None:
    lease = _claim(store, project_id)
    if not lease:
        return None
    token, fence = lease
    try:
        action = advance_project(store, project_id, token, fence)
        checkpoint_project(store, project_id)
        return {
            "project_id": project_id,
            "action": action,
            "status": _latest_status(store, project_id),
        }
    finally:
        _release(store, project_id, token, fence)


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
            WHERE project_id = ?
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
