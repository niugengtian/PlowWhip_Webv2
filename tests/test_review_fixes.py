import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from plowwhip.execution import (
    _persist_probe_result,
    _provider_log,
    _write_interruption_is_unsafe,
)
from plowwhip.host_bridge import HostJobManager
from plowwhip.provider import record_model_call
from plowwhip.store import Store


class ReviewFixesTest(unittest.TestCase):
    def test_external_write_interruption_is_fail_closed(self):
        state = {"failure_class": "external_interruption"}
        self.assertTrue(_write_interruption_is_unsafe("write", state))
        self.assertFalse(_write_interruption_is_unsafe("read", state))
        self.assertFalse(
            _write_interruption_is_unsafe("write", {"failure_class": "process"})
        )

    def test_recovery_hold_converges_to_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state = root_path / "state"
            state.mkdir()
            job_id = "a" * 32
            (state / f"{job_id}.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "status": "dispatching",
                        "pid": None,
                        "started_at": time.time() - 60,
                        "timeout_seconds": 10,
                        "recovery_hold_until": time.time() - 1,
                        "isolated_workspace": False,
                    }
                )
            )
            manager = HostJobManager(state, (root_path,))
            result = manager.status(job_id)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["failure_class"], "dispatch_outcome_unknown")

    def test_bridge_terminal_streams_are_private_redacted_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            manager = HostJobManager(root_path / "state", (root_path,))
            job_id = "b" * 32
            directory = manager._output_directory(job_id)
            directory.mkdir()
            stdout = directory / "stdout.segment-000001.log"
            stderr = directory / "stderr.segment-000001.log"
            stdout.write_text(
                "token=abcdefghijklmnop\n"
                + ("x" * 300_000)
                + '\n{"input_tokens":1,"output_tokens":1}\n'
            )
            stderr.write_text("Bearer abcdefghijklmnop\n")
            record = {
                "job_id": job_id,
                "started_at": time.time(),
                "ended_at": None,
                "isolated_workspace": False,
            }
            manager._finish(record)
            for path in (stdout, stderr):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertLessEqual(path.stat().st_size, 262_144)
                self.assertNotIn("abcdefghijklmnop", path.read_text())

    def test_restart_watchdog_enforces_persisted_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state = root_path / "state"
            manager = HostJobManager(state, (root_path,))
            job_id = "c" * 32
            manager._write(
                {
                    "job_id": job_id,
                    "status": "orphan_running",
                    "pid": 123,
                    "process_identity": "stable",
                    "started_at": time.time() - 2,
                    "timeout_seconds": 1,
                    "cancel_requested": False,
                    "isolated_workspace": False,
                }
            )
            with patch(
                "plowwhip.host_bridge._same_process",
                side_effect=[True, False, False],
            ), patch("plowwhip.host_bridge._signal_process") as signal_process:
                manager._watch_orphan(job_id)
            result = manager._read(job_id)
            self.assertEqual(result["failure_class"], "timeout")
            self.assertEqual(result["returncode"], 124)
            signal_process.assert_called()

    def test_provider_persistent_log_is_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            store = type("StoreStub", (), {"data_root": Path(root)})()
            path, body = _provider_log(
                store,
                {"project_id": "p", "id": "t", "role_key": "fullstack"},
                {"session_generation": 1, "sequence": 1},
                {
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": "api_key=abcdefghijklmnop secret-safe-tail",
                        }
                    ]
                },
            )
            self.assertIn(b"[REDACTED]", body)
            self.assertNotIn(b"abcdefghijklmnop", body)
            self.assertTrue(str(path).endswith("sequence-000001.log"))

    def test_task_model_budget_sets_needs_decision(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE task_sessions(id TEXT PRIMARY KEY, settings_json TEXT);
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, project_id TEXT, outcome TEXT,
                public_status TEXT, phase TEXT, wait_reason TEXT,
                fault_code TEXT, next_action_at REAL, next_action_kind TEXT,
                updated_at REAL, spec_revision INTEGER, role_key TEXT
            );
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            CREATE TABLE model_calls(
                id TEXT PRIMARY KEY, task_id TEXT, task_session_id TEXT,
                session_generation INTEGER, provider_key TEXT, model TEXT,
                usage_kind TEXT, input_tokens INTEGER,
                cached_input_tokens INTEGER, output_tokens INTEGER,
                normalized_total INTEGER, created_at REAL
            );
            CREATE TABLE host_jobs(
                id TEXT PRIMARY KEY, status TEXT, ended_at REAL,
                returncode INTEGER, output_ref TEXT, failure_code TEXT
            );
            CREATE TABLE artifacts(
                id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
                kind TEXT, path TEXT, sha256 TEXT, bytes INTEGER,
                acceptance_id TEXT, revision INTEGER, created_at REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO task_sessions VALUES (?, ?)",
            (
                "s",
                json.dumps(
                    {"values": {"max_model_calls": 1, "max_total_tokens": 10}}
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, project_id, public_status, spec_revision, role_key
            ) VALUES ('t', 'p', 'in_progress', 1, 'provider_probe')
            """
        )
        connection.execute(
            "INSERT INTO host_jobs(id, status) VALUES ('j', 'running')"
        )
        record_model_call(
            connection, "t", "s", 1, "codex_cli", "single", 8, 0, 2
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(task["public_status"], "needs_decision")
        self.assertEqual(task["fault_code"], "scope")
        self.assertEqual(
            connection.execute(
                "SELECT kind FROM task_events"
            ).fetchone()["kind"],
            "model_budget_reached",
        )
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)

            class StoreStub:
                data_root = root_path

                def relative_data_path(self, path):
                    return path.relative_to(self.data_root).as_posix()

            outcome = _persist_probe_result(
                StoreStub(),
                connection,
                connection.execute("SELECT * FROM tasks WHERE id = 't'").fetchone(),
                {
                    "id": "j",
                    "task_session_id": "s",
                    "session_generation": 1,
                    "sequence": 1,
                    "purpose": "execute",
                },
                {
                    "provider_key": "codex_cli",
                    "mode": "minimal",
                    "available": False,
                    "detail": "token=abcdefghijklmnop",
                    "model_invoked": True,
                    "returncode": 0,
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "model": "test",
                },
                time.time(),
            )
            task = connection.execute("SELECT * FROM tasks").fetchone()
            self.assertEqual(outcome, "needs_decision")
            self.assertEqual(task["public_status"], "needs_decision")
            self.assertEqual(task["fault_code"], "scope")
            log_path = next(root_path.rglob("sequence-000001.log"))
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("abcdefghijklmnop", log_path.read_text())
        connection.close()

    def test_task_model_budget_uses_real_store_schema(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = Store(root_path / "state.db", root_path / "data")
            store.initialize()
            now = time.time()
            connection = store.connect()
            connection.execute(
                "INSERT INTO projects(id, display_name, created_at) VALUES ('p', 'p', ?)",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, idempotency_key, created_at
                ) VALUES ('m', 'p', 'owner', 'budget', 'budget', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at
                ) VALUES ('g', 'p', 'm', 'budget', '{}', '[]', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, goal_id, spec_json, acceptance_json,
                    public_status, phase, created_at, updated_at
                ) VALUES ('t', 'p', 'g', '{}', '[]', 'in_progress', 'execute', ?, ?)
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO workers(id, project_id, role_key, created_at)
                VALUES ('w', 'p', 'fullstack', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO task_sessions(
                    id, task_id, worker_id, role_key, role_snapshot_json,
                    settings_json, created_at
                ) VALUES ('s', 't', 'w', 'fullstack', '{}', ?, ?)
                """,
                (
                    json.dumps(
                        {"values": {"max_model_calls": 1, "max_total_tokens": 10}}
                    ),
                    now,
                ),
            )
            record_model_call(
                connection, "t", "s", 1, "codex_cli", "single", 8, 0, 2
            )
            task = connection.execute(
                "SELECT public_status, fault_code FROM tasks WHERE id = 't'"
            ).fetchone()
            self.assertEqual(tuple(task), ("needs_decision", "scope"))
            connection.close()


if __name__ == "__main__":
    unittest.main()
