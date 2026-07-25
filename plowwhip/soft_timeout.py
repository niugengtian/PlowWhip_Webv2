"""Project-butler soft timeout: report, double budget (≤3), ledger the miss."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .intake import canonical_json
from .lifecycle_state import write_task_fields
from .provider import extend_provider_job_timeout
from .timeout_policy import (
    MAX_SOFT_TIMEOUTS,
    append_timeout_ledger,
    classify_timeout,
    ledger_timeout_note,
    next_soft_timeout_seconds,
    soft_timeout_allowed,
)

SoftTimeoutResult = Literal[
    "extended",
    "hard_idle",
    "soft_cap_reached",
    "skipped",
    "extend_failed",
]


def try_soft_timeout_extension(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    state: dict[str, object],
    *,
    now: float | None = None,
    repo_root: Path | None = None,
    size: str | None = None,
) -> SoftTimeoutResult:
    """Extend an active HostJob on soft budget miss; never double hard idle."""
    current = float(now if now is not None else time.time())
    if str(state.get("status") or "") not in {
        "dispatching",
        "running",
        "cancelling",
        "orphan_running",
        "recovery_hold",
    }:
        return "skipped"
    if state.get("deadline_reached_at") is None and (
        task["deadline_at"] is None or float(task["deadline_at"]) > current
    ):
        return "skipped"

    io_phase = (
        str(state.get("io_phase") or "").strip() or None
    )
    last_io_at = state.get("last_io_at")
    try:
        last_io_value = float(last_io_at) if last_io_at is not None else None
    except (TypeError, ValueError):
        last_io_value = None
    timeout_class = classify_timeout(
        io_phase=io_phase,
        last_io_at=last_io_value,
        now=current,
    )
    dispatch = json.loads(job["dispatch_json"])
    soft_count = int(dispatch.get("soft_timeout_count") or 0)
    previous = int(
        state.get("timeout_seconds")
        or dispatch.get("timeout_seconds")
        or 600
    )

    if timeout_class == "hard":
        _record_butler_notice(
            connection,
            task,
            current,
            (
                f"[超时-hard] Task {task['id'][:8]}… 空闲/无 I/O，"
                f"不翻倍续命（io_phase={io_phase or 'idle'}）。"
            ),
            {
                "kind": "timeout_notice",
                "timeout_class": "hard",
                "task_id": task["id"],
                "waiting": False,
            },
        )
        _append_ledger(
            repo_root,
            task,
            attempt=soft_count + 1,
            previous_seconds=previous,
            next_seconds=None,
            timeout_class="hard",
            io_phase=io_phase,
            reason="hard idle — 无近期模型/工具 I/O",
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'task_timeout_hard', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "io_phase": io_phase,
                        "last_io_at": last_io_value,
                        "timeout_seconds": previous,
                    }
                ),
                current,
            ),
        )
        return "hard_idle"

    if not soft_timeout_allowed(soft_count):
        _record_butler_notice(
            connection,
            task,
            current,
            (
                f"[超时-cap] Task {task['id'][:8]}… soft 续命已达 "
                f"{MAX_SOFT_TIMEOUTS} 次，停止自动翻倍。"
            ),
            {
                "kind": "timeout_notice",
                "timeout_class": "soft_cap",
                "task_id": task["id"],
                "waiting": False,
            },
        )
        _append_ledger(
            repo_root,
            task,
            attempt=soft_count + 1,
            previous_seconds=previous,
            next_seconds=None,
            timeout_class="soft",
            io_phase=io_phase,
            reason="soft 续命次数已满",
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'task_timeout_soft_cap', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "soft_timeout_count": soft_count,
                        "timeout_seconds": previous,
                    }
                ),
                current,
            ),
        )
        return "soft_cap_reached"

    next_budget = next_soft_timeout_seconds(previous, size)
    try:
        extend_provider_job_timeout(job["id"], next_budget)
    except (OSError, ValueError, RuntimeError):
        return "extend_failed"

    soft_count += 1
    dispatch["soft_timeout_count"] = soft_count
    dispatch["timeout_seconds"] = next_budget
    dispatch["last_soft_timeout_at"] = current
    dispatch.pop("timeout_stage", None)
    connection.execute(
        "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
        (canonical_json(dispatch), job["id"]),
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "deadline_at": current + next_budget,
            "public_status": "in_progress",
            "wait_reason": (
                f"[超时-soft {soft_count}/{MAX_SOFT_TIMEOUTS}] "
                f"预算 {previous}s→{next_budget}s；模型仍在推进，已上报管家续命"
            ),
            "fault_code": None,
            "next_action_at": current + 1,
            "next_action_kind": "poll",
            "updated_at": current,
        },
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'task_timeout_soft_extended', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "host_job_id": job["id"],
                    "attempt": soft_count,
                    "previous_seconds": previous,
                    "next_seconds": next_budget,
                    "io_phase": io_phase,
                }
            ),
            current,
        ),
    )
    _record_butler_notice(
        connection,
        task,
        current,
        (
            f"[超时-soft {soft_count}/{MAX_SOFT_TIMEOUTS}] "
            f"Task {task['id'][:8]}… 预算 {previous}s→{next_budget}s"
            f"（io_phase={io_phase or 'unknown'}）。台账：超时阈值判断有误。"
        ),
        {
            "kind": "timeout_notice",
            "timeout_class": "soft",
            "task_id": task["id"],
            "attempt": soft_count,
            "previous_seconds": previous,
            "next_seconds": next_budget,
            "waiting": False,
        },
    )
    _append_ledger(
        repo_root,
        task,
        attempt=soft_count,
        previous_seconds=previous,
        next_seconds=next_budget,
        timeout_class="soft",
        io_phase=io_phase,
        reason="仍有模型/工具 I/O，墙钟预算不足",
    )
    return "extended"


def _record_butler_notice(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    now: float,
    content: str,
    action: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO messages(
            id, project_id, role, content, action_json,
            idempotency_key, created_at, processed_at
        ) VALUES (?, ?, 'butler', ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task["project_id"],
            content,
            canonical_json(action),
            f"timeout:{task['id']}:{action.get('attempt')}:{action.get('timeout_class')}:{int(now)}",
            now,
            now,
        ),
    )


def _append_ledger(
    repo_root: Path | None,
    task: sqlite3.Row,
    *,
    attempt: int,
    previous_seconds: int,
    next_seconds: int | None,
    timeout_class: str,
    io_phase: str | None,
    reason: str,
) -> None:
    root = repo_root
    if root is None:
        root = Path(__file__).resolve().parents[1]
    note = ledger_timeout_note(
        task_id=str(task["id"]),
        project_id=str(task["project_id"]),
        attempt=attempt,
        previous_seconds=previous_seconds,
        next_seconds=next_seconds,
        timeout_class=timeout_class,  # type: ignore[arg-type]
        io_phase=io_phase,
        reason=reason,
    )
    try:
        append_timeout_ledger(root, note)
    except OSError:
        return
