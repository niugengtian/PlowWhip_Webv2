"""MECH-07: same-problem recovery hard-caps at 5; further continue is blocked."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from plowwhip.execution import _fallback_provider_generation
from plowwhip.intake import PROJECT_SETTING_LIMITS, canonical_json
from plowwhip.lifecycle import _reject_continue_for_recovery_cap
from plowwhip.lifecycle_state import lifecycle_write_scope
from plowwhip.recovery_policy import (
    MAX_SAME_PROBLEM_RETRIES,
    clamp_retry_count,
    count_recovery_attempts,
    failure_signature,
    recovery_cap_reached,
    recovery_cap_wait_reason,
)
from plowwhip.store import Store


class RecoveryCapUnitTest(unittest.TestCase):
    def test_setting_limit_and_clamp(self):
        self.assertEqual(PROJECT_SETTING_LIMITS["retry_count"], (0, 5))
        self.assertEqual(clamp_retry_count(10), 5)
        self.assertEqual(clamp_retry_count(3), 3)
        self.assertEqual(MAX_SAME_PROBLEM_RETRIES, 5)
        self.assertIn("blocking mechanism gap", recovery_cap_wait_reason(5))


class RecoveryCapStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state.db", self.root / "data")
        self.store.initialize()
        self.project_id = "cap-project"
        self.task_id = "cap-task"
        now = time.time()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, created_at)
                VALUES (?, 'cap', ?)
                """,
                (self.project_id, now),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, idempotency_key, created_at
                ) VALUES ('cap-message', ?, 'owner', 'x', 'cap-message', ?)
                """,
                (self.project_id, now),
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at
                ) VALUES ('cap-goal', ?, 'cap-message', 'cap goal', '{}', '[]', ?)
                """,
                (self.project_id, now),
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, goal_id, spec_json, acceptance_json,
                    public_status, phase, role_key, checker_role_key,
                    retry_count, created_at, updated_at
                ) VALUES (?, ?, 'cap-goal', ?, '[]', 'needs_decision',
                          'provider_recovery', 'fullstack', 'independent_checker',
                          0, ?, ?)
                """,
                (
                    self.task_id,
                    self.project_id,
                    canonical_json(
                        {
                            "kind": "provider_task",
                            "instruction": "write report",
                            "mode": "standard",
                        }
                    ),
                    now,
                    now,
                ),
            )
            worker_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO workers(id, project_id, role_key, created_at)
                VALUES (?, ?, 'fullstack', ?)
                """,
                (worker_id, self.project_id, now),
            )
            self.session_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO task_sessions(
                    id, task_id, worker_id, role_key, role_snapshot_json,
                    settings_json, created_at
                ) VALUES (?, ?, ?, 'fullstack', '{}', ?, ?)
                """,
                (
                    self.session_id,
                    self.task_id,
                    worker_id,
                    canonical_json(
                        {
                            "values": {
                                "retry_count": 5,
                                "provider_order": {"fullstack": ["deepseek"]},
                            }
                        }
                    ),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_generations(
                    id, task_session_id, generation, provider_key, status,
                    created_at
                ) VALUES (?, ?, 1, 'deepseek', 'active', ?)
                """,
                (uuid4().hex, self.session_id, now),
            )
            for _ in range(MAX_SAME_PROBLEM_RETRIES):
                connection.execute(
                    """
                    INSERT INTO task_events(
                        project_id, task_id, kind, detail_json, created_at
                    ) VALUES (?, ?, 'provider_retry', ?, ?)
                    """,
                    (
                        self.project_id,
                        self.task_id,
                        canonical_json(
                            {
                                "failure_signature": failure_signature(
                                    1, "provider_recovery", "provider_recovery"
                                )
                            }
                        ),
                        now,
                    ),
                )

    def tearDown(self):
        self.tmp.cleanup()

    def test_count_and_cap(self):
        connection = self.store.connect()
        try:
            self.assertEqual(
                count_recovery_attempts(connection, self.task_id),
                MAX_SAME_PROBLEM_RETRIES,
            )
            self.assertTrue(recovery_cap_reached(connection, self.task_id))
        finally:
            connection.close()

    def test_new_spec_revision_opens_a_new_failure_bucket(self):
        connection = self.store.connect()
        try:
            before = failure_signature(1, "provider_recovery", "provider_recovery")
            self.assertEqual(len(before), 24)
            connection.execute(
                "UPDATE tasks SET spec_revision = 2 WHERE id = ?", (self.task_id,)
            )
            self.assertEqual(count_recovery_attempts(connection, self.task_id), 0)
            self.assertFalse(recovery_cap_reached(connection, self.task_id))
        finally:
            connection.close()

    def test_fallback_blocked_at_cap(self):
        connection = self.store.connect()
        try:
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (self.task_id,)
            ).fetchone()
            job = {
                "id": "job-cap",
                "task_session_id": self.session_id,
                "session_generation": 1,
            }
            with lifecycle_write_scope("advance_project"):
                next_provider = _fallback_provider_generation(
                    self.store,
                    connection,
                    task,
                    job,
                    "deepseek",
                    time.time(),
                    retry_same_provider=True,
                )
            self.assertIsNone(next_provider)
            kinds = [
                row["kind"]
                for row in connection.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? ORDER BY rowid",
                    (self.task_id,),
                )
            ]
            self.assertIn("recovery_cap_reached", kinds)
            wait = connection.execute(
                "SELECT wait_reason, fault_code FROM tasks WHERE id = ?",
                (self.task_id,),
            ).fetchone()
            self.assertIn("blocking mechanism gap", wait["wait_reason"] or "")
            self.assertEqual(wait["fault_code"], "scope")
        finally:
            connection.close()

    def test_continue_rejected_at_cap(self):
        connection = self.store.connect()
        try:
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (self.task_id,)
            ).fetchone()
            blocked = _reject_continue_for_recovery_cap(
                connection, task, {"id": "msg-cap"}, time.time()
            )
            self.assertIsNotNone(blocked)
            event, detail = blocked
            self.assertEqual(event, "decision_rejected")
            self.assertEqual(detail["reason"], "recovery_cap_reached")
            self.assertEqual(detail["attempts"], MAX_SAME_PROBLEM_RETRIES)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
