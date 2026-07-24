import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from plowwhip.git_publish_worker import (
    PublishError,
    _safe_tracked_tree,
    _ssh_command,
    _validated_spec,
    inspect as inspect_publish,
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
                "expected_head": self.head,
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
        spec["authorization"] = {
            "action_kind": "git_publish_force_with_lease",
            "target_scope": f"{remote}#refs/heads/blue",
            "expected_head": self.head,
            "expected_remote_head": "b" * 40,
            "expires_at": time.time() + 60,
        }
        spec["publish_mode"] = "force_with_lease"
        spec["expected_remote_head"] = "b" * 40
        self.assertEqual(
            _validated_spec(json.dumps(spec).encode())["publish_mode"],
            "force_with_lease",
        )
        spec["authorization"]["expected_remote_head"] = "c" * 40
        with self.assertRaisesRegex(PublishError, "remote SHA"):
            _validated_spec(json.dumps(spec).encode())
        inspected = _validated_spec(
            json.dumps(
                {
                    "kind": "git_publish",
                    "operation": "inspect",
                    "remote_ssh": remote,
                    "branch": "blue",
                    "expected_head": self.head,
                }
            ).encode()
        )
        self.assertEqual(inspected["operation"], "inspect")

        (self.project / ".env").write_text("SECRET=value\n", encoding="utf-8")
        self._git("add", ".env")
        self._git("commit", "-m", "tracked secret filename")
        with self.assertRaisesRegex(PublishError, "sensitive filename"):
            _safe_tracked_tree(self.project)

    def test_common_high_confidence_secret_patterns_are_rejected(self):
        patterns = {
            "aws.txt": "ASIAABCDEFGHIJKLMNOP",
            "google.txt": "AIza" + "A" * 35,
            "slack.txt": "xoxb-" + "A" * 20,
            "gitlab.txt": "glpat-" + "A" * 20,
        }
        for name, secret in patterns.items():
            with self.subTest(name=name):
                path = self.project / name
                path.write_text(secret + "\n", encoding="utf-8")
                self._git("add", name)
                self._git("commit", "-m", name)
                with self.assertRaisesRegex(PublishError, "possible credential"):
                    _safe_tracked_tree(self.project)
                self._git("rm", name)
                self._git("commit", "-m", f"remove {name}")

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

    def test_inspection_reads_heads_without_pushing(self):
        bare = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            capture_output=True,
            text=True,
            check=True,
        )
        publish(
            self.project,
            {
                "remote_ssh": str(bare),
                "branch": "blue",
                "expected_head": self.head,
            },
        )
        (self.project / "LOCAL.md").write_text("local\n", encoding="utf-8")
        self._git("add", "LOCAL.md")
        self._git("commit", "-m", "local only")
        local_head = self._git("rev-parse", "HEAD").stdout.strip()
        result = inspect_publish(
            self.project,
            {
                "remote_ssh": str(bare),
                "branch": "blue",
                "expected_head": local_head,
            },
        )
        self.assertEqual(result["kind"], "git_publish_inspection")
        self.assertEqual(result["relationship"], "different")
        self.assertFalse(result["external_write"])
        self.assertEqual(result["local_head"], local_head)
        self.assertEqual(result["remote_head"], self.head)
        remote_head = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/blue"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(remote_head, self.head)

    def test_diverged_remote_requires_an_exact_force_with_lease(self):
        bare = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            capture_output=True,
            text=True,
            check=True,
        )
        publish(
            self.project,
            {
                "remote_ssh": str(bare),
                "branch": "blue",
                "expected_head": self.head,
            },
        )
        other = self.root / "other"
        subprocess.run(
            ["git", "clone", "--branch", "blue", str(bare), str(other)],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "other@example.invalid"],
            cwd=other,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Other"],
            cwd=other,
            capture_output=True,
            text=True,
            check=True,
        )
        (other / "REMOTE.md").write_text("remote\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "REMOTE.md"],
            cwd=other,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "remote change"],
            cwd=other,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "blue"],
            cwd=other,
            capture_output=True,
            text=True,
            check=True,
        )
        remote_head = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/blue"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        (self.project / "LOCAL.md").write_text("local\n", encoding="utf-8")
        self._git("add", "LOCAL.md")
        self._git("commit", "-m", "local change")
        local_head = self._git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(PublishError) as raised:
            publish(
                self.project,
                {
                    "remote_ssh": str(bare),
                    "branch": "blue",
                    "expected_head": local_head,
                },
            )
        self.assertEqual(raised.exception.code, "remote_history_conflict")
        self.assertEqual(raised.exception.details["remote_head"], remote_head)

        with self.assertRaises(PublishError) as stale:
            publish(
                self.project,
                {
                    "remote_ssh": str(bare),
                    "branch": "blue",
                    "expected_head": local_head,
                    "publish_mode": "force_with_lease",
                    "expected_remote_head": "f" * 40,
                },
            )
        self.assertEqual(stale.exception.code, "lease_mismatch")

        result = publish(
            self.project,
            {
                "remote_ssh": str(bare),
                "branch": "blue",
                "expected_head": local_head,
                "publish_mode": "force_with_lease",
                "expected_remote_head": remote_head,
            },
        )
        self.assertEqual(result["publish_mode"], "force_with_lease")
        self.assertEqual(result["previous_remote_head"], remote_head)
        self.assertEqual(result["remote_head"], local_head)

    def test_identity_selection_is_scoped_to_a_private_ssh_file(self):
        ssh_root = self.root / ".ssh"
        ssh_root.mkdir()
        identity = ssh_root / "id_ed25519"
        identity.write_text("fixture", encoding="utf-8")
        identity.chmod(0o600)
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "PLOW_WHIP_GIT_SSH_IDENTITY_FILE": str(identity),
            },
            clear=True,
        ):
            command = _ssh_command()
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn(str(identity), command)

        outside = self.root / "outside-key"
        outside.write_text("fixture", encoding="utf-8")
        outside.chmod(0o600)
        with (
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root),
                    "PLOW_WHIP_GIT_SSH_IDENTITY_FILE": str(outside),
                },
                clear=True,
            ),
            self.assertRaisesRegex(PublishError, "inside ~/.ssh"),
        ):
            _ssh_command()


if __name__ == "__main__":
    unittest.main()
