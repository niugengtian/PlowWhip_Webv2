from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from plow_whip_web.domain.model import DomainError
from plow_whip_web.runtime.butler import route_goal
from plow_whip_web.runtime.execution_policy import ExecutionRoute
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing


RoleKind = str
WorkItemKind = Literal["coordination", "implementation", "verification"]

# Shared worktree write lanes must serialize. Do not infer from titles/providers.
SHARED_WORKTREE_SERIAL_ROLES: tuple[str, ...] = (
    "backend",
    "frontend",
    "devops_sre",
)

MODEL_PM_IMPLEMENTED = True


@dataclass(frozen=True, slots=True)
class PlannedWorkItem:
    ordinal: int
    role: RoleKind
    kind: WorkItemKind
    title: str
    objective: str
    depends_on_ordinals: tuple[int, ...]
    acceptance: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    provider: str | None = None
    auto_generated: bool = False


@dataclass(frozen=True, slots=True)
class GoalPlan:
    status: Literal["planned", "needs_planning"]
    missing_gates: tuple[str, ...]
    rationale: tuple[str, ...]
    items: tuple[PlannedWorkItem, ...]
    route: ExecutionRoute | None = None
    model_invoked: bool = False
    model_pm_implemented: bool = True


def plan_goal_work_items(
    *,
    title: str,
    objective: str,
    sizing_inputs: TaskSizingInputs,
    structured_items: list[dict[str, Any]] | None = None,
    execution_policy: dict[str, Any] | None = None,
    role_providers: dict[str, str] | None = None,
    model_invoked: bool = False,
) -> GoalPlan:
    """Build a bounded semantic role DAG; completion still uses task-local Gates."""
    gate_inputs = replace(sizing_inputs, independent_review_required=False)
    preview = estimate_task_sizing(gate_inputs)
    if preview["status"] == "needs_planning":
        return GoalPlan(
            status="needs_planning",
            missing_gates=tuple(preview["missing_gates"]),
            rationale=tuple(preview["rationale"]),
            items=(),
        )

    decision = route_goal(str(preview["size_class"]), execution_policy)
    policy, route = decision.policy, decision.route
    if structured_items is not None:
        items = _parse_structured_items(structured_items, route=route)
        # Multi-role structured plans keep named roles; force milestone route
        # semantics instead of collapsing every lane to one full-stack role.
        distinct_roles = {item.role for item in items}
        if len(items) > 1 or distinct_roles - {"fullstack"}:
            route = "capability-milestones"
        if route != "capability-milestones" and len(items) != 1:
            raise DomainError(f"{route} requires exactly one ephemeral work item")
        if len(items) > int(policy["max_milestones"]):
            raise DomainError("structured plan exceeds bounded milestone limit")
        items = _enforce_shared_worktree_serial(items)
        rationale = (
            "bounded milestone input accepted",
            f"project_execution_route={route}",
            "structured_role_identity_preserved",
        )
    else:
        items = _default_items(
            title=title,
            objective=objective,
            sizing_inputs=sizing_inputs,
            route=route,
            max_milestones=int(policy["max_milestones"]),
        )
        rationale = list(preview["rationale"]) + [
            f"project_execution_route={route}",
            "butler_aggregate_is_coordination_source",
            "shared_worktree_serial_dag",
        ]

    if role_providers:
        items = [
            replace(item, provider=role_providers.get(item.role, item.provider))
            for item in items
        ]
    items = _ensure_independent_verification(
        items,
        title=title,
        provider=(role_providers or {}).get("verification"),
    )
    validated = validate_goal_plan(
        items,
        available_roles={
            "fullstack", "backend", "frontend", "ui",
            "devops_sre", "verification", "web3",
        },
        sizing_preview=preview,
    )
    return GoalPlan(
        status="planned",
        missing_gates=(),
        rationale=tuple(rationale) + validated["rationale"],
        items=tuple(validated["items"]),
        route=route,
        model_invoked=model_invoked,
    )


def validate_goal_plan(
    items: list[PlannedWorkItem],
    *,
    available_roles: set[str],
    sizing_preview: dict[str, Any],
) -> dict[str, Any]:
    """Schema, capability, dependency, and verification checks. 0 Token."""
    if not items:
        raise DomainError("goal plan must contain at least one work item")
    if len(items) > 6:
        raise DomainError("goal plan exceeds 6 work items")
    ordinals = [item.ordinal for item in items]
    if len(set(ordinals)) != len(ordinals):
        raise DomainError("goal plan ordinals must be unique")
    if sorted(ordinals) != list(range(1, len(items) + 1)):
        raise DomainError("goal plan ordinals must be contiguous from 1")

    seen: set[int] = set()
    for item in items:
        if item.role not in available_roles:
            raise DomainError(f"unknown or unavailable role capability: {item.role}")
        if item.kind not in {"implementation", "verification"}:
            raise DomainError(f"invalid work item kind: {item.kind}")
        if (item.role == "verification") != (item.kind == "verification"):
            raise DomainError("verification role and work item kind must match")
        if not item.title.strip() or not item.objective.strip():
            raise DomainError("work item title/objective required")
        for dep in item.depends_on_ordinals:
            if dep not in seen:
                raise DomainError(f"work item {item.ordinal} depends on unseen ordinal {dep}")
            if dep >= item.ordinal:
                raise DomainError("dependencies must refer to earlier ordinals")
        seen.add(item.ordinal)

    return {
        "items": items,
        "rationale": (
            "schema_ok",
            "capability_ok",
            "dependency_ok",
            "task_local_verification_gate_ok",
        ),
    }


def child_sizing_inputs(
    *,
    base: TaskSizingInputs,
    item: PlannedWorkItem,
    total_items: int,
) -> TaskSizingInputs:
    """Derive per-child 0-Token sizing inputs from work-item metadata."""
    if item.kind == "verification":
        return replace(
            base,
            layers_touched=max(1, min(2, base.layers_touched)),
            components_touched=max(1, min(2, base.components_touched)),
            estimated_files_changed=max(1, min(2, base.estimated_files_changed)),
            has_migration=False,
            has_deploy=False,
            independent_review_required=False,
            risk_level="low" if base.risk_level == "low" else "medium",
        )
    share = max(1, total_items)
    return replace(
        base,
        layers_touched=max(1, (base.layers_touched + share - 1) // share),
        components_touched=max(1, (base.components_touched + share - 1) // share),
        estimated_files_changed=max(1, (base.estimated_files_changed + share - 1) // share),
        has_deploy=base.has_deploy,
        has_migration=base.has_migration,
        independent_review_required=False,
    )


def plan_to_dict(plan: GoalPlan) -> dict[str, Any]:
    return {
        "status": plan.status,
        "missing_gates": list(plan.missing_gates),
        "rationale": list(plan.rationale),
        "model_invoked": plan.model_invoked,
        "model_pm_implemented": MODEL_PM_IMPLEMENTED,
        "route": plan.route,
        "items": [
            {
                "ordinal": item.ordinal,
                "role": item.role,
                "kind": item.kind,
                "title": item.title,
                "objective": item.objective,
                "depends_on_ordinals": list(item.depends_on_ordinals),
                "acceptance": list(item.acceptance),
                "artifacts": list(item.artifacts),
                "provider": item.provider,
                "auto_generated": item.auto_generated,
            }
            for item in plan.items
        ],
    }


def _default_items(
    *,
    title: str,
    objective: str,
    sizing_inputs: TaskSizingInputs,
    route: ExecutionRoute,
    max_milestones: int,
) -> list[PlannedWorkItem]:
    if route == "ephemeral-fullstack":
        count, role = 1, "fullstack"
    else:
        roles = ["backend"]
        if sizing_inputs.layers_touched >= 2:
            roles.append("frontend")
        if sizing_inputs.components_touched >= 3:
            roles.append("ui")
        if sizing_inputs.has_deploy or sizing_inputs.has_migration:
            roles.append("devops_sre")
        count = min(max_milestones, len(roles))

    constraints = []
    if sizing_inputs.has_migration:
        constraints.append("migration safety and schema evidence")
    if sizing_inputs.has_deploy:
        constraints.append("rollback-safe deploy evidence")
    suffix = f" Include {', '.join(constraints)}." if constraints else ""
    selected = (
        [role] * count
        if route != "capability-milestones"
        else roles[:count]
    )
    items = [
        PlannedWorkItem(
            ordinal=index,
            role=selected[index - 1],
            kind="implementation",
            title=(
                title
                if count == 1
                else f"{title} · {selected[index - 1]} {index}/{count}"
            ),
            objective=(
                f"{objective}\n\nExecution route: {route}. "
                f"Complete the {selected[index - 1]} "
                f"role lane {index}/{count}; its verification Gate "
                f"must pass before termination.{suffix}"
            ),
            depends_on_ordinals=(),
        )
        for index in range(1, count + 1)
    ]
    return _enforce_shared_worktree_serial(items)


def _parse_structured_items(
    raw_items: list[dict[str, Any]], *, route: ExecutionRoute
) -> list[PlannedWorkItem]:
    """Preserve explicit roles from plan_items; never rewrite to route aliases."""
    del route  # Route may influence count limits elsewhere; roles stay faithful.
    items: list[PlannedWorkItem] = []
    for raw in raw_items:
        kind = str(raw.get("kind") or "")
        if kind not in {"implementation", "verification"}:
            raise DomainError("structured work item kind must be implementation or verification")
        depends = raw.get("depends_on_ordinals") or []
        if not isinstance(depends, list):
            raise DomainError("depends_on_ordinals must be a list")
        acceptance = raw.get("acceptance") or []
        artifacts = raw.get("artifacts") or []
        if not isinstance(acceptance, list) or not isinstance(artifacts, list):
            raise DomainError("acceptance/artifacts must be lists")
        role = str(raw.get("role") or "").strip()
        if not role:
            raise DomainError("structured plan_items require an explicit role")
        items.append(
            PlannedWorkItem(
                ordinal=int(raw["ordinal"]),
                role=role,
                kind=kind,  # type: ignore[arg-type]
                title=str(raw["title"]),
                objective=str(raw["objective"]),
                depends_on_ordinals=tuple(int(dep) for dep in depends),
                acceptance=tuple(str(item) for item in acceptance),
                artifacts=tuple(str(item) for item in artifacts),
                provider=(str(raw["provider"]) if raw.get("provider") else None),
            )
        )
    return items


def _enforce_shared_worktree_serial(
    items: list[PlannedWorkItem],
) -> list[PlannedWorkItem]:
    """Serialize backend → frontend → devops_sre on a shared worktree via DAG."""
    serial_set = set(SHARED_WORKTREE_SERIAL_ROLES)
    last_serial_ordinal: int | None = None
    rewritten: list[PlannedWorkItem] = []
    for item in sorted(items, key=lambda entry: entry.ordinal):
        deps = set(item.depends_on_ordinals)
        if item.role in serial_set and last_serial_ordinal is not None:
            deps.add(last_serial_ordinal)
        rewritten.append(replace(item, depends_on_ordinals=tuple(sorted(deps))))
        if item.role in serial_set:
            last_serial_ordinal = item.ordinal
    return rewritten


def _ensure_independent_verification(
    items: list[PlannedWorkItem],
    *,
    title: str,
    provider: str | None,
) -> list[PlannedWorkItem]:
    implementations = [
        item for item in items if item.kind == "implementation"
    ]
    if not implementations:
        return items
    verifications = [
        item for item in items if item.kind == "verification"
    ]
    implementation_ordinals = {
        item.ordinal for item in implementations
    }
    if not verifications:
        if len(items) >= 6:
            raise DomainError(
                "plan must reserve one of six work-item slots for independent verification"
            )
        items = [
            *items,
            PlannedWorkItem(
                ordinal=len(items) + 1,
                role="verification",
                kind="verification",
                title=f"{title} · 独立验收",
                objective=(
                    "只读复验所有候选实现及声明的验收项；"
                    "输出结构化 PASS 或 CHANGES_REQUIRED，禁止补代码。"
                ),
                depends_on_ordinals=tuple(sorted(implementation_ordinals)),
                provider=provider,
                auto_generated=True,
            ),
        ]
        return items
    verifier = verifications[-1]
    if verifier.ordinal <= max(implementation_ordinals):
        raise DomainError("independent verification must follow every implementation item")
    required_dependencies = tuple(
        sorted(set(verifier.depends_on_ordinals) | implementation_ordinals)
    )
    return [
        replace(item, depends_on_ordinals=required_dependencies)
        if item.ordinal == verifier.ordinal else item
        for item in items
    ]
