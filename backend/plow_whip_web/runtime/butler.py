from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from plow_whip_web.domain.model import DomainError, ProviderUnavailableError
from plow_whip_web.runtime.execution_policy import (
    ExecutionRoute,
    project_execution_policy,
    route_for_size,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing

if TYPE_CHECKING:
    from plow_whip_web.providers.pool import ProviderPool
    from plow_whip_web.runtime.model_call_ledger import ModelCallLedger


PLANNER_RULES = (
    "dev.think_before_coding@1",
    "dev.simplicity_first@1",
    "dev.surgical_changes@1",
    "dev.goal_driven_execution@1",
)
PLANNER_ROLES = {
    "backend", "frontend", "ui", "devops_sre", "verification", "fullstack",
}
MAX_PLAN_ITEMS = 6
KNOWN_PROVIDERS = ("codex", "cursor", "deepseek", "kimi")


@dataclass(frozen=True, slots=True)
class ButlerRoute:
    policy: dict[str, Any]
    route: ExecutionRoute


@dataclass(frozen=True, slots=True)
class ButlerPlanningResult:
    draft: dict[str, Any] | None
    status: str
    call_id: str | None
    error_class: str | None = None
    external_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ButlerChatResult:
    content: str
    status: str
    call_id: str | None
    external_session_id: str | None = None
    error_class: str | None = None


class ButlerPlanner:
    """One bounded, ledgered intake call; deterministic gates validate its proposal."""

    def __init__(
        self,
        provider_pool: ProviderPool,
        model_calls: ModelCallLedger,
        *,
        provider: str = "codex",
        timeout_seconds: int = 180,
    ) -> None:
        self.provider_pool = provider_pool
        self.model_calls = model_calls
        self.provider = provider
        self.timeout_seconds = max(10, min(int(timeout_seconds), 600))

    def plan(
        self,
        *,
        project: dict[str, Any],
        instruction: str,
        current_draft: dict[str, Any],
        proposal_revision: int,
        idempotency_key: str,
        templates: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        session_id: str | None = None,
        provider_name: str | None = None,
        required_worker_provider: str | None = None,
    ) -> ButlerPlanningResult:
        planner_provider = provider_name or self.provider
        worker_provider = required_worker_provider or planner_provider
        try:
            provider = self.provider_pool.require_available(planner_provider)
        except Exception as error:
            return ButlerPlanningResult(
                draft=None,
                status="fallback",
                call_id=None,
                error_class=type(error).__name__,
                external_session_id=session_id,
            )
        base_prompt = _planner_prompt(
            instruction=instruction,
            current_draft=current_draft,
            project=project,
            templates=templates,
            rules=rules,
            butler_provider=planner_provider,
            required_worker_provider=worker_provider,
        )
        active_session = session_id
        contract_error: str | None = None
        for attempt in range(2):
            call_key = (
                idempotency_key
                if attempt == 0
                else f"{idempotency_key}:contract-repair:1"
            )
            receipt = self.model_calls.prepare(
                idempotency_key=call_key,
                call_kind="butler_planner",
                provider=planner_provider,
                model=str(provider.get("model") or planner_provider),
                project_id=str(project["id"]),
                proposal_revision=proposal_revision,
                session_id=active_session,
            )
            if receipt["status"] in {"completed", "failed"}:
                return ButlerPlanningResult(
                    draft=None,
                    status="duplicate",
                    call_id=str(receipt["call_id"]),
                    error_class=receipt.get("error_class"),
                    external_session_id=active_session,
                )
            self.model_calls.dispatched(
                receipt["call_id"], raw_status="dispatched"
            )
            result = None
            try:
                result = self.provider_pool.bridge.execute(
                    provider=provider,
                    project_path=str(project.get("host_path") or project["path"]),
                    prompt=(
                        base_prompt
                        if contract_error is None
                        else _planner_contract_repair_prompt(contract_error)
                    ),
                    session_id=active_session,
                    timeout_seconds=self.timeout_seconds,
                )
                active_session = result.external_session_id or active_session
                if result.returncode != 0:
                    raise ProviderUnavailableError(
                        result.stderr or "project Butler planning failed"
                    )
                proposal = _parse_model_proposal(result.stdout)
                draft = validate_planner_proposal(
                    proposal,
                    provider_pool=self.provider_pool,
                    templates=templates,
                    rules=rules,
                    required_worker_provider=worker_provider,
                )
            except Exception as error:
                self.model_calls.settle(
                    receipt["call_id"],
                    result.as_dict() if result is not None else None,
                    failed=True,
                    error_class=(
                        result.failure_class
                        if result is not None and result.failure_class
                        else type(error).__name__
                    ),
                    session_id=active_session,
                    raw_status=(
                        f"returncode:{result.returncode}"
                        if result is not None
                        else "exception"
                    ),
                )
                if (
                    attempt == 0
                    and result is not None
                    and result.returncode == 0
                    and isinstance(error, (DomainError, TypeError, ValueError))
                ):
                    contract_error = str(error)
                    continue
                return ButlerPlanningResult(
                    draft=None,
                    status="fallback",
                    call_id=str(receipt["call_id"]),
                    error_class=type(error).__name__,
                    external_session_id=active_session,
                )
            self.model_calls.settle(
                receipt["call_id"],
                result.as_dict(),
                session_id=active_session,
                raw_status=f"returncode:{result.returncode}",
            )
            return ButlerPlanningResult(
                draft=draft,
                status="planned",
                call_id=str(receipt["call_id"]),
                external_session_id=active_session,
            )
        raise AssertionError("bounded planner repair loop exhausted")

    def chat_global(
        self,
        *,
        provider_name: str,
        project_path: str,
        instruction: str,
        overview: dict[str, Any],
        conversation_revision: int,
        idempotency_key: str,
        session_id: str | None,
    ) -> ButlerChatResult:
        try:
            provider = self.provider_pool.require_available(provider_name)
        except Exception as error:
            return ButlerChatResult(
                content="当前 Provider 不可用，已保留本轮消息，可在恢复后继续。",
                status="provider_suspended",
                call_id=None,
                external_session_id=session_id,
                error_class=type(error).__name__,
            )
        receipt = self.model_calls.prepare(
            idempotency_key=idempotency_key,
            call_kind="butler_planner",
            provider=provider_name,
            model=str(provider.get("model") or provider_name),
            proposal_revision=conversation_revision,
            session_id=session_id,
        )
        if receipt["status"] in {"completed", "failed"}:
            return ButlerChatResult(
                content="该轮消息已处理，请查看当前对话历史。",
                status="duplicate",
                call_id=str(receipt["call_id"]),
                external_session_id=session_id,
                error_class=receipt.get("error_class"),
            )
        self.model_calls.dispatched(
            receipt["call_id"], raw_status="dispatched"
        )
        result = None
        try:
            prompt = (
                "You are the global Butler. Answer the user directly and concisely. "
                "You may inspect and summarize the supplied canonical project overview, "
                "but you must not create project work or pretend that a project Butler "
                "has accepted a Goal. When project execution is requested, identify the "
                "target project and tell the control plane to hand off into that project's "
                "own Butler conversation. Do not invent state.\n\n"
                f"User message:\n{instruction}\n\n"
                "Canonical overview:\n"
                f"{json.dumps(overview, ensure_ascii=False, sort_keys=True)}"
            )
            result = self.provider_pool.bridge.execute(
                provider=provider,
                project_path=project_path,
                prompt=prompt,
                session_id=session_id,
                timeout_seconds=self.timeout_seconds,
            )
            if result.returncode != 0:
                raise ProviderUnavailableError(
                    result.stderr or "global Butler call failed"
                )
        except Exception as error:
            self.model_calls.settle(
                receipt["call_id"],
                result.as_dict() if result is not None else None,
                failed=True,
                error_class=(
                    result.failure_class
                    if result is not None and result.failure_class
                    else type(error).__name__
                ),
                session_id=(
                    result.external_session_id if result is not None else session_id
                ),
            )
            return ButlerChatResult(
                content="本轮调用失败，消息和会话断点已保留；Provider 恢复后可继续。",
                status="provider_suspended",
                call_id=str(receipt["call_id"]),
                external_session_id=(
                    result.external_session_id if result is not None else session_id
                ),
                error_class=type(error).__name__,
            )
        self.model_calls.settle(
            receipt["call_id"],
            result.as_dict(),
            session_id=result.external_session_id,
            raw_status=f"returncode:{result.returncode}",
        )
        return ButlerChatResult(
            content=result.stdout.strip(),
            status="completed",
            call_id=str(receipt["call_id"]),
            external_session_id=result.external_session_id,
        )


def route_goal(
    size_class: str, execution_policy: dict[str, Any] | None = None
) -> ButlerRoute:
    """Canonical Butler entry for one project-level routing decision."""
    policy = project_execution_policy(execution_policy)
    return ButlerRoute(policy=policy, route=route_for_size(size_class, policy))


def deterministic_goal_draft(
    instruction: str, draft: dict[str, Any]
) -> dict[str, Any]:
    """Conservative, inspectable fallback. It never invents missing acceptance."""
    result = dict(draft)
    text = " ".join(
        [
            instruction,
            str(result.get("objective") or ""),
            " ".join(str(item) for item in result.get("scope") or []),
        ]
    ).lower()
    backend = any(
        token in text
        for token in ("backend", "后端", "api", "database", "sqlite", "migration")
    )
    frontend = any(
        token in text
        for token in ("frontend", "前端", "界面", "ui", "browser", "浏览器")
    )
    deploy = any(token in text for token in ("deploy", "部署", "release", "发布"))
    migration = any(token in text for token in ("migration", "迁移", "schema"))
    layers = max(1, int(backend) + int(frontend) + int(deploy))
    components = max(
        1,
        len(set(re.findall(r"\b(api|ui|web|database|sqlite|worker|provider)\b", text)))
        + int(backend)
        + int(frontend),
    )
    verification_count = max(
        1,
        sum(token in text for token in ("pytest", "test", "lint", "typecheck", "build")),
    )
    sizing = {
        "layers_touched": layers,
        "components_touched": components,
        "estimated_files_changed": max(1, components * 2),
        "has_migration": migration,
        "has_deploy": deploy,
        "verification_commands_count": verification_count,
        "estimated_verification_seconds": max(60, verification_count * 60),
        "external_dependencies_count": 0,
        "risk_level": "high" if deploy and migration else "medium" if layers >= 2 else "low",
        "independent_review_required": layers >= 2,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    if not result.get("sizing_inputs"):
        result["sizing_inputs"] = sizing
    if not result.get("provider"):
        result["provider"] = "codex"
    default_provider = str(result["provider"])
    roles = ["backend"] if backend or not frontend else []
    if frontend:
        roles.append("frontend")
    if deploy:
        roles.append("devops_sre")
    roles.append("verification")
    objective = str(result.get("objective") or instruction).strip()
    title = str(result.get("title") or objective[:120]).strip()
    supplied_role_providers = bool(result.get("role_providers"))
    if not supplied_role_providers:
        result["role_providers"] = {role: default_provider for role in roles}
    if not result.get("plan_items") and not supplied_role_providers:
        result["plan_items"] = [
            {
                "ordinal": ordinal,
                "role": role,
                "kind": "verification" if role == "verification" else "implementation",
                "title": f"{title} · {role}",
                "objective": (
                    f"{objective}\n\nComplete the {role} lane with deterministic evidence."
                ),
                "depends_on_ordinals": [] if ordinal == 1 else [ordinal - 1],
                "acceptance": list(result.get("acceptance") or []),
                "artifacts": list(result.get("artifacts") or []),
                "provider": default_provider,
            }
            for ordinal, role in enumerate(roles[:MAX_PLAN_ITEMS], 1)
        ]
    return result


def explicit_provider(instruction: str, fallback: str) -> str:
    """Honor a provider named by the owner before any model call is attempted."""
    lowered = instruction.lower()
    for provider in KNOWN_PROVIDERS:
        if re.search(rf"(?<![a-z0-9-]){re.escape(provider)}(?![a-z0-9-])", lowered):
            return provider
    return fallback


def validate_planner_proposal(
    proposal: dict[str, Any],
    *,
    provider_pool: ProviderPool,
    templates: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    required_worker_provider: str,
) -> dict[str, Any]:
    draft = dict(proposal.get("goal_spec") or proposal)
    if draft.get("provider") != required_worker_provider:
        raise DomainError(
            "planner changed the owner-selected default Worker provider"
        )
    missing_semantic: list[str] = []
    for field in ("objective", "boundaries", "acceptance"):
        value = draft.get(field)
        if field == "objective":
            if not isinstance(value, str) or not value.strip():
                missing_semantic.append(field)
        elif not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            missing_semantic.append(field)
    if len(missing_semantic) > 1:
        raise DomainError("planner may leave at most one genuinely ambiguous field")
    sizing = draft.get("sizing_inputs")
    if not isinstance(sizing, dict):
        raise DomainError("planner sizing_inputs are required")
    inputs = TaskSizingInputs(**sizing)
    if estimate_task_sizing(inputs)["status"] != "estimated":
        raise DomainError("planner sizing gates are incomplete")
    items = draft.get("plan_items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAN_ITEMS:
        raise DomainError("planner must return 1..6 bounded plan_items")
    seen: set[int] = set()
    providers: set[str] = set()
    planned_roles: set[str] = set()
    for expected, item in enumerate(items, 1):
        if not isinstance(item, dict) or int(item.get("ordinal") or 0) != expected:
            raise DomainError("planner ordinals must be contiguous from 1")
        role = str(item.get("role") or "")
        kind = str(item.get("kind") or "")
        if role not in PLANNER_ROLES:
            raise DomainError(f"planner role unavailable: {role}")
        if kind not in {"implementation", "verification"}:
            raise DomainError(f"planner work item kind invalid: {kind}")
        if (role == "verification") != (kind == "verification"):
            raise DomainError("verification role and work item kind must match")
        dependencies = item.get("depends_on_ordinals") or []
        if not isinstance(dependencies, list) or any(int(dep) not in seen for dep in dependencies):
            raise DomainError("planner DAG dependencies must refer to earlier items")
        if expected > 1 and role in {"frontend", "devops_sre", "verification"}:
            if expected - 1 not in {int(dep) for dep in dependencies}:
                raise DomainError("shared worktree lanes must be serial")
        seen.add(expected)
        planned_roles.add(role)
    role_providers = draft.get("role_providers")
    if not isinstance(role_providers, dict):
        raise DomainError("planner role_providers are required")
    if set(role_providers) != planned_roles:
        raise DomainError("planner role_providers must match planned roles exactly")
    for item in items:
        role = str(item["role"])
        item_provider = str(item.get("provider") or "")
        if item_provider != str(role_providers[role]):
            raise DomainError(
                f"planner provider mismatch between role and work item: {role}"
            )
        providers.add(item_provider)
    providers.update(str(value) for value in role_providers.values())
    if not providers or "" in providers:
        raise DomainError("planner providers are incomplete")
    for provider in providers:
        provider_pool.require_ready(provider)

    template_refs = draft.get("role_templates")
    rule_refs = draft.get("role_rules")
    if not isinstance(template_refs, dict) or not isinstance(rule_refs, dict):
        raise DomainError("planner template and rule references are required")
    catalog = {
        str(item["capability"]): f"{item['template_id']}@{item['revision']}"
        for item in templates
    }
    rule_catalog = {
        f"{item['rule_id']}@{item['revision']}" for item in rules
    }
    for role in {str(item["role"]) for item in items}:
        if template_refs.get(role) != catalog.get(role):
            raise DomainError(f"planner template reference invalid for {role}")
        refs = rule_refs.get(role)
        if not isinstance(refs, list) or any(ref not in rule_catalog for ref in refs):
            raise DomainError(f"planner rule references invalid for {role}")
        # The four development conventions are control-plane invariants, not
        # optional text the planning model must remember to reproduce.
        rule_refs[role] = [
            *PLANNER_RULES,
            *(ref for ref in refs if ref not in PLANNER_RULES),
        ]
    return draft


def _planner_prompt(
    *,
    instruction: str,
    current_draft: dict[str, Any],
    project: dict[str, Any],
    templates: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    butler_provider: str,
    required_worker_provider: str,
) -> str:
    facts = {
        "project": {
            "id": project["id"],
            "name": project["name"],
            "path": project["path"],
            "execution_policy": project.get("execution_policy") or {},
        },
        "available_templates": [
            {
                "role": item["capability"],
                "ref": f"{item['template_id']}@{item['revision']}",
            }
            for item in templates
            if item["capability"] in PLANNER_ROLES
        ],
        "available_rules": [
            f"{item['rule_id']}@{item['revision']}" for item in rules
        ],
        "mandatory_development_rules": list(PLANNER_RULES),
        "butler_provider": butler_provider,
        "required_worker_provider": required_worker_provider,
        "max_plan_items": MAX_PLAN_ITEMS,
    }
    return (
        "You are the project Butler planning model. Return one JSON object only. "
        "The top-level object must be {\"goal_spec\": {...}}. goal_spec must contain "
        "title:string, objective:string, boundaries:list[string], "
        "acceptance:list[string], sizing_inputs:object, plan_items:list[object], "
        "provider:string, role_providers:object, role_templates:object, and "
        "role_rules:object. Acceptance entries must be plain strings, never objects. "
        "Each plan item must contain exactly ordinal:int, role:string, "
        "kind:\"implementation\"|\"verification\", title:string, objective:string, "
        "depends_on_ordinals:list[int], and provider:string. Ordinals are contiguous "
        "from 1; dependencies refer only to earlier ordinals. Use only roles listed "
        "in available_templates; never create global_butler or project_butler work "
        "items. The verification role must use kind=verification and every other "
        "role must use kind=implementation. role_providers keys must exactly match "
        "planned roles, and every plan item provider must equal its role provider. "
        "role_templates and role_rules use the same planned role keys. The "
        "control plane will always prepend mandatory_development_rules to every "
        "planned role, so role_rules should list only additional relevant rules. "
        "sizing_inputs must contain only these keys: layers_touched, "
        "components_touched, estimated_files_changed, has_migration, has_deploy, "
        "verification_commands_count, estimated_verification_seconds, "
        "external_dependencies_count, risk_level, independent_review_required, "
        "gate_artifact, gate_boundary, gate_verification, gate_dependency. "
        "Use real estimates from the instruction and facts, never the placeholder "
        "1 layer/1 component/1 file/low sizing. Plan at most 6 items. Shared worktree "
        "implementation lanes and the final verification lane must form a serial DAG. "
        "Every role/provider/template/rule must reference the supplied catalog. "
        "If a material ambiguity prevents a safe proposal, omit only that semantic "
        "field so deterministic intake asks exactly one question. Do not claim "
        "dispatch or completion. Treat objections, corrections, and questions about "
        "your reasoning as conversational input; never copy them blindly into a "
        "goal field. The owner-selected required_worker_provider must remain the "
        "default for Goal provider, role_providers, and plan_items unless the "
        "instruction explicitly assigns another provider to a named role. "
        "butler_provider identifies only your own planning runtime and must not "
        "silently replace the Worker provider.\n\n"
        f"Instruction:\n{instruction}\n\n"
        f"Current draft:\n{json.dumps(current_draft, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Canonical facts:\n{json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
    )


def _planner_contract_repair_prompt(error: str) -> str:
    return (
        "Your previous proposal was semantically useful but violated the exact "
        f"control-plane JSON contract: {error}. Correct only the structure and "
        "contract mismatch. Preserve the owner's objective, boundaries, acceptance "
        "meaning, selected Worker provider, and intended serial dependencies. Return "
        "one corrected {\"goal_spec\": {...}} JSON object only, with no commentary."
    )


def _parse_model_proposal(output: str) -> dict[str, Any]:
    candidates: list[Any] = []
    clean = output.strip()
    if clean:
        candidates.append(clean)
    for line in reversed(output.splitlines()):
        if line.strip():
            candidates.append(line.strip())
    for candidate in candidates:
        value: Any = candidate
        for _ in range(4):
            if isinstance(value, dict):
                if "goal_spec" in value or "objective" in value:
                    return value
                nested = _find_nested(value)
                if nested is None:
                    break
                value = nested
                continue
            if not isinstance(value, str):
                break
            text = value.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start < 0 or end <= start:
                    break
                try:
                    value = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    break
    raise DomainError("planner returned no valid JSON proposal")


def _find_nested(value: Any) -> Any | None:
    if isinstance(value, dict):
        for key in ("result", "output_text", "text", "content", "message"):
            nested = value.get(key)
            if nested is not None:
                return nested
        # Codex CLI JSONL wraps the final agent message under item.text.
        for nested in reversed(list(value.values())):
            found = _find_nested(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in reversed(value):
            found = _find_nested(nested)
            if found is not None:
                return found
    return None


__all__ = [
    "ButlerPlanner",
    "ButlerChatResult",
    "ButlerPlanningResult",
    "ButlerRoute",
    "deterministic_goal_draft",
    "explicit_provider",
    "project_execution_policy",
    "route_goal",
    "validate_planner_proposal",
]
