from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


VERSION = "plowwhip-git-publish 1"
MAX_SPEC_BYTES = 65_536
MAX_SCAN_BYTES = 64 * 1_048_576
REMOTE = re.compile(
    r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$"
)
BRANCH = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,126}[A-Za-z0-9])?$")
SECRET = re.compile(
    rb"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    rb"\bsk-[A-Za-z0-9_-]{20,}\b|"
    rb"\bghp_[A-Za-z0-9]{20,}\b|"
    rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    rb"\bAKIA[0-9A-Z]{16}\b)"
)


class PublishError(RuntimeError):
    pass


def _git(
    cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise PublishError(detail[-1][:500] if detail else f"git {args[0]} failed")
    return completed


def _validated_spec(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_SPEC_BYTES:
        raise PublishError("publish spec is empty or too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PublishError("publish spec is not valid JSON") from error
    if not isinstance(value, dict) or value.get("kind") != "git_publish":
        raise PublishError("publish spec kind is invalid")
    remote = str(value.get("remote_ssh") or "")
    branch = str(value.get("branch") or "")
    expected_head = str(value.get("expected_head") or "")
    authorization = value.get("authorization")
    if not REMOTE.fullmatch(remote):
        raise PublishError("only an exact GitHub SSH remote is allowed")
    if (
        not BRANCH.fullmatch(branch)
        or ".." in branch
        or branch.endswith((".lock", ".", "/"))
        or branch.startswith((".", "/"))
    ):
        raise PublishError("branch is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise PublishError("expected_head must be a full Git commit SHA")
    scope = f"{remote}#refs/heads/{branch}"
    try:
        expires_at = float(
            authorization.get("expires_at") if isinstance(authorization, dict) else 0
        )
    except (TypeError, ValueError) as error:
        raise PublishError("publish authorization expiry is invalid") from error
    if not isinstance(authorization, dict) or not (
        authorization.get("action_kind") == "git_publish"
        and authorization.get("target_scope") == scope
        and expires_at >= time.time()
    ):
        raise PublishError("publish authorization is missing, expired, or out of scope")
    return value


def _safe_tracked_tree(cwd: Path) -> dict[str, int | bool]:
    status = _git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        raise PublishError("workspace is dirty; commit selection requires a new decision")
    names = _git(cwd, "ls-files", "-z").stdout.split("\0")
    files = 0
    scanned = 0
    root = cwd.resolve()
    for name in filter(None, names):
        relative = Path(name)
        lowered = [part.lower() for part in relative.parts]
        if (
            any(part == ".env" or part.startswith(".env.") for part in lowered)
            or relative.name.lower() in {"credentials.json", "id_rsa", "id_ed25519"}
            or relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        ):
            raise PublishError(f"sensitive filename is tracked: {name}")
        path = cwd / relative
        try:
            path.resolve().relative_to(root)
        except ValueError as error:
            raise PublishError(f"tracked path escapes workspace: {name}") from error
        if path.is_symlink():
            raise PublishError(f"tracked symlink requires review: {name}")
        if not path.is_file():
            continue
        size = path.stat().st_size
        if scanned + size > MAX_SCAN_BYTES:
            raise PublishError("secret scan byte limit exceeded")
        body = path.read_bytes()
        scanned += len(body)
        if SECRET.search(body):
            raise PublishError(f"possible credential found in tracked file: {name}")
        files += 1
    return {
        "secret_scan_passed": True,
        "files_scanned": files,
        "bytes_scanned": scanned,
    }


def publish(cwd: Path, spec: dict[str, object]) -> dict[str, object]:
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip()
    if inside != "true":
        raise PublishError("workspace is not a Git repository")
    local_head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
    if local_head != spec["expected_head"]:
        raise PublishError("workspace HEAD changed after authorization")
    scan = _safe_tracked_tree(cwd)
    remote = str(spec["remote_ssh"])
    branch = str(spec["branch"])
    push = _git(cwd, "push", "--porcelain", remote, f"HEAD:refs/heads/{branch}")
    remote_lines = _git(cwd, "ls-remote", remote, f"refs/heads/{branch}").stdout.splitlines()
    remote_head = remote_lines[0].split()[0] if remote_lines else ""
    if remote_head != local_head:
        raise PublishError("remote branch SHA does not match local HEAD")
    return {
        "kind": "git_publish",
        "remote_ssh": remote,
        "branch": branch,
        "local_head": local_head,
        "remote_head": remote_head,
        "pushed": True,
        "push_summary": (push.stdout or push.stderr).strip()[-1_000:],
        **scan,
    }


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print(VERSION)
        return 0
    try:
        spec = _validated_spec(sys.stdin.buffer.read(MAX_SPEC_BYTES + 1))
        result = publish(Path.cwd(), spec)
    except (OSError, PublishError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps(
                {
                    "kind": "git_publish",
                    "status": "failed",
                    "error": str(error)[:500],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
