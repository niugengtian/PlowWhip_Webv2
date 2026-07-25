"""MECH-02: unified Provider result ingestion."""

from __future__ import annotations

import json
import unittest

from plowwhip.planner import PLANNER_RESULT_PREFIX, parse_planner_result
from plowwhip.result_ingest import ingest_provider_result


class ResultIngestTest(unittest.TestCase):
    def test_ingest_folds_bare_marker_line_into_agent_text(self):
        body = (
            PLANNER_RESULT_PREFIX
            + '{"classification":{"size":"medium","reasons":["a","b"],'
            '"information_sufficient":false,"requires_owner_choice":false,'
            '"workflow":"standard","upgrade_path":["simple","medium"],'
            '"upgrade_evidence":[{"from":"simple","to":"medium","facts":["x"]}]},'
            '"confidence":0.8,"plan":null,"blocking_reason":"need scope"}'
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "worker.progress", "turn": 1}),
                body,
                json.dumps({"type": "worker.completed", "status": "completed"}),
            ]
        )
        ingested = ingest_provider_result(stdout)
        self.assertTrue(ingested["has_structured_marker"])
        self.assertIn("structured_marker", ingested["sources"])
        self.assertIn(PLANNER_RESULT_PREFIX, str(ingested["agent_text"]))
        parsed = parse_planner_result(stdout)
        self.assertIsNone(parsed["plan"])

    def test_ingest_reads_harvested_message_events(self):
        body = (
            PLANNER_RESULT_PREFIX
            + '{"classification":{"size":"medium","reasons":["a","b"],'
            '"information_sufficient":false,"requires_owner_choice":false,'
            '"workflow":"standard","upgrade_path":["simple","medium"],'
            '"upgrade_evidence":[{"from":"simple","to":"medium","facts":["x"]}]},'
            '"confidence":0.8,"plan":null,"blocking_reason":"need scope"}'
        )
        stdout = json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": body},
            },
            ensure_ascii=False,
        )
        ingested = ingest_provider_result(stdout)
        self.assertIn("jsonl_message", ingested["sources"])
        parsed = parse_planner_result(stdout)
        self.assertIsNone(parsed["plan"])
        self.assertEqual(parsed["blocking_reason"], "need scope")


if __name__ == "__main__":
    unittest.main()
