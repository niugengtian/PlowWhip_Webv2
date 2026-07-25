"""LIVE-DS-23: isomorphic 继续 after missing Artifact + no-progress."""

from __future__ import annotations

import sqlite3
import unittest

from plowwhip.continue_policy import (
    reject_isomorphic_empty_delivery_continue,
    wait_indicates_missing_delivery,
)


class ContinuePolicyTest(unittest.TestCase):
    def test_wait_markers(self):
        self.assertTrue(
            wait_indicates_missing_delivery(
                "declared workspace Artifact is missing from the complete "
                "post-execution snapshot"
            )
        )
        self.assertTrue(
            wait_indicates_missing_delivery("formal result manifest is empty")
        )
        self.assertFalse(wait_indicates_missing_delivery("Checker exhausted"))

    def test_reject_when_missing_and_recent_no_progress(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks(id TEXT, wait_reason TEXT);
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            CREATE TABLE host_jobs(
                id TEXT, task_id TEXT, sequence INTEGER,
                failure_code TEXT, dispatch_json TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('task-1', ?)",
            (
                "declared workspace Artifact is missing from the complete "
                "post-execution snapshot",
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events
            VALUES ('p','task-1','executed',?,1)
            """,
            (
                '{"failure_class":"internal_tool_no_progress","returncode":1}',
            ),
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        reason = reject_isomorphic_empty_delivery_continue(connection, task)
        self.assertIsNotNone(reason)
        self.assertIn("isomorphic", reason or "")

    def test_allow_when_missing_without_no_progress(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks(id TEXT, wait_reason TEXT);
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            CREATE TABLE host_jobs(
                id TEXT, task_id TEXT, sequence INTEGER,
                failure_code TEXT, dispatch_json TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('task-2', 'formal result manifest is empty')"
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        self.assertIsNone(
            reject_isomorphic_empty_delivery_continue(connection, task)
        )


if __name__ == "__main__":
    unittest.main()
