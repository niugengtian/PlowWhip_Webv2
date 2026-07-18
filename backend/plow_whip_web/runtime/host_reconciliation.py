from __future__ import annotations

from typing import Literal


DispatchOutcome = Literal["accepted", "rejected", "unknown"]
RECONCILIATION_SECONDS = 120
_UNCONFIRMED_STATUSES = {"dispatching", "unknown", "recovery_hold"}


def dispatch_outcome(status: str, *, host_pid: int | None = None) -> DispatchOutcome:
    """Reduce a Host snapshot to the only three dispatch outcomes."""
    if status == "rejected":
        return "rejected"
    if status in _UNCONFIRMED_STATUSES and host_pid is None:
        return "unknown"
    return "accepted"


def requires_reconciliation(outcome: str, status: str) -> bool:
    """Recovery hold is bounded even after an earlier accepted dispatch."""
    return outcome == "unknown" or status == "recovery_hold"


def reconciliation_deadline_modifier(
    seconds: int = RECONCILIATION_SECONDS,
) -> str:
    return f"+{max(1, seconds)} seconds"
