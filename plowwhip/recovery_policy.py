"""Same-problem recovery hard cap.

If the same Task keeps recovering (provider retry/fallback, owner continue,
planner/checker retry) past this limit, further automatic or "继续" recovery is
fail-closed. That surfaces a mechanism gap that must be fixed — not more burns.
"""

from __future__ import annotations

import sqlite3
import hashlib
import json

# Owner rule: same problem ≤5 retries; the 6th attempt means a blocking gap.
MAX_SAME_PROBLEM_RETRIES = 5

RECOVERY_EVENT_KINDS = (
    "provider_retry",
    "provider_fallback",
    "planner_retry_requested",
    "decision_retry",
    "checker_retry_requested",
)


def clamp_retry_count(value: object) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    return max(0, min(count, MAX_SAME_PROBLEM_RETRIES))


def failure_signature(
    spec_revision: object,
    phase: object,
    failure_class: object,
    result_identity: object = None,
) -> str:
    """Stable bucket identity for retries of one unchanged failure."""
    payload = {
        "spec_revision": int(spec_revision or 0),
        "phase": str(phase or ""),
        "failure_class": str(failure_class or ""),
        "result_identity": result_identity,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def count_recovery_attempts(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    phase: str | None = None,
    failure_class: str | None = None,
    result_identity: object = None,
) -> int:
    task = connection.execute(
        "SELECT spec_revision, phase, fault_code FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not task:
        return 0
    expected = failure_signature(
        task["spec_revision"],
        phase or task["phase"],
        failure_class or task["fault_code"] or task["phase"],
        result_identity,
    )
    placeholders = ",".join("?" for _ in RECOVERY_EVENT_KINDS)
    rows = connection.execute(
        f"""
        SELECT detail_json FROM task_events
        WHERE task_id = ? AND kind IN ({placeholders})
        """,
        (task_id, *RECOVERY_EVENT_KINDS),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, json.JSONDecodeError):
            detail = {}
        signature = detail.get("failure_signature")
        if signature is None:
            # Pre-V3 events lack the immutable failure identity and cannot be
            # safely assigned to a later revision.
            continue
        if signature == expected:
            count += 1
    return count


def recovery_cap_reached(
    connection: sqlite3.Connection, task_id: str
) -> bool:
    return count_recovery_attempts(connection, task_id) >= MAX_SAME_PROBLEM_RETRIES


def recovery_cap_wait_reason(attempts: int, prior: str | None = None) -> str:
    prior_text = (prior or "").strip()
    message = (
        f"same problem recovered {attempts} times "
        f"(cap={MAX_SAME_PROBLEM_RETRIES}); blocking mechanism gap must be fixed "
        "before continue"
    )
    if prior_text and prior_text not in message:
        return f"{message}; prior={prior_text[:240]}"
    return message
