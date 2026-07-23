import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plowwhip.app import make_server
from plowwhip.butler import conversation
from plowwhip.cronner import run as run_cronner, run_until_idle, tick
from plowwhip.intake import (
    archive_project,
    create_project,
    normalize_instruction,
    submit_action,
    submit_message,
)
from plowwhip.lifecycle import LeaseLost, advance_project
from plowwhip.monitor import (
    monitor_snapshot,
    projects_snapshot,
    settings_library_snapshot,
    snapshot,
    token_snapshot,
)
from plowwhip.planner import normalize_plan
from plowwhip.provider import provider_adapter, provider_facts, record_model_call
from plowwhip.store import Store


class VerticalSliceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "state.db"
        self.data = self.root / "data"
        self.store = Store(self.db, self.data)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_message_to_verified_done(self):
        first = submit_message(
            self.store, "project-a", "写入 result.txt: 闭环完成", "request-1"
        )
        duplicate = submit_message(
            self.store, "project-a", "ignored duplicate", "request-1"
        )
        self.assertEqual(first, duplicate)

        with self.assertRaises(LeaseLost):
            advance_project(self.store, "project-a", "not-a-lease", 0)

        actions = run_until_idle(self.store)
        self.assertEqual([item["action"] for item in actions], ["intake", "execute", "verify"])

        before = self._row_counts()
        view = snapshot(self.db, self.data, "project-a")
        after = self._row_counts()
        self.assertEqual(before, after, "Monitor must not mutate canonical state")
        self.assertEqual(view["task"]["public_status"], "done")
        self.assertEqual(view["task"]["outcome"], "done")
        self.assertEqual([item["kind"] for item in view["artifacts"]], ["artifact", "evidence"])
        evidence = json.loads(Path(view["artifacts"][1]["path"]).read_text())
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["acceptance_id"], "artifact_content_sha256")
        self.assertEqual(
            view["last_output"],
            [json.dumps(evidence, ensure_ascii=False, sort_keys=True)],
        )

        connection = self.store.connect()
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM host_jobs").fetchone()[0], 2)
            sessions = connection.execute(
                "SELECT role_key, role_snapshot_json, settings_json FROM task_sessions ORDER BY role_key"
            ).fetchall()
            generations = connection.execute(
                "SELECT generation, status, handoff_ref FROM session_generations"
            ).fetchall()
            jobs = connection.execute(
                "SELECT task_session_id, session_generation, purpose FROM host_jobs ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row["role_key"] for row in sessions], ["deterministic", "deterministic_checker"])
        self.assertTrue(all(json.loads(row["settings_json"])["sources"] for row in sessions))
        role_snapshot = json.loads(sessions[0]["settings_json"])
        self.assertEqual(role_snapshot["sources"]["max_runtime_seconds"], "v1_default")
        self.assertEqual(len(json.loads(sessions[0]["role_snapshot_json"])["library"]), 3)
        self.assertEqual([(row["generation"], row["status"]) for row in generations], [(1, "archived"), (1, "archived")])
        self.assertTrue(all(row["handoff_ref"] for row in generations))
        self.assertEqual([row["purpose"] for row in jobs], ["execute", "check"])
        self.assertEqual(len({row["task_session_id"] for row in jobs}), 2)
        self.assertEqual({row["session_generation"] for row in jobs}, {1})
        handoffs = list((self.data / "projects" / "project-a" / "tasks" / view["task"]["id"] / "handoffs").glob("*/current.json"))
        self.assertEqual(len(handoffs), 2)

    def test_default_settings_upgrade_without_overwriting_project_policy(self):
        old_provider_order = {
            "deterministic": ["local"],
            "deterministic_checker": ["local"],
        }
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE settings SET value_json = ?
                WHERE scope = 'global' AND setting_key = 'provider_order'
                """,
                (json.dumps(old_provider_order),),
            )
            connection.execute(
                """
                UPDATE settings SET value_json = '7', source = 'owner'
                WHERE scope = 'global' AND setting_key = 'retry_count'
                """
            )

        self.store.initialize()

        connection = self.store.connect()
        try:
            rows = {
                row["setting_key"]: row
                for row in connection.execute(
                    """
                    SELECT setting_key, value_json, source FROM settings
                    WHERE scope = 'global'
                      AND setting_key IN ('provider_order', 'retry_count')
                    """
                )
            }
        finally:
            connection.close()
        provider_order = json.loads(rows["provider_order"]["value_json"])
        self.assertEqual(
            provider_order["provider_probe"],
            ["codex_cli", "cursor_cli", "deepseek", "kimi"],
        )
        self.assertEqual(rows["provider_order"]["source"], "v1_default")
        self.assertEqual(json.loads(rows["retry_count"]["value_json"]), 7)
        self.assertEqual(rows["retry_count"]["source"], "owner")

    def test_owner_wake_is_queued_but_cronner_remains_the_only_driver(self):
        submit_message(self.store, "wake", "写入 wake.txt: done", "wake-message")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        waiting = snapshot(self.db, self.data, "wake")
        submit_action(
            self.store,
            "wake",
            waiting["task"]["id"],
            "wake",
            "",
            "wake-action",
        )
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["wake", "execute", "verify"],
        )
        done = snapshot(self.db, self.data, "wake")
        self.assertEqual(done["task"]["public_status"], "done")
        self.assertIn("wake_requested", {item["kind"] for item in done["events"]})

    def test_failed_evidence_converges_to_needs_decision(self):
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(id, created_at) VALUES ('project-b', ?)",
                (time.time(),),
            )
            connection.execute(
                """
                INSERT INTO settings(
                    id, scope, project_id, setting_key, value_json, source, updated_at
                ) VALUES ('project-b:retry_count', 'project', 'project-b',
                          'retry_count', '0', 'test_policy', ?)
                """,
                (time.time(),),
            )
        submit_message(self.store, "project-b", "write result.txt: expected", "request-2")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        connection = self.store.connect()
        try:
            frozen = json.loads(
                connection.execute(
                    """
                    SELECT settings_json FROM task_sessions
                    WHERE role_key = 'deterministic'
                    """
                ).fetchone()["settings_json"]
            )
        finally:
            connection.close()
        self.assertEqual(frozen["values"]["retry_count"], 0)
        self.assertEqual(
            frozen["sources"]["retry_count"], "project:project-b:test_policy"
        )
        self.assertEqual(tick(self.store)[0]["action"], "execute")

        view = snapshot(self.db, self.data, "project-b")
        Path(view["artifacts"][0]["path"]).write_text("tampered")
        result = tick(self.store)[0]
        self.assertEqual(result["status"], "needs_decision")

        view = snapshot(self.db, self.data, "project-b")
        self.assertEqual(view["task"]["public_status"], "needs_decision")
        self.assertEqual(view["task"]["fault_code"], "verification")
        evidence = json.loads(Path(view["artifacts"][1]["path"]).read_text())
        self.assertFalse(evidence["passed"])
        self.assertEqual(tick(self.store), [])

        submit_action(
            self.store,
            "project-b",
            view["task"]["id"],
            "provide_decision",
            "write result.txt: revised",
            "request-2-decision",
        )
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["provide_decision", "execute", "verify"],
        )
        revised = snapshot(self.db, self.data, "project-b")
        self.assertEqual(revised["task"]["public_status"], "done")
        self.assertEqual(revised["task"]["spec_revision"], 2)
        self.assertEqual({item["revision"] for item in revised["artifacts"]}, {1, 2})
        self.assertEqual(len({item["path"] for item in revised["artifacts"]}), 4)

    def test_tampered_output_is_repaired_before_owner_is_disturbed(self):
        submit_message(self.store, "repair", "write result.txt: expected", "repair-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "execute")
        view = snapshot(self.db, self.data, "repair")
        Path(view["artifacts"][0]["path"]).write_text("tampered")
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["verify", "repair", "verify"],
        )
        done = snapshot(self.db, self.data, "repair")
        self.assertEqual(done["task"]["outcome"], "done")
        self.assertEqual(done["task"]["retry_count"], 1)
        self.assertEqual(
            [event["kind"] for event in reversed(done["events"])],
            ["task_created", "executed", "verified", "repaired", "verified"],
        )

    def test_unrecognized_instruction_requires_decision_without_execution(self):
        submit_message(self.store, "project-c", "please decide for me", "request-3")
        actions = run_until_idle(self.store)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "needs_decision")
        view = snapshot(self.db, self.data, "project-c")
        self.assertEqual(view["task"]["phase"], "intake")
        self.assertEqual(view["task"]["fault_code"], "scope")
        self.assertEqual(view["artifacts"], [])
        connection = self.store.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_sessions").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_restart_recovers_queue_in_strict_project_order(self):
        submit_message(self.store, "restart", "write first.txt: first", "restart-1")
        submit_message(self.store, "restart", "write second.txt: second", "restart-2")
        self.assertEqual(tick(self.store)[0]["action"], "intake")

        restarted = Store(self.db, self.data)
        self.assertEqual(
            [item["action"] for item in run_until_idle(restarted)],
            ["execute", "verify", "intake", "execute", "verify"],
        )
        connection = restarted.connect()
        try:
            rows = connection.execute(
                "SELECT public_status, outcome FROM tasks ORDER BY created_at, rowid"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([tuple(row) for row in rows], [("done", "done"), ("done", "done")])

    def test_cancel_rerun_and_complete_schema(self):
        submit_message(self.store, "cancel", "write result.txt: first", "cancel-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "execute")
        task = snapshot(self.db, self.data, "cancel")["task"]

        submit_action(self.store, "cancel", task["id"], "cancel", "", "cancel-2")
        result = tick(self.store)[0]
        self.assertEqual((result["action"], result["status"]), ("cancel", "cancelled"))
        cancelled = snapshot(self.db, self.data, "cancel")
        self.assertEqual(cancelled["task"]["outcome"], "cancelled")
        self.assertEqual(tick(self.store), [])
        connection = self.store.connect()
        try:
            self.assertEqual(
                {tuple(row) for row in connection.execute("SELECT generation, status FROM session_generations")},
                {(1, "archived")},
            )
        finally:
            connection.close()

        submit_action(self.store, "cancel", task["id"], "rerun", "", "cancel-3")
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["rerun", "execute", "verify"],
        )
        rerun = snapshot(self.db, self.data, "cancel")
        self.assertEqual(rerun["task"]["id"], task["id"])
        self.assertEqual(rerun["task"]["outcome"], "done")
        self.assertEqual(len({item["path"] for item in rerun["artifacts"]}), 3)

        connection = self.store.connect()
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            generations = connection.execute(
                "SELECT generation, status FROM session_generations ORDER BY task_session_id, generation"
            ).fetchall()
            jobs = connection.execute(
                "SELECT sequence, session_generation, purpose FROM host_jobs ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            tables,
            {
                "projects",
                "messages",
                "goals",
                "plans",
                "tasks",
                "task_dependencies",
                "workers",
                "task_sessions",
                "session_generations",
                "host_jobs",
                "artifacts",
                "task_events",
                "model_calls",
                "library_items",
                "settings",
            },
        )
        self.assertEqual(
            [(row["generation"], row["status"]) for row in generations],
            [(1, "archived"), (2, "archived"), (1, "archived"), (2, "archived")],
        )
        self.assertEqual(
            [tuple(row) for row in jobs],
            [(1, 1, "execute"), (2, 2, "execute"), (3, 2, "check")],
        )

    def test_versioned_plan_runs_serial_dag(self):
        submit_message(self.store, "plan", "build two files", "plan-1")
        run_until_idle(self.store)
        placeholder = snapshot(self.db, self.data, "plan")["task"]
        plan = {
            "summary": "two deterministic steps",
            "alternatives": [
                {
                    "name": "serial",
                    "scope": "two files",
                    "cost": "low",
                    "risk": "low",
                    "reversible": True,
                    "acceptance": "two hashes",
                },
                {
                    "name": "manual",
                    "scope": "two files",
                    "cost": "high",
                    "risk": "low",
                    "reversible": True,
                    "acceptance": "manual review",
                },
            ],
            "selected": 0,
            "tasks": [
                {"key": "first", "instruction": "write first.txt: first"},
                {
                    "key": "second",
                    "instruction": "write second.txt: second",
                    "depends_on": ["first"],
                    "sprint": 2,
                    "settings": {
                        "deterministic": {"max_runtime_seconds": 30},
                        "deterministic_checker": {"monitor_tail_lines": 10},
                    },
                },
            ],
        }
        submit_action(
            self.store,
            "plan",
            placeholder["id"],
            "provide_plan",
            "",
            "plan-2",
            plan,
        )
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["provide_plan", "execute", "verify", "ready", "execute", "verify"],
        )
        connection = self.store.connect()
        try:
            tasks = connection.execute(
                "SELECT id, outcome, sprint FROM tasks WHERE project_id = 'plan' ORDER BY rowid"
            ).fetchall()
            selected = connection.execute(
                "SELECT revision FROM plans WHERE selected = 1"
            ).fetchone()["revision"]
            dependencies = connection.execute(
                "SELECT COUNT(*) AS count FROM task_dependencies"
            ).fetchone()["count"]
            session_count = connection.execute(
                "SELECT COUNT(*) AS count FROM task_sessions"
            ).fetchone()["count"]
            second_settings = [
                json.loads(row["settings_json"])
                for row in connection.execute(
                    """
                    SELECT settings_json FROM task_sessions
                    WHERE task_id = ? ORDER BY role_key
                    """,
                    (tasks[1]["id"],),
                )
            ]
        finally:
            connection.close()
        self.assertEqual(tasks[0]["id"], placeholder["id"])
        self.assertEqual([(row["outcome"], row["sprint"]) for row in tasks], [("done", 1), ("done", 2)])
        self.assertEqual((selected, dependencies), (2, 1))
        self.assertEqual(session_count, 4)
        self.assertEqual(second_settings[0]["values"]["max_runtime_seconds"], 30)
        self.assertEqual(second_settings[0]["sources"]["max_runtime_seconds"], "task_role")
        self.assertEqual(second_settings[1]["values"]["monitor_tail_lines"], 10)

        submit_message(self.store, "blocked", "build two files", "blocked-1")
        blocked_placeholder = run_until_idle(self.store)[0]
        self.assertEqual(blocked_placeholder["status"], "needs_decision")
        blocked_task = snapshot(self.db, self.data, "blocked")["task"]
        submit_action(
            self.store,
            "blocked",
            blocked_task["id"],
            "provide_plan",
            "",
            "blocked-2",
            plan,
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_plan")
        self.assertEqual(snapshot(self.db, self.data, "blocked")["task"]["id"], blocked_task["id"])
        submit_action(
            self.store, "blocked", blocked_task["id"], "cancel", "", "blocked-3"
        )
        self.assertEqual(tick(self.store)[0]["action"], "cancel")
        self.assertEqual(tick(self.store)[0]["action"], "dependency_blocked")
        blocked = snapshot(self.db, self.data, "blocked")["task"]
        self.assertNotEqual(blocked["id"], blocked_task["id"])
        self.assertEqual(blocked["public_status"], "needs_decision")

        cyclic = {**plan, "tasks": [
            {"key": "a", "instruction": "write a.txt: a", "depends_on": ["b"]},
            {"key": "b", "instruction": "write b.txt: b", "depends_on": ["a"]},
        ]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            normalize_plan(cyclic)
        external = {**plan, "tasks": [
            {"key": "a", "instruction": "write a.txt: a", "settings": {"deterministic": {"provider_order": ["codex_cli"]}}},
            {"key": "b", "instruction": "write b.txt: b", "depends_on": ["a"]},
        ]}
        with self.assertRaisesRegex(ValueError, "external Provider"):
            normalize_plan(external)

    def test_provider_facts_and_token_normalization_are_fail_closed(self):
        submit_message(self.store, "usage", "write result.txt: usage", "usage-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        connection = self.store.connect()
        try:
            row = connection.execute(
                """
                SELECT task.id AS task_id, session.id AS session_id
                FROM tasks task JOIN task_sessions session ON session.task_id = task.id
                WHERE task.project_id = 'usage' AND session.role_key = 'deterministic'
                """
            ).fetchone()
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "single", 10, 8, 2,
                ),
                12,
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 100, 60, 20,
                ),
                120,
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 130, 90, 25,
                ),
                35,
            )
            with self.assertRaisesRegex(ValueError, "subset"):
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "single", 1, 2, 0,
                )
            with self.assertRaisesRegex(ValueError, "cannot decrease"):
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 140, 89, 30,
                )
            with self.assertRaisesRegex(ValueError, "cannot decrease"):
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 140, 105, 30,
                )
            connection.commit()
            totals = [
                item["normalized_total"]
                for item in connection.execute("SELECT normalized_total FROM model_calls ORDER BY rowid")
            ]
        finally:
            connection.close()
        self.assertEqual(totals, [12, 120, 35])
        usage = token_snapshot(self.db, self.data)
        self.assertEqual(
            {
                key: usage["all_history"][key]
                for key in (
                    "total_tokens",
                    "input_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            },
            {
                "total_tokens": 167,
                "input_tokens": 140,
                "cached_input_tokens": 98,
                "uncached_input_tokens": 42,
                "output_tokens": 27,
            },
        )
        self.assertAlmostEqual(
            usage["all_history"]["ratios"]["input_per_output"], 140 / 27
        )
        self.assertAlmostEqual(
            usage["all_history"]["ratios"]["cached_per_uncached"], 98 / 42
        )
        self.assertEqual(usage["today"]["total_tokens"], 167)
        self.assertEqual(usage["trend"][-1]["total_tokens"], 167)
        self.assertEqual(usage["projects"][0]["project_id"], "usage")
        self.assertEqual(usage["models"][0]["model"], "deterministic")
        self.assertEqual(usage["sessions"][0]["task_session_id"], row["session_id"])
        self.assertTrue(usage["sessions"][0]["worker_id"])
        self.assertEqual(usage["sessions"][0]["worker_role"], "deterministic")
        self.assertEqual(provider_facts("deterministic")[0]["available"], True)
        self.assertTrue(all(not item["available"] for item in provider_facts("planner")))
        self.assertIsNone(provider_adapter("local").report_context_usage())
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            provider_adapter("codex_cli")

    def test_project_archive_preserves_history_and_monitor_is_read_only(self):
        create_project(self.store, "archive-me", "archive-create")
        history = conversation(self.db, self.data, "archive-me")
        self.assertEqual(history["project"]["id"], "archive-me")
        self.assertEqual(history["messages"][0]["content"], "create_project")

        before = self._row_counts()
        state = monitor_snapshot(self.db, self.data)
        after = self._row_counts()
        self.assertEqual(before, after)
        self.assertTrue(state["read_only"])
        self.assertEqual(state["database"]["journal_mode"], "wal")
        self.assertEqual(state["database"]["quick_check"], ["ok"])
        self.assertEqual(state["database"]["schema_version"], 2)

        archive_project(
            self.store, "archive-me", "archive-me", "archive-confirmed"
        )
        self.assertEqual(projects_snapshot(self.db, self.data)["projects"], [])
        archived_history = conversation(self.db, self.data, "archive-me")
        self.assertEqual(
            [item["content"] for item in archived_history["messages"]],
            ["create_project", "archive_project"],
        )
        state = monitor_snapshot(self.db, self.data)
        self.assertEqual(state["summary"]["projects"], 0)
        self.assertEqual(state["summary"]["archived_projects"], 1)

        create_project(self.store, "archive-me", "archive-restore")
        self.assertEqual(
            projects_snapshot(self.db, self.data)["projects"][0]["project_id"],
            "archive-me",
        )
        submit_message(
            self.store, "archive-me", "写入 active.txt: active", "archive-active"
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with self.assertRaisesRegex(ValueError, "active task"):
            archive_project(
                self.store, "archive-me", "archive-me", "archive-rejected"
            )

    def test_settings_and_library_are_indexed_and_read_only(self):
        state = settings_library_snapshot(self.db, self.data)
        self.assertEqual(len(state["settings"]), 9)
        self.assertEqual(len(state["library"]), 6)
        self.assertEqual(
            {item["kind"] for item in state["library"]},
            {"role", "rule", "worker_template"},
        )
        role_path = self.data / "library" / "roles" / "deterministic.md"
        role_path.write_text(role_path.read_text() + "\nExtra deterministic boundary.\n")
        self.store.initialize()
        updated = settings_library_snapshot(self.db, self.data)
        role = next(item for item in updated["library"] if item["item_key"] == "deterministic")
        self.assertEqual(role["revision"], 2)
        self.assertTrue(all(item["sha256_matches"] for item in updated["library"]))

    def test_provider_probe_tasks_record_zero_and_minimal_token_evidence(self):
        spec, _ = normalize_instruction("探测 Provider codex_cli: minimal")
        self.assertEqual(spec["kind"], "authorization_required")
        requests = []

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, self.headers["Authorization"], payload))
                if self.path == "/v1/probe":
                    body = {"available": True, "detail": "codex-cli test"}
                else:
                    body = {
                        "returncode": 0,
                        "stdout": "PLOWWHIP_PROBE_OK\n",
                        "stderr": "",
                        "input_tokens": 30,
                        "cached_input_tokens": 10,
                        "output_tokens": 2,
                        "model": "codex-test",
                    }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        environment = {
            "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
            "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
            "PLOW_WHIP_PROBE_PROJECT_PATH": str(self.root),
        }
        try:
            with patch.dict(os.environ, environment):
                submit_message(
                    self.store,
                    "monitor-probe-codex_cli",
                    "探测 Provider codex_cli: 0token",
                    "probe-zero",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    ["intake", "execute", "verify"],
                )
                zero = snapshot(
                    self.db, self.data, "monitor-probe-codex_cli"
                )
                self.assertEqual(zero["task"]["public_status"], "done")
                executor = next(
                    item for item in zero["sessions"] if item["role_key"] == "provider_probe"
                )
                self.assertEqual(executor["provider_key"], "codex_cli")
                zero_evidence = json.loads(
                    next(
                        Path(item["path"]).read_text()
                        for item in zero["artifacts"]
                        if item["kind"] == "evidence"
                    )
                )
                self.assertTrue(zero_evidence["passed"])
                self.assertFalse(zero_evidence["model_invoked"])
                self.assertEqual(zero_evidence["total_tokens"], 0)

                submit_message(
                    self.store,
                    "monitor-probe-codex_cli",
                    "探测 Provider codex_cli: minimal 确认 codex_cli",
                    "probe-minimal",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    ["intake", "execute", "verify"],
                )
                minimal = snapshot(
                    self.db, self.data, "monitor-probe-codex_cli"
                )
                self.assertEqual(minimal["task"]["public_status"], "done")
                self.assertEqual(minimal["model_usage"][0]["normalized_total"], 32)
                state = monitor_snapshot(self.db, self.data)
                codex = next(
                    item
                    for item in state["providers"]
                    if item["provider_key"] == "codex_cli"
                )
                self.assertEqual(codex["latest_probe"]["result"]["total_tokens"], 32)
                self.assertEqual(codex["latest_probe"]["public_status"], "done")
                self.assertEqual(codex["zero_probe"]["result"]["total_tokens"], 0)
                self.assertEqual(
                    codex["readiness"]["recent_execution_health"], "healthy"
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()
        self.assertEqual([item[0] for item in requests], ["/v1/probe", "/v1/execute"])
        self.assertTrue(all(item[1] == "Bearer test-token" for item in requests))

    def _row_counts(self):
        connection = sqlite3.connect(str(self.db))
        try:
            return tuple(
                connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "projects",
                    "messages",
                    "goals",
                    "tasks",
                    "host_jobs",
                    "artifacts",
                    "task_events",
                )
            )
        finally:
            connection.close()


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = Store(root / "state.db", root / "data")
        self.store.initialize()
        self.server = make_server(self.store, "127.0.0.1", 0)
        self.stop = threading.Event()
        self.cronner = threading.Thread(
            target=run_cronner, args=(self.store, self.stop, 0.01), daemon=True
        )
        self.http = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.cronner.start()
        self.http.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.cronner.join()
        self.http.join()
        self.temporary.cleanup()

    def test_http_intake_decision_and_automatic_completion(self):
        with urlopen(self.base + "/", timeout=2) as response:
            html = response.read().decode()
            self.assertIn("Plow Whip · 无人值守控制台", html)
            self.assertIn("SQLite WAL", html)
            self.assertEqual(html.count("data-view="), 7)
            self.assertIn("Evidence Trail", html)
            self.assertIn("Token 计量", html)
            self.assertIn("Monitor 只读", html)
            self.assertIn("Provider 探针", html)
            self.assertIn("立即唤醒", html)
            self.assertIn("处理待决定", html)
            self.assertIn("探测 Provider codex_cli: 0token", html)
            self.assertIn("任务泳道", html)
            self.assertEqual(html.count("data-task-lane="), 4)
            self.assertIn("HostJob / Session", html)
            self.assertIn("项目管家", html)
            self.assertIn("设置与资源库", html)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with urlopen(self.base + "/api/settings-library", timeout=2) as response:
            settings_library = json.load(response)
        self.assertEqual(len(settings_library["library"]), 6)

        status, _ = self._post(
            "/api/actions",
            {
                "kind": "create_project",
                "project_id": "empty-web",
                "idempotency_key": "empty-web-create",
            },
        )
        self.assertEqual(status, 202)
        status, _ = self._post(
            "/api/actions",
            {
                "kind": "archive_project",
                "project_id": "empty-web",
                "confirmation": "empty-web",
                "idempotency_key": "empty-web-archive",
            },
        )
        self.assertEqual(status, 202)

        status, _ = self._post(
            "/api/messages",
            {
                "project_id": "web",
                "content": "需要主人决定",
                "idempotency_key": "web-message-1",
            },
        )
        self.assertEqual(status, 202)
        waiting = self._wait_for("web", "needs_decision")

        status, decision = self._post(
            "/api/actions",
            {
                "project_id": "web",
                "task_id": waiting["task"]["id"],
                "kind": "provide_decision",
                "instruction": "写入 web.txt: 自动完成",
                "idempotency_key": "web-decision-1",
            },
        )
        self.assertEqual(status, 202)
        done = self._wait_for("web", "done")
        self.assertEqual(done["task"]["spec_revision"], 2)
        self.assertEqual(
            [event["kind"] for event in reversed(done["events"])],
            ["needs_decision", "decision_applied", "executed", "verified"],
        )
        self.assertEqual({item["revision"] for item in done["artifacts"]}, {2})
        with urlopen(self.base + "/api/search?q=web.txt", timeout=2) as response:
            found = json.load(response)
        self.assertEqual(
            {item["kind"] for item in found["results"]},
            {"task", "message", "artifact"},
        )

        with urlopen(f"{self.base}/api/projects", timeout=2) as response:
            projects = json.load(response)
        self.assertEqual(projects["projects"][0]["task_id"], done["task"]["id"])
        self.assertNotIn(
            "empty-web", {project["project_id"] for project in projects["projects"]}
        )
        with urlopen(
            f"{self.base}/api/tasks/{done['task']['id']}", timeout=2
        ) as response:
            task = json.load(response)
        self.assertEqual(task["task"]["id"], done["task"]["id"])
        with urlopen(self.base + "/api/token", timeout=2) as response:
            usage = json.load(response)
        self.assertEqual(usage["all_history"]["total_tokens"], 0)
        with urlopen(self.base + "/api/monitor", timeout=2) as response:
            monitor = json.load(response)
        self.assertTrue(monitor["read_only"])
        self.assertEqual(monitor["summary"]["archived_projects"], 1)
        with urlopen(
            self.base + "/api/butler?project_id=web", timeout=2
        ) as response:
            butler = json.load(response)
        self.assertEqual(butler["project"]["id"], "web")
        self.assertGreaterEqual(len(butler["messages"]), 2)

        status, duplicate = self._post(
            "/api/actions",
            {
                "project_id": "web",
                "task_id": waiting["task"]["id"],
                "kind": "provide_decision",
                "instruction": "写入 ignored.txt: ignored",
                "idempotency_key": "web-decision-1",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(decision, duplicate)

        with self.assertRaises(HTTPError) as error:
            self._post("/api/actions", {"project_id": "web"})
        self.assertEqual(error.exception.code, 400)

        request = Request(
            self.base + "/api/messages",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://evil.invalid"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(self.store, "0.0.0.0", 0)
        server = make_server(self.store, "0.0.0.0", 0, allow_non_loopback=True)
        server.server_close()

    def _post(self, path, payload):
        request = Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def _wait_for(self, project_id, status):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with urlopen(f"{self.base}/api/projects/{project_id}", timeout=2) as response:
                state = json.load(response)
            if state["task"] and state["task"]["public_status"] == status:
                return state
            time.sleep(0.01)
        self.fail(f"project {project_id} did not reach {status}")


if __name__ == "__main__":
    unittest.main()
