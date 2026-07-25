"""Bounded, actionable owner choices for a Task in needs_decision."""

from __future__ import annotations


_TEMPLATES = {
    "authorize_plan": ("授权采用计划", "在当前边界内安装已验证的 Plan", "将创建并推进 Plan Task"),
    "cancel_task": ("取消 Task", "停止当前工作且保留已记录 Evidence", "Task 将进入取消终态"),
    "retry_planner": ("重试 Planner", "重新生成可解析的单一可执行 Plan", "返回规划阶段"),
    "retry_checker": ("重试 Checker", "重新独立核验当前交付", "返回检查阶段"),
    "change_strategy": ("调整策略", "要求以不同边界或方法重新规划", "等待新的明确策略"),
    "shrink_goal": ("缩小目标", "将目标收窄为可验证子集", "创建新的收窄 Spec"),
}


def build_decision_options(
    *,
    scenario: str,
    reason: str,
    basis: str = "",
) -> list[dict[str, object]]:
    """Return 2-7 non-isomorphic options appropriate to the blocked phase."""
    choices = {
        "plan": ("authorize_plan", "retry_planner", "cancel_task"),
        "recovery_cap": ("change_strategy", "shrink_goal", "cancel_task"),
        "checker": ("retry_checker", "change_strategy", "cancel_task"),
        "empty_delivery": ("change_strategy", "shrink_goal", "cancel_task"),
    }.get(scenario, ("retry_planner", "change_strategy", "cancel_task"))
    return [
        {
            "option_id": option_id,
            "title": _TEMPLATES[option_id][0],
            "reason": reason,
            "basis": basis or scenario,
            "pros": [_TEMPLATES[option_id][1]],
            "cons": ["需要主人明确选择；不会自动重复同一失败路径"],
            "effect": _TEMPLATES[option_id][2],
        }
        for option_id in choices
    ]
