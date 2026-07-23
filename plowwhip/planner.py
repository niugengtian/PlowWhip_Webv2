from __future__ import annotations

import re

from .intake import normalize_instruction


TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_SETTING_LIMITS = {
    "max_runtime_seconds": (1, 86_400),
    "stop_grace_seconds": (0, 300),
    "handoff_max_bytes": (512, 1_048_576),
    "checkpoint_max_bytes": (512, 1_048_576),
    "monitor_tail_lines": (1, 1_000),
    "monitor_tail_bytes": (256, 1_048_576),
    "retry_count": (0, 10),
    "retry_backoff_seconds": (0, 86_400),
}


def normalize_plan(plan: object) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    alternatives = plan.get("alternatives")
    tasks = plan.get("tasks")
    selected = plan.get("selected")
    if not isinstance(alternatives, list) or len(alternatives) < 2:
        raise ValueError("large plan requires at least two alternatives")
    comparison = {"name", "scope", "cost", "risk", "reversible", "acceptance"}
    if any(
        not isinstance(item, dict)
        or not comparison <= item.keys()
        or not all(str(item[key]).strip() for key in comparison - {"reversible"})
        or not isinstance(item["reversible"], bool)
        for item in alternatives
    ):
        raise ValueError("each alternative must compare scope, cost, risk, reversibility and acceptance")
    if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(alternatives):
        raise ValueError("selected alternative is invalid")
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= 50:
        raise ValueError("plan requires 2-50 tasks")

    normalized = []
    keys = set()
    for item in tasks:
        if not isinstance(item, dict) or not TASK_KEY.fullmatch(str(item.get("key", ""))):
            raise ValueError("each task requires a safe key")
        key = item["key"]
        if key in keys:
            raise ValueError(f"duplicate task key: {key}")
        keys.add(key)
        spec, acceptance = normalize_instruction(str(item.get("instruction", "")))
        if spec["kind"] != "write_text":
            raise ValueError(f"task {key} is not deterministic")
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(value, str) for value in dependencies
        ):
            raise ValueError(f"task {key} has invalid dependencies")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"task {key} has duplicate dependencies")
        sprint = item.get("sprint", 1)
        if isinstance(sprint, bool) or not isinstance(sprint, int) or not 1 <= sprint <= 10_000:
            raise ValueError(f"task {key} has invalid sprint")
        role_key = str(item.get("role_key", "deterministic"))
        if role_key != "deterministic":
            raise ValueError(f"task {key} requires a disabled Provider")
        settings = _normalize_task_settings(key, role_key, item.get("settings", {}))
        normalized.append(
            {
                "key": key,
                "spec": {**spec, "task_key": key},
                "acceptance": acceptance,
                "depends_on": dependencies,
                "sprint": sprint,
                "role_key": role_key,
                "settings": settings,
            }
        )

    if any(dependency not in keys for item in normalized for dependency in item["depends_on"]):
        raise ValueError("dependency refers to an unknown task")
    ordered = []
    remaining = {item["key"]: item for item in normalized}
    # ponytail: O(n²) is bounded to 50 tasks; use an indegree map only if that ceiling grows.
    while remaining:
        ready = [item for item in remaining.values() if set(item["depends_on"]) <= {x["key"] for x in ordered}]
        if not ready:
            raise ValueError("task dependency graph contains a cycle")
        for item in ready:
            ordered.append(item)
            del remaining[item["key"]]

    return {
        "alternatives": alternatives,
        "selected": selected,
        "summary": str(plan.get("summary", "")),
        "tasks": ordered,
    }


def _normalize_task_settings(task_key: str, role_key: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} settings must be an object")
    allowed_roles = {role_key, "deterministic_checker"}
    if any(key not in allowed_roles or not isinstance(value, dict) for key, value in raw.items()):
        raise ValueError(f"task {task_key} settings must be keyed by its two roles")
    normalized = {}
    for target_role, values in raw.items():
        role_values = {}
        for name, value in values.items():
            if name == "provider_order":
                if value != ["local"]:
                    raise ValueError(f"task {task_key} cannot enable an external Provider")
                role_values[name] = value
                continue
            limits = TASK_SETTING_LIMITS.get(name)
            if not limits or isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"task {task_key} has invalid setting {name}")
            if not limits[0] <= value <= limits[1]:
                raise ValueError(f"task {task_key} setting {name} is out of range")
            role_values[name] = value
        normalized[target_role] = role_values
    return normalized
