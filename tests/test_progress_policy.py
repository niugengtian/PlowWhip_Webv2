"""MECH-01: role-aware ProgressPolicy for read vs write HostJobs."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from plowwhip.host_bridge import _context_policy
from plowwhip.progress_policy import (
    PROGRESS_EXPLORATION,
    PROGRESS_MUTATION,
    merge_context_policy,
    resolve_progress_policy,
)
from plowwhip.provider import start_provider_job


class ProgressPolicyTest(unittest.TestCase):
    def test_read_and_planner_resolve_to_exploration(self):
        read_policy = resolve_progress_policy(access="read")
        planner_policy = resolve_progress_policy(
            access="read", workspace_kind="planner"
        )
        self.assertEqual(read_policy["progress_mode"], PROGRESS_EXPLORATION)
        self.assertEqual(planner_policy["progress_mode"], PROGRESS_EXPLORATION)
        self.assertGreaterEqual(int(read_policy["tool_no_progress_limit"]), 96)
        self.assertGreaterEqual(int(read_policy["max_turns"]), 48)

    def test_write_project_resolves_to_mutation(self):
        policy = resolve_progress_policy(access="write", workspace_kind="project")
        self.assertEqual(policy["progress_mode"], PROGRESS_MUTATION)
        self.assertLessEqual(int(policy["tool_no_progress_limit"]), 12)

    def test_audit_report_delivery_gets_extended_turn_budget(self):
        policy = resolve_progress_policy(
            access="write",
            workspace_kind="project",
            delivery="audit_report",
        )
        self.assertEqual(policy["progress_mode"], PROGRESS_EXPLORATION)
        self.assertGreaterEqual(int(policy["max_turns"]), 96)
        self.assertEqual(policy.get("progress_delivery"), "audit_report")

    def test_explicit_probe_limits_override_role_defaults(self):
        merged = merge_context_policy(
            {"max_turns": 1, "tool_no_progress_limit": 1},
            access="read",
            workspace_kind="planner",
        )
        self.assertEqual(merged["progress_mode"], PROGRESS_EXPLORATION)
        self.assertEqual(merged["max_turns"], 1)
        self.assertEqual(merged["tool_no_progress_limit"], 1)

    def test_bridge_context_policy_branches_on_access(self):
        exploration = _context_policy({}, access="read", workspace_kind="planner")
        mutation = _context_policy({}, access="write", workspace_kind="project")
        self.assertEqual(exploration["progress_mode"], PROGRESS_EXPLORATION)
        self.assertEqual(mutation["progress_mode"], PROGRESS_MUTATION)
        self.assertGreaterEqual(int(exploration["tool_no_progress_limit"]), 96)
        self.assertEqual(int(mutation["tool_no_progress_limit"]), 12)
        capped = _context_policy(
            {"tool_no_progress_limit": 300},
            access="read",
            workspace_kind="planner",
        )
        self.assertEqual(int(capped["tool_no_progress_limit"]), 256)

    def test_start_provider_job_payload_uses_exploration_for_planner(self):
        captured: dict[str, object] = {}

        def fake_post(
            base_url, token, path, payload, timeout, max_bytes=None
        ):
            captured["payload"] = payload
            return {"status": "running", "job_id": payload["job_id"]}

        with (
            patch(
                "plowwhip.provider._bridge_configuration",
                return_value=("http://127.0.0.1:9", "x" * 24),
            ),
            patch("plowwhip.provider._bridge_post", side_effect=fake_post),
        ):
            start_provider_job(
                "job-progress-1",
                "deepseek",
                "/tmp/project",
                "plan this",
                session_id="sess-1",
                timeout_seconds=60,
                access="read",
                workspace_kind="planner",
                workspace_key="wk",
            )
        policy = captured["payload"]["context_policy"]
        self.assertEqual(policy["progress_mode"], PROGRESS_EXPLORATION)
        self.assertGreaterEqual(int(policy["tool_no_progress_limit"]), 96)

    def test_start_provider_job_payload_uses_mutation_for_write(self):
        captured: dict[str, object] = {}

        def fake_post(
            base_url, token, path, payload, timeout, max_bytes=None
        ):
            captured["payload"] = payload
            return {"status": "running", "job_id": payload["job_id"]}

        with (
            patch(
                "plowwhip.provider._bridge_configuration",
                return_value=("http://127.0.0.1:9", "x" * 24),
            ),
            patch("plowwhip.provider._bridge_post", side_effect=fake_post),
        ):
            start_provider_job(
                "job-progress-2",
                "deepseek",
                "/tmp/project",
                "implement this",
                session_id="sess-2",
                timeout_seconds=60,
                access="write",
                workspace_kind="project",
            )
        policy = captured["payload"]["context_policy"]
        self.assertEqual(policy["progress_mode"], PROGRESS_MUTATION)
        self.assertEqual(int(policy["tool_no_progress_limit"]), 12)


if __name__ == "__main__":
    unittest.main()
