from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .intake import declared_step_count, normalize_instruction
from .provider import (
    ACTIVE_HOST_JOB_STATUSES,
    HostBridgeError,
    PROVIDERS,
    provider_agent_text,
    provider_job_output,
    provider_job_status,
    start_provider_job,
)


TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESULT_FORMATS = {
    "binary",
    "csv",
    "html",
    "json",
    "markdown",
    "pdf",
    "text",
    "yaml",
}
SIZE_ORDER = ("simple", "medium", "large")
GENERIC_COMPONENTS = {"all", "entire-project", "fullstack", "integrated", "project"}
PLANNER_RESULT_PREFIX = "PLOWWHIP_PLANNER_RESULT "
HIGH_RISK_TERMS = (
    "部署",
    "上线",
    "切流",
    "迁移",
    "永久删除",
    "付款",
    "发布",
    "权限变更",
    "生产",
)
LARGE_TERMS = (
    "前端和后端",
    "前后端",
    "多角色",
    "多个项目",
    "多 sprint",
    "长期",
    "比较方案",
    "架构",
    *HIGH_RISK_TERMS,
)
TASK_SETTING_LIMITS = {
    "max_runtime_seconds": (1, 86_400),
    "stop_grace_seconds": (0, 300),
    "handoff_max_bytes": (512, 1_048_576),
    "checkpoint_max_bytes": (512, 1_048_576),
    "context_max_bytes": (1_024, 1_048_576),
    "session_segment_max_bytes": (1_024, 1_048_576),
    "native_compact_input_tokens": (1_000, 100_000_000),
    "rotation_input_tokens": (1_000, 100_000_000),
    "max_model_calls": (1, 100_000),
    "max_total_tokens": (1_000, 1_000_000_000),
    "monitor_tail_lines": (1, 1_000),
    "monitor_tail_bytes": (256, 1_048_576),
    "retry_count": (0, 10),
    "retry_backoff_seconds": (0, 86_400),
}
MODEL_PROVIDERS = {"codex_cli", "cursor_cli", "deepseek", "kimi"}
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
NEGATED_RISK = re.compile(
    r"(?:不要|禁止|不得|严禁|无需|不允许|避免)[^，。；;.!?\n]{0,24}$"
)


@dataclass(frozen=True)
class PlannerStep:
    kind: str
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    project_path: str
    workspace_key: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    input_facts: dict
    context_policy: dict[str, object]
    model: str | None = None


def instruction_facts(content: str, kind: str) -> dict[str, object]:
    """Collect bounded intake facts without deciding the semantic task size."""
    lowered = content.lower()
    numbered_steps = declared_step_count(content)
    high_risk = [
        term for term in HIGH_RISK_TERMS if _has_positive_term(lowered, term)
    ]
    possible_multi_scope = [
        term for term in LARGE_TERMS if _has_positive_term(lowered, term)
    ]
    return {
        "normalized_kind": kind,
        "declared_step_count": numbered_steps,
        "possible_multi_scope_terms": list(dict.fromkeys(possible_multi_scope)),
        "possible_high_risk_terms": list(dict.fromkeys(high_risk)),
    }


def _has_positive_term(content: str, term: str) -> bool:
    return any(
        not NEGATED_RISK.search(content[max(0, match.start() - 32) : match.start()])
        for match in re.finditer(re.escape(term), content)
    )


def planner_prompt(instruction: str, project_id: str, input_facts: dict) -> str:
    return (
        "Analyze this formal Goal semantically before any Task is created. "
        "Do not modify files.\n"
        f"Project: {project_id}\nGoal: {instruction}\n"
        f"Intake facts (non-authoritative): "
        f"{json.dumps(input_facts, ensure_ascii=False, sort_keys=True)}\n"
        "You are the only semantic sizing authority. Return exactly one of simple, "
        "medium, or large. Simple means one deterministic action. Medium means one "
        "professional Worker owns one complete responsibility boundary. Multiple "
        "independent modules, roles, deliverables, dependencies, Sprints, migration, "
        "deployment, or high risk require large. Explain the semantic evidence; never "
        "copy an intake hint as the sole reason.\n"
        "For simple or medium return exactly one executable Task and one selected "
        "alternative. For large return at least two genuine alternatives comparing "
        "name, scope, cost, risk, reversible, and acceptance, then emit a serializable "
        "DAG with 2-50 bounded Tasks. Every Plan needs required_coverage and a selection "
        "object. Each alternative needs objective_metrics with exactly the Plan coverage, "
        "integer estimated_effort, and integer risk_level 0-4. selection.mode may be "
        "objective only when the selected alternative objectively dominates every other "
        "alternative on the same coverage, effort, risk, and reversibility; otherwise use "
        "owner_required. Each task needs key, instruction, depends_on, sprint, role_key, "
        'a 1-20 item acceptance array shaped exactly as '
        '{"id":"stable-id","expected":"bounded expected result"}, '
        "responsibility with one safe component id and one complete deliverable, inputs, "
        "result, checker, authorization_boundary, optional earliest_start_delay_seconds, "
        "optional deadline_seconds, and optional settings. result needs type "
        "artifact|workspace_change|evidence, exact coverage, and every acceptance_id; "
        "artifact results also need a safe relative workspace path and format. inputs "
        "When the owner asks for a reusable script, the artifact must be one Python "
        "file and result.script_contract must name language=python, one public "
        "callable distinct from cli_entry=main, bounded exit_codes including zero "
        "and a non-zero code, stdout/stderr contracts, and three distinct "
        "acceptance_ids for callable, cli, and io behavior. "
        "A PLOWWHIP_BUTLER_SEMANTIC_QUERY is a read-only Butler query, never a "
        "business workspace change. It must remain one medium fullstack Task with "
        "exact acceptance ids semantic_summary_sources and semantic_summary_scope; "
        "the summary may cite only the supplied canonical refs. "
        "must include owner_instruction for root tasks and one task_result for every "
        "dependency. checker must name the derived independent checker role and all "
        "acceptance ids. authorization_boundary must describe none, recoverable, or "
        "external capability with exact allowed_actions and target_scope. Every large "
        "Task must explicitly set max_runtime_seconds. Use role_key fullstack for code work or "
        "deterministic only for exact '写入 relative-path: content' instructions. "
        "Honor an explicit Cursor assignment with "
        'settings.fullstack.provider_order=["cursor_cli"] and an explicit Codex '
        'assignment with settings.fullstack.provider_order=["codex_cli"]. '
        "For an explicitly requested GitHub SSH publish, keep the exact URL and branch "
        "in a bounded instruction that explicitly says SSH 上传, 推送, or 发布, and use "
        "role_key git_publisher. When the Goal explicitly enumerates Git publish, "
        "Cursor review, and Codex repair as three steps, return exactly those three "
        "serial tasks in that order. The Git publisher already performs secret "
        "scanning; do not add a separate preflight or a final publish unless the Goal "
        "explicitly asks for one. Set reversible to a JSON boolean; a bounded "
        "explanatory string is also accepted for compatibility. "
        "Classify workflow as standard or audit_then_repair. Use "
        "audit_then_repair when repair planning depends on a requested audit: the "
        "first audited responsibility must deliver the complete workspace file "
        "audit-report.md and every later repair/plan Task must depend on that "
        "checked Task result. "
        "Do not add deployment, deletion, payment, publishing, permission changes, "
        "or scope absent from the Goal. Finish with one line beginning "
        f"{PLANNER_RESULT_PREFIX!r} followed by "
        '{"classification":{"size":"simple|medium|large","reasons":["..."],'
        '"information_sufficient":true,"requires_owner_choice":false,'
        '"workflow":"standard|audit_then_repair",'
        '"upgrade_path":["simple"],"upgrade_evidence":[]},'
        '"confidence":0.95,"plan":{"alternatives":[],"selected":0,'
        '"selection":{"mode":"objective","basis":["..."]},'
        '"required_coverage":["..."],"summary":"...","tasks":[]}}. If the final '
        "size is medium use upgrade_path simple→medium with one evidence item; if large "
        "use simple→medium→large with two evidence items. If information is insufficient, set "
        'information_sufficient=false, provide one bounded blocking_reason, and set '
        "plan to null. Never ask the owner directly."
    )


def perform_planner_step(step: PlannerStep) -> dict[str, object]:
    stage = step.kind
    try:
        state = (
            start_provider_job(
                step.job_id,
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                timeout_seconds=step.timeout_seconds,
                context_policy=step.context_policy,
                access="read",
                workspace_kind="planner",
                workspace_key=step.workspace_key,
                model=step.model,
            )
            if step.kind == "start"
            else provider_job_status(step.job_id)
        )
        stage = "output"
        return {
            "ok": True,
            "state": state,
            "output": provider_job_output(
                step.job_id,
                complete=str(state.get("status"))
                not in ACTIVE_HOST_JOB_STATUSES,
            ),
        }
    except HostBridgeError as error:
        return {
            "ok": False,
            "error": type(error).__name__,
            "failure_kind": (
                "rejected"
                if step.kind == "start" and stage == "start" and error.rejected
                else "transport"
            ),
            "failure_stage": stage,
            "error_status": error.status,
            "error_detail": error.detail,
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}


def parse_planner_result(output: str) -> dict:
    output = provider_agent_text(output)
    line = next(
        (
            value
            for value in reversed(output.splitlines())
            if value.startswith(PLANNER_RESULT_PREFIX)
        ),
        "",
    )
    if not line:
        raise ValueError("Planner did not return a structured result")
    try:
        payload = json.loads(line[len(PLANNER_RESULT_PREFIX) :])
    except json.JSONDecodeError as error:
        raise ValueError("Planner returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Planner result must be an object")
    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError("Planner confidence must be between 0 and 1")
    classification = payload.get("classification")
    if not isinstance(classification, dict):
        raise ValueError("Planner classification must be an object")
    size = classification.get("size")
    if size not in {"simple", "medium", "large"}:
        raise ValueError("Planner size must be simple, medium, or large")
    reasons = classification.get("reasons")
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(
            isinstance(reason, str)
            and reason.strip()
            and len(reason.encode()) <= 1024
            for reason in reasons
        )
    ):
        raise ValueError("Planner classification requires bounded semantic reasons")
    information_sufficient = classification.get("information_sufficient")
    requires_owner_choice = classification.get("requires_owner_choice")
    workflow = classification.get("workflow")
    if not isinstance(information_sufficient, bool):
        raise ValueError("Planner information_sufficient must be boolean")
    if not isinstance(requires_owner_choice, bool):
        raise ValueError("Planner requires_owner_choice must be boolean")
    if workflow not in {"standard", "audit_then_repair"}:
        raise ValueError("Planner workflow must be standard or audit_then_repair")
    if workflow == "audit_then_repair" and size != "large":
        raise ValueError("audit_then_repair workflow requires a large Plan")
    upgrade_path, upgrade_evidence = _normalize_upgrade(
        size,
        classification.get("upgrade_path"),
        classification.get("upgrade_evidence"),
    )
    normalized_classification = {
        "size": size,
        "reasons": reasons,
        "information_sufficient": information_sufficient,
        "requires_owner_choice": requires_owner_choice,
        "workflow": workflow,
        "upgrade_path": upgrade_path,
        "upgrade_evidence": upgrade_evidence,
    }
    if not information_sufficient:
        blocking_reason = payload.get("blocking_reason")
        if (
            not isinstance(blocking_reason, str)
            or not blocking_reason.strip()
            or len(blocking_reason.encode()) > 4096
            or payload.get("plan") is not None
        ):
            raise ValueError(
                "insufficient Planner result requires one blocking reason and no plan"
            )
        return {
            "confidence": float(confidence),
            "classification": normalized_classification,
            "blocking_reason": blocking_reason.strip(),
            "plan": None,
        }
    normalized_plan = normalize_plan(
        payload.get("plan"),
        size=size,
        requires_owner_choice=requires_owner_choice,
        workflow=workflow,
    )
    normalized_plan["classification"] = normalized_classification
    return {
        "confidence": float(confidence),
        "classification": normalized_classification,
        "blocking_reason": None,
        "plan": normalized_plan,
    }


def _normalize_upgrade(
    size: str, raw_path: object, raw_evidence: object
) -> tuple[list[str], list[dict[str, object]]]:
    final_index = SIZE_ORDER.index(size)
    expected_path = list(SIZE_ORDER[: final_index + 1])
    if raw_path != expected_path:
        raise ValueError(
            "Planner upgrade_path must be monotonic from simple to the final size"
        )
    if not isinstance(raw_evidence, list) or len(raw_evidence) != final_index:
        raise ValueError("Planner upgrade_evidence must justify every size upgrade")
    normalized = []
    for index, item in enumerate(raw_evidence):
        expected_from = SIZE_ORDER[index]
        expected_to = SIZE_ORDER[index + 1]
        facts = item.get("facts") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("from") != expected_from
            or item.get("to") != expected_to
            or not isinstance(facts, list)
            or not facts
            or not all(
                isinstance(fact, str)
                and fact.strip()
                and len(fact.encode()) <= 1024
                for fact in facts
            )
        ):
            raise ValueError(
                f"Planner upgrade {expected_from} to {expected_to} lacks bounded facts"
            )
        normalized.append(
            {
                "from": expected_from,
                "to": expected_to,
                "facts": [fact.strip() for fact in facts],
            }
        )
    return expected_path, normalized


def normalize_plan(
    plan: object,
    *,
    size: str = "large",
    requires_owner_choice: bool = False,
    workflow: str = "standard",
) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if size not in {"simple", "medium", "large"}:
        raise ValueError("plan size must be simple, medium, or large")
    alternatives = plan.get("alternatives")
    tasks = plan.get("tasks")
    selected = plan.get("selected")
    required_coverage = _normalize_coverage(
        "plan", plan.get("required_coverage")
    )
    selection = _normalize_selection(
        plan.get("selection"), requires_owner_choice
    )
    minimum_alternatives = 2 if size == "large" else 1
    if (
        not isinstance(alternatives, list)
        or len(alternatives) < minimum_alternatives
    ):
        raise ValueError(
            "large plan requires at least two alternatives"
            if size == "large"
            else "simple and medium plans require one selected alternative"
        )
    comparison = {
        "name",
        "scope",
        "cost",
        "risk",
        "reversible",
        "acceptance",
        "objective_metrics",
    }
    if any(
        not isinstance(item, dict)
        or not comparison <= item.keys()
        or not all(
            str(item[key]).strip()
            for key in comparison - {"reversible", "objective_metrics"}
        )
        or not (
            isinstance(item["reversible"], bool)
            or (
                isinstance(item["reversible"], str)
                and 0 < len(item["reversible"].encode()) <= 512
            )
        )
        for item in alternatives
    ):
        raise ValueError("each alternative must compare scope, cost, risk, reversibility and acceptance")
    normalized_alternatives = [
        {
            **item,
            "objective_metrics": _normalize_objective_metrics(
                str(item["name"]),
                item["objective_metrics"],
                required_coverage,
            ),
        }
        for item in alternatives
    ]
    if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(alternatives):
        raise ValueError("selected alternative is invalid")
    if selection["mode"] == "objective":
        _validate_objective_selection(normalized_alternatives, selected, size)
    if (
        not isinstance(tasks, list)
        or not (
            2 <= len(tasks) <= 50
            if size == "large"
            else len(tasks) == 1
        )
    ):
        raise ValueError(
            "large plan requires 2-50 tasks"
            if size == "large"
            else "simple and medium plans require exactly one task"
        )

    normalized = []
    keys = set()
    for item in tasks:
        if not isinstance(item, dict) or not TASK_KEY.fullmatch(str(item.get("key", ""))):
            raise ValueError("each task requires a safe key")
        key = item["key"]
        if key in keys:
            raise ValueError(f"duplicate task key: {key}")
        keys.add(key)
        instruction = item.get("instruction")
        if instruction is None and isinstance(item.get("spec"), dict):
            # Real Planner output sometimes wraps the requested instruction in
            # `spec`. Re-derive every executable field from that instruction;
            # never trust model-supplied kind, path, Provider, or Git target.
            instruction = item["spec"].get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"task {key} requires an instruction")
        spec, acceptance = normalize_instruction(instruction)
        if (
            spec["kind"] == "provider_task"
            and isinstance(item.get("result"), dict)
            and item["result"].get("type") == "artifact"
        ):
            spec = {**spec, "workspace_change_required": True}
        if spec["kind"] == "write_text":
            default_role, checker_role = "deterministic", "deterministic_checker"
        elif spec["kind"] == "provider_probe":
            default_role = "provider_probe"
            checker_role = (
                "independent_checker"
                if spec["mode"] == "minimal"
                else "deterministic_checker"
            )
        elif spec["kind"] == "provider_task":
            default_role, checker_role = "fullstack", "independent_checker"
        elif spec["kind"] == "git_publish":
            default_role, checker_role = "git_publisher", "deterministic_checker"
        else:
            raise ValueError(f"task {key} is outside the executable boundary")
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(value, str) for value in dependencies
        ):
            raise ValueError(f"task {key} has invalid dependencies")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"task {key} has duplicate dependencies")
        sprint = item.get("sprint", 1)
        if isinstance(sprint, str) and sprint.isdigit():
            sprint = int(sprint)
        if isinstance(sprint, bool) or not isinstance(sprint, int) or not 1 <= sprint <= 10_000:
            raise ValueError(f"task {key} has invalid sprint")
        role_key = str(item.get("role_key", default_role))
        if role_key != default_role:
            raise ValueError(f"task {key} role does not match its instruction")
        if size == "simple" and default_role not in {
            "deterministic",
            "git_publisher",
            "provider_probe",
        }:
            raise ValueError(
                f"task {key} is not a deterministic simple action"
            )
        if size == "medium" and default_role != "fullstack":
            raise ValueError(
                f"task {key} is not one professional Worker responsibility"
            )
        if default_role == "git_publisher" and item.get("settings"):
            raise ValueError(f"task {key} Git publisher does not accept Provider settings")
        settings = _normalize_task_settings(
            key, role_key, checker_role, item.get("settings", {})
        )
        acceptance = _normalize_task_acceptance(
            key, item.get("acceptance"), acceptance
        )
        if spec.get("butler_semantic_query") and [
            value["id"] for value in acceptance
        ] != ["semantic_summary_sources", "semantic_summary_scope"]:
            raise ValueError(
                f"task {key} Butler summary must keep its two source/scope acceptances"
            )
        responsibility = _normalize_responsibility(
            key, item.get("responsibility"), size
        )
        inputs = _normalize_task_inputs(
            key, item.get("inputs"), dependencies
        )
        result = _normalize_task_result(
            key,
            item.get("result"),
            spec,
            acceptance,
        )
        checker = _normalize_task_checker(
            key,
            item.get("checker"),
            checker_role,
            acceptance,
        )
        authorization_boundary = _normalize_authorization_boundary(
            key,
            item.get("authorization_boundary"),
            spec,
        )
        max_runtime_seconds = _normalize_task_runtime(
            key, item.get("max_runtime_seconds"), size
        )
        if max_runtime_seconds is not None:
            executor_settings = settings.setdefault(role_key, {})
            frozen_runtime = executor_settings.get("max_runtime_seconds")
            if (
                frozen_runtime is not None
                and frozen_runtime != max_runtime_seconds
            ):
                raise ValueError(
                    f"task {key} has conflicting max_runtime_seconds"
                )
            executor_settings["max_runtime_seconds"] = max_runtime_seconds
        earliest_start_delay_seconds = _bounded_schedule_value(
            key, "earliest_start_delay_seconds", item.get("earliest_start_delay_seconds"), 0
        )
        deadline_seconds = _bounded_schedule_value(
            key, "deadline_seconds", item.get("deadline_seconds"), None
        )
        normalized.append(
            {
                "key": key,
                "spec": {
                    **spec,
                    "task_key": key,
                    "task_contract": {
                        "responsibility": responsibility,
                        "inputs": inputs,
                        "result": result,
                        "acceptance": acceptance,
                        "checker": checker,
                        "depends_on": dependencies,
                        "max_runtime_seconds": max_runtime_seconds,
                        "authorization_boundary": authorization_boundary,
                    },
                },
                "acceptance": acceptance,
                "depends_on": dependencies,
                "sprint": sprint,
                "role_key": role_key,
                "checker_role": checker_role,
                "settings": settings,
                "responsibility": responsibility,
                "inputs": inputs,
                "result": result,
                "checker": checker,
                "max_runtime_seconds": max_runtime_seconds,
                "authorization_boundary": authorization_boundary,
                "earliest_start_delay_seconds": earliest_start_delay_seconds,
                "deadline_seconds": deadline_seconds,
            }
        )

    if any(dependency not in keys for item in normalized for dependency in item["depends_on"]):
        raise ValueError("dependency refers to an unknown task")
    for item in normalized:
        declared_inputs = {
            value["task_key"]
            for value in item["inputs"]
            if value["kind"] == "task_result"
        }
        if declared_inputs != set(item["depends_on"]):
            raise ValueError(
                f"task {item['key']} inputs must map every dependency exactly once"
            )
    coverage_owners: dict[str, str] = {}
    for item in normalized:
        for coverage_id in item["result"]["coverage"]:
            if coverage_id in coverage_owners:
                raise ValueError(
                    f"coverage {coverage_id} is owned by multiple tasks"
                )
            coverage_owners[coverage_id] = item["key"]
    if set(coverage_owners) != set(required_coverage):
        raise ValueError("Task results must cover required_coverage exactly")
    responsibility_ids = [
        item["responsibility"]["component"] for item in normalized
    ]
    if size == "large" and len(responsibility_ids) != len(set(responsibility_ids)):
        raise ValueError("large Plan Tasks require unique responsibility components")
    _validate_workflow_gate(normalized, workflow, size)
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
        "size": size,
        "alternatives": normalized_alternatives,
        "selected": selected,
        "selection": selection,
        "required_coverage": required_coverage,
        "summary": str(plan.get("summary", "")),
        "tasks": ordered,
        "workflow": workflow,
    }


def _normalize_coverage(label: str, raw: object) -> list[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > 100
        or len(raw) != len(set(raw))
        or any(
            not isinstance(value, str) or not TASK_KEY.fullmatch(value)
            for value in raw
        )
    ):
        raise ValueError(f"{label} requires unique safe coverage ids")
    return list(raw)


def _validate_workflow_gate(
    tasks: list[dict], workflow: str, size: str
) -> None:
    if workflow == "standard":
        return
    if workflow != "audit_then_repair" or size != "large":
        raise ValueError("invalid Planner workflow gate")
    audit_tasks = [
        item
        for item in tasks
        if item["result"].get("type") == "artifact"
        and item["result"].get("artifact", {}).get("path")
        == "audit-report.md"
    ]
    if len(audit_tasks) != 1:
        raise ValueError(
            "audit_then_repair requires exactly one complete audit-report.md Task"
        )
    audit_key = audit_tasks[0]["key"]
    dependencies = {
        item["key"]: set(item["depends_on"]) for item in tasks
    }

    def depends_on_audit(task_key: str, seen: set[str]) -> bool:
        direct = dependencies[task_key]
        if audit_key in direct:
            return True
        return any(
            dependency not in seen
            and depends_on_audit(dependency, seen | {dependency})
            for dependency in direct
        )

    if audit_tasks[0]["depends_on"] or any(
        item["key"] != audit_key
        and not depends_on_audit(item["key"], {item["key"]})
        for item in tasks
    ):
        raise ValueError(
            "repair planning must depend on the checked audit-report.md Task"
        )


def _normalize_selection(
    raw: object, requires_owner_choice: bool
) -> dict[str, object]:
    basis = raw.get("basis") if isinstance(raw, dict) else None
    mode = raw.get("mode") if isinstance(raw, dict) else None
    if (
        mode not in {"objective", "owner_required"}
        or not isinstance(basis, list)
        or not basis
        or not all(
            isinstance(value, str)
            and value.strip()
            and len(value.encode()) <= 1024
            for value in basis
        )
    ):
        raise ValueError("Plan selection requires a mode and bounded basis")
    if requires_owner_choice != (mode == "owner_required"):
        raise ValueError(
            "Plan selection mode conflicts with requires_owner_choice"
        )
    return {"mode": mode, "basis": [value.strip() for value in basis]}


def _normalize_objective_metrics(
    alternative_name: str, raw: object, required_coverage: list[str]
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(
            f"alternative {alternative_name} requires objective_metrics"
        )
    coverage = _normalize_coverage(
        f"alternative {alternative_name}", raw.get("coverage")
    )
    effort = raw.get("estimated_effort")
    risk = raw.get("risk_level")
    if set(coverage) != set(required_coverage):
        raise ValueError(
            f"alternative {alternative_name} must cover the complete Plan"
        )
    if (
        isinstance(effort, bool)
        or not isinstance(effort, int)
        or not 1 <= effort <= 1_000_000
        or isinstance(risk, bool)
        or not isinstance(risk, int)
        or not 0 <= risk <= 4
    ):
        raise ValueError(
            f"alternative {alternative_name} has invalid objective metrics"
        )
    return {
        "coverage": coverage,
        "estimated_effort": effort,
        "risk_level": risk,
    }


def _validate_objective_selection(
    alternatives: list[dict], selected: int, size: str
) -> None:
    if size != "large":
        return
    chosen = alternatives[selected]
    if not isinstance(chosen["reversible"], bool):
        raise ValueError(
            "objective selection requires boolean reversibility"
        )
    chosen_metrics = chosen["objective_metrics"]
    for index, candidate in enumerate(alternatives):
        if index == selected:
            continue
        if not isinstance(candidate["reversible"], bool):
            raise ValueError(
                "objective selection requires boolean reversibility"
            )
        metrics = candidate["objective_metrics"]
        no_worse = (
            chosen_metrics["estimated_effort"]
            <= metrics["estimated_effort"]
            and chosen_metrics["risk_level"] <= metrics["risk_level"]
            and int(chosen["reversible"]) >= int(candidate["reversible"])
        )
        strictly_better = (
            chosen_metrics["estimated_effort"]
            < metrics["estimated_effort"]
            or chosen_metrics["risk_level"] < metrics["risk_level"]
            or int(chosen["reversible"]) > int(candidate["reversible"])
        )
        if not no_worse or not strictly_better:
            raise ValueError(
                "objective auto-selection lacks a dominating alternative"
            )


def _normalize_responsibility(
    task_key: str, raw: object, size: str
) -> dict[str, str]:
    component = raw.get("component") if isinstance(raw, dict) else None
    deliverable = raw.get("deliverable") if isinstance(raw, dict) else None
    if (
        not isinstance(component, str)
        or not TASK_KEY.fullmatch(component)
        or not isinstance(deliverable, str)
        or not deliverable.strip()
        or len(deliverable.encode()) > 4096
    ):
        raise ValueError(
            f"task {task_key} requires one component and complete deliverable"
        )
    if size == "large" and component.lower() in GENERIC_COMPONENTS:
        raise ValueError(
            f"task {task_key} uses a non-atomic responsibility component"
        )
    return {
        "component": component,
        "deliverable": deliverable.strip(),
    }


def _normalize_task_inputs(
    task_key: str, raw: object, dependencies: list[str]
) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw or len(raw) > 50:
        raise ValueError(f"task {task_key} requires bounded inputs")
    normalized = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind == "owner_instruction":
            source = item.get("source")
            if source != "goal":
                raise ValueError(
                    f"task {task_key} owner input must come from the Goal"
                )
            value = {"kind": kind, "source": "goal"}
            identity = (kind, "goal")
        elif kind == "task_result":
            dependency = item.get("task_key")
            if not isinstance(dependency, str) or not TASK_KEY.fullmatch(
                dependency
            ):
                raise ValueError(f"task {task_key} has invalid Task result input")
            value = {"kind": kind, "task_key": dependency}
            identity = (kind, dependency)
        else:
            raise ValueError(f"task {task_key} has unsupported input kind")
        if identity in seen:
            raise ValueError(f"task {task_key} has duplicate inputs")
        seen.add(identity)
        normalized.append(value)
    if not dependencies and not any(
        value["kind"] == "owner_instruction" for value in normalized
    ):
        raise ValueError(f"root task {task_key} must consume the owner instruction")
    return normalized


def _normalize_task_result(
    task_key: str,
    raw: object,
    spec: dict,
    acceptance: list[dict],
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} requires a result contract")
    result_type = raw.get("type")
    if result_type not in {"artifact", "workspace_change", "evidence"}:
        raise ValueError(f"task {task_key} has invalid result type")
    coverage = _normalize_coverage(
        f"task {task_key} result", raw.get("coverage")
    )
    acceptance_ids = raw.get("acceptance_ids")
    expected_acceptance_ids = [item["id"] for item in acceptance]
    if acceptance_ids != expected_acceptance_ids:
        raise ValueError(
            f"task {task_key} result must map every acceptance id exactly"
        )
    artifact = raw.get("artifact")
    if result_type == "artifact":
        artifact = _normalize_artifact_contract(task_key, artifact)
    elif artifact is not None:
        raise ValueError(
            f"task {task_key} non-file result cannot declare an Artifact"
        )
    kind = spec["kind"]
    expected_type = (
        "artifact"
        if kind == "write_text"
        else "evidence"
        if kind in {"provider_probe", "git_publish"}
        else (
            "artifact"
            if result_type == "artifact"
            and spec.get("workspace_change_required")
            else "workspace_change"
            if spec.get("workspace_change_required")
            else "evidence"
        )
        if kind == "provider_task"
        else None
    )
    if expected_type is not None and result_type != expected_type:
        raise ValueError(
            f"task {task_key} result type conflicts with its executable contract"
        )
    if kind == "write_text" and artifact["path"] != spec["target"]:
        raise ValueError(
            f"task {task_key} Artifact path must match its write target"
        )
    if (
        kind == "provider_task"
        and result_type == "artifact"
        and not spec.get("workspace_change_required")
    ):
        raise ValueError(
            f"task {task_key} workspace Artifact requires write-capable execution"
        )
    result = {
        "type": result_type,
        "coverage": coverage,
        "acceptance_ids": list(acceptance_ids),
    }
    if artifact is not None:
        result["artifact"] = artifact
    if raw.get("script_contract") is not None:
        if result_type != "artifact":
            raise ValueError(
                f"task {task_key} reusable script must be an Artifact"
            )
        result["script_contract"] = _normalize_script_contract(
            task_key,
            raw["script_contract"],
            expected_acceptance_ids,
            artifact,
        )
    return result


def _normalize_script_contract(
    task_key: str,
    raw: object,
    acceptance_ids: list[str],
    artifact: dict[str, str],
) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("language") != "python":
        raise ValueError(f"task {task_key} script language must be python")
    callable_name = raw.get("callable")
    cli_entry = raw.get("cli_entry")
    exit_codes = raw.get("exit_codes")
    stdout = raw.get("stdout")
    stderr = raw.get("stderr")
    mapped = raw.get("acceptance_ids")
    if (
        artifact.get("format") != "text"
        or not artifact.get("path", "").endswith(".py")
        or not isinstance(callable_name, str)
        or not callable_name.isidentifier()
        or callable_name.startswith("_")
        or cli_entry != "main"
        or callable_name == cli_entry
        or not isinstance(exit_codes, list)
        or len(exit_codes) != len(set(exit_codes))
        or any(isinstance(code, bool) or not isinstance(code, int) for code in exit_codes)
        or 0 not in exit_codes
        or not any(code != 0 for code in exit_codes)
        or not isinstance(stdout, str)
        or not stdout.strip()
        or not isinstance(stderr, str)
        or not stderr.strip()
        or not isinstance(mapped, dict)
        or set(mapped) != {"callable", "cli", "io"}
        or len(set(mapped.values())) != 3
        or any(value not in acceptance_ids for value in mapped.values())
    ):
        raise ValueError(f"task {task_key} has an incomplete script contract")
    return {
        "language": "python",
        "callable": callable_name,
        "cli_entry": "main",
        "exit_codes": list(exit_codes),
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
        "acceptance_ids": dict(mapped),
    }


def _normalize_artifact_contract(task_key: str, raw: object) -> dict[str, str]:
    path = raw.get("path") if isinstance(raw, dict) else None
    result_format = raw.get("format") if isinstance(raw, dict) else None
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or PurePosixPath(path).as_posix() != path
        or ".." in PurePosixPath(path).parts
        or result_format not in RESULT_FORMATS
    ):
        raise ValueError(
            f"task {task_key} Artifact requires a safe relative path and format"
        )
    return {"path": path, "format": result_format}


def _normalize_task_checker(
    task_key: str,
    raw: object,
    expected_role: str,
    acceptance: list[dict],
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} requires a Checker contract")
    acceptance_ids = [item["id"] for item in acceptance]
    if (
        raw.get("role_key") != expected_role
        or raw.get("acceptance_ids") != acceptance_ids
    ):
        raise ValueError(
            f"task {task_key} Checker must independently map every acceptance"
        )
    return {
        "role_key": expected_role,
        "acceptance_ids": acceptance_ids,
    }


def _normalize_authorization_boundary(
    task_key: str, raw: object, spec: dict
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} requires an authorization boundary")
    kind = spec["kind"]
    if (
        kind == "provider_task"
        and _requests_unsupported_external_effect(
            str(spec.get("instruction") or "")
        )
    ):
        raise ValueError(
            f"task {task_key} requests an external or irreversible effect "
            "without a dedicated authorized runtime executor"
        )
    if kind == "git_publish":
        expected = {
            "level": "external",
            "allowed_actions": ["git_publish"],
            "target_scope": (
                f"{spec['remote_ssh']}#refs/heads/{spec['branch']}"
            ),
        }
    elif kind == "write_text":
        expected = {
            "level": "recoverable",
            "allowed_actions": ["write_workspace"],
            "target_scope": "project_workspace",
        }
    elif kind == "provider_probe":
        expected = {
            "level": "none",
            "allowed_actions": ["probe_provider"],
            "target_scope": f"provider:{spec['provider_key']}",
        }
    elif spec.get("workspace_change_required"):
        expected = {
            "level": "recoverable",
            "allowed_actions": ["write_workspace", "run_checks"],
            "target_scope": "project_workspace",
        }
    else:
        expected = {
            "level": "none",
            "allowed_actions": ["read_workspace"],
            "target_scope": "project_workspace",
        }
    if raw != expected:
        raise ValueError(
            f"task {task_key} authorization boundary conflicts with runtime"
        )
    return expected


def _requests_unsupported_external_effect(instruction: str) -> bool:
    lowered = instruction.lower()
    if re.search(
        r"(?:不执行|不要执行|仅准备|只准备|只读|清单|方案|分析|审查|模拟|dry[- ]?run)",
        lowered,
    ):
        return False
    return any(_has_positive_term(lowered, term) for term in HIGH_RISK_TERMS)


def _normalize_task_runtime(
    task_key: str, value: object, size: str
) -> int | None:
    if value is None:
        if size == "large":
            raise ValueError(
                f"large task {task_key} requires max_runtime_seconds"
            )
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 86_400
    ):
        raise ValueError(f"task {task_key} has invalid max_runtime_seconds")
    return value


def _normalize_task_acceptance(
    task_key: str, raw: object, fallback: list[dict]
) -> list[dict]:
    if raw is None:
        return fallback
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise ValueError(f"task {task_key} requires 1-20 acceptance items")
    normalized = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"task {task_key} acceptance must contain objects")
        acceptance_id = str(item.get("id") or "")
        expected = str(
            item.get("expected") or item.get("expected_result") or ""
        ).strip()
        if (
            not TASK_KEY.fullmatch(acceptance_id)
            or acceptance_id in seen
            or not expected
            or len(expected.encode()) > 4096
        ):
            raise ValueError(f"task {task_key} has invalid acceptance")
        seen.add(acceptance_id)
        normalized.append(
            {
                "id": acceptance_id,
                "kind": "planner_acceptance",
                "expected": expected,
            }
        )
    return normalized


def _bounded_schedule_value(
    task_key: str, name: str, value: object, default: int | None
) -> int | None:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 31_536_000
        or (name == "deadline_seconds" and value == 0)
    ):
        raise ValueError(f"task {task_key} has invalid {name}")
    return value


def _normalize_task_settings(
    task_key: str, role_key: str, checker_role: str, raw: object
) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} settings must be an object")
    allowed_roles = {role_key, checker_role}
    if any(key not in allowed_roles or not isinstance(value, dict) for key, value in raw.items()):
        raise ValueError(f"task {task_key} settings must be keyed by its two roles")
    normalized = {}
    for target_role, values in raw.items():
        role_values = {}
        for name, value in values.items():
            if name == "provider_order":
                allowed = {"local", *PROVIDERS}
                if (
                    not isinstance(value, list)
                    or not value
                    or len(value) != len(set(value))
                    or any(provider not in allowed for provider in value)
                ):
                    raise ValueError(f"task {task_key} has invalid Provider order")
                if role_key == "deterministic" and value != ["local"]:
                    raise ValueError(f"task {task_key} deterministic role requires local")
                role_values[name] = value
                continue
            if name == "provider_models":
                if (
                    not isinstance(value, dict)
                    or not value
                    or any(
                        provider not in MODEL_PROVIDERS
                        or not isinstance(model, str)
                        or not MODEL_NAME.fullmatch(model)
                        for provider, model in value.items()
                    )
                ):
                    raise ValueError(f"task {task_key} has invalid Provider models")
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
