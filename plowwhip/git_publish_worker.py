from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path


VERSION = "plowwhip-git-publish 3"
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
    rb"\bAKIA[0-9A-Z]{16}\b|"
    rb"\bASIA[0-9A-Z]{16}\b|"
    rb"\bAIza[0-9A-Za-z_-]{35}\b|"
    rb"\bxox[baprs]-[0-9A-Za-z-]{10,}\b|"
    rb"\bglpat-[0-9A-Za-z_-]{20,}\b|"
    rb"\b(?:npm_[0-9A-Za-z]{30,}|pypi-[0-9A-Za-z_-]{50,})\b)"
)


class PublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: str = "publish_failed",
        **details: object,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _git(
    cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_SSH_COMMAND"] = _ssh_command()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
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
        raise PublishError(
            "\n".join(detail[-3:])[:500]
            if detail
            else f"git {args[0]} failed"
        )
    return completed


def _ssh_command() -> str:
    argv = ["ssh", "-o", "BatchMode=yes"]
    configured = os.environ.get("PLOW_WHIP_GIT_SSH_IDENTITY_FILE")
    if configured:
        identity = Path(configured).expanduser().resolve()
        ssh_root = (Path.home() / ".ssh").resolve()
        try:
            identity.relative_to(ssh_root)
        except ValueError as error:
            raise PublishError("Git SSH identity must be inside ~/.ssh") from error
        if not identity.is_file() or identity.stat().st_mode & 0o077:
            raise PublishError("Git SSH identity is missing or has unsafe permissions")
        argv.extend(["-o", "IdentitiesOnly=yes", "-i", str(identity)])
    return shlex.join(argv)


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
    operation = str(value.get("operation") or "publish")
    publish_mode = str(value.get("publish_mode") or "fast_forward")
    expected_remote_head = str(value.get("expected_remote_head") or "")
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
    if operation not in {"inspect", "publish"}:
        raise PublishError("operation is invalid")
    value["operation"] = operation
    if operation == "inspect":
        return value
    if publish_mode not in {"fast_forward", "force_with_lease"}:
        raise PublishError("publish_mode is invalid")
    scope = f"{remote}#refs/heads/{branch}"
    try:
        expires_at = float(
            authorization.get("expires_at") if isinstance(authorization, dict) else 0
        )
    except (TypeError, ValueError) as error:
        raise PublishError("publish authorization expiry is invalid") from error
    action_kind = (
        "git_publish_force_with_lease"
        if publish_mode == "force_with_lease"
        else "git_publish"
    )
    if not isinstance(authorization, dict) or not (
        authorization.get("action_kind") == action_kind
        and authorization.get("target_scope") == scope
        and authorization.get("expected_head") == expected_head
        and expires_at >= time.time()
    ):
        raise PublishError("publish authorization is missing, expired, or out of scope")
    if publish_mode == "force_with_lease" and not (
        re.fullmatch(r"[0-9a-f]{40}", expected_remote_head)
        and authorization.get("expected_remote_head") == expected_remote_head
    ):
        raise PublishError("force-with-lease authorization is missing the remote SHA")
    value["publish_mode"] = publish_mode
    return value


def inspect(cwd: Path, spec: dict[str, object]) -> dict[str, object]:
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree").stdout.strip()
    if inside != "true":
        raise PublishError("workspace is not a Git repository")
    local_head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
    if local_head != spec["expected_head"]:
        raise PublishError(
            "workspace HEAD changed before inspection",
            "local_head_changed",
            expected_head=spec["expected_head"],
            local_head=local_head,
        )
    remote = str(spec["remote_ssh"])
    branch = str(spec["branch"])
    lines = _git(cwd, "ls-remote", remote, f"refs/heads/{branch}").stdout.splitlines()
    remote_head = lines[0].split()[0] if lines else ""
    return {
        "kind": "git_publish_inspection",
        "remote_ssh": remote,
        "branch": branch,
        "local_head": local_head,
        "remote_head": remote_head,
        "branch_exists": bool(remote_head),
        "relationship": "match" if remote_head == local_head else "different",
        "external_write": False,
    }


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
    publish_mode = str(spec.get("publish_mode") or "fast_forward")
    remote_lines = _git(
        cwd, "ls-remote", remote, f"refs/heads/{branch}"
    ).stdout.splitlines()
    previous_remote_head = remote_lines[0].split()[0] if remote_lines else ""
    push_args = ["push", "--porcelain"]
    if publish_mode == "force_with_lease":
        expected_remote_head = str(spec.get("expected_remote_head") or "")
        if previous_remote_head != expected_remote_head:
            raise PublishError(
                "remote branch moved after the owner decision",
                "lease_mismatch",
                branch=branch,
                local_head=local_head,
                expected_remote_head=expected_remote_head,
                remote_head=previous_remote_head,
            )
        push_args.append(
            f"--force-with-lease=refs/heads/{branch}:{expected_remote_head}"
        )
    push_args.extend([remote, f"HEAD:refs/heads/{branch}"])
    push = _git(cwd, *push_args, check=False)
    if push.returncode:
        push_output = "\n".join(
            part for part in (push.stdout, push.stderr) if part
        )
        detail = push_output.strip().splitlines()
        conflict = previous_remote_head and any(
            marker in push_output.lower()
            for marker in (
                "non-fast-forward",
                "fetch first",
                "remote contains work that you do not have locally",
            )
        )
        raise PublishError(
            "\n".join(detail[-3:])[:500] if detail else "git push failed",
            "remote_history_conflict" if conflict else "push_rejected",
            branch=branch,
            local_head=local_head,
            remote_head=previous_remote_head,
        )
    remote_lines = _git(
        cwd, "ls-remote", remote, f"refs/heads/{branch}"
    ).stdout.splitlines()
    remote_head = remote_lines[0].split()[0] if remote_lines else ""
    if remote_head != local_head:
        raise PublishError("remote branch SHA does not match local HEAD")
    return {
        "kind": "git_publish",
        "remote_ssh": remote,
        "branch": branch,
        "local_head": local_head,
        "remote_head": remote_head,
        "previous_remote_head": previous_remote_head,
        "publish_mode": publish_mode,
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
        result = (
            inspect(Path.cwd(), spec)
            if spec["operation"] == "inspect"
            else publish(Path.cwd(), spec)
        )
    except (OSError, PublishError, subprocess.TimeoutExpired) as error:
        failure = {
            "kind": "git_publish",
            "status": "failed",
            "error": str(error)[:500],
        }
        if isinstance(error, PublishError):
            failure.update({"code": error.code, **error.details})
        print(
            json.dumps(failure, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
