from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .intake import declared_step_count, normalize_instruction
from .planner_errors import (
    INVALID_CLASSIFICATION,
    INVALID_COVERAGE,
    INVALID_PLAN,
    INVALID_UPGRADE_PATH,
    MISSING_STRUCTURED_RESULT,
    SANDBOX_FALSE_INSUFFICIENT,
    PlannerContractError,
)
from .provider import (
    ACTIVE_HOST_JOB_STATUSES,
    HostBridgeError,
    PROVIDERS,
    provider_job_output,
    provider_job_status,
    start_provider_job,
)
from .result_ingest import ingest_provider_result


TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AUDIT_DELIVERY_HINT = re.compile(
    r"(?:审查|审计|只读|代码审|audit|review|inspect|analy[sz]e|runtime-audits)",
    re.IGNORECASE,
)
REPORT_ARTIFACT_HINT = re.compile(
    r"(?:audit|review|report|runtime-audits|审查|审计)",
    re.IGNORECASE,
)
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
    "retry_count": (0, 5),
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


PLANNER_PRODUCT_DISCIPLINE = (
    "Product discipline (bounded): "
    "(1) Pin problem, boundary, and Non-Goals before splitting Tasks; do not invent "
    "deployment, GTM, research, or extra preflight absent from the Goal. "
    "(2) Every Task must carry verifiable acceptance with a stable id and expected "
    "result; vague later/improve/polish is not a Task. "
    "(3) Say no to scope creep in structured reasons: accept, defer, or refuse — "
    "never silently absorb. "
    "(4) Put trade-offs in classification.reasons and selection.basis; never hide them. "
    "(5) Completion means Artifact/Evidence contracts, not narrative success; prefer "
    "reuse of script library hits and existing paths over regenerating equivalents. "
)


def planner_prompt(instruction: str, project_id: str, input_facts: dict) -> str:
    return (
        "Analyze this formal Goal semantically before any Task is created. "
        "Do not modify files.\n"
        f"Project: {project_id}\nGoal: {instruction}\n"
        f"Intake facts (non-authoritative): "
        f"{json.dumps(input_facts, ensure_ascii=False, sort_keys=True)}\n"
        "The Planner HostJob uses a private empty sandbox that intentionally does not "
        "contain project files. Never set information_sufficient=false merely because "
        "those files are absent from the sandbox. Size and emit a complete plan from "
        "the Goal text, intake facts, and named relative paths; the later Worker runs "
        "against the real project workspace.\n"
        f"{PLANNER_PRODUCT_DISCIPLINE}"
        "You are the only semantic sizing authority. Return exactly one of simple, "
        "medium, or large. Simple means one deterministic action. Medium means one "
        "professional Worker owns one complete responsibility boundary. Multiple "
        "independent modules, roles, deliverables, dependencies, Sprints, migration, "
        "deployment, or high risk require large. Explain the semantic evidence; never "
        "copy an intake hint as the sole reason.\n"
        "Return one executable Plan with rationale in classification.reasons and "
        "selection.basis. Return one selected alternative for compatibility with the "
        "wire format. For large emit a serializable "
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
        "When the Goal needs local scripts, treat input_facts.script_library as the "
        "authoritative reuse catalog. Prefer reuse of item_key@revision already listed; "
        "do not regenerate an equivalent module when a library hit covers the need. "
        "Missing modules must be single-responsibility Python files with a module "
        "docstring that starts with 'summary: ...' describing the function, then a "
        "generate Task that declares result.script_contract. After modules exist, add a "
        "compose_and_run Task whose instruction is exactly "
        "'执行本地脚本 <relative.py>' or '执行本地脚本 library:<item_key>[@revision]' "
        "with optional ' -- argv...' and role_key local_script_runner. "
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
        "deterministic only for exact '写入 relative-path: content' instructions, "
        "and local_script_runner only for exact '执行本地脚本 ...' instructions. "
        "Default model Provider order is cursor_cli then deepseek; do not require "
        "codex_cli unless the Goal explicitly assigns Codex. "
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
        "For a pure code-review / audit Goal that only needs a report, prefer a "
        "medium fullstack Task whose result is an Artifact report path under "
        "docs/ or docs/runtime-audits/; the control plane tags it audit_delivery "
        "so multi-file reads count as progress before the report write. "
        "For analysis that must not modify the project tree, use result type "
        "evidence and a read-only instruction. "
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


def parse_planner_result(
    output: str, *, goal_instruction: str | None = None
) -> dict:
    try:
        return _parse_planner_result_body(
            output, goal_instruction=goal_instruction
        )
    except PlannerContractError:
        raise
    except ValueError as error:
        raise _contract_error_from_value_error(error) from error


def _contract_error_from_value_error(error: ValueError) -> PlannerContractError:
    message = str(error)
    lowered = message.lower()
    if "upgrade_path" in lowered or "upgrade_evidence" in lowered:
        code = INVALID_UPGRADE_PATH
    elif "coverage" in lowered:
        code = INVALID_COVERAGE
    elif "classification" in lowered or "confidence" in lowered or "size must" in lowered:
        code = INVALID_CLASSIFICATION
    elif (
        "structured result" in lowered
        or "planner result must" in lowered
        or "did not return" in lowered
    ):
        code = MISSING_STRUCTURED_RESULT
    else:
        code = INVALID_PLAN
    return PlannerContractError(code, message)


def _parse_planner_result_body(
    output: str, *, goal_instruction: str | None = None
) -> dict:
    output = str(ingest_provider_result(output)["agent_text"])
    payload = _extract_planner_payload(output)
    if not isinstance(payload, dict):
        raise PlannerContractError(
            MISSING_STRUCTURED_RESULT, "Planner result must be an object"
        )
    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise PlannerContractError(
            INVALID_CLASSIFICATION, "Planner confidence must be between 0 and 1"
        )
    classification = payload.get("classification")
    if not isinstance(classification, dict):
        raise PlannerContractError(
            INVALID_CLASSIFICATION, "Planner classification must be an object"
        )
    size = classification.get("size")
    if size not in {"simple", "medium", "large"}:
        raise PlannerContractError(
            INVALID_CLASSIFICATION, "Planner size must be simple, medium, or large"
        )
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
        raise PlannerContractError(
            INVALID_CLASSIFICATION,
            "Planner classification requires bounded semantic reasons",
        )
    information_sufficient = classification.get("information_sufficient")
    requires_owner_choice = classification.get("requires_owner_choice")
    workflow = classification.get("workflow")
    if not isinstance(information_sufficient, bool):
        raise PlannerContractError(
            INVALID_CLASSIFICATION, "Planner information_sufficient must be boolean"
        )
    if not isinstance(requires_owner_choice, bool):
        raise PlannerContractError(
            INVALID_CLASSIFICATION, "Planner requires_owner_choice must be boolean"
        )
    if workflow not in {"standard", "audit_then_repair"}:
        raise PlannerContractError(
            INVALID_CLASSIFICATION,
            "Planner workflow must be standard or audit_then_repair",
        )
    if workflow == "audit_then_repair" and size != "large":
        # Models often label a single audit_delivery medium Task as
        # audit_then_repair; that workflow is only meaningful for large
        # multi-task repair DAGs. Coerce instead of burning another generation.
        workflow = "standard"
    upgrade_path, upgrade_evidence = _normalize_upgrade(
        size,
        classification.get("upgrade_path"),
        classification.get("upgrade_evidence"),
        reasons=reasons,
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
        reasons_text = " ".join(reasons)
        sandbox_absence = any(
            token in reasons_text
            for token in (
                "工作区为空",
                "工作区中完全不存在",
                "workspace is empty",
                "do not exist",
                "does not exist",
                "files are absent",
                "sandbox",
            )
        )
        if sandbox_absence and payload.get("plan") is None:
            # Private Planner sandbox is empty by design; demand a Goal-derived plan.
            raise PlannerContractError(
                SANDBOX_FALSE_INSUFFICIENT,
                "Planner sandbox is empty by design; return a complete plan from Goal text",
            )
        blocking_reason = payload.get("blocking_reason")
        if (
            (not isinstance(blocking_reason, str) or not blocking_reason.strip())
            and reasons
        ):
            blocking_reason = reasons[0]
        if (
            not isinstance(blocking_reason, str)
            or not blocking_reason.strip()
            or len(blocking_reason.encode()) > 4096
            or payload.get("plan") is not None
        ):
            raise PlannerContractError(
                INVALID_PLAN,
                "insufficient Planner result requires one blocking reason and no plan",
            )
        return {
            "confidence": float(confidence),
            "classification": normalized_classification,
            "blocking_reason": blocking_reason.strip(),
            "plan": None,
        }
    if _can_salvage_audit_delivery_plan(
        goal_instruction,
        size=size,
        information_sufficient=information_sufficient,
        requires_owner_choice=requires_owner_choice,
    ):
        # MECH-08: for audit_delivery Goals, always use the control-plane wire
        # Plan. Model nested JSON — even when coerceable — must not re-enter the
        # independent Planner Checker loop.
        salvage_note = "audit_delivery_control_plane_template"
        try:
            normalize_plan(
                payload.get("plan"),
                size=size,
                requires_owner_choice=requires_owner_choice,
                workflow=workflow,
            )
        except ValueError as error:
            salvage_note = str(error)
        template = build_audit_delivery_plan(
            str(goal_instruction or ""),
            size=size,
            classification=normalized_classification,
            model_summary=_audit_model_summary(payload),
        )
        normalized_plan = normalize_plan(
            template,
            size=size,
            requires_owner_choice=requires_owner_choice,
            workflow="standard",
        )
        normalized_plan["classification"] = normalized_classification
        return {
            "confidence": float(confidence),
            "classification": normalized_classification,
            "blocking_reason": None,
            "plan": normalized_plan,
            "plan_source": "control_plane_audit_template",
            "salvage_error": salvage_note,
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


DEFAULT_AUDIT_REPORT_PATH = "docs/runtime-audits/AUDIT_REPORT.md"
DEFAULT_AUDIT_CHAPTERS = ("总判", "主线证据", "机制验证", "风险与建议")
_PROVIDER_LOCK_PATTERNS = (
    (
        re.compile(
            r"(?:仅使用|唯一\s*Provider\s*=?|provider_key\s*=|only)\s*"
            r"deepseek",
            re.I,
        ),
        "deepseek",
    ),
    (
        re.compile(
            r"(?:仅使用|唯一\s*Provider\s*=?|provider_key\s*=|only)\s*"
            r"cursor(?:_cli)?",
            re.I,
        ),
        "cursor_cli",
    ),
    (
        re.compile(
            r"(?:仅使用|唯一\s*Provider\s*=?|provider_key\s*=|only)\s*"
            r"codex(?:_cli)?",
            re.I,
        ),
        "codex_cli",
    ),
    (
        re.compile(
            r"(?:仅使用|唯一\s*Provider\s*=?|provider_key\s*=|only)\s*kimi",
            re.I,
        ),
        "kimi",
    ),
)


def _can_salvage_audit_delivery_plan(
    goal_instruction: str | None,
    *,
    size: str,
    information_sufficient: bool,
    requires_owner_choice: bool,
) -> bool:
    if not goal_instruction or not information_sufficient or requires_owner_choice:
        return False
    if size not in {"simple", "medium"}:
        return False
    return audit_delivery_intent({}, goal_instruction)


def _audit_model_summary(payload: dict) -> str:
    plan = payload.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("summary"), str):
        summary = plan["summary"].strip()
        if summary:
            return summary[:2000]
    classification = payload.get("classification")
    if isinstance(classification, dict):
        reasons = classification.get("reasons")
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], str):
            return str(reasons[0]).strip()[:2000]
    return "bounded audit report delivery"


def report_path_from_goal(goal_instruction: str) -> str:
    match = re.search(
        r"(docs/runtime-audits/[A-Za-z0-9._/-]+\.md)",
        goal_instruction or "",
    )
    return match.group(1) if match else DEFAULT_AUDIT_REPORT_PATH


def chapters_from_goal(goal_instruction: str) -> list[str]:
    text = goal_instruction or ""
    # E2E / MECH track: four explicit chapters; do not also keep the shorter
    # DEFAULT "风险与建议" / "机制验证" siblings that substring-match the Goal.
    if (
        "总判" in text
        and "主线证据" in text
        and ("MECH-01" in text or "MECH-01～07" in text or "MECH-01~07" in text)
    ):
        chapters = ["总判", "主线证据", "MECH-01～07"]
        if "无人值守" in text:
            chapters.append("无人值守/优质代码/省 Token 风险与建议")
        else:
            chapters.append("风险与建议")
        return chapters
    found: list[str] = []
    for chapter in DEFAULT_AUDIT_CHAPTERS:
        if chapter in text:
            found.append(chapter)
    if "MECH-01" in text or "MECH-01～07" in text or "MECH-01~07" in text:
        if "机制验证" in found:
            found[found.index("机制验证")] = "MECH-01～07"
        elif "MECH-01～07" not in found:
            found.insert(min(2, len(found)), "MECH-01～07")
    if "无人值守" in text and not any("无人值守" in item for item in found):
        found.append("无人值守/优质代码/省 Token 风险与建议")
    return found or list(DEFAULT_AUDIT_CHAPTERS)


def provider_lock_from_goal(goal_instruction: str) -> list[str] | None:
    """Return an explicit provider_order only when the Goal locks one vendor.

    Mechanism default is provider-agnostic: no settings.provider_order.
    """
    text = goal_instruction or ""
    for pattern, provider_key in _PROVIDER_LOCK_PATTERNS:
        if pattern.search(text):
            return [provider_key]
    return None


def build_audit_delivery_plan(
    goal_instruction: str,
    *,
    size: str,
    classification: dict[str, object],
    model_summary: str = "",
) -> dict[str, object]:
    """Control-plane wire Plan for audit_delivery Goals (any Planner Provider)."""
    if size not in {"simple", "medium"}:
        raise ValueError("audit delivery template supports only simple or medium")
    path = report_path_from_goal(goal_instruction)
    chapters = chapters_from_goal(goal_instruction)
    coverage = [path]
    summary = (model_summary or "bounded audit report delivery").strip()[:2000]
    acceptance = [
        {
            "id": "report_exists",
            "expected": f"File {path} exists after the task completes",
        },
        {
            "id": "report_non_empty",
            "expected": f"File {path} is non-empty",
        },
    ]
    for index, chapter in enumerate(chapters, start=1):
        acceptance.append(
            {
                "id": f"chapter_{index}",
                "expected": f"Report contains heading '{chapter}'",
            }
        )
    acceptance_ids = [item["id"] for item in acceptance]
    instruction = (
        f"Perform a bounded read-only code audit for this Goal and write one "
        f"Markdown report to {path}.\n\n"
        f"Goal:\n{goal_instruction.strip()}\n\n"
        "Hard boundaries:\n"
        f"- Only create/overwrite {path}.\n"
        "- Do not modify plowwhip/, tests/, Dockerfile, or other source paths.\n"
        "- Do not run git/Docker/deploy/external send.\n"
        "- Report must include these non-empty chapter headings: "
        + ", ".join(chapters)
        + ".\n"
        "- Independent Checker verifies only report existence, non-empty bytes, "
        "and required headings; do not require source re-read.\n"
    )
    task: dict[str, object] = {
        "key": "audit_delivery",
        "instruction": instruction,
        "depends_on": [],
        "sprint": 1,
        "role_key": "fullstack",
        "acceptance": acceptance,
        "responsibility": {
            "component": "audit_delivery",
            "deliverable": path,
        },
        "inputs": [{"kind": "owner_instruction", "source": "goal"}],
        "result": {
            "type": "artifact",
            "coverage": list(coverage),
            "acceptance_ids": list(acceptance_ids),
            "path": path,
            "format": "markdown",
        },
        "checker": {
            "role": "independent_checker",
            "acceptance_ids": list(acceptance_ids),
        },
        "authorization_boundary": {
            "level": "recoverable",
            "allowed_actions": ["write_workspace", "run_checks"],
            "target_scope": "project_workspace",
        },
    }
    provider_lock = provider_lock_from_goal(goal_instruction)
    if provider_lock:
        task["settings"] = {
            "fullstack": {"provider_order": list(provider_lock)},
            "independent_checker": {"provider_order": list(provider_lock)},
        }
    return {
        "summary": summary,
        "required_coverage": list(coverage),
        "selected": 0,
        "selection": {
            "mode": "objective",
            "basis": [
                "Control-plane audit_delivery template: one fullstack report task"
            ],
        },
        "alternatives": [
            {
                "name": "audit_delivery",
                "scope": f"One fullstack Worker delivers {path}",
                "cost": "bounded",
                "risk": "0",
                "reversible": True,
                "acceptance": "report exists, non-empty, required chapters present",
                "objective_metrics": {
                    "coverage": list(coverage),
                    "estimated_effort": 1,
                    "risk_level": 0,
                },
            }
        ],
        "tasks": [task],
        "classification": classification,
    }


def audit_delivery_intent(spec: dict[str, object], instruction: str = "") -> bool:
    """True when the Task is an audit/report delivery (reads first, then report)."""
    if spec.get("audit_delivery") is True:
        return True
    text = instruction or str(spec.get("instruction") or "")
    if AUDIT_DELIVERY_HINT.search(text):
        return True
    contract = spec.get("task_contract")
    result = contract.get("result") if isinstance(contract, dict) else None
    artifact = result.get("artifact") if isinstance(result, dict) else None
    path = str(artifact.get("path") or "") if isinstance(artifact, dict) else ""
    return bool(path and REPORT_ARTIFACT_HINT.search(path))


def _extract_planner_payload(output: str) -> dict:
    candidates: list[str] = []
    for value in reversed(output.splitlines()):
        if value.startswith(PLANNER_RESULT_PREFIX):
            candidates.append(value[len(PLANNER_RESULT_PREFIX) :])
        elif PLANNER_RESULT_PREFIX in value:
            candidates.append(
                value[value.find(PLANNER_RESULT_PREFIX) + len(PLANNER_RESULT_PREFIX) :]
            )
    marker = output.rfind(PLANNER_RESULT_PREFIX)
    if marker >= 0:
        candidates.append(output[marker + len(PLANNER_RESULT_PREFIX) :])
    for candidate in candidates:
        payload = _loads_json_object(candidate)
        if payload is not None:
            return payload
    raise ValueError("Planner did not return a structured result")


def _loads_json_object(text: str) -> dict | None:
    text = text.lstrip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        try:
            payload, _end = decoder.raw_decode(text)
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _normalize_upgrade(
    size: str,
    raw_path: object,
    raw_evidence: object,
    *,
    reasons: list[str],
) -> tuple[list[str], list[dict[str, object]]]:
    final_index = SIZE_ORDER.index(size)
    expected_path = list(SIZE_ORDER[: final_index + 1])
    if raw_path is None:
        raw_path = expected_path
    elif isinstance(raw_path, str):
        parts = [
            part.strip()
            for part in raw_path.replace("→", "->").replace("=>", "->").split("->")
            if part.strip()
        ]
        raw_path = parts if parts else raw_path
    elif (
        isinstance(raw_path, list)
        and len(raw_path) == 1
        and isinstance(raw_path[0], str)
        and ("→" in raw_path[0] or "->" in raw_path[0] or "=>" in raw_path[0])
    ):
        # Models often emit ["simple→medium"] instead of ["simple", "medium"].
        parts = [
            part.strip()
            for part in raw_path[0]
            .replace("→", "->")
            .replace("=>", "->")
            .split("->")
            if part.strip()
        ]
        raw_path = parts if parts else raw_path
    if raw_path != expected_path:
        raise ValueError(
            "Planner upgrade_path must be monotonic from simple to the final size"
        )
    if raw_evidence is None:
        raw_evidence = []
    if (
        isinstance(raw_evidence, list)
        and raw_evidence
        and all(isinstance(item, str) for item in raw_evidence)
    ):
        facts = [item.strip() for item in raw_evidence if item.strip()]
        if not facts:
            facts = [reason.strip() for reason in reasons if reason.strip()][:3]
        raw_evidence = [
            {
                "from": SIZE_ORDER[index],
                "to": SIZE_ORDER[index + 1],
                "facts": facts,
            }
            for index in range(final_index)
        ]
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
    minimum_alternatives = 1
    # LIVE-DS-17: DeepSeek sometimes omits alternatives[] for medium, or leaves
    # comparison fields null. Synthesize / fill before the strict gate.
    if size in {"simple", "medium"} and (
        not isinstance(alternatives, list) or len(alternatives) == 0
    ):
        alternatives = [
            {
                "name": "selected",
                "scope": str(plan.get("summary") or "single bounded task")[:2000],
                "cost": "bounded",
                "risk": "0",
                "reversible": True,
                "acceptance": "task acceptance contract",
                "objective_metrics": {
                    "coverage": list(required_coverage),
                    "estimated_effort": 1,
                    "risk_level": 0,
                },
            }
        ]
        if not isinstance(selected, int):
            selected = 0
    if (
        not isinstance(alternatives, list)
        or len(alternatives) < minimum_alternatives
    ):
        raise ValueError(
            "plan requires one selected alternative"
        )
    # DeepSeek/Cursor often invent divergent narrative coverage ids across
    # required_coverage, alternatives, and task.result. For simple/medium the
    # Plan coverage is authoritative; align children before further checks.
    if size in {"simple", "medium"}:
        aligned_alternatives = []
        for item in alternatives:
            if not isinstance(item, dict):
                aligned_alternatives.append(item)
                continue
            metrics = item.get("objective_metrics")
            if not isinstance(metrics, dict):
                metrics = {}
            if "coverage" not in metrics and isinstance(
                metrics.get("plan_coverage"), list
            ):
                metrics = {**metrics, "coverage": list(metrics["plan_coverage"])}
            item = {
                **item,
                "name": item.get("name") or "alternative",
                "scope": item.get("scope")
                or item.get("name")
                or str(plan.get("summary") or "scoped work"),
                "cost": (
                    item.get("cost")
                    if item.get("cost") not in (None, "")
                    else "bounded"
                ),
                "risk": (
                    item.get("risk")
                    if item.get("risk") not in (None, "")
                    else metrics.get("risk_level", 0)
                ),
                "reversible": (
                    item["reversible"]
                    if isinstance(item.get("reversible"), (bool, str))
                    else True
                ),
                "acceptance": (
                    item.get("acceptance")
                    if item.get("acceptance") not in (None, "")
                    else "acceptance covered by task contract"
                ),
                "objective_metrics": {
                    **metrics,
                    "coverage": list(required_coverage),
                    "estimated_effort": metrics.get("estimated_effort", 1),
                    "risk_level": metrics.get("risk_level", 0),
                },
            }
            aligned_alternatives.append(item)
        alternatives = aligned_alternatives
        if isinstance(tasks, list):
            aligned_tasks = []
            for item in tasks:
                if not isinstance(item, dict):
                    aligned_tasks.append(item)
                    continue
                result = item.get("result")
                if isinstance(result, dict):
                    item = {
                        **item,
                        "result": {
                            **result,
                            "coverage": list(required_coverage),
                        },
                    }
                aligned_tasks.append(item)
            tasks = aligned_tasks
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
        if spec["kind"] == "provider_task":
            raw_result = item.get("result") if isinstance(item.get("result"), dict) else {}
            result_type = raw_result.get("type")
            if result_type == "artifact":
                # Report Artifact needs a bounded workspace write, but audit/review
                # Goals keep exploration progress (MECH-05) via audit_delivery.
                was_read_only = not bool(spec.get("workspace_change_required", True))
                auditish = was_read_only or bool(
                    AUDIT_DELIVERY_HINT.search(instruction)
                    or REPORT_ARTIFACT_HINT.search(str(raw_result.get("path") or ""))
                )
                spec = {
                    **spec,
                    "workspace_change_required": True,
                    **({"audit_delivery": True} if auditish else {}),
                }
            elif result_type == "evidence" and not spec.get(
                "workspace_change_required", True
            ):
                # Pure read-only analysis: keep evidence + no workspace delta.
                spec = {**spec, "audit_delivery": True}
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
        elif spec["kind"] == "local_script":
            default_role, checker_role = (
                "local_script_runner",
                "deterministic_checker",
            )
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
            "local_script_runner",
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
        if default_role == "local_script_runner" and item.get("settings"):
            raise ValueError(
                f"task {key} local script runner does not accept Provider settings"
            )
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
            key,
            item.get("responsibility"),
            size,
            fallback_deliverable=_responsibility_fallback_deliverable(item),
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


def _coerce_coverage_id(value: str) -> str:
    text = value.strip()
    if TASK_KEY.fullmatch(text):
        return text
    # Path-like coverage from real models → filename when it is already safe.
    name = Path(text).name.strip()
    if name and TASK_KEY.fullmatch(name):
        return name
    # Non-ASCII / Chinese labels collide if only punctuation is stripped
    # (e.g. chapter_总判 and chapter_主线证据 both become "chapter").
    if re.search(r"[^A-Za-z0-9._\-]", text):
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")[:40]
        if prefix and prefix[0].isalnum():
            candidate = f"{prefix}_{digest}"[:64]
            if TASK_KEY.fullmatch(candidate):
                return candidate
        return f"cov_{digest}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    if cleaned and cleaned[0].isalnum() and TASK_KEY.fullmatch(cleaned[:64]):
        return cleaned[:64]
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"cov_{digest}"


def _normalize_coverage(label: str, raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise ValueError(f"{label} requires unique safe coverage ids")
    if any(not isinstance(value, str) or not value.strip() for value in raw):
        raise ValueError(f"{label} requires unique safe coverage ids")
    coerced = [_coerce_coverage_id(str(value)) for value in raw]
    if len(coerced) != len(set(coerced)):
        raise ValueError(f"{label} requires unique safe coverage ids")
    if any(not TASK_KEY.fullmatch(value) for value in coerced):
        raise ValueError(f"{label} requires unique safe coverage ids")
    return coerced


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
    required_set = set(required_coverage)
    coverage_set = set(coverage)
    # Real models often add extra narrative coverage ids; accept supersets.
    if coverage_set != required_set:
        if required_set <= coverage_set:
            coverage = list(required_coverage)
        else:
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


def _responsibility_fallback_deliverable(item: dict) -> str | None:
    raw_result = item.get("result")
    if isinstance(raw_result, dict):
        artifact = raw_result.get("artifact")
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
            return str(artifact["path"]).strip() or None
        if isinstance(raw_result.get("path"), str):
            return str(raw_result["path"]).strip() or None
    instruction = item.get("instruction")
    if isinstance(instruction, str) and "docs/runtime-audits/" in instruction:
        match = re.search(
            r"(docs/runtime-audits/[A-Za-z0-9._/-]+\.md)",
            instruction,
        )
        if match:
            return match.group(1)
    return None


def _normalize_responsibility(
    task_key: str,
    raw: object,
    size: str,
    *,
    fallback_deliverable: str | None = None,
) -> dict[str, str]:
    # LIVE-DS-17: DeepSeek often emits responsibility as a one-item list, uses
    # component_id/complete instead of component/deliverable, or puts Chinese /
    # prose in component. Coerce to the wire shape before fail-closed.
    if isinstance(raw, list):
        raw = next((item for item in raw if isinstance(item, dict)), None)
    if isinstance(raw, str) and raw.strip():
        raw = {"component": task_key, "deliverable": raw.strip()}
    component = None
    deliverable = None
    if isinstance(raw, dict):
        component = (
            raw.get("component")
            or raw.get("component_id")
            or raw.get("component_name")
        )
        deliverable = (
            raw.get("deliverable")
            or raw.get("complete")
            or raw.get("complete_deliverable")
            or raw.get("deliverable_path")
            or raw.get("path")
        )
        if isinstance(deliverable, dict):
            deliverable = deliverable.get("path") or deliverable.get("deliverable")
    if isinstance(component, str) and not TASK_KEY.fullmatch(component):
        # Models often put path globs / Chinese labels here; fall back to task key.
        component = task_key if TASK_KEY.fullmatch(task_key) else None
    if not isinstance(component, str) or not TASK_KEY.fullmatch(component):
        component = task_key if TASK_KEY.fullmatch(task_key) else None
    if not isinstance(deliverable, str) or not deliverable.strip():
        deliverable = fallback_deliverable
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


def _coerce_descriptive_task_inputs(
    dependencies: list[str],
) -> list[dict[str, str]]:
    coerced: list[dict[str, str]] = [{"kind": "owner_instruction", "source": "goal"}]
    for dependency in dependencies:
        coerced.append({"kind": "task_result", "task_key": dependency})
    return coerced


def _normalize_task_inputs(
    task_key: str, raw: object, dependencies: list[str]
) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        # Cursor often returns a descriptive input map (source files, globs,
        # allowlists) instead of the wire shape [{kind, source|task_key}, ...].
        # Treat non-empty descriptive maps — and explicit owner_instruction —
        # as consuming the Goal for root/dependent tasks.
        consumes_goal = (
            "owner_instruction" in raw
            or raw.get("kind") == "owner_instruction"
            or (
                bool(raw)
                and not any(
                    key in raw
                    for key in ("task_key", "task_result", "task_results")
                )
            )
        )
        if consumes_goal:
            raw = _coerce_descriptive_task_inputs(dependencies)
        else:
            raw = [
                {"kind": "task_result", "task_key": dependency}
                for dependency in dependencies
            ]
    elif isinstance(raw, list) and raw:
        # LIVE-DS-15: DeepSeek often emits file-path strings or descriptive
        # objects as inputs (e.g. ["plowwhip/a.py", ...]) instead of the wire
        # shape. Coerce those to owner_instruction (+ dependency results).
        wire_kinds = {"owner_instruction", "task_result"}
        has_wire = any(
            isinstance(item, dict) and item.get("kind") in wire_kinds
            for item in raw
        )
        descriptive_only = all(
            isinstance(item, str)
            or (
                isinstance(item, dict)
                and item.get("kind") not in wire_kinds
            )
            for item in raw
        )
        if not has_wire and descriptive_only:
            raw = _coerce_descriptive_task_inputs(dependencies)
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
    coverage_raw = raw.get("coverage")
    if coverage_raw is None and isinstance(raw.get("coverage_id"), str):
        coverage_raw = [raw["coverage_id"]]
    coverage = _normalize_coverage(
        f"task {task_key} result", coverage_raw
    )
    acceptance_ids = raw.get("acceptance_ids")
    if isinstance(acceptance_ids, list):
        acceptance_ids = [
            _coerce_coverage_id(str(item)) if isinstance(item, str) else item
            for item in acceptance_ids
        ]
    expected_acceptance_ids = [item["id"] for item in acceptance]
    if acceptance_ids != expected_acceptance_ids:
        raise ValueError(
            f"task {task_key} result must map every acceptance id exactly"
        )
    artifact = raw.get("artifact")
    if result_type == "artifact" and not isinstance(artifact, dict):
        path = raw.get("path") or raw.get("artifact_path")
        if not isinstance(path, str) or not path.strip():
            # LIVE-DS-17: path sometimes only appears in coverage / prose.
            for candidate in coverage:
                if isinstance(candidate, str) and candidate.endswith(
                    (".md", ".txt", ".json", ".html", ".yaml", ".yml")
                ):
                    path = candidate
                    break
        if isinstance(path, str) and path.strip():
            artifact = {
                "path": path.strip(),
                "format": raw.get("format") or "markdown",
            }
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
    if isinstance(result_format, str):
        aliases = {
            "md": "markdown",
            "markdown": "markdown",
            "txt": "text",
            "text": "text",
            "htm": "html",
            "html": "html",
            "yml": "yaml",
            "yaml": "yaml",
        }
        result_format = aliases.get(result_format.strip().lower(), result_format)
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
    role_key = raw.get("role_key") or raw.get("role")
    provided_ids = raw.get("acceptance_ids")
    if isinstance(provided_ids, list):
        provided_ids = [
            _coerce_coverage_id(str(item)) if isinstance(item, str) else item
            for item in provided_ids
        ]
    if role_key != expected_role:
        # Cursor often invents descriptive Checker role names
        # (e.g. independent_audit_doc_checker) for the independent Checker.
        role_text = str(role_key or "").lower().replace("-", "_")
        expected_text = expected_role.lower().replace("-", "_")
        aliases = {
            "independent_checker": (
                "independent_checker",
                "independent_audit",
                "independent_audit_doc_checker",
                "checker",
            ),
        }
        allowed = aliases.get(expected_text, (expected_text,))
        if not (
            role_text == expected_text
            or role_text in allowed
            or (
                expected_text == "independent_checker"
                and "checker" in role_text
            )
        ):
            raise ValueError(
                f"task {task_key} Checker must independently map every acceptance"
            )
        role_key = expected_role
    if provided_ids != acceptance_ids:
        if (
            isinstance(provided_ids, list)
            and provided_ids
            and all(isinstance(item, str) for item in provided_ids)
            and set(provided_ids) <= set(acceptance_ids)
        ):
            # Cursor often lists a subset; lifecycle still requires full coverage.
            provided_ids = acceptance_ids
        elif expected_role == "independent_checker" and (
            provided_ids is None
            or (
                isinstance(provided_ids, list)
                and all(isinstance(item, str) for item in provided_ids)
            )
        ):
            # LIVE-DS-17 / LIVE-DS-12: DeepSeek invents parallel / typo checker ids;
            # independent Checker always covers the frozen Task acceptance set.
            provided_ids = acceptance_ids
        else:
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
    elif kind == "local_script":
        expected = {
            "level": "recoverable",
            "allowed_actions": ["write_workspace", "run_checks"],
            "target_scope": "project_workspace",
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
    # DeepSeek often emits a prose authorization_boundary string.
    if isinstance(raw, str) and raw.strip():
        prose = raw.lower()
        if expected["level"] == "recoverable" and (
            "write" in prose
            or "overwrite" in prose
            or "create" in prose
            or "写入" in prose
            or "覆盖" in prose
        ):
            return expected
        if expected["level"] == "none" and (
            "read" in prose or "只读" in prose or "readonly" in prose
        ):
            return expected
        if expected["level"] == "recoverable" and (
            "read-only" in prose or "只读" in prose
        ):
            # Audit report delivery: prose stresses read-only analysis plus one
            # bounded report write — accept the recoverable runtime boundary.
            return expected
        raise ValueError(
            f"task {task_key} authorization boundary conflicts with runtime"
        )
    if not isinstance(raw, dict):
        raise ValueError(f"task {task_key} requires an authorization boundary")
    if raw == expected:
        return expected
    level = str(raw.get("level") or raw.get("capability") or "").lower()
    actions = raw.get("allowed_actions") or []
    action_text = " ".join(str(item).lower() for item in actions)
    description = str(raw.get("description") or raw.get("target_scope") or "")
    description_l = description.lower()
    if expected["level"] == "recoverable" and (
        level
        in {
            "recoverable",
            "recoverable_workspace_write",
            "write",
            "none",  # DeepSeek often labels audit write as type=none
        }
        or "write" in action_text
        or "create_or_overwrite" in action_text
        or "write_artifact" in action_text
        or "write_specific" in action_text
        or "write_deliverable" in action_text
        or "写入" in description
        or "write" in description_l
        or "overwrite" in description_l
        or "只读" in description  # audit: read analysis + one report write
    ):
        return expected
    if expected["level"] == "none" and (
        level
        in {
            "none",
            "read_only",
            "readonly",
            "read",
        }
        or "read" in action_text
        or "只读" in description
        or "read" in description_l
    ):
        return expected
    if expected["level"] == "external" and level in {
        "external",
        "external_capability",
    }:
        return expected
    raise ValueError(
        f"task {task_key} authorization boundary conflicts with runtime"
    )


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
    from .timeout_policy import clamp_timeout_seconds, default_timeout_seconds

    if value is None:
        if size == "large":
            raise ValueError(
                f"large task {task_key} requires max_runtime_seconds"
            )
        # LIVE-DS-22: every Task gets a sized wall-clock budget.
        return clamp_timeout_seconds(default_timeout_seconds(size), size)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 86_400
    ):
        raise ValueError(f"task {task_key} has invalid max_runtime_seconds")
    return clamp_timeout_seconds(value, size)


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
        raw_id = str(item.get("id") or "").strip()
        acceptance_id = _coerce_coverage_id(raw_id) if raw_id else ""
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
    raw = {
        key: value
        for key, value in raw.items()
        if key in allowed_roles and isinstance(value, dict)
    }
    if (
        role_key in raw
        and checker_role not in raw
        and isinstance(raw[role_key].get("provider_order"), list)
    ):
        raw = {
            **raw,
            checker_role: {
                "provider_order": list(raw[role_key]["provider_order"])
            },
        }
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
            # Models often inject provider_key; Provider order is the contract.
            if name == "provider_key":
                continue
            limits = TASK_SETTING_LIMITS.get(name)
            if not limits or isinstance(value, bool) or not isinstance(value, int):
                # Ignore unknown keys instead of failing closed on DeepSeek extras.
                continue
            if not limits[0] <= value <= limits[1]:
                raise ValueError(f"task {task_key} setting {name} is out of range")
            role_values[name] = value
        normalized[target_role] = role_values
    return normalized
