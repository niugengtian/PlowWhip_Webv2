"""LIVE-DS-21: control-plane audit report proof salvages false Checker rejects."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from plowwhip.verification import (
    _deterministic_audit_report_verdict,
    _heading_from_acceptance_expected,
)


class AuditDeterministicCheckerTest(unittest.TestCase):
    def test_heading_extraction(self):
        self.assertEqual(
            _heading_from_acceptance_expected("Report contains heading '总判'"),
            "总判",
        )
        self.assertEqual(
            _heading_from_acceptance_expected(
                "报告含无人值守/优质代码/省 Token 风险与建议"
            ),
            "无人值守/优质代码/省 Token 风险与建议",
        )

    def test_deterministic_pass_when_report_has_chapters(self):
        spec = {
            "project_path": "/workspace",
            "instruction": "audit_delivery report",
            "task_contract": {
                "result": {
                    "type": "artifact",
                    "artifact": {
                        "path": "docs/runtime-audits/AUDIT.md",
                        "format": "markdown",
                    },
                }
            },
        }
        expected = [
            {"id": "report_exists", "expected": "File exists"},
            {"id": "report_non_empty", "expected": "File is non-empty"},
            {"id": "chapter_1", "expected": "Report contains heading '总判'"},
            {"id": "chapter_2", "expected": "Report contains heading '主线证据'"},
        ]
        body = "# 总判\nok\n# 主线证据\nok\n"
        with patch(
            "plowwhip.verification.workspace_snapshot",
            return_value={
                "requested": [
                    {
                        "path": "docs/runtime-audits/AUDIT.md",
                        "bytes": len(body.encode()),
                        "sha256": "a" * 64,
                        "text": body,
                    }
                ]
            },
        ):
            verdict = _deterministic_audit_report_verdict(spec, expected)
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertEqual(verdict["decision_reason"], "control_plane_audit_report_proof")

    def test_deterministic_fail_when_chapter_missing(self):
        spec = {
            "project_path": "/workspace",
            "task_contract": {
                "result": {
                    "type": "artifact",
                    "artifact": {"path": "docs/runtime-audits/AUDIT.md"},
                }
            },
        }
        expected = [
            {"id": "report_exists", "expected": "exists"},
            {"id": "report_non_empty", "expected": "non-empty"},
            {"id": "chapter_1", "expected": "Report contains heading '总判'"},
        ]
        body = "no required heading here\n"
        with patch(
            "plowwhip.verification.workspace_snapshot",
            return_value={
                "requested": [
                    {
                        "path": "docs/runtime-audits/AUDIT.md",
                        "bytes": len(body.encode()),
                        "sha256": "b" * 64,
                        "text": body,
                    }
                ]
            },
        ):
            verdict = _deterministic_audit_report_verdict(spec, expected)
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["verdict"], "CHANGES_REQUIRED")


if __name__ == "__main__":
    unittest.main()
