"""Role-aware progress contract for Provider HostJobs.

Workspace writes are progress only for mutation work. Planner, Checker, and
read-only audits use exploration progress (bounded turns / unique reads), so
json-worker adapters are not forced into a Cursor-shaped write-or-die loop.
"""

from __future__ import annotations

PROGRESS_EXPLORATION = "exploration"
PROGRESS_MUTATION = "mutation"
PROGRESS_MODES = frozenset({PROGRESS_EXPLORATION, PROGRESS_MUTATION})

_EXPLORATION = {
    "progress_mode": PROGRESS_EXPLORATION,
    # Advisory only for exploration; wall-clock + I/O phase own failure.
    "tool_no_progress_limit": 256,
    "max_turns": 48,
}
# Audit/report delivery still explores many files, then must write one Artifact.
# DeepSeek json-worker regularly exhausts 48 turns before the report write.
_AUDIT_REPORT = {
    "progress_mode": PROGRESS_EXPLORATION,
    "tool_no_progress_limit": 256,
    "max_turns": 96,
    "progress_delivery": "audit_report",
}
_MUTATION = {
    "progress_mode": PROGRESS_MUTATION,
    "tool_no_progress_limit": 12,
    "max_turns": 24,
}


def resolve_progress_policy(
    *,
    access: str,
    workspace_kind: str = "project",
    delivery: str | None = None,
) -> dict[str, object]:
    """Derive progress semantics from HostJob access / workspace kind / delivery."""
    if access not in {"read", "write"}:
        raise ValueError("Provider access must be read or write")
    if workspace_kind not in {"project", "planner"}:
        raise ValueError("unsupported Provider workspace kind")
    if delivery not in {None, "audit_report"}:
        raise ValueError("unsupported progress delivery")
    # Audit/report HostJobs may write a report Artifact but still need many
    # unique reads before the first write (MECH-05 / LIVE-DS-09).
    if delivery == "audit_report":
        return dict(_AUDIT_REPORT)
    if access == "read" or workspace_kind == "planner":
        return dict(_EXPLORATION)
    return dict(_MUTATION)


def merge_context_policy(
    context_policy: dict[str, object] | None,
    *,
    access: str,
    workspace_kind: str = "project",
    delivery: str | None = None,
) -> dict[str, object]:
    """Merge caller context with role-resolved progress fields.

    Explicit keys in ``context_policy`` win (e.g. probe jobs with max_turns=1).
    Missing progress fields are filled from the role policy.
    """
    raw = dict(context_policy or {})
    if delivery is None and raw.get("progress_delivery") == "audit_report":
        delivery = "audit_report"
    resolved = resolve_progress_policy(
        access=access, workspace_kind=workspace_kind, delivery=delivery
    )
    merged = {**resolved, **raw}
    if delivery == "audit_report":
        merged["progress_delivery"] = "audit_report"
    if "progress_mode" not in raw:
        merged["progress_mode"] = resolved["progress_mode"]
    if "tool_no_progress_limit" not in raw:
        merged["tool_no_progress_limit"] = resolved["tool_no_progress_limit"]
    if "max_turns" not in raw:
        merged["max_turns"] = resolved["max_turns"]
    mode = str(merged.get("progress_mode") or resolved["progress_mode"])
    if mode not in PROGRESS_MODES:
        raise ValueError("progress_mode must be exploration or mutation")
    merged["progress_mode"] = mode
    return merged
