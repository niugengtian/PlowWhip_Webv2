"""Control-plane audit_delivery Plan template (provider-agnostic salvage)."""

from __future__ import annotations

import json
import unittest

from plowwhip.planner import (
    PLANNER_RESULT_PREFIX,
    build_audit_delivery_plan,
    chapters_from_goal,
    parse_planner_result,
    provider_lock_from_goal,
)


class AuditDeliveryTemplateTest(unittest.TestCase):
    def test_mech_e2e_chapters_are_exactly_four(self):
        chapters = chapters_from_goal(
            "报告四章非空：总判；主线证据；MECH-01～07；"
            "无人值守/优质代码/省 Token 风险与建议。"
        )
        self.assertEqual(
            chapters,
            [
                "总判",
                "主线证据",
                "MECH-01～07",
                "无人值守/优质代码/省 Token 风险与建议",
            ],
        )

    def test_provider_lock_only_when_goal_explicit(self):
        self.assertIsNone(
            provider_lock_from_goal(
                "只读审查代码并写入 docs/runtime-audits/AUDIT_REPORT.md"
            )
        )
        self.assertEqual(
            provider_lock_from_goal("仅使用 deepseek 作为唯一 Provider"),
            ["deepseek"],
        )
        self.assertEqual(
            provider_lock_from_goal("only cursor_cli for this Goal"),
            ["cursor_cli"],
        )

    def test_build_template_has_no_provider_order_by_default(self):
        plan = build_audit_delivery_plan(
            "审查 plowwhip/ 并交付 docs/runtime-audits/AUDIT_REPORT.md，含总判与主线证据",
            size="medium",
            classification={
                "size": "medium",
                "reasons": ["单一报告"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [],
            },
        )
        task = plan["tasks"][0]
        self.assertNotIn("settings", task)
        self.assertEqual(
            task["result"]["path"],
            "docs/runtime-audits/AUDIT_REPORT.md",
        )

    def test_salvage_when_model_omits_plan_for_audit_goal(self):
        goal = (
            "有界只读代码审查，交付 docs/runtime-audits/MY_AUDIT.md，"
            "报告含总判、主线证据、机制验证、风险与建议。"
        )
        # Classification-only result: common when the model narrates correctly
        # but never emits a wire-shaped plan. Control plane must assemble it.
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["一名 Worker 一份报告"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["跨文件综合"],
            },
            "confidence": 0.9,
            "plan": None,
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False),
            goal_instruction=goal,
        )
        self.assertEqual(parsed["plan_source"], "control_plane_audit_template")
        task = parsed["plan"]["tasks"][0]
        self.assertEqual(task["key"], "audit_delivery")
        self.assertTrue(task["spec"].get("audit_delivery"))
        self.assertEqual(
            task["result"]["artifact"]["path"],
            "docs/runtime-audits/MY_AUDIT.md",
        )
        self.assertNotIn("provider_order", json.dumps(task.get("settings") or {}))

    def test_salvage_when_nested_plan_still_invalid_for_audit_goal(self):
        goal = "只读审计并写 docs/runtime-audits/MY_AUDIT.md，含总判。"
        broken = {
            "classification": {
                "size": "medium",
                "reasons": ["单一报告"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["综合"],
            },
            "confidence": 0.9,
            "plan": {
                "summary": "audit",
                "required_coverage": ["docs/runtime-audits/MY_AUDIT.md"],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一"]},
                "alternatives": [
                    {
                        "name": "a",
                        "scope": "s",
                        "cost": "c",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "ok",
                        "objective_metrics": {
                            "coverage": ["docs/runtime-audits/MY_AUDIT.md"],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "!!!invalid-key!!!",
                        "instruction": "write",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [],
                        "responsibility": {},
                        "inputs": [],
                        "result": {"type": "artifact"},
                        "checker": {},
                        "authorization_boundary": {},
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(broken, ensure_ascii=False),
            goal_instruction=goal,
        )
        self.assertEqual(parsed["plan_source"], "control_plane_audit_template")
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["artifact"]["path"],
            "docs/runtime-audits/MY_AUDIT.md",
        )

    def test_audit_goal_always_uses_template_even_if_model_plan_parses(self):
        goal = "只读审计并写 docs/runtime-audits/MY_AUDIT.md，含总判。"
        # A coerceable model plan must still be replaced for audit Goals.
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一报告"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["综合"],
            },
            "confidence": 0.9,
            "plan": {
                "summary": "model plan",
                "required_coverage": ["docs/runtime-audits/MY_AUDIT.md"],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一"]},
                "alternatives": [
                    {
                        "name": "a",
                        "scope": "s",
                        "cost": "c",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "ok",
                        "objective_metrics": {
                            "coverage": ["docs/runtime-audits/MY_AUDIT.md"],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "audit_delivery",
                        "instruction": "write report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "exists"}
                        ],
                        "responsibility": {
                            "component": "audit_delivery",
                            "deliverable": "docs/runtime-audits/MY_AUDIT.md",
                        },
                        "inputs": {"owner_instruction": "audit"},
                        "result": {
                            "type": "artifact",
                            "coverage": ["docs/runtime-audits/MY_AUDIT.md"],
                            "acceptance_ids": ["report_exists"],
                            "path": "docs/runtime-audits/MY_AUDIT.md",
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists"],
                        },
                        "authorization_boundary": {
                            "level": "recoverable",
                            "allowed_actions": ["write_workspace"],
                            "target_scope": "project_workspace",
                        },
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False),
            goal_instruction=goal,
        )
        self.assertEqual(parsed["plan_source"], "control_plane_audit_template")
        # Template acceptance is richer than the model's single id.
        self.assertGreater(
            len(parsed["plan"]["tasks"][0]["acceptance"]),
            1,
        )

    def test_salvage_does_not_apply_to_non_audit_goal(self):
        broken = {
            "classification": {
                "size": "medium",
                "reasons": ["实现功能"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["多步"],
            },
            "confidence": 0.9,
            "plan": {
                "summary": "implement",
                "required_coverage": ["feature"],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一"]},
                "alternatives": [],
                "tasks": [
                    {
                        "key": "implement",
                        "instruction": "改代码",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [{"id": "done", "expected": "done"}],
                        "responsibility": {"component": "x"},
                        "inputs": ["a.py"],
                        "result": {
                            "type": "workspace_change",
                            "coverage": ["feature"],
                            "acceptance_ids": ["done"],
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["done"],
                        },
                        "authorization_boundary": {
                            "level": "recoverable",
                            "allowed_actions": ["write_workspace"],
                            "target_scope": "project_workspace",
                        },
                    }
                ],
            },
        }
        with self.assertRaises(Exception):
            parse_planner_result(
                PLANNER_RESULT_PREFIX + json.dumps(broken, ensure_ascii=False),
                goal_instruction="实现一个新功能并修改源码",
            )

    def test_explicit_provider_lock_copied_into_template_settings(self):
        plan = build_audit_delivery_plan(
            "仅使用 deepseek。审查并写 docs/runtime-audits/X.md，含总判。",
            size="medium",
            classification={
                "size": "medium",
                "reasons": ["单一报告"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [],
            },
        )
        self.assertEqual(
            plan["tasks"][0]["settings"]["fullstack"]["provider_order"],
            ["deepseek"],
        )


if __name__ == "__main__":
    unittest.main()
