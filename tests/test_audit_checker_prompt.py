"""LIVE-DS-12: audit Checker validates report delivery, not a second code review."""

from __future__ import annotations

import unittest

from plowwhip.verification import _checker_context_policy, _checker_prompt


class _Row(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class AuditCheckerPromptTest(unittest.TestCase):
    def test_audit_checker_prompt_is_report_only(self):
        task = _Row(
            {
                "id": "task-audit",
                "spec_revision": 2,
                "acceptance_json": (
                    '[{"id":"report_exists","expected":"report file exists"},'
                    '{"id":"chapter_summary","expected":"contains 总判"}]'
                ),
            }
        )
        spec = {
            "kind": "provider_task",
            "instruction": (
                "Read plowwhip/lifecycle.py plowwhip/execution.py plowwhip/planner.py "
                "and write a deep code review to docs/runtime-audits/REPORT.md"
            ),
            "audit_delivery": True,
            "path": "docs/runtime-audits/REPORT.md",
            "workspace_change_required": True,
        }
        execution = {
            "workspace_changed": True,
            "provider_key": "deepseek",
            "result_artifacts": [
                {"kind": "output", "path": "docs/runtime-audits/REPORT.md"}
            ],
        }
        prompt = _checker_prompt(task, spec, execution, "")
        self.assertIn("LIVE-DS-12", prompt)
        self.assertIn("report Artifact generated with real content", prompt)
        self.assertIn("docs/runtime-audits/REPORT.md", prompt)
        self.assertNotIn("plowwhip/lifecycle.py", prompt)
        self.assertNotIn("deep code review", prompt)
        policy = _checker_context_policy({}, spec)
        self.assertEqual(policy["max_turns"], 16)
        self.assertEqual(policy["progress_mode"], "exploration")


if __name__ == "__main__":
    unittest.main()
