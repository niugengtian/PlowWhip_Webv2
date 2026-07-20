from __future__ import annotations

from typing import Any, Literal


POLICY_VERSION = "butler-v2"
ExecutionRoute = Literal[
    "ephemeral-fullstack", "capability-milestones"
]

DEFAULT_PROJECT_EXECUTION_POLICY: dict[str, Any] = {
    "version": POLICY_VERSION,
    "routing": {
        "XS": "ephemeral-fullstack",
        "S": "ephemeral-fullstack",
        "M": "ephemeral-fullstack",
        "L": "capability-milestones",
        "XL": "capability-milestones",
    },
    "max_milestones": 6,
    "verification_gate_required": True,
    "release_worker_on_terminal": True,
}


def project_execution_policy(value: dict[str, Any] | None = None) -> dict[str, Any]:
    unknown = set(value or {}) - set(DEFAULT_PROJECT_EXECUTION_POLICY)
    if unknown:
        raise ValueError(f"unknown execution policy fields: {', '.join(sorted(unknown))}")
    if (
        value
        and "routing" in value
        and value["routing"] != DEFAULT_PROJECT_EXECUTION_POLICY["routing"]
    ):
        raise ValueError(f"execution policy routing is fixed by {POLICY_VERSION}")
    policy = {
        **DEFAULT_PROJECT_EXECUTION_POLICY,
        **(value or {}),
        "routing": dict(DEFAULT_PROJECT_EXECUTION_POLICY["routing"]),
    }
    if policy["version"] != POLICY_VERSION:
        raise ValueError(f"unsupported execution policy version: {policy['version']}")
    if not 2 <= int(policy["max_milestones"]) <= 6:
        raise ValueError("max_milestones must be between 2 and 6")
    if policy["verification_gate_required"] is not True:
        raise ValueError("verification_gate_required cannot be disabled")
    if policy["release_worker_on_terminal"] is not True:
        raise ValueError("release_worker_on_terminal cannot be disabled")
    return policy


def route_for_size(size_class: str, policy: dict[str, Any]) -> ExecutionRoute:
    route = policy["routing"].get(size_class)
    if route not in {
        "ephemeral-fullstack", "capability-milestones"
    }:
        raise ValueError(f"unsupported execution route for {size_class}: {route}")
    return route
