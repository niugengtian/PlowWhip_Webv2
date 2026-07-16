from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import PolicyViolationError


class CommandPolicy:
    """Conservative static boundary for the generic local command provider."""

    def validate(self, project_path: Path, command: dict[str, Any]) -> None:
        root = project_path.resolve()
        argv = command.get("argv", [])
        for argument in argv[1:]:
            if "\x00" in argument:
                raise PolicyViolationError("command contains NUL")
            candidate = Path(argument).expanduser()
            if candidate.is_absolute() and not _is_within(candidate, root):
                raise PolicyViolationError(f"command argument escapes project root: {argument}")
            if argument in {"..", "../"} or argument.startswith("../"):
                raise PolicyViolationError(f"command argument escapes project root: {argument}")


class Redactor:
    patterns = (
        re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    )

    @classmethod
    def redact(cls, value: str) -> str:
        redacted = value
        for pattern in cls.patterns:
            redacted = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", redacted)
        return redacted


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False
