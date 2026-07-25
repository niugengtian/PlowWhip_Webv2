from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from plowwhip.io_phase import derive_io_phase
from plowwhip.soft_timeout import try_soft_timeout_extension
from plowwhip.timeout_policy import (
    MAX_SOFT_TIMEOUTS,
    classify_timeout,
    clamp_timeout_seconds,
    default_timeout_seconds,
    effective_increment_since,
    next_soft_timeout_seconds,
    soft_extension_requires_increment,
)


class TimeoutPolicyTest(unittest.TestCase):
    def test_classify_awaiting_model_is_soft(self):
        self.assertEqual(
            classify_timeout(
                io_phase="awaiting_model",
                last_io_at=time.time(),
                now=time.time(),
            ),
            "soft",
        )

    def test_classify_stale_idle_is_hard(self):
        self.assertEqual(
            classify_timeout(
                io_phase="idle",
                last_io_at=time.time() - 1_000,
                now=time.time(),
            ),
            "hard",
        )

    def test_classify_missing_last_io_is_soft_once(self):
        self.assertEqual(
            classify_timeout(io_phase="idle", last_io_at=None, now=time.time()),
            "soft",
        )

    def test_default_and_double_budgets(self):
        self.assertEqual(default_timeout_seconds("medium"), 1_800)
        self.assertEqual(next_soft_timeout_seconds(600, "simple"), 1_200)
        self.assertEqual(clamp_timeout_seconds(30, "simple"), 120)

    def test_derive_io_phase_from_events(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "worker.progress", "turn": 1, "ts": 1.0}),
                json.dumps({"type": "model.request_started", "ts": 2.0}),
            ]
        )
        derived = derive_io_phase(stdout)
        self.assertEqual(derived["io_phase"], "awaiting_model")
        self.assertEqual(derived["last_io_at"], 2.0)

    def test_soft_timeout_extends_and_caps(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, project_id TEXT, deadline_at REAL,
                public_status TEXT, phase TEXT, wait_reason TEXT,
                fault_code TEXT, next_action_at REAL, next_action_kind TEXT,
                updated_at REAL, spec_revision INTEGER DEFAULT 1
            );
            CREATE TABLE host_jobs(
                id TEXT PRIMARY KEY, task_id TEXT, status TEXT, dispatch_json TEXT
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, project_id TEXT, role TEXT, content TEXT,
                action_json TEXT, idempotency_key TEXT UNIQUE,
                created_at REAL, processed_at REAL
            );
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            """
        )
        now = time.time()
        connection.execute(
            """
            INSERT INTO tasks VALUES (
                'task1', 'proj', ?, 'in_progress', 'execute_wait', NULL,
                NULL, ?, 'poll', ?, 1
            )
            """,
            (now - 1, now + 1, now),
        )
        connection.execute(
            """
            INSERT INTO host_jobs VALUES (
                'job1', 'task1', 'running', ?
            )
            """,
            (json.dumps({"timeout_seconds": 600, "soft_timeout_count": 0}),),
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        job = connection.execute("SELECT * FROM host_jobs").fetchone()
        state = {
            "status": "running",
            "deadline_reached_at": now,
            "timeout_seconds": 600,
            "io_phase": "awaiting_model",
            "last_io_at": now,
        }
        from plowwhip.lifecycle_state import lifecycle_write_scope

        with lifecycle_write_scope("advance_project"), patch(
            "plowwhip.soft_timeout.extend_provider_job_timeout",
            return_value={"timeout_seconds": 1200},
        ) as extend:
            outcome = try_soft_timeout_extension(
                connection, task, job, state, now=now, repo_root=Path(tempfile.mkdtemp())
            )
        self.assertEqual(outcome, "extended")
        extend.assert_called_once()
        updated = connection.execute("SELECT * FROM tasks").fetchone()
        self.assertGreater(float(updated["deadline_at"]), now)
        self.assertIn("soft", str(updated["wait_reason"]))
        dispatch = json.loads(
            connection.execute("SELECT dispatch_json FROM host_jobs").fetchone()[0]
        )
        self.assertEqual(dispatch["soft_timeout_count"], 1)
        self.assertEqual(dispatch["timeout_seconds"], 1200)

        # Cap after MAX_SOFT_TIMEOUTS
        connection.execute(
            "UPDATE host_jobs SET dispatch_json = ?",
            (
                json.dumps(
                    {
                        "timeout_seconds": 4800,
                        "soft_timeout_count": MAX_SOFT_TIMEOUTS,
                    }
                ),
            ),
        )
        job = connection.execute("SELECT * FROM host_jobs").fetchone()
        task = connection.execute("SELECT * FROM tasks").fetchone()
        with lifecycle_write_scope("advance_project"), patch(
            "plowwhip.soft_timeout.extend_provider_job_timeout"
        ):
            capped = try_soft_timeout_extension(
                connection,
                task,
                job,
                {**state, "timeout_seconds": 4800},
                now=now,
                repo_root=Path(tempfile.mkdtemp()),
            )
        self.assertEqual(capped, "soft_cap_reached")
    def test_hard_idle_does_not_extend(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, project_id TEXT, deadline_at REAL,
                public_status TEXT, phase TEXT, wait_reason TEXT,
                fault_code TEXT, next_action_at REAL, next_action_kind TEXT,
                updated_at REAL, spec_revision INTEGER DEFAULT 1
            );
            CREATE TABLE host_jobs(
                id TEXT PRIMARY KEY, task_id TEXT, status TEXT, dispatch_json TEXT
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, project_id TEXT, role TEXT, content TEXT,
                action_json TEXT, idempotency_key TEXT UNIQUE,
                created_at REAL, processed_at REAL
            );
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            """
        )
        now = time.time()
        connection.execute(
            """
            INSERT INTO tasks VALUES (
                'task2', 'proj', ?, 'in_progress', 'execute_wait', NULL,
                NULL, ?, 'poll', ?, 1
            )
            """,
            (now - 1, now + 1, now),
        )
        connection.execute(
            """
            INSERT INTO host_jobs VALUES (
                'job2', 'task2', 'running', ?
            )
            """,
            (json.dumps({"timeout_seconds": 600}),),
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        job = connection.execute("SELECT * FROM host_jobs").fetchone()
        with patch("plowwhip.soft_timeout.extend_provider_job_timeout") as extend:
            outcome = try_soft_timeout_extension(
                connection,
                task,
                job,
                {
                    "status": "running",
                    "deadline_reached_at": now,
                    "timeout_seconds": 600,
                    "io_phase": "idle",
                    "last_io_at": now - 1_000,
                },
                now=now,
                repo_root=Path(tempfile.mkdtemp()),
            )
        self.assertEqual(outcome, "hard_idle")
        extend.assert_not_called()

    def test_t14_second_soft_requires_effective_increment(self):
        self.assertFalse(soft_extension_requires_increment(0))
        self.assertTrue(soft_extension_requires_increment(1))
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, project_id TEXT, deadline_at REAL,
                public_status TEXT, phase TEXT, wait_reason TEXT,
                fault_code TEXT, next_action_at REAL, next_action_kind TEXT,
                updated_at REAL, spec_revision INTEGER DEFAULT 1
            );
            CREATE TABLE host_jobs(
                id TEXT PRIMARY KEY, task_id TEXT, status TEXT, dispatch_json TEXT
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, project_id TEXT, role TEXT, content TEXT,
                action_json TEXT, idempotency_key TEXT UNIQUE,
                created_at REAL, processed_at REAL
            );
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            """
        )
        now = time.time()
        connection.execute(
            """
            INSERT INTO tasks VALUES (
                'task3', 'proj', ?, 'in_progress', 'execute_wait', NULL,
                NULL, ?, 'poll', ?, 1
            )
            """,
            (now - 1, now + 1, now),
        )
        connection.execute(
            """
            INSERT INTO host_jobs VALUES (
                'job3', 'task3', 'running', ?
            )
            """,
            (
                json.dumps(
                    {
                        "timeout_seconds": 1200,
                        "soft_timeout_count": 1,
                        "last_soft_timeout_at": now - 60,
                    }
                ),
            ),
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        job = connection.execute("SELECT * FROM host_jobs").fetchone()
        state = {
            "status": "running",
            "deadline_reached_at": now,
            "timeout_seconds": 1200,
            "io_phase": "awaiting_model",
            "last_io_at": now,
        }
        from plowwhip.lifecycle_state import lifecycle_write_scope

        with lifecycle_write_scope("advance_project"), patch(
            "plowwhip.soft_timeout.extend_provider_job_timeout"
        ) as extend:
            stalled = try_soft_timeout_extension(
                connection,
                task,
                job,
                state,
                now=now,
                repo_root=Path(tempfile.mkdtemp()),
            )
        self.assertEqual(stalled, "stall_no_increment")
        extend.assert_not_called()

        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES ('proj', 'task3', 'artifact_registered', '{}', ?)
            """,
            (now - 10,),
        )
        self.assertTrue(
            effective_increment_since(
                connection, "task3", since=now - 60, dispatch={}
            )
        )
        job = connection.execute("SELECT * FROM host_jobs").fetchone()
        with lifecycle_write_scope("advance_project"), patch(
            "plowwhip.soft_timeout.extend_provider_job_timeout",
            return_value={"timeout_seconds": 2400},
        ) as extend:
            extended = try_soft_timeout_extension(
                connection,
                task,
                job,
                state,
                now=now,
                repo_root=Path(tempfile.mkdtemp()),
            )
        self.assertEqual(extended, "extended")
        extend.assert_called_once()
        event = connection.execute(
            """
            SELECT detail_json FROM task_events
            WHERE kind = 'task_timeout_soft_extended'
            ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
        detail = json.loads(event["detail_json"])
        self.assertTrue(detail.get("effective_increment"))


if __name__ == "__main__":
    unittest.main()
