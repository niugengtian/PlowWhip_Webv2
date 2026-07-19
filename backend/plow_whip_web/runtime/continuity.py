from __future__ import annotations

import json
import re
from typing import Any

from plow_whip_web.domain.model import DomainError, InvalidTransitionError


LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "context_max_bytes": (4096, 1_048_576),
    "checkpoint_max_bytes": (512, 262_144),
    "handoff_max_bytes": (512, 262_144),
    "observation_tail_lines": (1, 200),
    "observation_max_bytes": (1024, 1_048_576),
    "rotation_max_bytes": (16_384, 16_777_216),
    "max_same_failure": (1, 20),
    "max_no_progress": (1, 20),
    "session_no_progress_rotation_threshold": (1, 20),
}

_DECLARATION = re.compile(
    r"(?m)^Continuity-Limits:\s*(\{[^\n]*\})\s*$"
)


def resolve_continuity_limits(
    settings: dict[str, Any], conventions: list[dict[str, Any]]
) -> dict[str, Any]:
    values = {key: int(settings[key]) for key in LIMIT_BOUNDS}
    sources = {key: "global_setting" for key in LIMIT_BOUNDS}
    for convention in conventions:
        declaration = _parse_declaration(str(convention.get("content") or ""))
        if not declaration:
            continue
        scope = str(convention.get("scope") or "unknown")
        scope_id = str(convention.get("scope_id") or "unknown")
        revision = int(convention.get("revision") or 0)
        for key, raw in declaration.items():
            if key not in LIMIT_BOUNDS:
                raise DomainError(f"unknown continuity limit: {key}")
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise DomainError(f"continuity limit must be an integer: {key}")
            lower, upper = LIMIT_BOUNDS[key]
            if not lower <= raw <= upper:
                raise DomainError(
                    f"continuity limit {key} must be between {lower} and {upper}"
                )
            values[key] = raw
            sources[key] = f"{scope}_convention:{scope_id}@{revision}"
    warnings: list[str] = []
    if values["handoff_max_bytes"] + values["checkpoint_max_bytes"] > values[
        "context_max_bytes"
    ]:
        warnings.append(
            "checkpoint and handoff maxima exceed the Context maximum; "
            "lower-priority sections may be truncated"
        )
    if values["observation_max_bytes"] > values["rotation_max_bytes"]:
        warnings.append(
            "one observation may exceed the hot journal rotation maximum"
        )
    return {"values": values, "sources": sources, "warnings": warnings}


def bounded_same_task_object(
    value: dict[str, Any], task_id: str, *, maximum_bytes: int, label: str
) -> dict[str, Any]:
    supplied = value.get("task_id")
    if supplied is not None and supplied != task_id:
        raise InvalidTransitionError(f"{label} cannot cross Task identity")
    bounded = {**value, "task_id": task_id}
    size = len(
        json.dumps(
            bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    if size > maximum_bytes:
        raise DomainError(
            f"{label} is {size} bytes; effective maximum is {maximum_bytes} bytes"
        )
    return bounded


def _parse_declaration(content: str) -> dict[str, Any]:
    matches = _DECLARATION.findall(content)
    if not matches:
        return {}
    if len(matches) > 1:
        raise DomainError("Convention contains multiple Continuity-Limits declarations")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as error:
        raise DomainError(f"invalid Continuity-Limits JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise DomainError("Continuity-Limits must be a JSON object")
    return value
