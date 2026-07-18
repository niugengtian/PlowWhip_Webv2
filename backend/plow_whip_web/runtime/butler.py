from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plow_whip_web.runtime.execution_policy import (
    ExecutionRoute,
    project_execution_policy,
    route_for_size,
)


@dataclass(frozen=True, slots=True)
class ButlerRoute:
    policy: dict[str, Any]
    route: ExecutionRoute


def route_goal(
    size_class: str, execution_policy: dict[str, Any] | None = None
) -> ButlerRoute:
    """Canonical Butler entry for one project-level routing decision."""
    policy = project_execution_policy(execution_policy)
    return ButlerRoute(policy=policy, route=route_for_size(size_class, policy))


__all__ = ["ButlerRoute", "project_execution_policy", "route_goal"]
