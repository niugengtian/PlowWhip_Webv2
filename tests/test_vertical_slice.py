import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plowwhip.app import make_server
from plowwhip.cronner import run as run_cronner, run_until_idle, tick
from plowwhip.intake import submit_action, submit_message
from plowwhip.lifecycle import LeaseLost, advance_project
from plowwhip.monitor import settings_library_snapshot, snapshot
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
            connection.commit()
            totals = [
                item["normalized_total"]
                for item in connection.execute("SELECT normalized_total FROM model_calls ORDER BY rowid")
            ]
        finally:
            connection.close()
        self.assertEqual(totals, [12, 120, 35])
        self.assertEqual(provider_facts("deterministic")[0]["available"], True)
        self.assertTrue(all(not item["available"] for item in provider_facts("planner")))
        self.assertIsNone(provider_adapter("local").report_context_usage())
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            provider_adapter("codex_cli")

    def test_settings_and_library_are_indexed_and_read_only(self):
        state = settings_library_snapshot(self.db, self.data)
        self.assertEqual(len(state["settings"]), 9)
        self.assertEqual(len(state["library"]), 4)
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
            self.assertIn("全局首页", html)
            self.assertIn("Task 详情", html)
            self.assertIn("设置与资源库", html)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with urlopen(self.base + "/api/settings-library", timeout=2) as response:
            settings_library = json.load(response)
        self.assertEqual(len(settings_library["library"]), 4)

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
        with urlopen(
            f"{self.base}/api/tasks/{done['task']['id']}", timeout=2
        ) as response:
            task = json.load(response)
        self.assertEqual(task["task"]["id"], done["task"]["id"])

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
