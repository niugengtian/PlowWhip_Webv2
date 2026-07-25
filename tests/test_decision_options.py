from __future__ import annotations

import unittest
import json
import tempfile
import time
from pathlib import Path

from plowwhip.decision_policy import build_decision_options
from plowwhip.intake import canonical_json, submit_action
from plowwhip.lifecycle import _apply_action
from plowwhip.lifecycle_state import lifecycle_write_scope
from plowwhip.store import Store


class DecisionOptionsTest(unittest.TestCase):
    def test_schema_is_bounded_and_complete(self):
        options = build_decision_options(
            scenario="plan", reason="plan requires a choice", basis="classification"
        )
        self.assertTrue(2 <= len(options) <= 7)
        for option in options:
            self.assertEqual(
                set(option),
                {"option_id", "title", "reason", "basis", "pros", "cons", "effect"},
            )

    def test_recovery_cap_has_no_isomorphic_continue(self):
        options = build_decision_options(
            scenario="recovery_cap", reason="mechanism gap"
        )
        self.assertNotIn("continue", {item["option_id"] for item in options})

    def test_select_option_queues_and_applies_cancel(self):
        task_id = "a" * 32
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "state.db", root / "data")
            store.initialize()
            now = time.time()
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO projects(id, display_name, created_at) VALUES ('p', 'p', ?)",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO messages(id, project_id, role, content, idempotency_key, created_at)
                    VALUES ('source', 'p', 'owner', 'goal', 'source', ?)""",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO goals(id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at)
                    VALUES ('g', 'p', 'source', 'goal', '{}', '[]', ?)""",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO tasks(id, project_id, goal_id, spec_json, acceptance_json,
                    public_status, phase, created_at, updated_at)
                    VALUES (?, 'p', 'g', ?, '[]', 'needs_decision', 'plan', ?, ?)""",
                    (task_id, canonical_json({"kind": "provider_task", "instruction": "work"}), now, now),
                )
                connection.execute(
                    """INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
                    VALUES ('p', ?, 'decision_options', ?, ?)""",
                    (
                        task_id,
                        canonical_json(
                            {
                                "options": build_decision_options(
                                    scenario="plan", reason="choose"
                                )
                            }
                        ),
                        now,
                    ),
                )
            message_id = submit_action(
                store, "p", task_id, "select_option", "", "choose-cancel",
                option_id="cancel_task",
            )
            with store.transaction() as connection:
                message = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                self.assertEqual(json.loads(message["action_json"])["option_id"], "cancel_task")
                with lifecycle_write_scope("advance_project"):
                    self.assertEqual(_apply_action(connection, message), "cancel")
                task = connection.execute(
                    "SELECT outcome FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                self.assertEqual(task["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
