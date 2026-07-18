#!/usr/bin/env python3
"""Unified local lifecycle tool for PlowWhip Web V2.

The default source is the latest commit on GitHub's main branch. Remote releases
are checked out below a tool-owned runtime directory, so this command never
switches, resets, or cleans the developer's current worktree.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from typing import Iterator, Sequence
import urllib.error
import urllib.request


TOOL_ROOT = Path(__file__).resolve().parents[1]
CLI_VERSION = "0.2.0"
CLI_MARKER = "# managed-by: plow-whip-web"
DEFAULT_RUNTIME_DIR = Path.home() / ".plow-whip-web"
DEFAULT_REPOSITORY = "https://github.com/niugengtian/PlowWhip_Webv2.git"
DEFAULT_REF = "main"
PROJECT = "plow-whip-web-v2"
SERVICE = "control-plane"
IMAGE = "plow-whip-web-v2:local"
CONTROL_URL = "http://127.0.0.1:8742"
BRIDGE_LABEL = "com.plow-whip-web.host-bridge"
BRIDGE_PORT = 8765
RELEASE_LABEL = "org.opencontainers.image.revision"


def _manual_text() -> str:
    return f"""PLOW-WHIP-WEB(1)                 PlowWhip Web V2                 PLOW-WHIP-WEB(1)

NAME
    plow-whip-web - manage PlowWhip Web V2 source, Docker runtime, and Host Bridge

VERSION
    {CLI_VERSION}

SYNOPSIS
    plow-whip-web -h
    plow-whip-web -v
    plow-whip-web -man
    plow-whip-web COMMAND [OPTIONS]

QUICK START
    plow-whip-web init
    plow-whip-web configure --project-root /Users/you/work
    plow-whip-web rebuild
    plow-whip-web status

SOURCE AND BUILD
    rebuild
        Fetch the configured source, rebuild the Docker image, recreate the
        control-plane container, restart the Bridge with the same release,
        and verify the runtime.

        The default source is the latest GitHub main branch. Use --ref for a
        branch, tag, or commit; use --source local --local-source PATH for a
        local worktree. Local worktrees are never reset or cleaned.

    Common examples:
        plow-whip-web rebuild --pull
        plow-whip-web rebuild --no-cache
        plow-whip-web rebuild --ref release/v1
        plow-whip-web rebuild --source local --local-source "$PWD"

LIFECYCLE
    plow-whip-web start all
    plow-whip-web start all --latest
    plow-whip-web restart container
    plow-whip-web restart bridge
    plow-whip-web restart all
    plow-whip-web stop all

    Lifecycle changes are refused while live, unconsumed Host Jobs exist.
    --force bypasses that guard and should only be used after operator review.

CONFIGURATION
    plow-whip-web configure --project-root PATH
    plow-whip-web configure --source github --ref main
    plow-whip-web configure --rotate-bridge-token
    plow-whip-web configure --deepseek-key-file PRIVATE_FILE

    Configuration and secrets are stored under ~/.plow-whip-web with private
    permissions. Provider secrets are not copied into Git, Docker images, or
    the launchd plist.

STATUS
    plow-whip-web status

    Reports the prepared release, container health, Bridge readiness, database
    health, and live Host Job count. Status is read-only.

UNINSTALL
    plow-whip-web uninstall all
        Remove the LaunchAgent and container while preserving named volumes,
        the image, configuration, and managed releases.

    plow-whip-web uninstall all --remove-image
        Also remove the local image.

    plow-whip-web uninstall all --purge-data --purge-runtime --remove-image --yes
        Permanently remove named volumes and managed runtime state. Destructive
        purge options require --yes.

CLI INSTALLATION
    plow-whip-web install-cli
    plow-whip-web uninstall-cli

    The default command location is ~/.local/bin/plow-whip-web. The installer
    refuses to overwrite an unmanaged command unless --force is explicitly used.

FILES
    ~/.plow-whip-web/ops.json       non-secret lifecycle configuration
    ~/.plow-whip-web/.env.local     private Bridge/provider environment
    ~/.plow-whip-web/releases/      managed immutable source releases
    ~/.plow-whip-web/host-bridge/   sanitized Host Job state
    ~/.plow-whip-web/ops.lock       single-writer lifecycle lock

MORE HELP
    plow-whip-web COMMAND -h
    Documentation: docs/OPS_TOOLKIT.zh-CN.md
"""


class _ManualAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        parser._print_message(_manual_text(), sys.stdout)
        parser.exit()


class OpsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "ops.json"

    @property
    def env_file(self) -> Path:
        return self.root / ".env.local"

    @property
    def lock_file(self) -> Path:
        return self.root / "ops.lock"

    @property
    def source_cache(self) -> Path:
        return self.root / "source-cache.git"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def venvs(self) -> Path:
        return self.root / "venvs"

    @property
    def current(self) -> Path:
        return self.root / "current.json"

    @property
    def bridge_state(self) -> Path:
        return self.root / "host-bridge"

    @property
    def bridge_log(self) -> Path:
        return self.root / "host-bridge.log"


@dataclass(frozen=True, slots=True)
class Release:
    source_dir: Path
    sha: str
    source_mode: str
    repository: str | None
    ref: str | None
    venv_dir: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "source_dir": str(self.source_dir),
            "sha": self.sha,
            "source_mode": self.source_mode,
            "repository": self.repository,
            "ref": self.ref,
            "venv_dir": str(self.venv_dir),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }


class Runner:
    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in argv]
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=check,
            text=True,
            capture_output=capture,
        )

    @staticmethod
    def which(name: str) -> str | None:
        return shutil.which(name)


def _default_config() -> dict[str, object]:
    return {
        "source": {
            "mode": "github",
            "repository": DEFAULT_REPOSITORY,
            "ref": DEFAULT_REF,
            "local_path": str(TOOL_ROOT),
        },
        "bridge": {
            "project_roots": [str(TOOL_ROOT.parent.resolve())],
            "url": f"http://host.docker.internal:{BRIDGE_PORT}",
            "port": BRIDGE_PORT,
        },
        "timezone": "Asia/Shanghai",
    }


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def _load_config(paths: RuntimePaths, *, create: bool = True) -> dict[str, object]:
    if not paths.config.exists():
        config = _default_config()
        if create:
            _write_private_json(paths.config, config)
        return config
    try:
        config = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OpsError(f"invalid configuration file: {paths.config}: {error}") from error
    if not isinstance(config, dict):
        raise OpsError(f"configuration root must be an object: {paths.config}")
    return config


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    _require_private_file(path)
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OpsError(f"{path}:{number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise OpsError(f"{path}:{number}: invalid environment variable name")
        values[name] = value.strip()
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise OpsError(f"environment value for {name} contains a newline")
    ordered = ["PLOW_WHIP_BRIDGE_TOKEN", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"]
    names = [name for name in ordered if name in values]
    names.extend(sorted(set(values) - set(names)))
    lines = [
        "# Managed by scripts/plow_whip_ops.py. Keep this file private.",
        "# Edit optional provider variables here, then run: restart bridge",
        "",
    ]
    lines.extend(f"{name}={values[name]}" for name in names)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _ensure_env(paths: RuntimePaths, config: dict[str, object]) -> dict[str, str]:
    values = _read_env(paths.env_file)
    values.setdefault("PLOW_WHIP_BRIDGE_TOKEN", secrets.token_hex(24))
    values.setdefault("DEEPSEEK_API_KEY", "")
    values.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
    bridge = _mapping(config, "bridge")
    values["PLOW_WHIP_BRIDGE_URL"] = str(
        bridge.get("url", f"http://host.docker.internal:{BRIDGE_PORT}")
    )
    values["TZ"] = str(config.get("timezone", "Asia/Shanghai"))
    _write_env(paths.env_file, values)
    return values


def _require_private_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise OpsError(f"required private file is missing or unsafe: {path}")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise OpsError(f"{path} must have mode 600")


def _mapping(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise OpsError(f"configuration field {name!r} must be an object")
    return value


@contextlib.contextmanager
def _operation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OpsError("another PlowWhip lifecycle operation is active") from error
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _require_commands(runner: Runner, *names: str) -> None:
    missing = [name for name in names if not runner.which(name)]
    if missing:
        raise OpsError(f"missing required commands: {', '.join(missing)}")


def _git_output(runner: Runner, argv: Sequence[str], *, cwd: Path | None = None) -> str:
    return runner.run(argv, cwd=cwd, capture=True).stdout.strip()


def _validate_source(source_dir: Path) -> None:
    required = ("compose.yaml", "Dockerfile", "pyproject.toml", ".dockerignore")
    missing = [name for name in required if not (source_dir / name).is_file()]
    if missing:
        raise OpsError(f"source is missing required files: {', '.join(missing)}")
    rules = {
        line.strip()
        for line in (source_dir / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }
    missing_rules = {".env", ".env.local", ".env.*.local"} - rules
    if missing_rules:
        raise OpsError(f"unsafe Docker context; missing ignore rules: {sorted(missing_rules)}")


def _resolve_remote_sha(runner: Runner, cache: Path, ref: str) -> str:
    candidates = (ref, f"refs/heads/{ref}", f"refs/tags/{ref}")
    for candidate in candidates:
        result = runner.run(
            ["git", "--git-dir", cache, "rev-parse", "--verify", f"{candidate}^{{commit}}"],
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    raise OpsError(f"remote ref does not resolve to a commit: {ref}")


def _prepare_release(
    paths: RuntimePaths,
    config: dict[str, object],
    runner: Runner,
    *,
    source_mode: str | None = None,
    repository: str | None = None,
    ref: str | None = None,
    local_source: Path | None = None,
) -> Release:
    _require_commands(runner, "git", "docker")
    source = _mapping(config, "source")
    mode = source_mode or str(source.get("mode", "github"))
    selected_ref = ref or str(source.get("ref", DEFAULT_REF))
    selected_repository = repository or str(source.get("repository", DEFAULT_REPOSITORY))
    if mode == "local":
        source_dir = (local_source or Path(str(source.get("local_path", TOOL_ROOT)))).expanduser().resolve()
        _validate_source(source_dir)
        sha = _git_output(runner, ["git", "rev-parse", "HEAD"], cwd=source_dir)
        venv_key = f"local-{sha[:12]}"
        return Release(source_dir, sha, mode, None, None, paths.venvs / venv_key)
    if mode != "github":
        raise OpsError("source mode must be 'github' or 'local'")

    paths.releases.mkdir(parents=True, exist_ok=True)
    if not paths.source_cache.exists():
        runner.run(["git", "clone", "--mirror", selected_repository, paths.source_cache])
    else:
        runner.run(
            ["git", "--git-dir", paths.source_cache, "remote", "set-url", "origin", selected_repository]
        )
        runner.run(["git", "--git-dir", paths.source_cache, "fetch", "--prune", "origin"])
    sha = _resolve_remote_sha(runner, paths.source_cache, selected_ref)
    source_dir = paths.releases / sha
    if not source_dir.exists():
        temporary = paths.releases / f".{sha}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            runner.run(["git", "clone", "--no-checkout", "--dissociate", paths.source_cache, temporary])
            runner.run(["git", "checkout", "--detach", sha], cwd=temporary)
            temporary.replace(source_dir)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    _validate_source(source_dir)
    actual = _git_output(runner, ["git", "rev-parse", "HEAD"], cwd=source_dir)
    if actual != sha:
        raise OpsError(f"managed release SHA mismatch: expected {sha}, got {actual}")
    return Release(source_dir, sha, mode, selected_repository, selected_ref, paths.venvs / sha)


def _prepare_bridge_runtime(release: Release, runner: Runner) -> None:
    python = release.venv_dir / "bin" / "python"
    if not python.exists():
        release.venv_dir.parent.mkdir(parents=True, exist_ok=True)
        runner.run([sys.executable, "-m", "venv", release.venv_dir])
    runner.run(
        [python, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", release.source_dir]
    )
    worker = release.venv_dir / "bin" / "simple-worker"
    if not worker.is_file():
        raise OpsError(f"Bridge runtime installation is incomplete: {worker}")


def _save_current(paths: RuntimePaths, release: Release) -> None:
    _write_private_json(paths.current, release.as_dict())


def _load_current(paths: RuntimePaths) -> Release:
    if not paths.current.is_file():
        raise OpsError("PlowWhip is not initialized; run: init")
    try:
        payload = json.loads(paths.current.read_text(encoding="utf-8"))
        release = Release(
            source_dir=Path(payload["source_dir"]),
            sha=str(payload["sha"]),
            source_mode=str(payload["source_mode"]),
            repository=payload.get("repository"),
            ref=payload.get("ref"),
            venv_dir=Path(payload["venv_dir"]),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OpsError(f"invalid current release metadata: {paths.current}") from error
    _validate_source(release.source_dir)
    return release


def _compose(release: Release, paths: RuntimePaths, *args: str) -> list[str | Path]:
    command: list[str | Path] = ["docker", "compose"]
    if paths.env_file.is_file():
        command.extend(("--env-file", paths.env_file))
    command.extend(("-f", release.source_dir / "compose.yaml", *args))
    return command


def _release_environment(release: Release) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PLOW_WHIP_RELEASE_SHA"] = release.sha
    return environment


def _get_json(url: str, *, timeout: float = 5) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise OpsError(f"expected JSON object from {url}")
    return payload


def _wait_control_plane(release: Release, runner: Runner, *, timeout: float = 180) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            health = _get_json(f"{CONTROL_URL}/health")
            if health.get("status") != "ok":
                raise OpsError("control-plane health status is not ok")
            database = health.get("database")
            if not isinstance(database, dict) or database.get("journal_mode") != "wal":
                raise OpsError("control-plane database is not in WAL mode")
            revision = runner.run(
                ["docker", "image", "inspect", IMAGE, "--format", f'{{{{ index .Config.Labels "{RELEASE_LABEL}" }}}}'],
                capture=True,
            ).stdout.strip()
            if revision != release.sha:
                raise OpsError(f"runtime image revision mismatch: {revision!r}")
            return health
        except (OpsError, OSError, urllib.error.URLError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            last_error = error
            time.sleep(2)
    raise OpsError(f"control-plane did not become healthy: {last_error}")


def _active_host_jobs(runner: Runner | None = None) -> int | None:
    if runner is not None and runner.which("docker"):
        containers = runner.run(
            [
                "docker", "ps", "-q",
                "--filter", f"label=com.docker.compose.project={PROJECT}",
                "--filter", f"label=com.docker.compose.service={SERVICE}",
            ],
            capture=True,
            check=False,
        )
        ids = [line for line in containers.stdout.splitlines() if line.strip()]
        if len(ids) == 1:
            query = (
                "import sqlite3;"
                "c=sqlite3.connect('/data/plow-whip-web.sqlite3');"
                "print(c.execute('SELECT COUNT(*) FROM host_jobs WHERE consumed_at IS NULL').fetchone()[0])"
            )
            result = runner.run(
                ["docker", "exec", ids[0], "python", "-c", query],
                capture=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
    try:
        scheduler = _get_json(f"{CONTROL_URL}/api/scheduler/status")
    except (OSError, urllib.error.URLError, json.JSONDecodeError, OpsError):
        return None
    runtime = scheduler.get("runtime")
    last_result = runtime.get("last_result") if isinstance(runtime, dict) else None
    host_jobs = last_result.get("host_jobs") if isinstance(last_result, dict) else None
    active = host_jobs.get("active") if isinstance(host_jobs, dict) else None
    return active if isinstance(active, int) else None


def _guard_host_jobs(runner: Runner, *, force: bool) -> None:
    active = _active_host_jobs(runner)
    if active and not force:
        raise OpsError(f"refusing lifecycle change while {active} Host Job(s) are active; use --force only after review")


def _bridge_token(paths: RuntimePaths) -> str:
    token = _read_env(paths.env_file).get("PLOW_WHIP_BRIDGE_TOKEN", "")
    if len(token) < 24:
        raise OpsError("PLOW_WHIP_BRIDGE_TOKEN must contain at least 24 characters")
    return token


def _bridge_probe(paths: RuntimePaths, config: dict[str, object], *, timeout: float = 5) -> dict[str, object]:
    bridge = _mapping(config, "bridge")
    port = int(bridge.get("port", BRIDGE_PORT))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/probe",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {_bridge_token(paths)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise OpsError("Host Bridge probe returned an invalid response")
    return payload


def _wait_bridge(paths: RuntimePaths, config: dict[str, object], *, listening: bool, timeout: float = 25) -> None:
    port = int(_mapping(config, "bridge").get("port", BRIDGE_PORT))
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
            if not listening:
                time.sleep(0.2)
                continue
            _bridge_probe(paths, config)
            return
        except (OSError, urllib.error.URLError, json.JSONDecodeError, OpsError) as error:
            last_error = error
            if not listening:
                return
            time.sleep(0.3)
    expected = "start" if listening else "stop"
    detail = f": {last_error}" if listening and last_error else ""
    raise OpsError(f"Host Bridge did not {expected} on port {port}{detail}")


def _launchd_paths() -> tuple[Path, str, str]:
    if sys.platform != "darwin":
        raise OpsError("persistent Host Bridge management currently requires macOS launchd")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{BRIDGE_LABEL}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{BRIDGE_LABEL}.plist"
    return plist, domain, target


def _bridge_plist(release: Release, paths: RuntimePaths, config: dict[str, object], runner: Runner) -> dict[str, object]:
    bridge = _mapping(config, "bridge")
    project_roots = [Path(str(root)).expanduser().resolve() for root in bridge.get("project_roots", [])]
    if not project_roots:
        raise OpsError("at least one Bridge project root must be configured")
    python = release.venv_dir / "bin" / "python"
    worker = release.venv_dir / "bin" / "simple-worker"
    if not python.is_file() or not worker.is_file():
        raise OpsError("Bridge runtime is missing; run: init")
    path_entries = [python.parent, worker.parent]
    for executable in ("codex", "cursor", "claude"):
        resolved = runner.which(executable)
        if resolved:
            path_entries.append(Path(resolved).resolve().parent)
    path_entries.extend(
        Path(item)
        for item in (
            "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
            "/usr/local/sbin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        )
    )
    arguments: list[str] = [
        str(python), "-m", "plow_whip_web.host_bridge",
        "--env-file", str(paths.env_file),
        "--state-dir", str(paths.bridge_state),
        "--port", str(int(bridge.get("port", BRIDGE_PORT))),
    ]
    for root in project_roots:
        arguments.extend(("--project-root", str(root)))
    return {
        "Label": BRIDGE_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(release.source_dir),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(map(str, path_entries)))},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.bridge_log),
        "StandardErrorPath": str(paths.bridge_log),
    }


def _start_bridge(release: Release, paths: RuntimePaths, config: dict[str, object], runner: Runner, *, force: bool) -> dict[str, object]:
    _guard_host_jobs(runner, force=force)
    _require_private_file(paths.env_file)
    plist, domain, target = _launchd_paths()
    payload = _bridge_plist(release, paths, config, runner)
    paths.bridge_state.mkdir(parents=True, exist_ok=True)
    paths.bridge_state.chmod(0o700)
    plist.parent.mkdir(parents=True, exist_ok=True)
    temporary = plist.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.chmod(0o600)
    runner.run(["launchctl", "bootout", target], check=False, capture=True)
    try:
        _wait_bridge(paths, config, listening=False, timeout=5)
    except OpsError as error:
        raise OpsError(f"port is still owned by a non-managed Bridge; {error}") from error
    temporary.replace(plist)
    runner.run(["launchctl", "bootstrap", domain, plist])
    runner.run(["launchctl", "enable", target])
    _wait_bridge(paths, config, listening=True)
    return {"bridge": "running", "label": BRIDGE_LABEL, "plist": str(plist)}


def _stop_bridge(paths: RuntimePaths, config: dict[str, object], runner: Runner, *, uninstall: bool = False) -> dict[str, object]:
    plist, _domain, target = _launchd_paths()
    runner.run(["launchctl", "bootout", target], check=False, capture=True)
    _wait_bridge(paths, config, listening=False, timeout=5)
    if uninstall and plist.exists():
        plist.unlink()
    return {"bridge": "uninstalled" if uninstall else "stopped", "plist": str(plist)}


def _container_to_bridge(release: Release, paths: RuntimePaths, runner: Runner) -> None:
    code = (
        "import json,os,urllib.request;"
        "r=urllib.request.Request(os.environ['PLOW_WHIP_BRIDGE_URL'].rstrip('/')+'/v1/probe',"
        "data=b'{}',headers={'Authorization':'Bearer '+os.environ['PLOW_WHIP_BRIDGE_TOKEN'],"
        "'Content-Type':'application/json'},method='POST');"
        "print(json.loads(urllib.request.urlopen(r,timeout=5).read())['status'])"
    )
    runner.run(_compose(release, paths, "exec", "-T", SERVICE, "python", "-c", code))


def _rebuild(
    release: Release,
    paths: RuntimePaths,
    config: dict[str, object],
    runner: Runner,
    *,
    no_cache: bool,
    pull: bool,
    restart_bridge: bool,
    force: bool,
) -> dict[str, object]:
    _guard_host_jobs(runner, force=force)
    _require_private_file(paths.env_file)
    build_args = ["build"]
    if pull:
        build_args.append("--pull")
    if no_cache:
        build_args.append("--no-cache")
    build_args.append(SERVICE)
    environment = _release_environment(release)
    runner.run(_compose(release, paths, *build_args), env=environment)
    runner.run(
        _compose(release, paths, "up", "-d", "--no-build", "--force-recreate", SERVICE),
        env=environment,
    )
    health = _wait_control_plane(release, runner)
    _save_current(paths, release)
    bridge_result: dict[str, object] | None = None
    if restart_bridge:
        bridge_result = _start_bridge(release, paths, config, runner, force=force)
        _container_to_bridge(release, paths, runner)
    return {
        "container": "healthy",
        "release_sha": release.sha,
        "source": str(release.source_dir),
        "database": health.get("database"),
        "bridge": bridge_result,
    }


def _configure(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, object]:
    config = _load_config(paths)
    source = _mapping(config, "source")
    bridge = _mapping(config, "bridge")
    if args.source:
        source["mode"] = args.source
    if args.repo:
        source["repository"] = args.repo
    if args.ref:
        source["ref"] = args.ref
    if args.local_source:
        source["local_path"] = str(args.local_source.expanduser().resolve())
    if args.project_root:
        roots = [str(path.expanduser().resolve()) for path in args.project_root]
        if len(set(roots)) != len(roots):
            raise OpsError("duplicate project roots are not allowed")
        bridge["project_roots"] = roots
    if args.timezone:
        config["timezone"] = args.timezone
    if args.bridge_url:
        bridge["url"] = args.bridge_url
    if args.bridge_port:
        if not 1 <= args.bridge_port <= 65535:
            raise OpsError("Bridge port must be between 1 and 65535")
        bridge["port"] = args.bridge_port
        if not args.bridge_url:
            bridge["url"] = f"http://host.docker.internal:{args.bridge_port}"
    _write_private_json(paths.config, config)
    values = _ensure_env(paths, config)
    if args.rotate_bridge_token:
        values["PLOW_WHIP_BRIDGE_TOKEN"] = secrets.token_hex(24)
    if args.deepseek_key_file:
        _require_private_file(args.deepseek_key_file.expanduser())
        key = args.deepseek_key_file.expanduser().read_text(encoding="utf-8").strip()
        if not key:
            raise OpsError("DeepSeek key file is empty")
        values["DEEPSEEK_API_KEY"] = key
    if args.clear_deepseek_key:
        values["DEEPSEEK_API_KEY"] = ""
    if args.deepseek_model:
        values["DEEPSEEK_MODEL"] = args.deepseek_model
    _write_env(paths.env_file, values)
    return {
        "config": str(paths.config),
        "env_file": str(paths.env_file),
        "source": source,
        "bridge": bridge,
        "timezone": config["timezone"],
        "bridge_token": "configured",
        "deepseek_key": "configured" if values.get("DEEPSEEK_API_KEY") else "not configured",
    }


def _status(paths: RuntimePaths, config: dict[str, object], runner: Runner) -> dict[str, object]:
    release: Release | None = None
    with contextlib.suppress(OpsError):
        release = _load_current(paths)
    container = "unknown"
    health: dict[str, object] | None = None
    try:
        health = _get_json(f"{CONTROL_URL}/health", timeout=2)
        container = "healthy" if health.get("status") == "ok" else "unhealthy"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, OpsError):
        container = "stopped"
    try:
        probe = _bridge_probe(paths, config, timeout=2)
        bridge = "ready" if probe.get("status") else "reachable"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, OpsError):
        bridge = "stopped"
    return {
        "release_sha": release.sha if release else None,
        "source": str(release.source_dir) if release else None,
        "container": container,
        "bridge": bridge,
        "health": health,
        "active_host_jobs": _active_host_jobs(runner),
        "data_preserved": True,
    }


def _cli_destination(bin_dir: Path) -> Path:
    return bin_dir.expanduser().resolve() / "plow-whip-web"


def _install_cli(bin_dir: Path, *, force: bool) -> dict[str, object]:
    destination = _cli_destination(bin_dir)
    if destination.exists():
        existing = destination.read_text(encoding="utf-8", errors="replace")
        if CLI_MARKER not in existing and not force:
            raise OpsError(
                f"refusing to replace an unmanaged command: {destination}; "
                "use --force only after inspecting it"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher = "\n".join(
        (
            "#!/bin/sh",
            CLI_MARKER,
            "set -eu",
            f"exec {shlex.quote(sys.executable)} "
            f"{shlex.quote(str(Path(__file__).resolve()))} \"$@\"",
            "",
        )
    )
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(launcher, encoding="utf-8")
    temporary.chmod(0o755)
    temporary.replace(destination)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    return {
        "cli": "installed",
        "command": str(destination),
        "version": CLI_VERSION,
        "on_path": str(destination.parent) in path_entries,
        "path_hint": (
            None
            if str(destination.parent) in path_entries
            else f'add to shell profile: export PATH="{destination.parent}:$PATH"'
        ),
    }


def _uninstall_cli(bin_dir: Path) -> dict[str, object]:
    destination = _cli_destination(bin_dir)
    if not destination.exists():
        return {"cli": "not installed", "command": str(destination)}
    existing = destination.read_text(encoding="utf-8", errors="replace")
    if CLI_MARKER not in existing:
        raise OpsError(f"refusing to remove an unmanaged command: {destination}")
    destination.unlink()
    return {"cli": "uninstalled", "command": str(destination)}


def _source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", choices=("github", "local"))
    parser.add_argument("--repo", help=f"Git repository URL (default: {DEFAULT_REPOSITORY})")
    parser.add_argument("--ref", help=f"branch, tag, or commit (default: {DEFAULT_REF})")
    parser.add_argument("--local-source", type=Path, help="local source path; never reset or clean it")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plow-whip-web",
        description=(
            "PlowWhip Web V2 lifecycle CLI — manage GitHub source, Docker container, "
            "and the macOS Host Bridge."
        ),
        epilog=(
            "Examples:\n"
            "  plow-whip-web init\n"
            "  plow-whip-web rebuild --pull\n"
            "  plow-whip-web restart all\n"
            "  plow-whip-web status\n"
            "  plow-whip-web uninstall all\n"
            "\n"
            "Run 'plow-whip-web COMMAND -h' for command-specific options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {CLI_VERSION}",
        help="show version and exit",
    )
    parser.add_argument(
        "-man",
        "--manual",
        action=_ManualAction,
        nargs=0,
        help="show the full operations manual and exit",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=DEFAULT_RUNTIME_DIR,
        help=f"managed config/source/runtime directory (default: {DEFAULT_RUNTIME_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser(
        "init",
        help="initialize config, source, and Bridge runtime",
        description="Create private config, fetch the selected source, and prepare the Bridge venv.",
    )
    _source_options(initialize)

    configure = subparsers.add_parser(
        "configure",
        help="configure source, Bridge, timezone, and providers",
    )
    _source_options(configure)
    configure.add_argument("--project-root", type=Path, action="append", help="replace Bridge allowlisted roots; repeatable")
    configure.add_argument("--timezone")
    configure.add_argument("--bridge-url")
    configure.add_argument("--bridge-port", type=int)
    configure.add_argument("--rotate-bridge-token", action="store_true")
    configure.add_argument("--deepseek-key-file", type=Path, help="read the secret from a mode-protected file")
    configure.add_argument("--clear-deepseek-key", action="store_true")
    configure.add_argument("--deepseek-model")

    rebuild = subparsers.add_parser(
        "rebuild",
        help="fetch source, rebuild image, and recreate runtime",
    )
    _source_options(rebuild)
    rebuild.add_argument("--no-cache", action="store_true")
    rebuild.add_argument("--pull", action="store_true", help="pull newer Docker base images")
    rebuild.add_argument("--skip-bridge", action="store_true", help="do not install/restart Bridge with the same release")
    rebuild.add_argument("--force", action="store_true", help="allow lifecycle change despite reported active Host Jobs")

    lifecycle_help = {
        "start": "start container, Bridge, or all components",
        "restart": "restart container, Bridge, or all components",
        "stop": "stop container, Bridge, or all components",
    }
    for name in ("start", "restart", "stop"):
        command = subparsers.add_parser(name, help=lifecycle_help[name])
        command.add_argument("target", choices=("all", "container", "bridge"))
        command.add_argument("--force", action="store_true")
        if name == "start":
            command.add_argument("--latest", action="store_true", help="rebuild from configured source before starting all")
            command.add_argument("--pull", action="store_true")
            command.add_argument("--no-cache", action="store_true")

    uninstall = subparsers.add_parser(
        "uninstall",
        help="uninstall components; preserve data unless explicitly purged",
    )
    uninstall.add_argument("target", choices=("all", "container", "bridge"))
    uninstall.add_argument("--purge-data", action="store_true", help="delete named Docker volumes")
    uninstall.add_argument("--purge-runtime", action="store_true", help="delete managed source, venv, config, and Bridge state")
    uninstall.add_argument("--remove-image", action="store_true")
    uninstall.add_argument("--yes", action="store_true", help="required with destructive purge options")
    uninstall.add_argument("--force", action="store_true")

    subparsers.add_parser("status", help="show release, container, Bridge, and Host Job status")
    subparsers.add_parser("manual", help="show the full operations manual")

    install_cli = subparsers.add_parser(
        "install-cli",
        help="install the plow-whip-web command into a user bin directory",
    )
    install_cli.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="command directory (default: ~/.local/bin)",
    )
    install_cli.add_argument(
        "--force",
        action="store_true",
        help="replace an existing unmanaged command after manual inspection",
    )

    uninstall_cli = subparsers.add_parser(
        "uninstall-cli",
        help="remove only the CLI entry installed by install-cli",
    )
    uninstall_cli.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        help="command directory (default: ~/.local/bin)",
    )
    return parser


def _release_from_args(args: argparse.Namespace, paths: RuntimePaths, config: dict[str, object], runner: Runner) -> Release:
    return _prepare_release(
        paths,
        config,
        runner,
        source_mode=getattr(args, "source", None),
        repository=getattr(args, "repo", None),
        ref=getattr(args, "ref", None),
        local_source=getattr(args, "local_source", None),
    )


def execute(args: argparse.Namespace, *, runner: Runner | None = None) -> dict[str, object]:
    runner = runner or Runner()
    if args.command == "install-cli":
        return _install_cli(args.bin_dir, force=args.force)
    if args.command == "uninstall-cli":
        return _uninstall_cli(args.bin_dir)
    paths = RuntimePaths(args.runtime_dir.expanduser().resolve())
    config = _load_config(paths, create=args.command not in {"status", "uninstall"})
    if args.command == "configure":
        return _configure(args, paths)
    if args.command == "status":
        return _status(paths, config, runner)
    if args.command == "uninstall" and (args.purge_data or args.purge_runtime) and not args.yes:
        raise OpsError("--yes is required with --purge-data or --purge-runtime")
    if args.command == "uninstall" and args.purge_runtime and args.target != "all":
        raise OpsError("--purge-runtime requires target 'all' so no running component loses its runtime")
    if args.command == "uninstall" and (args.purge_data or args.remove_image) and args.target == "bridge":
        raise OpsError("--purge-data and --remove-image require target 'container' or 'all'")

    with _operation_lock(paths.lock_file):
        if args.command in {"init", "rebuild", "start", "restart"}:
            _ensure_env(paths, config)
        if args.command == "init":
            release = _release_from_args(args, paths, config, runner)
            _prepare_bridge_runtime(release, runner)
            _save_current(paths, release)
            return {
                "initialized": True,
                "release_sha": release.sha,
                "source": str(release.source_dir),
                "config": str(paths.config),
                "env_file": str(paths.env_file),
                "next": "run 'start all' or 'rebuild'",
            }
        if args.command == "rebuild":
            release = _release_from_args(args, paths, config, runner)
            _prepare_bridge_runtime(release, runner)
            return _rebuild(
                release, paths, config, runner,
                no_cache=args.no_cache, pull=args.pull,
                restart_bridge=not args.skip_bridge, force=args.force,
            )

        if args.command == "uninstall":
            try:
                release = _load_current(paths)
            except OpsError:
                release = Release(TOOL_ROOT, "unknown", "local", None, None, paths.venvs / "unknown")
                _validate_source(release.source_dir)
        else:
            release = _load_current(paths)
        if args.command in {"start", "restart"}:
            _guard_host_jobs(runner, force=args.force)
            if args.command == "start" and args.target == "all" and args.latest:
                prepared = _prepare_release(paths, config, runner)
                _prepare_bridge_runtime(prepared, runner)
                return _rebuild(
                    prepared, paths, config, runner,
                    no_cache=args.no_cache, pull=args.pull,
                    restart_bridge=True, force=args.force,
                )
            result: dict[str, object] = {}
            if args.target in {"all", "container"}:
                action = "restart" if args.command == "restart" else "up"
                command = _compose(release, paths, action, SERVICE)
                if action == "up":
                    command = _compose(release, paths, "up", "-d", "--no-build", SERVICE)
                runner.run(command, env=_release_environment(release))
                result["container"] = "healthy"
                _wait_control_plane(release, runner)
            if args.target in {"all", "bridge"}:
                result.update(_start_bridge(release, paths, config, runner, force=args.force))
                if args.target == "all":
                    _container_to_bridge(release, paths, runner)
            return result

        if args.command == "stop":
            _guard_host_jobs(runner, force=args.force)
            result = {}
            if args.target in {"all", "bridge"}:
                result.update(_stop_bridge(paths, config, runner))
            if args.target in {"all", "container"}:
                runner.run(_compose(release, paths, "stop", SERVICE))
                result["container"] = "stopped"
            return result

        if args.command == "uninstall":
            _guard_host_jobs(runner, force=args.force)
            result = {"data_preserved": not args.purge_data}
            if args.target in {"all", "bridge"}:
                result.update(_stop_bridge(paths, config, runner, uninstall=True))
            if args.target in {"all", "container"}:
                command = [*_compose(release, paths, "down", "--remove-orphans")]
                if args.purge_data:
                    command.append("-v")
                runner.run(command)
                result["container"] = "uninstalled"
                if args.remove_image:
                    runner.run(["docker", "image", "rm", IMAGE], check=False)
                    result["image"] = "removed"
            if args.purge_runtime:
                _require_safe_runtime_root(paths.root)
                for child in paths.root.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                paths.root.rmdir()
                result["runtime"] = "purged"
            return result
    raise OpsError(f"unsupported command: {args.command}")


def _require_safe_runtime_root(path: Path) -> None:
    resolved = path.resolve()
    forbidden = {Path("/"), Path.home().resolve(), TOOL_ROOT.resolve(), TOOL_ROOT.parent.resolve()}
    if resolved in forbidden or len(resolved.parts) < 3:
        raise OpsError(f"refusing to purge unsafe runtime directory: {resolved}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command == "manual":
        print(_manual_text(), end="")
        return 0
    try:
        result = execute(args)
    except (OpsError, OSError, subprocess.SubprocessError, urllib.error.URLError) as error:
        print(f"plow-whip operation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
