from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess

import pytest

from scripts import plow_whip_ops as ops


def _configure_args(runtime_dir: Path, *extra: str):
    return ops.build_parser().parse_args(["--runtime-dir", str(runtime_dir), "configure", *extra])


def test_default_configuration_uses_github_main(tmp_path: Path) -> None:
    paths = ops.RuntimePaths(tmp_path / "runtime")

    config = ops._load_config(paths)

    assert config["source"] == {
        "mode": "github",
        "repository": ops.DEFAULT_REPOSITORY,
        "ref": "main",
        "local_path": str(ops.TOOL_ROOT),
    }
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600


def test_configure_generates_private_token_without_exposing_it(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    args = _configure_args(
        runtime_dir,
        "--project-root",
        str(tmp_path / "projects"),
        "--rotate-bridge-token",
    )

    result = ops.execute(args)
    paths = ops.RuntimePaths(runtime_dir)
    env = ops._read_env(paths.env_file)

    assert len(env["PLOW_WHIP_BRIDGE_TOKEN"]) == 48
    assert env["PLOW_WHIP_BRIDGE_TOKEN"] not in json.dumps(result)
    assert result["bridge_token"] == "configured"
    assert stat.S_IMODE(paths.env_file.stat().st_mode) == 0o600


def test_configure_replaces_project_roots_and_source(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    first = tmp_path / "one"
    second = tmp_path / "two"
    args = _configure_args(
        runtime_dir,
        "--source",
        "local",
        "--local-source",
        str(tmp_path),
        "--project-root",
        str(first),
        "--project-root",
        str(second),
        "--timezone",
        "UTC",
    )

    result = ops.execute(args)

    assert result["source"]["mode"] == "local"
    assert result["source"]["local_path"] == str(tmp_path.resolve())
    assert result["bridge"]["project_roots"] == [str(first.resolve()), str(second.resolve())]
    assert result["timezone"] == "UTC"


def test_bridge_port_updates_container_bridge_url(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    result = ops.execute(_configure_args(runtime_dir, "--bridge-port", "8876"))

    assert result["bridge"]["port"] == 8876
    assert result["bridge"]["url"] == "http://host.docker.internal:8876"
    assert ops._read_env(ops.RuntimePaths(runtime_dir).env_file)["PLOW_WHIP_BRIDGE_URL"] == (
        "http://host.docker.internal:8876"
    )


def test_bridge_plist_contains_env_path_but_not_secret(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    paths = ops.RuntimePaths(runtime_dir)
    config = ops._load_config(paths)
    ops._ensure_env(paths, config)
    release = ops.Release(
        source_dir=ops.TOOL_ROOT,
        sha="a" * 40,
        source_mode="local",
        repository=None,
        ref=None,
        venv_dir=tmp_path / "venv",
    )
    (release.venv_dir / "bin").mkdir(parents=True)
    (release.venv_dir / "bin" / "python").touch()
    (release.venv_dir / "bin" / "simple-worker").touch()

    payload = ops._bridge_plist(release, paths, config, ops.Runner())
    serialized = json.dumps(payload)

    assert str(paths.env_file) in payload["ProgramArguments"]
    assert ops._bridge_token(paths) not in serialized


@pytest.mark.parametrize("candidate", [Path("/"), Path.home(), ops.TOOL_ROOT])
def test_runtime_purge_rejects_broad_paths(candidate: Path) -> None:
    with pytest.raises(ops.OpsError, match="unsafe runtime directory"):
        ops._require_safe_runtime_root(candidate)


def test_purge_requires_explicit_yes(tmp_path: Path) -> None:
    args = ops.build_parser().parse_args(
        ["--runtime-dir", str(tmp_path / "runtime"), "uninstall", "all", "--purge-data"]
    )

    with pytest.raises(ops.OpsError, match="--yes"):
        ops.execute(args)


def test_purge_runtime_requires_uninstall_all(tmp_path: Path) -> None:
    args = ops.build_parser().parse_args(
        [
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "uninstall",
            "container",
            "--purge-runtime",
            "--yes",
        ]
    )

    with pytest.raises(ops.OpsError, match="target 'all'"):
        ops.execute(args)


def test_status_does_not_create_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(ops, "_get_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(ops, "_active_host_jobs", lambda _runner=None: None)

    result = ops.execute(
        ops.build_parser().parse_args(["--runtime-dir", str(runtime_dir), "status"])
    )

    assert result["container"] == "stopped"
    assert not runtime_dir.exists()


def test_lifecycle_guard_reads_live_unconsumed_host_jobs_from_container() -> None:
    class ActiveJobRunner(ops.Runner):
        @staticmethod
        def which(name: str) -> str | None:
            return "/usr/local/bin/docker" if name == "docker" else None

        def run(self, argv, **_kwargs):
            command = [str(item) for item in argv]
            output = "container-id\n" if command[1:3] == ["ps", "-q"] else "2\n"
            return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ops.OpsError, match="2 Host Job"):
        ops._guard_host_jobs(ActiveJobRunner(), force=False)


def test_help_uses_public_command_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert ops.main([]) == 0

    output = capsys.readouterr().out
    assert output.startswith("usage: plow-whip-web")
    assert "install-cli" in output
    assert "uninstall-cli" in output


def test_short_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        ops.main(["-v"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"plow-whip-web {ops.CLI_VERSION}"


@pytest.mark.parametrize("arguments", [["-man"], ["--manual"], ["manual"]])
def test_manual_aliases(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    if arguments[0].startswith("-"):
        with pytest.raises(SystemExit) as exit_info:
            ops.main(arguments)
        assert exit_info.value.code == 0
    else:
        assert ops.main(arguments) == 0

    output = capsys.readouterr().out
    assert "PLOW-WHIP-WEB(1)" in output
    assert "QUICK START" in output
    assert "UNINSTALL" in output
    assert "purge options require --yes" in output


def test_install_and_uninstall_cli(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    parser = ops.build_parser()

    installed = ops.execute(
        parser.parse_args(["install-cli", "--bin-dir", str(bin_dir)])
    )
    command = Path(installed["command"])
    version = subprocess.run(
        [command, "-v"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert version.stdout.strip() == f"plow-whip-web {ops.CLI_VERSION}"
    assert stat.S_IMODE(command.stat().st_mode) == 0o755
    assert ops.CLI_MARKER in command.read_text(encoding="utf-8")

    uninstalled = ops.execute(
        parser.parse_args(["uninstall-cli", "--bin-dir", str(bin_dir)])
    )
    assert uninstalled["cli"] == "uninstalled"
    assert not command.exists()


def test_install_cli_refuses_unmanaged_existing_command(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "plow-whip-web"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(ops.OpsError, match="unmanaged command"):
        ops._install_cli(bin_dir, force=False)
