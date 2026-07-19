from __future__ import annotations

from typing import Any, Literal


EscalationClass = Literal[
    "credential_or_permission",
    "safety_or_irreversible",
    "conflicting_owner_directives",
    "unresolvable_requirement_ambiguity",
]

ESCALATION_CLASSES: tuple[EscalationClass, ...] = (
    "credential_or_permission",
    "safety_or_irreversible",
    "conflicting_owner_directives",
    "unresolvable_requirement_ambiguity",
)

HELP_REQUIRED_FIELDS = ("blocker", "evidence", "attempted_actions", "minimal_question")


def validate_worker_help_request(payload: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in HELP_REQUIRED_FIELDS if not _present(payload.get(field))]
    if missing:
        raise ValueError(f"worker help request missing fields: {', '.join(missing)}")
    evidence = payload.get("evidence")
    attempted = payload.get("attempted_actions")
    if not isinstance(evidence, (dict, list)):
        raise ValueError("evidence must be an object or list")
    if not isinstance(attempted, list) or not attempted:
        raise ValueError("attempted_actions must be a non-empty list")
    return {
        "blocker": str(payload["blocker"]).strip(),
        "evidence": evidence,
        "attempted_actions": [str(item).strip() for item in attempted if str(item).strip()],
        "minimal_question": str(payload["minimal_question"]).strip(),
        "model_invoked": False,
    }


def is_extreme_escalation(reason_class: str) -> bool:
    return reason_class in ESCALATION_CLASSES


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True
