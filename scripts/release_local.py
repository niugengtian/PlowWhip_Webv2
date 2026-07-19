from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "codex/sprint-9-execution-continuity"
PROJECT = "plow-whip-web-v2"
SERVICE = "control-plane"
BRIDGE_LABEL = "com.plow-whip-web.host-bridge"
CONTROL_URL = "http://127.0.0.1:8742"
RELEASE_LABEL = "org.opencontainers.image.revision"
REQUIRED_VOLUMES = {
    "plow-whip-web-v2-data": "/data",
    "plow-whip-web-v2-projects": "/projects",
}


class ReleaseError(RuntimeError):
    pass


def _run(
    argv: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def _private_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"required private file is missing or unsafe: {path}")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ReleaseError(f"{path} must not be group/other accessible; run chmod 600")


@contextlib.contextmanager
def deployment_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ReleaseError("another local release transaction is active") from error
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ReleaseError("another local release transaction is active") from error
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            with contextlib.suppress(OSError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ReleaseError("expected SHA must be 40 lowercase hexadecimal characters")
    return value


def verify_source(expected_sha: str) -> None:
    expected_sha = _validate_sha(expected_sha)
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != expected_sha:
        raise ReleaseError(f"HEAD mismatch: expected {expected_sha}, got {head}")
    if _run(["git", "status", "--porcelain"]).stdout.strip():
        raise ReleaseError("working tree must be clean before a release build")
    remote = _run(
        ["git", "ls-remote", "origin", f"refs/heads/{BRANCH}"]
    ).stdout.split()
    if not remote or remote[0] != expected_sha:
        actual = remote[0] if remote else "missing"
        raise ReleaseError(f"remote branch mismatch: expected {expected_sha}, got {actual}")
    ignored = _run(["git", "check-ignore", "-q", ".env.local"], check=False)
    if ignored.returncode != 0:
        raise ReleaseError(".env.local is not ignored by Git")
    rules = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }
    missing = {".env", ".env.local", ".env.*.local"} - rules
    if missing:
        raise ReleaseError(f"Docker ignore rules are incomplete: {sorted(missing)}")


def _get_json(url: str, *, timeout: float = 5) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _control_plane_ids() -> list[str]:
    result = _run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={SERVICE}",
        ]
    )
    return [line for line in result.stdout.splitlines() if line]


def verify_runtime(expected_sha: str) -> dict[str, object]:
    expected_sha = _validate_sha(expected_sha)
    container_ids = _control_plane_ids()
    if len(container_ids) != 1:
        raise ReleaseError(
            f"expected one {SERVICE} container, found {len(container_ids)}"
        )
    container = json.loads(
        _run(["docker", "inspect", container_ids[0]]).stdout
    )[0]
    state = container["State"]
    if not state["Running"] or state.get("RestartCount", container.get("RestartCount", 0)):
        raise ReleaseError("control-plane is not steadily running")
    if state.get("Health", {}).get("Status") != "healthy":
        raise ReleaseError("control-plane is not healthy")
    if container.get("RestartCount", 0) != 0:
        raise ReleaseError("control-plane restart_count is not zero")
    mounts = {
        item["Name"]: item["Destination"]
        for item in container.get("Mounts", [])
        if item.get("Type") == "volume"
    }
    if any(mounts.get(name) != destination for name, destination in REQUIRED_VOLUMES.items()):
        raise ReleaseError("named volume mount set changed")
    image = json.loads(
        _run(["docker", "image", "inspect", container["Image"]]).stdout
    )[0]
    revision = image.get("Config", {}).get("Labels", {}).get(RELEASE_LABEL)
    if revision != expected_sha:
        raise ReleaseError(f"runtime image revision mismatch: {revision!r}")
    health = _get_json(f"{CONTROL_URL}/health")
    database = health.get("database", {})
    if (
        health.get("status") != "ok"
        or database.get("journal_mode") != "wal"
        or not isinstance(database.get("migration_count"), int)
        or database["migration_count"] < 1
    ):
        raise ReleaseError("HTTP health, WAL, or migration count check failed")
    return {
        "container_id": container_ids[0],
        "image_id": container["Image"],
        "release_sha": revision,
        "health": "healthy",
        "restart_count": container.get("RestartCount", 0),
        "database": {
            "journal_mode": database["journal_mode"],
            "migration_count": database["migration_count"],
        },
        "volumes": sorted(REQUIRED_VOLUMES),
    }


def deploy(expected_sha: str, lock_file: Path) -> dict[str, object]:
    env_file = ROOT / ".env.local"
    _private_file(env_file)
    with deployment_lock(lock_file):
        verify_source(expected_sha)
        environment = os.environ.copy()
        environment["PLOW_WHIP_RELEASE_SHA"] = expected_sha
        process = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env_file),
                "up",
                "--build",
                "-d",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if process.returncode != 0:
            raise ReleaseError("docker compose release transaction failed")
        deadline = time.monotonic() + 180
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return verify_runtime(expected_sha)
            except (
                ReleaseError,
                OSError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as error:
                last_error = error
                time.sleep(2)
        raise ReleaseError(f"release did not become healthy: {last_error}")


def bridge_plist(
    *,
    repo_root: Path,
    env_file: Path,
    project_root: Path,
    state_dir: Path,
    home: Path,
    codex_path: Path,
) -> dict[str, object]:
    python = repo_root / ".venv" / "bin" / "python"
    simple_worker = repo_root / ".venv" / "bin" / "simple-worker"
    path_entries = [
        python.parent,
        simple_worker.parent,
        codex_path.parent,
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/opt/homebrew/sbin"),
        Path("/usr/local/bin"),
        Path("/usr/local/sbin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    stable_path = ":".join(dict.fromkeys(str(path) for path in path_entries))
    log_path = state_dir.parent / "host-bridge.log"
    return {
        "Label": BRIDGE_LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "plow_whip_web.host_bridge",
            "--env-file",
            str(env_file),
            "--project-root",
            str(project_root),
            "--state-dir",
            str(state_dir),
        ],
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {"PATH": stable_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def _port_listener_pids(port: int) -> list[int]:
    result = _run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        check=False,
    )
    return sorted({int(line) for line in result.stdout.splitlines() if line.isdigit()})


def _wait_for_port(port: int, *, listening: bool, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            current = sock.connect_ex(("127.0.0.1", port)) == 0
        if current is listening:
            return
        time.sleep(0.2)
    state = "listen" if listening else "stop listening"
    raise ReleaseError(f"port {port} did not {state} in time")


def _ensure_no_active_host_jobs() -> None:
    scheduler = _get_json(f"{CONTROL_URL}/api/scheduler/status")
    active = (
        scheduler.get("runtime", {})
        .get("last_result", {})
        .get("host_jobs", {})
        .get("active")
    )
    if active != 0:
        raise ReleaseError(f"refusing Bridge replacement with active Host Jobs: {active}")


def install_bridge_macos(project_root: Path, lock_file: Path) -> dict[str, object]:
    if sys.platform != "darwin":
        raise ReleaseError("persistent Bridge installation is supported on macOS only")
    env_file = ROOT / ".env.local"
    _private_file(env_file)
    python = ROOT / ".venv" / "bin" / "python"
    simple_worker = ROOT / ".venv" / "bin" / "simple-worker"
    codex = shutil.which("codex")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ReleaseError(f"Bridge Python is not executable: {python}")
    if not simple_worker.is_file() or not os.access(simple_worker, os.X_OK):
        raise ReleaseError(f"simple-worker is not executable: {simple_worker}")
    if not codex:
        raise ReleaseError("codex is not available in the current login PATH")
    home = Path.home()
    state_dir = home / ".plow-whip-web" / "host-bridge"
    agents_dir = home / "Library" / "LaunchAgents"
    plist_path = agents_dir / f"{BRIDGE_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{BRIDGE_LABEL}"
    with deployment_lock(lock_file):
        _ensure_no_active_host_jobs()
        payload = bridge_plist(
            repo_root=ROOT,
            env_file=env_file,
            project_root=project_root.resolve(),
            state_dir=state_dir,
            home=home,
            codex_path=Path(codex).resolve(),
        )
        agents_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        temporary = plist_path.with_suffix(".plist.tmp")
        with temporary.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        temporary.chmod(0o600)
        temporary.replace(plist_path)
        _run(["launchctl", "bootout", target], check=False)
        if _port_listener_pids(8765):
            _wait_for_port(8765, listening=False)
        _run(["launchctl", "bootstrap", domain, str(plist_path)])
        _run(["launchctl", "enable", target])
        _wait_for_port(8765, listening=True)
        pids = _port_listener_pids(8765)
        if len(pids) != 1:
            raise ReleaseError(f"expected one Bridge listener, found {len(pids)}")
    return {
        "label": BRIDGE_LABEL,
        "plist": str(plist_path),
        "listener_count": 1,
        "pid": pids[0],
        "env_file": str(env_file),
        "project_root": str(project_root.resolve()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-writer local release and macOS Host Bridge entry point."
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path.home() / ".plow-whip-web" / "release.lock",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("deploy", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--expected-sha", required=True)
    bridge = subparsers.add_parser("install-bridge-macos")
    bridge.add_argument("--project-root", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "deploy":
            result = deploy(args.expected_sha, args.lock_file)
        elif args.command == "verify":
            verify_source(args.expected_sha)
            result = verify_runtime(args.expected_sha)
        else:
            result = install_bridge_macos(args.project_root, args.lock_file)
    except (ReleaseError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"release check failed: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
