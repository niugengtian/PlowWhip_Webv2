from __future__ import annotations

import json
import unittest

from plowwhip.provider import CHECKER_RESULT_PREFIX
from plowwhip.result_ingest import extract_structured_json, ingest_provider_result
from plowwhip.verification import _parse_checker_verdict


class CheckerResultIngestTest(unittest.TestCase):
    def test_extracts_clean_pass_when_escaped_duplicate_follows(self):
        clean = (
            CHECKER_RESULT_PREFIX
            + json.dumps(
                {
                    "verdict": "PASS",
                    "acceptances": [
                        {
                            "acceptance_id": "planner_contract",
                            "passed": True,
                            "actual_evidence": "bounded plan matches Goal",
                            "recheck_command": "reparse planner.json",
                        }
                    ],
                    "decision_reason": None,
                },
                ensure_ascii=False,
            )
        )
        escaped = clean.replace('"', '\\"')
        # Cursor-shaped: clean assistant extract + trailing escaped envelope junk.
        agent = "analysis ok\n" + clean + "\n" + escaped + '","session_id":"x"}'
        payload = extract_structured_json(agent, CHECKER_RESULT_PREFIX)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("verdict"), "PASS")
        verdict = _parse_checker_verdict(
            agent,
            [{"id": "planner_contract", "expected": "planner result contract"}],
            "Planner Artifact",
        )
        self.assertTrue(verdict["valid"])
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["verdict"], "PASS")

    def test_mid_line_marker_inside_jsonl_envelope(self):
        inner = {
            "verdict": "PASS",
            "acceptances": [
                {
                    "acceptance_id": "planner_contract",
                    "passed": True,
                    "actual_evidence": "ok evidence",
                    "recheck_command": "recheck",
                }
            ],
        }
        envelope = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": (
                        "notes\n"
                        + CHECKER_RESULT_PREFIX
                        + json.dumps(inner, ensure_ascii=False)
                    ),
                },
            },
            ensure_ascii=False,
        )
        ingested = ingest_provider_result(envelope + "\n")
        verdict = _parse_checker_verdict(
            str(ingested["agent_text"]),
            [{"id": "planner_contract", "expected": "x"}],
            "Planner Artifact",
        )
        self.assertTrue(verdict["passed"], verdict)

    def test_optional_recheck_and_extra_acceptance_still_pass(self):
        # LIVE-DS-27: empty recheck_command must not invalidate a PASS.
        agent = (
            CHECKER_RESULT_PREFIX
            + json.dumps(
                {
                    "verdict": "PASS",
                    "acceptances": [
                        {
                            "acceptance_id": "planner_contract",
                            "passed": True,
                            "actual_evidence": "bounded plan matches Goal",
                        },
                        {
                            "acceptance_id": "extra_note",
                            "passed": True,
                            "actual_evidence": "optional extra",
                            "recheck_command": "true",
                        },
                    ],
                    "decision_reason": None,
                },
                ensure_ascii=False,
            )
        )
        verdict = _parse_checker_verdict(
            agent,
            [{"id": "planner_contract", "expected": "planner result contract"}],
            "Planner Artifact",
        )
        self.assertTrue(verdict["valid"], verdict)
        self.assertTrue(verdict["passed"], verdict)
        self.assertEqual(
            verdict["acceptances"][0]["recheck_command"],
            "manual bounded recheck required",
        )


if __name__ == "__main__":
    unittest.main()

