"""Decide when Owner 「继续」 must not isomorphic-retry empty delivery."""

from __future__ import annotations

import json
import sqlite3

NO_PROGRESS_MARKERS = (
    "internal_tool_no_progress",
    "tool_no_progress_limit",
    "tool_no_progress_limit_exceeded",
    "bounded tool loop exceeded",
    "repeated_tool_call_no_progress",
)

MISSING_DELIVERY_MARKERS = (
    "declared workspace artifact is missing",
    "formal result manifest is empty",
)


def wait_indicates_missing_delivery(wait_reason: str | None) -> bool:
    text = str(wait_reason or "").lower()
    return any(marker in text for marker in MISSING_DELIVERY_MARKERS)


def recent_no_progress_failure(
    connection: sqlite3.Connection, task_id: str
) -> bool:
    rows = connection.execute(
        """
        SELECT kind, detail_json FROM task_events
        WHERE task_id = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 40
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        blob = f"{row['kind']}\n{row['detail_json'] or ''}"
        if any(marker in blob for marker in NO_PROGRESS_MARKERS):
            return True
    jobs = connection.execute(
        """
        SELECT failure_code, dispatch_json FROM host_jobs
        WHERE task_id = ?
        ORDER BY sequence DESC
        LIMIT 8
        """,
        (task_id,),
    ).fetchall()
    for job in jobs:
        blob = f"{job['failure_code'] or ''}\n{job['dispatch_json'] or ''}"
        if any(marker in blob for marker in NO_PROGRESS_MARKERS):
            return True
        try:
            dispatch = json.loads(job["dispatch_json"] or "{}")
        except json.JSONDecodeError:
            continue
        failure = str(dispatch.get("failure_class") or "")
        if failure in NO_PROGRESS_MARKERS or "no_progress" in failure:
            return True
    return False


def reject_isomorphic_empty_delivery_continue(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
) -> str | None:
    """Return wait_reason if 「继续」 must be rejected; else None.

    LIVE-DS-23: missing declared Artifact / empty formal manifest after a
    no-progress style HostJob must not decision_retry the same TaskSpec.
    Same-provider retry for other failures remains allowed (owner policy).
    """
    if not wait_indicates_missing_delivery(task["wait_reason"]):
        return None
    if not recent_no_progress_failure(connection, str(task["id"])):
        return None
    return (
        "missing Artifact after no-progress delivery; isomorphic 继续 is blocked. "
        "Cancel and shrink/replan the Goal, or change strategy — do not reopen the "
        "same HostJob shape"
    )
