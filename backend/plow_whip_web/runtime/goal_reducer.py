from __future__ import annotations

from collections.abc import Collection


def reduce_goal_status(
    *,
    safety_reason: str | None,
    child_statuses: Collection[str],
    all_completed: bool,
    has_autonomy_blocker: bool,
) -> tuple[str, str]:
    """Derive Goal state from current immutable specs, task facts, and evidence."""
    if safety_reason or has_autonomy_blocker:
        return "needs_human", safety_reason or "child_autonomy_blocker"
    if "terminal_failed" in child_statuses:
        return "terminal_failed", "child_terminal_failed"
    if "cancelled" in child_statuses:
        return "cancelled", "child_cancelled"
    if all_completed:
        return "completed", "task_gates_verified"
    return (
        "running",
        "child_replan_required"
        if "needs_human" in child_statuses
        else "tasks_in_progress",
    )
