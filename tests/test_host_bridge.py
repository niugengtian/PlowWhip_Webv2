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
    KNOWN_EXECUTABLE_PATHS,
    _execution_argv,
    _load_private_env,
    _parse_usage,
    _resolve_executable,
    _safe_environment,
    _staged_workspace_has_payload,
    _validated_capability,
    make_server,
)
from plowwhip.provider import provider_job_output


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
elif "DELETE" in prompt:
    (project / "delete-me.txt").unlink()
    (project / "after-delete.txt").write_text("applied")
elif "BIG" in prompt:
    sys.stdout.write("A" * (1024 * 1024 + 321))
    print("\\nBIG-RESULT-END")
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
        body = dict(body)
        if path == "/v1/jobs/start" and "capability" not in body:
            body["capability"] = {
                "tier": (
                    "read_only"
                    if body.get("access") == "read"
                    else "recoverable_workspace_write"
                ),
                "project_id": "test",
                "task_id": "test",
                "spec_revision": 1,
                "allowed_actions": (
                    ["read_workspace"]
                    if body.get("access") == "read"
                    else ["write_workspace", "run_checks"]
                ),
                "target_scope": "project_workspace",
            }
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
        with self.assertRaisesRegex(ValueError, "structured capability"):
            self.server.manager.start(  # type: ignore[attr-defined]
                {
                    "job_id": uuid4().hex,
                    "adapter": "json-worker",
                    "executable": str(self.worker),
                    "project_path": str(self.project),
                    "prompt": "missing capability",
                    "timeout_seconds": 10,
                    "access": "write",
                    "context_policy": {},
                }
            )

        status, rejected = self._post(
            "/v1/jobs/start",
            {
                "job_id": uuid4().hex,
                "adapter": "json-worker",
                "executable": str(self.worker),
                "project_path": str(self.project),
                "prompt": "api_key=abcdefghijklmnop",
                "timeout_seconds": 10,
                "access": "write",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("plaintext Secret", rejected["detail"])

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
            "model": "fixture-model",
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
        self.assertEqual(terminal["requested_model"], "fixture-model")
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

    def test_complete_output_is_chunked_to_eof_and_never_tail_overwritten(self):
        job_id = uuid4().hex
        payload = {
            "job_id": job_id,
            "adapter": "json-worker",
            "executable": str(self.worker),
            "project_path": str(self.project),
            "prompt": "BIG",
            "timeout_seconds": 10,
            "access": "write",
            "context_policy": {},
        }
        self.assertEqual(self._post("/v1/jobs/start", payload)[0], 202)
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "completed")

        offsets = {"stdout": 0, "stderr": 0}
        bodies = {"stdout": [], "stderr": []}
        while True:
            _, output = self._post(
                "/v1/jobs/output",
                {
                    "job_id": job_id,
                    "stdout_offset": offsets["stdout"],
                    "stderr_offset": offsets["stderr"],
                    "limit": 65_536,
                    "tail_lines": 20,
                },
            )
            for chunk in output["chunks"]:
                bodies[chunk["stream"]].append(chunk["text"])
            offsets = output["next_offsets"]
            if not output["has_more"]:
                break
        stdout = "".join(bodies["stdout"])
        self.assertGreater(len(stdout.encode()), 1024 * 1024)
        self.assertTrue(stdout.endswith("BIG-RESULT-END\n"))
        stream_ref = output["stream_refs"]["stdout"]
        self.assertEqual(stream_ref["bytes"], offsets["stdout"])
        self.assertTrue(stream_ref["complete"])
        persisted = self.state / job_id / "stdout.segment-000001.log"
        self.assertEqual(persisted.stat().st_size, stream_ref["bytes"])
        self.assertEqual(
            __import__("hashlib").sha256(persisted.read_bytes()).hexdigest(),
            stream_ref["sha256"],
        )
        with patch.dict(
            os.environ,
            {
                "PLOW_WHIP_BRIDGE_URL": self.url,
                "PLOW_WHIP_BRIDGE_TOKEN": self.token,
            },
        ):
            complete = provider_job_output(job_id, complete=True)
        complete_stdout = "".join(
            chunk["text"]
            for chunk in complete["chunks"]
            if chunk["stream"] == "stdout"
        )
        self.assertEqual(complete_stdout, stdout)
        self.assertTrue(complete["complete"])

    def test_recoverable_workspace_capability_renames_deletions(self):
        original = self.project / "delete-me.txt"
        original.write_text("preserve me")
        job_id = uuid4().hex
        status, _ = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "json-worker",
                "executable": str(self.worker),
                "project_path": str(self.project),
                "prompt": "DELETE",
                "timeout_seconds": 10,
                "access": "write",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["returncode"], 0)
        self.assertTrue(terminal["workspace_apply"]["applied"])
        self.assertFalse(original.exists())
        recoverable = list(self.project.glob("delete-me.txt.rm.*"))
        self.assertEqual(len(recoverable), 1)
        self.assertRegex(
            recoverable[0].name,
            r"^delete-me\.txt\.rm\.\d{14}$",
        )
        self.assertEqual(recoverable[0].read_text(), "preserve me")
        self.assertEqual(
            (self.project / "after-delete.txt").read_text(), "applied"
        )

    def test_staged_workspace_payload_detection(self):
        empty = self.root / "staged-empty"
        empty.mkdir()
        (empty / "subdir").mkdir()
        self.assertFalse(_staged_workspace_has_payload(empty))
        filled = self.root / "staged-filled"
        filled.mkdir()
        (filled / "docs").mkdir()
        (filled / "docs" / "out.md").write_text("artifact")
        self.assertTrue(_staged_workspace_has_payload(filled))

    def test_external_effect_capability_requires_scoped_live_authorization(self):
        base = {
            "tier": "authorized_external_effect",
            "project_id": "project",
            "task_id": "task",
            "spec_revision": 3,
        }
        authorization = {
            "project_id": "project",
            "task_id": "task",
            "spec_revision": 3,
            "action_kind": "git_publish",
            "target_scope": "git@example.invalid:repo.git#refs/heads/main",
            "expires_at": time.time() + 60,
        }
        validated = _validated_capability(
            {**base, "authorization": authorization},
            "git-publish",
            "write",
        )
        self.assertEqual(
            validated["authorization"]["target_scope"],
            authorization["target_scope"],
        )
        with self.assertRaisesRegex(ValueError, "stale or out of scope"):
            _validated_capability(
                {
                    **base,
                    "authorization": {
                        **authorization,
                        "expires_at": time.time() - 1,
                    },
                },
                "git-publish",
                "write",
            )
        with self.assertRaisesRegex(ValueError, "capability tier"):
            _validated_capability(
                {"tier": "recoverable_workspace_write"},
                "git-publish",
                "write",
            )

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
        private_env.write_text("UNRELATED_MODEL=must-not-be-env\n")
        with self.assertRaisesRegex(SystemExit, "unsupported private environment"):
            _load_private_env(private_env)
        # JSON Worker selects model via private env (no --model CLI flag).
        private_env.write_text("DEEPSEEK_MODEL=deepseek-v4-flash\n")
        private_env.chmod(0o600)
        with patch.dict("os.environ", {}, clear=True):
            _load_private_env(private_env)
            self.assertEqual(
                __import__("os").environ["DEEPSEEK_MODEL"],
                "deepseek-v4-flash",
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
        self.assertEqual(read_argv[-3:], ["--mode", "ask", "review only"])
        self.assertNotIn("--force", read_argv)
        write_argv = _execution_argv(
            "cursor",
            "cursor",
            self.project,
            "cursor-session",
            "implement",
            "write",
            context,
            model="cursor-test-model",
        )
        self.assertIn("--force", write_argv)
        self.assertNotIn("--mode", write_argv)
        self.assertIn("--model", write_argv)
        self.assertEqual(
            write_argv[write_argv.index("--model") + 1],
            "cursor-test-model",
        )

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
        bundled_codex = self.root / "codex"
        bundled_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bundled_codex.chmod(0o700)
        with (
            patch("plowwhip.host_bridge.shutil.which", return_value=None),
            patch.dict(
                KNOWN_EXECUTABLE_PATHS,
                {"codex": {bundled_codex}},
            ),
        ):
            self.assertEqual(
                _resolve_executable("codex", "codex"),
                str(bundled_codex),
            )

    def test_codex_read_job_uses_disposable_workspace_and_temp(self):
        codex = self.root / "codex"
        codex.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile

pathlib.Path("provider-write.txt").write_text("isolated", encoding="utf-8")
temporary = tempfile.mkdtemp()
print(json.dumps({
    "argv": sys.argv[1:],
    "cwd": str(pathlib.Path.cwd()),
    "temporary": temporary,
}))
""",
            encoding="utf-8",
        )
        codex.chmod(0o700)
        (self.project / "source.txt").write_text("canonical", encoding="utf-8")
        job_id = uuid4().hex

        status, _ = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "codex",
                "executable": "codex",
                "project_path": str(self.project),
                "prompt": "verify without changing the source workspace",
                "timeout_seconds": 10,
                "access": "read",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["returncode"], 0)
        self.assertTrue(terminal["isolated_workspace"])
        self.assertFalse((self.project / "provider-write.txt").exists())
        self.assertEqual((self.project / "source.txt").read_text(), "canonical")
        self.assertFalse((self.state / job_id / "workspace").exists())
        self.assertFalse((self.state / job_id / "tmp").exists())

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
        result = json.loads(stdout)
        self.assertNotEqual(result["cwd"], str(self.project))
        self.assertIn(str(self.state / job_id), result["cwd"])
        self.assertIn(str(self.state / job_id), result["temporary"])
        self.assertEqual(
            result["argv"][result["argv"].index("--sandbox") + 1],
            "workspace-write",
        )

    def test_planner_job_uses_private_read_only_bridge_workspace(self):
        codex = self.root / "codex"
        codex.write_text(
            """#!/usr/bin/env python3
import json
import pathlib

pathlib.Path("provider-write.txt").write_text("isolated", encoding="utf-8")
print(json.dumps({"cwd": str(pathlib.Path.cwd())}))
""",
            encoding="utf-8",
        )
        codex.chmod(0o700)
        workspace_key = "project-a-planner"
        job_id = uuid4().hex
        status, _ = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "codex",
                "executable": "codex",
                "project_path": "",
                "workspace_kind": "planner",
                "workspace_key": workspace_key,
                "prompt": "classify one immutable instruction",
                "timeout_seconds": 10,
                "access": "read",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertTrue(terminal["isolated_workspace"])
        planner_workspace = self.state / "planner-workspaces" / workspace_key
        self.assertTrue(planner_workspace.is_dir())
        self.assertFalse((planner_workspace / "provider-write.txt").exists())

        invalid = {
            "job_id": uuid4().hex,
            "adapter": "codex",
            "executable": "codex",
            "project_path": "",
            "workspace_kind": "planner",
            "workspace_key": "../escape",
            "prompt": "invalid",
            "timeout_seconds": 10,
            "access": "read",
            "context_policy": {},
        }
        self.assertEqual(self._post("/v1/jobs/start", invalid)[0], 400)
        invalid["job_id"] = uuid4().hex
        invalid["workspace_key"] = "project-a-write"
        invalid["access"] = "write"
        self.assertEqual(self._post("/v1/jobs/start", invalid)[0], 400)

    def test_cursor_first_job_bootstraps_one_session(self):
        cursor = self.root / "cursor"
        cursor.write_text(
            """#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] == ["agent", "create-chat"]:
    print("cursor-chat-created")
elif "--version" in sys.argv:
    print("cursor 1.0")
else:
    print(json.dumps({"type": "result", "session_id": "cursor-chat-created"}))
""",
            encoding="utf-8",
        )
        cursor.chmod(0o700)
        job_id = uuid4().hex
        status, started = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "cursor",
                "executable": "cursor",
                "project_path": str(self.project),
                "prompt": "bounded fixture",
                "timeout_seconds": 10,
                "access": "read",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(started["session_id"], "cursor-chat-created")
        terminal = self._wait_terminal(job_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["returncode"], 0)
        self.assertEqual(terminal["session_id"], "cursor-chat-created")

    def test_git_publish_adapter_is_internal_and_probeable(self):
        status, result = self._post(
            "/v1/probe",
            {"adapter": "git-publish", "executable": "git-publish"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["available"])
        self.assertIn("plowwhip-git-publish", result["detail"])
        inspect_argv = _execution_argv(
            "git-publish",
            "python",
            self.project,
            None,
            json.dumps({"kind": "git_publish", "operation": "inspect"}),
            "read",
            {},
        )
        self.assertTrue(inspect_argv[-1].endswith("git_publish_worker.py"))
        with self.assertRaisesRegex(ValueError, "requires read access"):
            _execution_argv(
                "git-publish",
                "python",
                self.project,
                None,
                json.dumps({"kind": "git_publish", "operation": "inspect"}),
                "write",
                {},
            )
        with self.assertRaisesRegex(ValueError, "requires write access"):
            _execution_argv(
                "git-publish",
                "python",
                self.project,
                None,
                json.dumps({"kind": "git_publish", "operation": "publish"}),
                "read",
                {},
            )
        job_id = uuid4().hex
        status, _started = self._post(
            "/v1/jobs/start",
            {
                "job_id": job_id,
                "adapter": "git-publish",
                "executable": "git-publish",
                "project_path": str(self.project),
                "prompt": json.dumps(
                    {
                        "kind": "git_publish",
                        "operation": "inspect",
                        "remote_ssh": "git@github.com:owner/repository.git",
                        "branch": "blue",
                        "expected_head": "a" * 40,
                    }
                ),
                "timeout_seconds": 10,
                "access": "read",
                "context_policy": {},
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(self._wait_terminal(job_id)["returncode"], 1)

    def test_host_jobs_inherit_only_the_ssh_agent_socket_not_key_material(self):
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "PATH": self.old_path,
                "SSH_AUTH_SOCK": "/private/tmp/test-agent.sock",
                "PLOW_WHIP_GIT_SSH_IDENTITY_FILE": (
                    "/Users/test/.ssh/id_ed25519"
                ),
                "DEEPSEEK_MODEL": "deepseek-v4-flash",
                "KIMI_MODEL": "moonshot-v1",
                "UNRELATED_SECRET": "must-not-pass",
            },
            clear=True,
        ):
            environment = _safe_environment()
        self.assertEqual(
            environment["SSH_AUTH_SOCK"], "/private/tmp/test-agent.sock"
        )
        self.assertEqual(
            environment["PLOW_WHIP_GIT_SSH_IDENTITY_FILE"],
            "/Users/test/.ssh/id_ed25519",
        )
        self.assertNotIn("UNRELATED_SECRET", environment)
        # Model choice for json-worker is env-backed; API keys stay private-env only.
        self.assertEqual(environment["DEEPSEEK_MODEL"], "deepseek-v4-flash")
        self.assertEqual(environment["KIMI_MODEL"], "moonshot-v1")


if __name__ == "__main__":
    unittest.main()
