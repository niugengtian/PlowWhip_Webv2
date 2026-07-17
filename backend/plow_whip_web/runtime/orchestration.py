from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from plow_whip_web.domain.model import DomainError
from plow_whip_web.roles import ROLE_KINDS
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing


RoleKind = str
WorkItemKind = Literal["coordination", "implementation", "verification"]

# Model PM is not safely wired this sprint. Deterministic validation + optional
# structured plan input only. Do not claim model PM completion.
MODEL_PM_IMPLEMENTED = False


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


@dataclass(frozen=True, slots=True)
class GoalPlan:
    status: Literal["planned", "needs_planning"]
    missing_gates: tuple[str, ...]
    rationale: tuple[str, ...]
    items: tuple[PlannedWorkItem, ...]
    model_invoked: Literal[False] = False
    model_pm_implemented: Literal[False] = False


def plan_goal_work_items(
    *,
    title: str,
    objective: str,
    sizing_inputs: TaskSizingInputs,
    structured_items: list[dict[str, Any]] | None = None,
) -> GoalPlan:
    """Validate a structured plan, or build a deterministic default (no keyword roles)."""
    gate_inputs = replace(sizing_inputs, independent_review_required=False)
    preview = estimate_task_sizing(gate_inputs)
    if preview["status"] == "needs_planning":
        return GoalPlan(
            status="needs_planning",
            missing_gates=tuple(preview["missing_gates"]),
            rationale=tuple(preview["rationale"]),
            items=(),
        )

    if structured_items is not None:
        items = _parse_structured_items(structured_items)
        rationale = (
            "structured plan accepted",
            "deterministic layer validates schema/capability/deps/budget/safety only",
            "model_pm_implemented=false",
        )
    else:
        items = _default_items(title=title, objective=objective, sizing_inputs=sizing_inputs)
        rationale = list(preview["rationale"]) + [
            "default deterministic template (no keyword role routing)",
            "model_pm_implemented=false",
        ]

    validated = validate_goal_plan(
        items,
        available_roles=set(ROLE_KINDS),
        sizing_preview=preview,
    )
    return GoalPlan(
        status="planned",
        missing_gates=(),
        rationale=tuple(rationale) + validated["rationale"],
        items=tuple(validated["items"]),
    )


def validate_goal_plan(
    items: list[PlannedWorkItem],
    *,
    available_roles: set[str],
    sizing_preview: dict[str, Any],
) -> dict[str, Any]:
    """Schema / capability / dependency / budget / safety checks. 0 Token."""
    if not items:
        raise DomainError("goal plan must contain at least one work item")
    if len(items) > 7:
        raise DomainError("goal plan exceeds 7 work items")
    ordinals = [item.ordinal for item in items]
    if len(set(ordinals)) != len(ordinals):
        raise DomainError("goal plan ordinals must be unique")
    if sorted(ordinals) != list(range(1, len(items) + 1)):
        raise DomainError("goal plan ordinals must be contiguous from 1")

    seen: set[int] = set()
    has_verification = False
    for item in items:
        if item.role not in available_roles:
            raise DomainError(f"unknown or unavailable role capability: {item.role}")
        if item.kind not in {"implementation", "verification"}:
            raise DomainError(f"invalid work item kind: {item.kind}")
        if item.kind == "verification":
            has_verification = True
            if item.role != "verification":
                raise DomainError("verification work items must use verification role")
        if not item.title.strip() or not item.objective.strip():
            raise DomainError("work item title/objective required")
        for dep in item.depends_on_ordinals:
            if dep not in seen:
                raise DomainError(f"work item {item.ordinal} depends on unseen ordinal {dep}")
            if dep >= item.ordinal:
                raise DomainError("dependencies must refer to earlier ordinals")
        seen.add(item.ordinal)

    if not has_verification:
        raise DomainError("goal plan must end with an independent verification work item")
    if items[-1].kind != "verification":
        raise DomainError("final work item must be verification")

    hard_cap = sizing_preview.get("total_token_hard_cap")
    if hard_cap is not None and int(hard_cap) > 1_500_000:
        raise DomainError("goal budget exceeds safety hard cap")

    return {
        "items": items,
        "rationale": (
            "schema_ok",
            "capability_ok",
            "dependency_ok",
            "budget_safety_ok",
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
    # Split complexity across implementation slices without keyword routing.
    share = max(1, total_items - 1)
    return replace(
        base,
        layers_touched=max(1, (base.layers_touched + share - 1) // share),
        components_touched=max(1, (base.components_touched + share - 1) // share),
        estimated_files_changed=max(1, (base.estimated_files_changed + share - 1) // share),
        has_deploy=base.has_deploy and item.role == "devops_sre",
        has_migration=base.has_migration and item.role in {"backend", "fullstack", "devops_sre"},
        independent_review_required=False,
    )


def plan_to_dict(plan: GoalPlan) -> dict[str, Any]:
    return {
        "status": plan.status,
        "missing_gates": list(plan.missing_gates),
        "rationale": list(plan.rationale),
        "model_invoked": False,
        "model_pm_implemented": MODEL_PM_IMPLEMENTED,
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
            }
            for item in plan.items
        ],
    }


def _default_items(
    *,
    title: str,
    objective: str,
    sizing_inputs: TaskSizingInputs,
) -> list[PlannedWorkItem]:
    """Deterministic template from sizing flags only — no title/objective keyword routing."""
    items: list[PlannedWorkItem] = []
    next_ordinal = 1
    previous: list[int] = []

    def _add(
        role: RoleKind,
        kind: WorkItemKind,
        item_title: str,
        item_objective: str,
        *,
        acceptance: tuple[str, ...] = (),
        artifacts: tuple[str, ...] = (),
    ) -> None:
        nonlocal next_ordinal, previous
        depends = tuple(previous[-1:]) if previous else ()
        items.append(
            PlannedWorkItem(
                ordinal=next_ordinal,
                role=role,
                kind=kind,
                title=item_title,
                objective=item_objective,
                depends_on_ordinals=depends,
                acceptance=acceptance,
                artifacts=artifacts,
            )
        )
        previous.append(next_ordinal)
        next_ordinal += 1

    if sizing_inputs.has_deploy:
        _add(
            "devops_sre",
            "implementation",
            f"{title} · 部署与切换",
            (
                f"{objective}\n\n"
                "Role slice: DevOps/SRE. Prepare rollback-safe deploy/switch steps, "
                "observability checks, and leave verification commands for the next item."
            ),
            acceptance=("deploy_plan_present", "rollback_note_present"),
        )

    layers = max(1, int(sizing_inputs.layers_touched))
    if layers >= 3:
        _add(
            "backend",
            "implementation",
            f"{title} · 后端切片",
            (
                f"{objective}\n\n"
                "Role slice: backend. Deliver API/data/service changes with tests and "
                "evidence paths for downstream UI/frontend and verification."
            ),
            acceptance=("backend_tests_green",),
            artifacts=("backend-done.txt",),
        )
        _add(
            "frontend",
            "implementation",
            f"{title} · 前端切片",
            (
                f"{objective}\n\n"
                "Role slice: frontend. Wire UI to backend contracts; keep change surface "
                "minimal and leave verification hooks."
            ),
            acceptance=("frontend_smoke_ok",),
            artifacts=("frontend-done.txt",),
        )
        if sizing_inputs.components_touched >= 4:
            _add(
                "ui",
                "implementation",
                f"{title} · UI 切片",
                (
                    f"{objective}\n\n"
                    "Role slice: UI. Improve accessibility and interaction clarity without "
                    "expanding product scope."
                ),
                acceptance=("ui_checklist_ok",),
            )
    elif not any(item.kind == "implementation" for item in items):
        _add(
            "backend",
            "implementation",
            f"{title} · 实现切片",
            (
                f"{objective}\n\n"
                "Role slice: backend. Deliver the minimal reliable change, keep file "
                "boundaries tight, and prepare deterministic verification evidence."
            ),
            acceptance=("implementation_done",),
            artifacts=("goal-done.txt",),
        )

    if sizing_inputs.has_migration and items:
        last_impl = next(
            item for item in reversed(items) if item.kind == "implementation"
        )
        items[items.index(last_impl)] = PlannedWorkItem(
            ordinal=last_impl.ordinal,
            role=last_impl.role,
            kind=last_impl.kind,
            title=last_impl.title,
            objective=(
                last_impl.objective
                + "\n\nInclude migration safety: forward migration, rollback note, "
                "and schema verification evidence."
            ),
            depends_on_ordinals=last_impl.depends_on_ordinals,
            acceptance=last_impl.acceptance + ("migration_safe",),
            artifacts=last_impl.artifacts,
        )

    prior_artifacts = tuple(
        path for item in items for path in item.artifacts
    )
    _add(
        "verification",
        "verification",
        f"{title} · 独立验收",
        (
            f"{objective}\n\n"
            "Role slice: independent verification. Reproduce acceptance criteria, "
            "run declared verification commands against prior artifacts, and refuse "
            "completion without evidence. Do not implement new product features."
        ),
        acceptance=("acceptance_reproduced", "prior_artifacts_verified"),
        artifacts=prior_artifacts,
    )

    if len(items) > 7:
        kept = items[:6] + [items[-1]]
        renumbered: list[PlannedWorkItem] = []
        for index, item in enumerate(kept, start=1):
            depends = (index - 1,) if index > 1 else ()
            renumbered.append(
                PlannedWorkItem(
                    ordinal=index,
                    role=item.role,
                    kind=item.kind,
                    title=item.title,
                    objective=item.objective,
                    depends_on_ordinals=depends,
                    acceptance=item.acceptance,
                    artifacts=item.artifacts,
                )
            )
        items = renumbered

    return items


def _parse_structured_items(raw_items: list[dict[str, Any]]) -> list[PlannedWorkItem]:
    items: list[PlannedWorkItem] = []
    for raw in raw_items:
        kind = str(raw.get("kind") or "")
        if kind not in {"implementation", "verification"}:
            raise DomainError(f"invalid structured plan kind: {kind}")
        depends = raw.get("depends_on_ordinals") or []
        if not isinstance(depends, list):
            raise DomainError("depends_on_ordinals must be a list")
        acceptance = raw.get("acceptance") or []
        artifacts = raw.get("artifacts") or []
        if not isinstance(acceptance, list) or not isinstance(artifacts, list):
            raise DomainError("acceptance/artifacts must be lists")
        items.append(
            PlannedWorkItem(
                ordinal=int(raw["ordinal"]),
                role=str(raw["role"]),
                kind=kind,  # type: ignore[arg-type]
                title=str(raw["title"]),
                objective=str(raw["objective"]),
                depends_on_ordinals=tuple(int(dep) for dep in depends),
                acceptance=tuple(str(item) for item in acceptance),
                artifacts=tuple(str(item) for item in artifacts),
            )
        )
    return items
