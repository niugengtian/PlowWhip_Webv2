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
from plowwhip.monitor import snapshot
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
        finally:
            connection.close()

    def test_failed_evidence_converges_to_needs_decision(self):
        submit_message(self.store, "project-b", "write result.txt: expected", "request-2")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
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
            ["decision", "execute", "verify"],
        )
        revised = snapshot(self.db, self.data, "project-b")
        self.assertEqual(revised["task"]["public_status"], "done")
        self.assertEqual(revised["task"]["spec_revision"], 2)
        self.assertEqual({item["revision"] for item in revised["artifacts"]}, {1, 2})
        self.assertEqual(len({item["path"] for item in revised["artifacts"]}), 4)

    def test_unrecognized_instruction_requires_decision_without_execution(self):
        submit_message(self.store, "project-c", "please decide for me", "request-3")
        actions = run_until_idle(self.store)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "needs_decision")
        view = snapshot(self.db, self.data, "project-c")
        self.assertEqual(view["task"]["phase"], "intake")
        self.assertEqual(view["task"]["fault_code"], "scope")
        self.assertEqual(view["artifacts"], [])

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
