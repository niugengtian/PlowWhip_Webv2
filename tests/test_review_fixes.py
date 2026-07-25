import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from plowwhip.execution import (
    _persist_probe_result,
    _provider_log,
    _write_interruption_is_unsafe,
)
from plowwhip.host_bridge import HostJobManager
from plowwhip.lifecycle_state import lifecycle_write_scope
from plowwhip.provider import (
    HostBridgeError,
    model_budget_fact,
    record_model_call,
)
from plowwhip.store import Store
from plowwhip.stream_capture import append_redacted_stream
from plowwhip.verification import CheckerStep, perform_checker_step


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

    def test_bridge_terminal_streams_are_private_redacted_and_complete(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            manager = HostJobManager(
                root_path / "state", (root_path.resolve(),)
            )
            job_id = "b" * 32
            directory = manager._output_directory(job_id)
            directory.mkdir()
            stdout = directory / "stdout.segment-000001.log"
            stderr = directory / "stderr.segment-000001.log"
            append_redacted_stream(
                BytesIO(
                    (
                        "token=abcdefghijklmnop\n"
                        "ghp_abcdefghijklmnopqrstuvwxyz\n"
                        "sk-abcdefghijklmnopqrstuvwxyz\n"
                        + ("x" * 300_000)
                        + '\n{"input_tokens":1,"output_tokens":1}\n'
                    ).encode()
                ),
                stdout,
            )
            append_redacted_stream(
                BytesIO(b"Bearer abcdefghijklmnop\n"),
                stderr,
            )
            record = {
                "job_id": job_id,
                "started_at": time.time(),
                "ended_at": None,
                "isolated_workspace": False,
            }
            before = {
                path: (path.stat().st_ino, path.stat().st_size, path.read_bytes())
                for path in (stdout, stderr)
            }
            manager._finish(record)
            for path in (stdout, stderr):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                if path == stdout:
                    self.assertGreater(path.stat().st_size, 262_144)
                self.assertNotIn("abcdefghijklmnop", path.read_text())
                self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", path.read_text())
                self.assertEqual(
                    (path.stat().st_ino, path.stat().st_size, path.read_bytes()),
                    before[path],
                )
                self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", path.read_text())

    def test_checker_start_rejection_is_classified_as_rejected(self):
        step = CheckerStep(
            "start",
            "project",
            "task",
            "job",
            "codex_cli",
            "/workspace",
            "check",
            None,
            60,
            {},
            {},
        )
        with patch(
            "plowwhip.verification.start_provider_job",
            side_effect=HostBridgeError(
                "rejected", status=400, detail="read access unsupported"
            ),
        ):
            facts = perform_checker_step(step)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["failure_kind"], "rejected")
        self.assertEqual(facts["failure_stage"], "start")

    def test_non_git_snapshot_fingerprints_changes_after_256th_file(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            for index in range(257):
                (root_path / f"{index:04d}.txt").write_text("before")
            manager = HostJobManager(
                root_path / "state", (root_path.resolve(),)
            )
            before = manager.snapshot({"project_path": str(root_path)})["git"]
            (root_path / "0256.txt").write_text("after!")
            after = manager.snapshot({"project_path": str(root_path)})["git"]
            self.assertEqual(before["files"], after["files"])
            self.assertTrue(before["truncated"])
            self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_restart_watchdog_reports_deadline_without_killing(self):
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
            self.assertIsNotNone(result["deadline_reached_at"])
            self.assertEqual(result["failure_class"], "external_interruption")
            self.assertEqual(result["returncode"], 125)
            signal_process.assert_not_called()

    def test_owned_process_deadline_is_fact_not_automatic_kill(self):
        class Process:
            pid = 321
            returncode = 0

            def __init__(self):
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("test-process", timeout)
                return 0

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            manager = HostJobManager(root_path / "state", (root_path,))
            job_id = "d" * 32
            output = manager._output_directory(job_id)
            output.mkdir(parents=True)
            (output / "stdout.segment-000001.log").write_text("")
            (output / "stderr.segment-000001.log").write_text("")
            manager._write(
                {
                    "job_id": job_id,
                    "status": "running",
                    "pid": 321,
                    "process_identity": "stable",
                    "started_at": time.time() - 1,
                    "timeout_seconds": 1,
                    "cancel_requested": False,
                    "isolated_workspace": False,
                    "output_ref": f"{job_id}/",
                }
            )
            process = Process()
            manager._processes[job_id] = process
            with patch(
                "plowwhip.host_bridge._signal_process"
            ) as signal_process:
                manager._wait(job_id, process, 1)
            result = manager._read(job_id)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["returncode"], 0)
            self.assertIsNotNone(result["deadline_reached_at"])
            signal_process.assert_not_called()

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

    def test_provider_returns_model_budget_fact_for_lifecycle_reducer(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE task_sessions(
                id TEXT PRIMARY KEY, task_id TEXT, settings_json TEXT
            );
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, project_id TEXT, outcome TEXT,
                public_status TEXT, phase TEXT, wait_reason TEXT,
                fault_code TEXT, next_action_at REAL, next_action_kind TEXT,
                updated_at REAL, spec_revision INTEGER, role_key TEXT,
                spec_json TEXT
            );
            CREATE TABLE task_events(
                project_id TEXT, task_id TEXT, kind TEXT,
                detail_json TEXT, created_at REAL
            );
            CREATE TABLE model_calls(
                id TEXT PRIMARY KEY, task_id TEXT, task_session_id TEXT,
                session_generation INTEGER, provider_key TEXT, model TEXT,
                usage_kind TEXT, physical_session_id TEXT, input_tokens INTEGER,
                cached_input_tokens INTEGER, output_tokens INTEGER,
                normalized_total INTEGER, created_at REAL
            );
            CREATE TABLE session_generations(
                task_session_id TEXT, generation INTEGER,
                external_session_id TEXT
            );
            CREATE TABLE host_jobs(
                id TEXT PRIMARY KEY, status TEXT, ended_at REAL,
                returncode INTEGER, output_ref TEXT, failure_code TEXT
            );
            CREATE TABLE artifacts(
                id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
                kind TEXT, path TEXT, sha256 TEXT, bytes INTEGER,
                acceptance_id TEXT, revision INTEGER, scope_json TEXT,
                source_task_id TEXT, created_at REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO task_sessions VALUES (?, ?, ?)",
            (
                "s",
                "t",
                json.dumps(
                    {"values": {"max_model_calls": 1, "max_total_tokens": 10}}
                ),
            ),
        )
        connection.execute(
            "INSERT INTO session_generations VALUES ('s', 1, 'physical-1')"
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, project_id, public_status, spec_revision, role_key, spec_json
            ) VALUES ('t', 'p', 'in_progress', 1, 'provider_probe', '{}')
            """
        )
        connection.execute(
            "INSERT INTO host_jobs(id, status) VALUES ('j', 'running')"
        )
        record_model_call(
            connection, "t", "s", 1, "codex_cli", "single", 8, 0, 2
        )
        task = connection.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(task["public_status"], "in_progress")
        self.assertIsNone(task["fault_code"])
        fact = model_budget_fact(connection, "t")
        self.assertEqual(fact["calls"], 1)
        self.assertEqual(fact["normalized_tokens"], 10)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            0,
        )
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)

            class StoreStub:
                data_root = root_path

                def relative_data_path(self, path):
                    return path.relative_to(self.data_root).as_posix()

                def resolve_data_path(self, path):
                    return (self.data_root / path).resolve()

            with lifecycle_write_scope("advance_project"):
                outcome = _persist_probe_result(
                    StoreStub(),
                    connection,
                    connection.execute(
                        "SELECT * FROM tasks WHERE id = 't'"
                    ).fetchone(),
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
            self.assertEqual(tuple(task), ("in_progress", None))
            self.assertEqual(model_budget_fact(connection, "t")["calls"], 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()
