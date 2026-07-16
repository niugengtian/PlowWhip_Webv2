from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    failure_class: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "failure_class": self.failure_class,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class GenericCommandProvider:
    name = "generic-command"
    model_invoked = False

    @staticmethod
    def estimate_tokens(_command: dict[str, Any]) -> int:
        return 0

    def execute(self, project_path: Path, command: dict[str, Any]) -> ExecutionResult:
        argv = command["argv"]
        timeout_seconds = int(command.get("timeout_seconds", 60))
        output_limit = int(command.get("output_limit_bytes", 131_072))
        started = monotonic()
        child_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("COV_CORE_") and not key.startswith("COVERAGE_")
        }
        try:
            completed = subprocess.run(
                argv,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired as error:
            return ExecutionResult(
                returncode=124,
                stdout=_bounded_text(error.stdout, output_limit),
                stderr=_bounded_text(error.stderr, output_limit),
                duration_ms=int((monotonic() - started) * 1000),
                failure_class="timeout",
            )
        except OSError as error:
            return ExecutionResult(
                returncode=126,
                stdout="",
                stderr=str(error)[:output_limit],
                duration_ms=int((monotonic() - started) * 1000),
                failure_class="command_unavailable",
            )
        return ExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout[:output_limit],
            stderr=completed.stderr[:output_limit],
            duration_ms=int((monotonic() - started) * 1000),
            failure_class=None if completed.returncode == 0 else "command_failed",
        )


def _bounded_text(value: bytes | str | None, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:limit]
    return value[:limit]
