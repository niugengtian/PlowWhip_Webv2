"""Same-problem recovery hard cap.

If the same Task keeps recovering (provider retry/fallback, owner continue,
planner/checker retry) past this limit, further automatic or "继续" recovery is
fail-closed. That surfaces a mechanism gap that must be fixed — not more burns.
"""

from __future__ import annotations

import sqlite3

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


def count_recovery_attempts(
    connection: sqlite3.Connection, task_id: str
) -> int:
    placeholders = ",".join("?" for _ in RECOVERY_EVENT_KINDS)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS n FROM task_events
        WHERE task_id = ? AND kind IN ({placeholders})
        """,
        (task_id, *RECOVERY_EVENT_KINDS),
    ).fetchone()
    return int(row["n"] if row else 0)


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
