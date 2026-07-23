from __future__ import annotations

import re

from .intake import normalize_instruction


TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_plan(plan: object) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    alternatives = plan.get("alternatives")
    tasks = plan.get("tasks")
    selected = plan.get("selected")
    if not isinstance(alternatives, list) or len(alternatives) < 2:
        raise ValueError("large plan requires at least two alternatives")
    comparison = {"name", "scope", "cost", "risk", "reversible", "acceptance"}
    if any(not isinstance(item, dict) or not comparison <= item.keys() for item in alternatives):
        raise ValueError("each alternative must compare scope, cost, risk, reversibility and acceptance")
    if not isinstance(selected, int) or not 0 <= selected < len(alternatives):
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
        normalized.append(
            {
                "key": key,
                "spec": {**spec, "task_key": key},
                "acceptance": acceptance,
                "depends_on": dependencies,
                "sprint": int(item.get("sprint", 1)),
                "role_key": str(item.get("role_key", "deterministic")),
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
