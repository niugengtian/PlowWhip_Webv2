"""LIVE-DS-13: annotated continue tokens must not rewrite TaskSpec."""

from __future__ import annotations

import unittest

from plowwhip.lifecycle import _is_continue_decision


class ContinueDecisionTest(unittest.TestCase):
    def test_exact_and_annotated_continue(self):
        self.assertTrue(_is_continue_decision("继续"))
        self.assertTrue(_is_continue_decision("继续：重试 Checker"))
        self.assertTrue(_is_continue_decision("重试"))
        self.assertTrue(_is_continue_decision("RETRY_PLANNER please"))
        self.assertFalse(_is_continue_decision("写一份新报告到 docs/x.md"))
        self.assertFalse(_is_continue_decision(""))


if __name__ == "__main__":
    unittest.main()
