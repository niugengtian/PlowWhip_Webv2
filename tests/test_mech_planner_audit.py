"""MECH-03/05/06: Planner contract codes, audit delivery, provider capabilities."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from plowwhip.execution import _execution_context_policy
from plowwhip.host_bridge import _context_policy
from plowwhip.planner import (
    PLANNER_PRODUCT_DISCIPLINE,
    PLANNER_RESULT_PREFIX,
    audit_delivery_intent,
    normalize_plan,
    parse_planner_result,
    planner_prompt,
)
from plowwhip.planner_errors import (
    SANDBOX_FALSE_INSUFFICIENT,
    PlannerContractError,
)
from plowwhip.progress_policy import PROGRESS_EXPLORATION, merge_context_policy
from plowwhip.provider import (
    HostBridgeError,
    provider_accepts_model_flag,
    start_provider_job,
)


class MechPlannerAuditTest(unittest.TestCase):
    def test_planner_prompt_includes_bounded_product_discipline(self):
        prompt = planner_prompt("审查代码并交付报告", "check-code", {})
        self.assertIn("Product discipline (bounded)", prompt)
        self.assertIn("Non-Goals", prompt)
        self.assertIn("never silently absorb", prompt)
        self.assertIn(PLANNER_PRODUCT_DISCIPLINE.strip(), prompt)
        self.assertNotIn("RICE Prioritization", prompt)
        self.assertNotIn("Go-to-Market", prompt)

    def test_sandbox_false_insufficient_has_typed_code(self):
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["所需审查的源文件在当前工作区中完全不存在；工作区为空"],
                "information_sufficient": False,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [
                    {"from": "simple", "to": "medium", "facts": ["跨文件"]}
                ],
            },
            "confidence": 0.9,
            "plan": None,
        }
        with self.assertRaises(PlannerContractError) as raised:
            parse_planner_result(
                PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
            )
        self.assertEqual(raised.exception.code, SANDBOX_FALSE_INSUFFICIENT)
        self.assertTrue(raised.exception.fallback_eligible)

    def test_audit_report_artifact_tags_audit_delivery(self):
        plan = {
            "summary": "readonly code review report",
            "required_coverage": ["CODE_REVIEW.md"],
            "selected": 0,
            "selection": {"mode": "objective", "basis": ["single"]},
            "alternatives": [
                {
                    "name": "single",
                    "scope": "one report",
                    "cost": "low",
                    "risk": "0",
                    "reversible": True,
                    "acceptance": "report ready",
                    "objective_metrics": {
                        "coverage": ["CODE_REVIEW.md"],
                        "estimated_effort": 1,
                        "risk_level": 0,
                    },
                }
            ],
            "tasks": [
                {
                    "key": "code_review",
                    "instruction": "审查当前代码并写入 docs/runtime-audits/CODE_REVIEW.md",
                    "depends_on": [],
                    "sprint": 1,
                    "role_key": "fullstack",
                    "acceptance": [
                        {"id": "report_exists", "expected": "report file exists"}
                    ],
                    "responsibility": {
                        "component_id": "code-review",
                        "deliverable": "docs/runtime-audits/CODE_REVIEW.md",
                    },
                    "inputs": {"owner_instruction": "review"},
                    "result": {
                        "type": "artifact",
                        "coverage": ["CODE_REVIEW.md"],
                        "acceptance_ids": ["report_exists"],
                        "path": "docs/runtime-audits/CODE_REVIEW.md",
                        "format": "markdown",
                    },
                    "checker": {
                        "role": "independent_checker",
                        "acceptance_ids": ["report_exists"],
                    },
                    "authorization_boundary": {
                        "capability": "none",
                        "allowed_actions": ["read", "write_artifact"],
                        "target_scope": "docs/runtime-audits/CODE_REVIEW.md",
                    },
                }
            ],
        }
        normalized = normalize_plan(plan, size="medium", requires_owner_choice=False)
        spec = normalized["tasks"][0]["spec"]
        self.assertTrue(spec["workspace_change_required"])
        self.assertTrue(spec.get("audit_delivery"))
        self.assertTrue(audit_delivery_intent(spec))

    def test_execution_context_policy_uses_exploration_for_audit_delivery(self):
        policy = _execution_context_policy(
            {},
            {
                "kind": "provider_task",
                "instruction": "审查代码并输出报告",
                "workspace_change_required": True,
                "audit_delivery": True,
            },
        )
        self.assertEqual(policy.get("progress_delivery"), "audit_report")
        merged = merge_context_policy(
            policy, access="write", workspace_kind="project"
        )
        self.assertEqual(merged["progress_mode"], PROGRESS_EXPLORATION)
        self.assertGreaterEqual(int(merged["max_turns"]), 96)
        bridge = _context_policy(policy, access="write", workspace_kind="project")
        self.assertEqual(bridge["progress_mode"], PROGRESS_EXPLORATION)
        self.assertGreaterEqual(int(bridge["tool_no_progress_limit"]), 96)
        self.assertGreaterEqual(int(bridge["max_turns"]), 96)

    def test_deepseek_rejects_non_default_model_for_fallback(self):
        self.assertFalse(provider_accepts_model_flag("deepseek"))
        self.assertTrue(provider_accepts_model_flag("cursor_cli"))
        with (
            patch(
                "plowwhip.provider._bridge_configuration",
                return_value=("http://127.0.0.1:9", "x" * 24),
            ),
            patch("plowwhip.provider._bridge_post") as post,
            self.assertRaises(HostBridgeError) as raised,
        ):
            start_provider_job(
                "job-model-1",
                "deepseek",
                "/tmp/project",
                "prompt",
                session_id="s1",
                timeout_seconds=60,
                access="read",
                workspace_kind="planner",
                workspace_key="wk",
                model="deepseek-v4-flash",
            )
        self.assertTrue(raised.exception.rejected)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
