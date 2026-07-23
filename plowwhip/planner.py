from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .intake import normalize_instruction
from .provider import (
    PROVIDERS,
    provider_job_output,
    provider_job_status,
    start_provider_job,
)


TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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
    "monitor_tail_lines": (1, 1_000),
    "monitor_tail_bytes": (256, 1_048_576),
    "retry_count": (0, 10),
    "retry_backoff_seconds": (0, 86_400),
}


@dataclass(frozen=True)
class PlannerStep:
    kind: str
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    project_path: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    classification: dict
    context_policy: dict[str, object]


def classify_instruction(content: str, kind: str) -> dict[str, object]:
    if kind in {"write_text", "provider_probe"}:
        return {"size": "simple", "reasons": [kind], "authorization_required": False}
    if kind == "git_publish":
        return {
            "size": "simple",
            "reasons": ["deterministic_external_git_publish"],
            "authorization_required": True,
        }
    lowered = content.lower()
    numbered_steps = len(
        re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s*\S+", content)
    )
    high_risk = [term for term in HIGH_RISK_TERMS if term in lowered]
    reasons = [term for term in LARGE_TERMS if term in lowered]
    if numbered_steps >= 3:
        reasons.append(f"{numbered_steps}_declared_steps")
    if reasons:
        return {
            "size": "large",
            "reasons": list(dict.fromkeys(reasons)),
            "authorization_required": bool(high_risk),
        }
    return {
        "size": "medium",
        "reasons": ["one_worker_owns_scope"],
        "authorization_required": False,
    }


def planner_prompt(instruction: str, project_id: str, classification: dict) -> str:
    return (
        "Create the smallest executable Plan for this Goal without modifying files.\n"
        f"Project: {project_id}\nGoal: {instruction}\n"
        f"Classification facts: {json.dumps(classification, ensure_ascii=False, sort_keys=True)}\n"
        "Return at least two genuine alternatives comparing name, scope, cost, risk, "
        "reversible, and acceptance. Select one and emit a serializable Task DAG with "
        "2-50 bounded tasks. Each task needs key, instruction, depends_on, sprint, "
        "role_key, a 1-20 item acceptance array with stable id and expected result, "
        "optional earliest_start_delay_seconds, optional deadline_seconds, and optional "
        "settings. Use role_key fullstack for code work or "
        "deterministic only for exact '写入 relative-path: content' instructions. "
        "Do not add deployment, deletion, payment, publishing, permission changes, "
        "or scope absent from the Goal. Finish with one line beginning "
        f"{PLANNER_RESULT_PREFIX!r} followed by "
        '{"confidence":0.95,"plan":{"alternatives":[],"selected":0,'
        '"summary":"...","tasks":[]}}.'
    )


def perform_planner_step(step: PlannerStep) -> dict[str, object]:
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
            )
            if step.kind == "start"
            else provider_job_status(step.job_id)
        )
        return {
            "ok": True,
            "state": state,
            "output": provider_job_output(step.job_id),
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}


def parse_planner_result(output: str) -> dict:
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
    return {"confidence": float(confidence), "plan": normalize_plan(payload.get("plan"))}


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
        if spec["kind"] == "write_text":
            default_role, checker_role = "deterministic", "deterministic_checker"
        elif spec["kind"] == "provider_task":
            default_role, checker_role = "fullstack", "independent_checker"
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
        if isinstance(sprint, bool) or not isinstance(sprint, int) or not 1 <= sprint <= 10_000:
            raise ValueError(f"task {key} has invalid sprint")
        role_key = str(item.get("role_key", default_role))
        if role_key != default_role:
            raise ValueError(f"task {key} role does not match its instruction")
        settings = _normalize_task_settings(
            key, role_key, checker_role, item.get("settings", {})
        )
        acceptance = _normalize_task_acceptance(
            key, item.get("acceptance"), acceptance
        )
        earliest_start_delay_seconds = _bounded_schedule_value(
            key, "earliest_start_delay_seconds", item.get("earliest_start_delay_seconds"), 0
        )
        deadline_seconds = _bounded_schedule_value(
            key, "deadline_seconds", item.get("deadline_seconds"), None
        )
        normalized.append(
            {
                "key": key,
                "spec": {**spec, "task_key": key},
                "acceptance": acceptance,
                "depends_on": dependencies,
                "sprint": sprint,
                "role_key": role_key,
                "checker_role": checker_role,
                "settings": settings,
                "earliest_start_delay_seconds": earliest_start_delay_seconds,
                "deadline_seconds": deadline_seconds,
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
        expected = str(item.get("expected") or "").strip()
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
            limits = TASK_SETTING_LIMITS.get(name)
            if not limits or isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"task {task_key} has invalid setting {name}")
            if not limits[0] <= value <= limits[1]:
                raise ValueError(f"task {task_key} setting {name} is out of range")
            role_values[name] = value
        normalized[target_role] = role_values
    return normalized
