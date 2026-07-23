import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from plowwhip.host_bridge import (
    HostJobManager,
    _execution_argv,
    _load_private_env,
    _parse_usage,
    _resolve_executable,
    make_server,
)


class HostBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.root / "state"
        self.token = "test-host-bridge-token-123456"
        self.worker = self.root / "simple-worker"
        self.worker.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
import time

if "--probe" in sys.argv:
    print("simple-worker 1.0")
    raise SystemExit(0)
project = pathlib.Path(sys.argv[sys.argv.index("--project") + 1])
session = sys.argv[sys.argv.index("--session") + 1]
prompt = sys.stdin.read()
with (project / "invocations.txt").open("a") as output:
    output.write("1\\n")
if "SLEEP" in prompt:
    time.sleep(30)
else:
    (project / "result.txt").write_text("done")
    for index in range(25):
        print(f"bounded-line-{index}")
    print(json.dumps({
        "type": "result",
        "session_id": session,
        "model": "fake-model",
        "input_tokens": 10,
        "cached_input_tokens": 3,
        "output_tokens": 2,
    }))
""",
            encoding="utf-8",
        )
        self.worker.chmod(0o700)
        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.root}{os.pathsep}{self.old_path}"
        self.server = make_server(
            "127.0.0.1",
            0,
            self.token,
            (self.root,),
            self.state,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ["PATH"] = self.old_path
        self.temporary.cleanup()

    def _post(
        self,
        path: str,
        body: dict[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{self.url}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token or self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _wait_terminal(self, job_id: str) -> dict[str, object]:
        for _ in range(100):
            _, state = self._post("/v1/jobs/status", {"job_id": job_id})
            if state["status"] not in {
                "dispatching",
                "running",
                "orphan_running",
                "cancelling",
                "recovery_hold",
            }:
                return state
            time.sleep(0.02)
        self.fail("Host Job did not reach a terminal status")

    def test_restricted_durable_job_and_restart_recovery(self):
        status, _ = self._post(
            "/v1/probe",
            {"adapter": "json-worker", "executable": str(self.worker)},
            token="wrong-token-that-is-long-enough",
        )
        self.assertEqual(status, 401)
        status, probe = self._post(
            "/v1/probe",
            {"adapter": "json-worker", "executable": str(self.worker)},
        )
        self.assertEqual(status, 200)
        self.assertTrue(probe["available"])

        _, before = self._post(
            "/v1/evidence/snapshot", {"project_path": str(self.project), "paths": []}
        )
        job_id = uuid4().hex
        payload = {
            "job_id": job_id,
            "adapter": "json-worker",
            "executable": str(self.worker),
            "project_path": str(self.project),
            "prompt": "write the deterministic fixture",
            "timeout_seconds": 10,
            "access": "write",
            "context_policy": {},
        }
        self.assertEqual(self._post("/v1/jobs/start", payload)[0], 202)
        self.assertEqual(self._post("/v1/jobs/start", payload)[0], 202)
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["returncode"], 0)
        self.assertEqual(terminal["input_tokens"], 10)
        self.assertEqual(terminal["cached_input_tokens"], 3)
        self.assertEqual(terminal["output_tokens"], 2)
        self.assertEqual(terminal["model"], "fake-model")
        self.assertEqual(
            (self.project / "invocations.txt").read_text().splitlines(),
            ["1"],
        )

        _, output = self._post(
            "/v1/jobs/output",
            {
                "job_id": job_id,
                "stdout_offset": -1,
                "stderr_offset": -1,
                "limit": 32768,
                "tail_lines": 20,
            },
        )
        stdout = "".join(
            chunk["text"]
            for chunk in output["chunks"]
            if chunk["stream"] == "stdout"
        )
        self.assertLessEqual(len(stdout.splitlines()), 20)
        self.assertIn('"session_id"', stdout)

        _, after = self._post(
            "/v1/evidence/snapshot", {"project_path": str(self.project), "paths": []}
        )
        self.assertNotEqual(
            before["git"]["fingerprint"], after["git"]["fingerprint"]
        )
        raw_state = (self.state / f"{job_id}.json").read_text()
        self.assertNotIn(payload["prompt"], raw_state)
        self.assertNotIn(self.token, raw_state)

        restarted = HostJobManager(self.state, (self.root.resolve(),))
        recovered = restarted.status(job_id)
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["returncode"], 0)

    def test_scope_executable_loopback_and_cancel_guards(self):
        private_env = self.root / "bridge.env"
        private_env.write_text("PLOW_WHIP_BRIDGE_TOKEN=file-token-is-long-enough-123\n")
        private_env.chmod(0o644)
        with self.assertRaises(SystemExit):
            _load_private_env(private_env)
        private_env.chmod(0o600)
        with patch.dict("os.environ", {}, clear=True):
            _load_private_env(private_env)
            self.assertEqual(
                __import__("os").environ["PLOW_WHIP_BRIDGE_TOKEN"],
                "file-token-is-long-enough-123",
            )

        outside = self.root.parent
        status, _ = self._post(
            "/v1/evidence/snapshot",
            {"project_path": str(outside), "paths": []},
        )
        self.assertEqual(status, 400)
        status, _ = self._post(
            "/v1/probe",
            {"adapter": "json-worker", "executable": "/bin/sh"},
        )
        self.assertEqual(status, 400)
        disguised = self.project / "codex"
        disguised.write_text("#!/bin/sh\nexit 0\n")
        disguised.chmod(0o700)
        status, _ = self._post(
            "/v1/probe",
            {"adapter": "codex", "executable": str(disguised)},
        )
        self.assertEqual(status, 400)
        with self.assertRaises(ValueError):
            make_server(
                "0.0.0.0",
                0,
                self.token,
                (self.root,),
                self.root / "other-state",
            )
        with self.assertRaises(ValueError):
            make_server(
                "localhost",
                0,
                self.token,
                (self.root,),
                self.root / "other-state",
            )

        job_id = uuid4().hex
        payload = {
            "job_id": job_id,
            "adapter": "json-worker",
            "executable": str(self.worker),
            "project_path": str(self.project),
            "prompt": "SLEEP",
            "timeout_seconds": 60,
            "access": "write",
            "context_policy": {},
        }
        self.assertEqual(self._post("/v1/jobs/start", payload)[0], 202)
        self.assertEqual(
            self._post("/v1/jobs/cancel", {"job_id": job_id})[0],
            202,
        )
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(terminal["returncode"], 130)

    def test_running_process_is_reconciled_and_cancelled_after_bridge_restart(self):
        job_id = uuid4().hex
        status, started = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "json-worker",
                "executable": str(self.worker),
                "project_path": str(self.project),
                "prompt": "SLEEP",
                "timeout_seconds": 60,
                "access": "write",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(started["status"], "running")
        if not started["process_identity"]:
            self.server.manager.cancel(job_id)  # type: ignore[attr-defined]
            self._wait_terminal(job_id)
            self.skipTest("process start identity is unavailable in this sandbox")

        restarted = HostJobManager(self.state, (self.root.resolve(),))
        reconciled = restarted.status(job_id)
        self.assertEqual(reconciled["status"], "orphan_running")
        self.assertTrue(reconciled["process_identity"])
        restarted.cancel(job_id)
        for _ in range(100):
            terminal = restarted.status(job_id)
            if terminal["status"] == "cancelled":
                break
            time.sleep(0.02)
        else:
            self.fail("restarted Bridge did not reconcile cancellation")
        self.assertEqual(terminal["returncode"], 130)

    def test_cursor_read_mode_and_cumulative_token_normalization(self):
        context = {
            "provider_compaction_token_limit": 120_000,
            "rotation_max_bytes": 65_536,
            "hot_max_bytes": 16_384,
            "warm_max_bytes": 8_192,
            "max_turns": 24,
            "tool_no_progress_limit": 6,
        }
        read_argv = _execution_argv(
            "cursor",
            "cursor",
            self.project,
            "cursor-session",
            "review only",
            "read",
            context,
        )
        self.assertEqual(read_argv[-3:], ["--mode", "plan", "review only"])
        self.assertNotIn("--force", read_argv)
        write_argv = _execution_argv(
            "cursor",
            "cursor",
            self.project,
            "cursor-session",
            "implement",
            "write",
            context,
        )
        self.assertIn("--force", write_argv)
        self.assertNotIn("--mode", write_argv)

        usage = _parse_usage(
            "\n".join(
                (
                    '{"type":"system","session_id":"cursor-session","model":"composer"}',
                    '{"type":"result","usage":{"inputTokens":10,'
                    '"cacheReadTokens":4,"outputTokens":2}}',
                    '{"type":"result","usage":{"inputTokens":20,'
                    '"cacheReadTokens":8,"outputTokens":5}}',
                )
            )
        )
        self.assertEqual(usage["session_id"], "cursor-session")
        self.assertEqual(usage["model"], "composer")
        self.assertEqual(usage["input_tokens"], 28)
        self.assertEqual(usage["cached_input_tokens"], 8)
        self.assertEqual(usage["output_tokens"], 5)
        with patch(
            "plowwhip.host_bridge.shutil.which",
            side_effect=lambda name: (
                "/usr/local/bin/cursor-agent"
                if name == "cursor-agent"
                else None
            ),
        ):
            self.assertEqual(
                _resolve_executable("cursor", "cursor"),
                "/usr/local/bin/cursor-agent",
            )


if __name__ == "__main__":
    unittest.main()
