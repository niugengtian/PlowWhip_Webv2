from __future__ import annotations
from typing import Any

from plow_whip_web.domain.model import DomainError, NotFoundError
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.runtime.orchestration import plan_goal_work_items
from plow_whip_web.runtime.sizing import TaskSizingInputs
from plow_whip_web.store.butler_repository import ButlerRepository
from plow_whip_web.store.goal_repository import GoalRepository
from plow_whip_web.store.project_repository import ProjectRepository


class ButlerService:
    def __init__(
        self,
        repository: ButlerRepository,
        goals: GoalRepository,
        projects: ProjectRepository,
        provider_pool: ProviderPool,
    ) -> None:
        self.repository = repository
        self.goals = goals
        self.projects = projects
        self.provider_pool = provider_pool

    def submit(self, **values: Any) -> dict[str, Any]:
        intake = self.repository.create(**values)
        return self._dispatch_if_ready(intake)

    def confirm(self, intake_id: str, **values: Any) -> dict[str, Any]:
        intake = self.repository.confirm(intake_id, **values)
        return self._dispatch_if_ready(intake)

    def _dispatch_if_ready(self, intake: dict[str, Any]) -> dict[str, Any]:
        if intake["status"] != "dispatching":
            return intake
        payload = intake["input"]
        project = self.projects.get(intake["project_id"])
        role_ids = {role["kind"]: role["id"] for role in project["roles"]}
        required = {"coordination", "verification"}
        if not required <= set(role_ids):
            raise NotFoundError("project role catalog cannot dispatch Butler goal")
        provider = self._select_provider(intake["project_id"], payload)
        sizing = _sizing(payload)
        plan_items = payload.get("plan_items")
        plan = plan_goal_work_items(
            title=str(payload.get("title") or intake["instruction"][:120]),
            objective=intake["instruction"],
            sizing_inputs=sizing,
            structured_items=plan_items if isinstance(plan_items, list) and plan_items else None,
        )
        if plan.status != "planned":
            raise DomainError(f"Butler proposal is not dispatchable: {plan.missing_gates}")
        bindings = self.projects.role_provider_bindings(intake["project_id"])
        requested_role_providers = payload.get("role_providers") or {}
        role_providers = {
            "coordination": bindings.get(
                "coordination",
                requested_role_providers.get("coordination", provider),
            )
        }
        for item in plan.items:
            if item.role not in role_ids:
                raise NotFoundError(f"project role missing: {item.role}")
            role_providers[item.role] = bindings.get(
                item.role,
                requested_role_providers.get(item.role, provider),
            )
        for selected_provider in sorted(set(role_providers.values())):
            self.provider_pool.require_ready(selected_provider)
        binding = self.projects.resolve_role(intake["project_id"], "coordination")
        verification = payload.get("verification") or [
            {"kind": "exit_code", "expected": 0}
        ]
        goal = self.goals.create_with_plan(
            title=str(payload.get("title") or intake["instruction"][:120]),
            objective=intake["instruction"],
            project_id=intake["project_id"],
            project_path=binding["project_path"],
            role_providers=role_providers,
            plan=plan,
            sizing_inputs=_sizing_dict(payload),
            verification=verification,
            role_ids=role_ids,
            idempotency_key=f"butler-goal:{intake['id']}",
            network_requirement=str(payload.get("network_requirement") or "none"),
            command=payload.get("command"),
        )
        dispatched = self.repository.dispatched(
            intake["id"],
            expected_revision=intake["revision"],
            goal_id=goal["id"],
            provider=provider,
            idempotency_key=f"butler-dispatched:{intake['id']}",
        )
        return dispatched

    def _select_provider(self, project_id: str, payload: dict[str, Any]) -> str:
        requested = payload.get("provider")
        connection = self.repository.database.connect()
        try:
            if requested:
                row = connection.execute(
                    """
                    SELECT name FROM provider_configs
                    WHERE name = ? AND enabled = 1 AND status = 'available'
                    """,
                    (requested,),
                ).fetchone()
                if row:
                    return row["name"]
            bound = self.projects.role_provider_bindings(project_id)
            for provider in bound.values():
                row = connection.execute(
                    """
                    SELECT name FROM provider_configs
                    WHERE name = ? AND enabled = 1 AND status = 'available'
                    """,
                    (provider,),
                ).fetchone()
                if row:
                    return row["name"]
            row = connection.execute(
                """
                SELECT name FROM provider_configs
                WHERE enabled = 1 AND status = 'available'
                ORDER BY model_invoked DESC, CASE name
                    WHEN 'codex' THEN 0 WHEN 'cursor' THEN 1 ELSE 2 END, name
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise DomainError("no available Provider for Butler dispatch")
            return row["name"]
        finally:
            connection.close()


def _sizing(payload: dict[str, Any]) -> TaskSizingInputs:
    return TaskSizingInputs(**_sizing_dict(payload))


def _sizing_dict(payload: dict[str, Any]) -> dict[str, Any]:
    supplied = payload.get("sizing_inputs") or {}
    defaults: dict[str, Any] = {
        "layers_touched": 1,
        "components_touched": 1,
        "estimated_files_changed": 1,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 1,
        "estimated_verification_seconds": 60,
        "external_dependencies_count": 0,
        "risk_level": "low",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    defaults.update(supplied)
    return defaults
