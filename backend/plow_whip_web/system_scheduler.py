from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SchedulerPlan:
    os: str
    backend: str
    supported: bool
    target: str | None
    command: list[str]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "os": self.os, "backend": self.backend, "supported": self.supported,
            "target": self.target, "command": self.command, "reason": self.reason,
        }


class SystemScheduler:
    def __init__(self, data_dir: Path, *, python_executable: str | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.python_executable = python_executable or sys.executable

    def plan(self) -> SchedulerPlan:
        system = platform.system()
        command = [
            self.python_executable, "-m", "plow_whip_web", "scheduler-tick",
            "--data-dir", str(self.data_dir),
        ]
        if system == "Darwin":
            target = Path.home() / "Library/LaunchAgents/com.plow-whip.web-v2.scheduler.plist"
            return SchedulerPlan(system, "launchd", True, str(target), command)
        if system == "Linux":
            target = Path.home() / ".config/systemd/user/plow-whip-web-v2.timer"
            return SchedulerPlan(system, "systemd-user", True, str(target), command)
        if system == "Windows":
            return SchedulerPlan(system, "task-scheduler", True, "PlowWhipWebV2Scheduler", command)
        return SchedulerPlan(system, "unsupported", False, None, command, "unsupported operating system")

    def install(self, *, interval_seconds: int, authorized: bool) -> dict[str, Any]:
        plan = self.plan()
        if not authorized:
            return {**plan.as_dict(), "installed": False, "authorization_required": True}
        if not plan.supported or not plan.target:
            return {**plan.as_dict(), "installed": False, "authorization_required": False}
        if plan.backend == "launchd":
            self._install_launchd(Path(plan.target), plan.command, interval_seconds)
        elif plan.backend == "systemd-user":
            self._install_systemd(Path(plan.target), plan.command, interval_seconds)
        else:
            self._install_windows(plan.command, interval_seconds)
        return {**plan.as_dict(), "installed": True, "authorization_required": False}

    @staticmethod
    def _install_launchd(target: Path, command: list[str], interval_seconds: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": "com.plow-whip.web-v2.scheduler",
            "ProgramArguments": command,
            "StartInterval": interval_seconds,
            "RunAtLoad": True,
            "ProcessType": "Background",
        }
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(payload))
        os.replace(temporary, target)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True, capture_output=True)

    @staticmethod
    def _install_systemd(timer: Path, command: list[str], interval_seconds: int) -> None:
        timer.parent.mkdir(parents=True, exist_ok=True)
        service = timer.with_suffix(".service")
        service.write_text(
            "[Unit]\nDescription=plow-whip Web v2 zero-token tick\n[Service]\nType=oneshot\nExecStart="
            + " ".join(_systemd_escape(item) for item in command) + "\n",
            encoding="utf-8",
        )
        timer.write_text(
            f"[Unit]\nDescription=plow-whip Web v2 scheduler\n[Timer]\nOnBootSec=10\nOnUnitActiveSec={interval_seconds}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", timer.name], check=True, capture_output=True)

    @staticmethod
    def _install_windows(command: list[str], interval_seconds: int) -> None:
        minutes = max(1, interval_seconds // 60)
        subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes),
             "/TN", "PlowWhipWebV2Scheduler", "/TR", subprocess.list2cmdline(command)],
            check=True, capture_output=True,
        )


def _systemd_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
