"""Task wall-clock timeout policy: soft (budget) vs hard (idle).

Planner/control-plane set per-Task budgets. Soft timeout reports to the
project butler, doubles the budget up to MAX_SOFT_TIMEOUTS, and records a
ledger note that the Task timeout estimate was wrong. Hard idle never
doubles — fail-closed to stop token burn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

MAX_SOFT_TIMEOUTS = 3
# No model/tool I/O for this long → hard idle (do not double).
IDLE_HARD_SECONDS = 300
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 86_400

# Control-plane clamps when Planner omits or under/over-shoots.
DEFAULT_TIMEOUT_BY_SIZE = {
    "simple": 600,
    "medium": 1_800,
    "large": 3_600,
}
TIMEOUT_BOUNDS_BY_SIZE = {
    "simple": (120, 3_600),
    "medium": (300, 14_400),
    "large": (600, 86_400),
}

ACTIVE_IO_PHASES = frozenset(
    {
        "awaiting_model",
        "model_response",
        "tool",
        "compacting",
    }
)

TimeoutClass = Literal["soft", "hard"]


def default_timeout_seconds(size: str | None) -> int:
    key = size if size in DEFAULT_TIMEOUT_BY_SIZE else "medium"
    return DEFAULT_TIMEOUT_BY_SIZE[key]


def clamp_timeout_seconds(value: int, size: str | None = None) -> int:
    seconds = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, int(value)))
    key = size if size in TIMEOUT_BOUNDS_BY_SIZE else None
    if key is None:
        return seconds
    low, high = TIMEOUT_BOUNDS_BY_SIZE[key]
    return max(low, min(high, seconds))


def next_soft_timeout_seconds(current: int, size: str | None = None) -> int:
    return clamp_timeout_seconds(max(MIN_TIMEOUT_SECONDS, int(current)) * 2, size)


def classify_timeout(
    *,
    io_phase: str | None,
    last_io_at: float | None,
    now: float,
    idle_hard_seconds: int = IDLE_HARD_SECONDS,
) -> TimeoutClass:
    """soft = still exchanging with the model/tools; hard = idle stall."""
    phase = str(io_phase or "idle").strip().lower()
    if phase in {"failed", "completed"}:
        return "hard"
    if phase in ACTIVE_IO_PHASES:
        return "soft"
    # LIVE-DS-22 / R1: no I/O stamp yet (first model call) → soft once, not hard.
    if last_io_at is None:
        return "soft"
    idle_for = max(0.0, float(now) - float(last_io_at))
    if idle_for < float(idle_hard_seconds):
        return "soft"
    return "hard"


def soft_timeout_allowed(count: int) -> bool:
    return 0 <= int(count) < MAX_SOFT_TIMEOUTS


def ledger_timeout_note(
    *,
    task_id: str,
    project_id: str,
    attempt: int,
    previous_seconds: int,
    next_seconds: int | None,
    timeout_class: TimeoutClass,
    io_phase: str | None,
    reason: str,
) -> str:
    doubled = (
        f"{previous_seconds}s → {next_seconds}s"
        if next_seconds is not None
        else f"{previous_seconds}s（不再翻倍）"
    )
    return (
        f"- LIVE-DS-22 / soft-timeout: Task `{task_id}` project `{project_id}` "
        f"class={timeout_class} attempt={attempt}/{MAX_SOFT_TIMEOUTS} "
        f"budget {doubled}; io_phase={io_phase or 'unknown'}; "
        f"结论：Task 超时阈值判断有误（{reason}）。\n"
    )


def append_timeout_ledger(
    repo_root: Path,
    note: str,
    *,
    ledger_relative: str = "docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md",
) -> Path | None:
    """Append a timeout mis-estimate note to the remediation ledger."""
    path = repo_root / ledger_relative
    if not path.is_file():
        return None
    marker = "\n## LIVE-DS-22 超时续命现场记录\n"
    body = path.read_text(encoding="utf-8")
    if marker.strip() not in body:
        body = body.rstrip() + "\n" + marker + "\n"
    body = body.rstrip() + "\n" + note
    path.write_text(body, encoding="utf-8")
    return path
