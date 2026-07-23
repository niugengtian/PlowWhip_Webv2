import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from plowwhip.git_publish_worker import (
    PublishError,
    _safe_tracked_tree,
    _validated_spec,
    publish,
)


class GitPublishWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        (self.project / "README.md").write_text("safe\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_scope_authorization_and_sensitive_file_rejection(self):
        remote = "git@github.com:owner/repository.git"
        spec = {
            "kind": "git_publish",
            "remote_ssh": remote,
            "branch": "blue",
            "expected_head": self.head,
            "authorization": {
                "action_kind": "git_publish",
                "target_scope": f"{remote}#refs/heads/blue",
                "expires_at": time.time() + 60,
            },
        }
        self.assertEqual(
            _validated_spec(json.dumps(spec).encode())["expected_head"],
            self.head,
        )
        spec["authorization"]["target_scope"] = f"{remote}#refs/heads/main"
        with self.assertRaisesRegex(PublishError, "out of scope"):
            _validated_spec(json.dumps(spec).encode())
        spec["authorization"]["expires_at"] = "not-a-time"
        with self.assertRaisesRegex(PublishError, "expiry is invalid"):
            _validated_spec(json.dumps(spec).encode())

        (self.project / ".env").write_text("SECRET=value\n", encoding="utf-8")
        self._git("add", ".env")
        self._git("commit", "-m", "tracked secret filename")
        with self.assertRaisesRegex(PublishError, "sensitive filename"):
            _safe_tracked_tree(self.project)

    def test_clean_head_is_pushed_and_remote_sha_is_verified(self):
        bare = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            capture_output=True,
            text=True,
            check=True,
        )
        result = publish(
            self.project,
            {
                "remote_ssh": str(bare),
                "branch": "blue",
                "expected_head": self.head,
            },
        )
        self.assertTrue(result["secret_scan_passed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(result["local_head"], self.head)
        self.assertEqual(result["remote_head"], self.head)
        remote_head = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/blue"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remote_head, self.head)


if __name__ == "__main__":
    unittest.main()
