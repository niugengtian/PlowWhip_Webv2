from __future__ import annotations

import re
from typing import Any


_FIELD_ORDER = ("objective", "boundaries", "acceptance")
_FIELD_WEIGHT = {"objective": 35, "boundaries": 30, "acceptance": 30}

_VAGUE_PATTERNS = (
    re.compile(r"^(做好|搞一下|弄一下|随便|尽快|优化一下|改一下).*$"),
    re.compile(r"能通过验收"),
    re.compile(r"^(完成|实现|处理).{0,6}$"),
    re.compile(r"看起来?可以"),
    re.compile(r"差不多就行"),
)

_ACCEPTANCE_MARKERS = (
    "通过", "exit", "test", "测试", "hash", "sha", "证据", "evidence",
    "lint", "typecheck", "build", "compile", "assert", "验收", "验证",
)
_BOUNDARY_MARKERS = (
    "只改", "不得", "禁止", "不修改", "不读取", "不共享", "仅", "允许",
    "scope", "边界", "不得绕过", "不提交", "不推送", "不部署",
)
_OBJECTIVE_MARKERS = (
    "实现", "交付", "修复", "迁移", "提供", "完成", "覆盖", "保证",
    "verify", "implement", "provide", "fix",
)


def assess_goal_semantics(draft: dict[str, Any]) -> dict[str, Any]:
    """Deterministic semantic gate. Non-empty fields alone never yield 95."""
    field_scores: dict[str, dict[str, Any]] = {}
    for field in _FIELD_ORDER:
        field_scores[field] = _score_field(field, draft.get(field))

    confidence = sum(
        int(round(_FIELD_WEIGHT[field] * field_scores[field]["ratio"]))
        for field in _FIELD_ORDER
    )
    gaps = [
        {
            "field": field,
            "reason": field_scores[field]["reason"],
            "ratio": field_scores[field]["ratio"],
        }
        for field in _FIELD_ORDER
        if field_scores[field]["ratio"] < 0.95
    ]
    return {
        "confidence": min(100, confidence),
        "ready": confidence >= 95 and not gaps,
        "gaps": gaps,
        "field_scores": field_scores,
        "model_invoked": False,
    }


def next_semantic_gap(draft: dict[str, Any]) -> str | None:
    assessment = assess_goal_semantics(draft)
    if not assessment["gaps"]:
        return None
    return str(assessment["gaps"][0]["field"])


def gap_question(field: str, draft: dict[str, Any]) -> str:
    """One gap-based question; wording depends on the current deficit."""
    assessment = assess_goal_semantics(draft)
    score = assessment["field_scores"].get(field) or {}
    reason = str(score.get("reason") or "missing")
    if field == "objective":
        if reason == "missing":
            return "这个目标最终要交付什么可验证结果？"
        if reason == "vague":
            return "当前目标仍偏模糊：请改写成具体、可验证的结果，而不是口号式描述。"
        return "请补充目标中仍然缺失的可执行结果与约束。"
    if field == "boundaries":
        if reason == "missing":
            return "这个目标允许改什么、明确不能动什么？"
        if reason == "vague":
            return "边界还不够具体：请写出允许修改的范围和明确禁止项。"
        return "请把边界写成可执行的允许/禁止列表。"
    if reason == "missing":
        return "用哪些可检查的证据判断目标已经完成？"
    if reason == "vague":
        return "验收标准仍不可验证：请写成具体命令、产物或哈希类证据，而不是“能通过验收”。"
    return "请补全仍缺少检查点的验收标准。"


def structured_fields_provided(draft: dict[str, Any]) -> bool:
    """True when the owner submitted objective/boundaries/acceptance explicitly."""
    return bool(draft.get("objective")) and bool(draft.get("boundaries")) and bool(
        draft.get("acceptance")
    )


def _score_field(field: str, value: Any) -> dict[str, Any]:
    text = _as_text(value)
    if not text.strip():
        return {"ratio": 0.0, "reason": "missing", "chars": 0}
    if _is_vague(text):
        return {"ratio": 0.35, "reason": "vague", "chars": len(text)}
    chars = len(text.strip())
    if field == "objective":
        if chars >= 16 or any(marker in text for marker in _OBJECTIVE_MARKERS):
            return {"ratio": 1.0, "reason": "ok", "chars": chars}
        return {"ratio": 0.55, "reason": "thin", "chars": chars}
    if field == "boundaries":
        items = _as_items(value)
        has_markers = any(
            any(marker in item for marker in _BOUNDARY_MARKERS) for item in items
        )
        if (len(items) >= 2 and chars >= 16) or (has_markers and chars >= 12):
            return {"ratio": 1.0, "reason": "ok", "chars": chars}
        if len(items) >= 1 and chars >= 8:
            return {"ratio": 0.6, "reason": "thin", "chars": chars}
        return {"ratio": 0.4, "reason": "thin", "chars": chars}
    items = _as_items(value)
    has_markers = any(
        any(marker in item.lower() for marker in _ACCEPTANCE_MARKERS) for item in items
    )
    if (len(items) >= 2 and chars >= 16) or (has_markers and chars >= 12):
        return {"ratio": 1.0, "reason": "ok", "chars": chars}
    if len(items) >= 1 and chars >= 8:
        return {"ratio": 0.55, "reason": "thin", "chars": chars}
    return {"ratio": 0.35, "reason": "thin", "chars": chars}


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _as_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [line.strip(" \t-•") for line in text.splitlines() if line.strip(" \t-•")]


def _is_vague(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    if len(compact) < 8:
        return True
    return any(pattern.search(text.strip()) for pattern in _VAGUE_PATTERNS)
