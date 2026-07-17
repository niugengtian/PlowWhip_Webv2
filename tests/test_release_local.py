from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import release_local


def test_docker_build_context_excludes_private_env_files() -> None:
    root = Path(__file__).resolve().parents[1]
    rules = {
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }
    assert {".env", ".env.local", ".env.*.local"} <= rules
    assert ".env.local.example" not in rules


def test_deployment_lock_rejects_a_second_writer(tmp_path: Path) -> None:
    lock_file = tmp_path / "release.lock"
    with release_local.deployment_lock(lock_file):
        with pytest.raises(
            release_local.ReleaseError,
            match="another local release transaction is active",
        ):
            with release_local.deployment_lock(lock_file):
                pass
    assert lock_file.stat().st_mode & 0o777 == 0o600


def test_bridge_plist_uses_explicit_paths_without_secret_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    payload = release_local.bridge_plist(
        repo_root=repo,
        env_file=repo / ".env.local",
        project_root=tmp_path / "projects",
        state_dir=home / ".plow-whip-web" / "host-bridge",
        home=home,
        codex_path=home / ".local" / "bin" / "codex",
    )

    encoded = json.dumps(payload)
    arguments = payload["ProgramArguments"]
    assert arguments[0] == str(repo / ".venv" / "bin" / "python")
    assert arguments[arguments.index("--env-file") + 1] == str(repo / ".env.local")
    assert arguments[arguments.index("--project-root") + 1] == str(tmp_path / "projects")
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    path_value = payload["EnvironmentVariables"]["PATH"]
    assert str(repo / ".venv" / "bin") in path_value
    assert str(home / ".local" / "bin") in path_value
    assert "/opt/homebrew/bin" in path_value
    assert "DEEPSEEK_API_KEY" not in encoded
    assert "PLOW_WHIP_BRIDGE_TOKEN" not in encoded


def test_deploy_runs_one_compose_transaction_with_revision(tmp_path: Path) -> None:
    expected = "a" * 40
    completed = subprocess.CompletedProcess([], 0, "", "")
    runtime = {"release_sha": expected, "health": "healthy"}
    with (
        patch.object(release_local, "_private_file"),
        patch.object(release_local, "verify_source") as verify_source,
        patch.object(release_local, "verify_runtime", return_value=runtime),
        patch.object(release_local.subprocess, "run", return_value=completed) as run,
    ):
        assert release_local.deploy(expected, tmp_path / "release.lock") == runtime

    verify_source.assert_called_once_with(expected)
    argv = run.call_args.args[0]
    assert argv == [
        "docker",
        "compose",
        "--env-file",
        str(release_local.ROOT / ".env.local"),
        "up",
        "--build",
        "-d",
    ]
    environment = run.call_args.kwargs["env"]
    assert environment["PLOW_WHIP_RELEASE_SHA"] == expected


def test_verify_runtime_requires_image_revision_and_named_volumes() -> None:
    expected = "b" * 40
    container = {
        "State": {
            "Running": True,
            "Health": {"Status": "healthy"},
        },
        "RestartCount": 0,
        "Image": "sha256:image",
        "Mounts": [
            {
                "Type": "volume",
                "Name": "plow-whip-web-v2-data",
                "Destination": "/data",
            },
            {
                "Type": "volume",
                "Name": "plow-whip-web-v2-projects",
                "Destination": "/projects",
            },
        ],
    }
    image = {
        "Config": {
            "Labels": {
                release_local.RELEASE_LABEL: expected,
            }
        }
    }
    inspect_results = [
        subprocess.CompletedProcess([], 0, json.dumps([container]), ""),
        subprocess.CompletedProcess([], 0, json.dumps([image]), ""),
    ]
    with (
        patch.object(release_local, "_control_plane_ids", return_value=["container"]),
        patch.object(release_local, "_run", side_effect=inspect_results),
        patch.object(
            release_local,
            "_get_json",
            return_value={
                "status": "ok",
                "database": {"journal_mode": "wal", "migration_count": 20},
            },
        ),
    ):
        result = release_local.verify_runtime(expected)

    assert result["release_sha"] == expected
    assert result["restart_count"] == 0
    assert result["volumes"] == [
        "plow-whip-web-v2-data",
        "plow-whip-web-v2-projects",
    ]


def test_verify_runtime_rejects_stale_image_revision() -> None:
    expected = "c" * 40
    container = {
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "RestartCount": 0,
        "Image": "sha256:image",
        "Mounts": [
            {"Type": "volume", "Name": name, "Destination": destination}
            for name, destination in release_local.REQUIRED_VOLUMES.items()
        ],
    }
    image = {
        "Config": {
            "Labels": {
                release_local.RELEASE_LABEL: "d" * 40,
            }
        }
    }
    with (
        patch.object(release_local, "_control_plane_ids", return_value=["container"]),
        patch.object(
            release_local,
            "_run",
            side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps([container]), ""),
                subprocess.CompletedProcess([], 0, json.dumps([image]), ""),
            ],
        ),
    ):
        with pytest.raises(release_local.ReleaseError, match="runtime image revision mismatch"):
            release_local.verify_runtime(expected)
