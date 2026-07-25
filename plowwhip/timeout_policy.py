"""Task wall-clock timeout policy: soft (budget) vs hard (idle).

Planner/control-plane set per-Task budgets. Soft timeout reports to the
project butler, doubles the budget up to MAX_SOFT_TIMEOUTS, and records a
ledger note that the Task timeout estimate was wrong. Hard idle never
doubles — fail-closed to stop token burn.

T-14: the 2nd and 3rd soft extensions also require effective increment
(new Artifact/Evidence/acceptance progress, or exploration frontier growth);
active I/O alone is not enough after the first soft.
"""

from __future__ import annotations

import json
import sqlite3
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

PROGRESS_EVENT_KINDS = frozenset(
    {
        "artifact_registered",
        "artifact_indexed",
        "evidence_recorded",
        "acceptance_progress",
        "workspace_revision",
        "exploration_frontier",
        "formal_result_manifest",
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


def effective_increment_since(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    since: float | None,
    dispatch: dict | None = None,
) -> bool:
    """T-14: true when Artifact/Evidence/frontier progressed after *since*."""
    since_at = float(since or 0.0)
    rows = connection.execute(
        """
        SELECT kind, detail_json, created_at FROM task_events
        WHERE task_id = ? AND created_at > ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 80
        """,
        (task_id, since_at),
    ).fetchall()
    for row in rows:
        kind = str(row["kind"] or "")
        if kind in PROGRESS_EVENT_KINDS or kind.startswith("artifact_"):
            return True
        if "evidence" in kind or "acceptance" in kind:
            return True
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        if not isinstance(detail, dict):
            continue
        if detail.get("effective_increment") is True:
            return True
        if detail.get("new_artifact") or detail.get("artifact_path"):
            return True
        frontier = detail.get("exploration_frontier")
        if isinstance(frontier, (int, float)) and frontier > 0:
            return True

    payload = dispatch if isinstance(dispatch, dict) else {}
    baseline = payload.get("soft_progress_baseline")
    current = payload.get("soft_progress_marker")
    if baseline is not None and current is not None and current != baseline:
        return True
    artifact_count = payload.get("result_artifact_count")
    baseline_count = payload.get("soft_artifact_count_baseline")
    try:
        if (
            artifact_count is not None
            and baseline_count is not None
            and int(artifact_count) > int(baseline_count)
        ):
            return True
    except (TypeError, ValueError):
        pass
    return False


def soft_extension_requires_increment(soft_count: int) -> bool:
    """After the first soft extension, further doubles need T-14 increment."""
    return int(soft_count) >= 1


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
