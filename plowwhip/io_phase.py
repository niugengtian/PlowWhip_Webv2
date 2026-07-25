"""Derive HostJob I/O phase from json-worker / provider stdout events."""

from __future__ import annotations

import json
from typing import Any


def derive_io_phase(
    stdout: str | bytes | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Return io_phase / last_io_at / last_event from newest recognizable events."""
    text = (
        stdout.decode("utf-8", errors="replace")
        if isinstance(stdout, (bytes, bytearray))
        else str(stdout or "")
    )
    phase = "idle"
    last_io_at: float | None = None
    last_event: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            continue
        stamped = event.get("ts") or event.get("at") or event.get("time")
        try:
            stamp = float(stamped) if stamped is not None else None
        except (TypeError, ValueError):
            stamp = None
        if event_type == "model.request_started":
            phase = "awaiting_model"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type in {"model.response_completed", "model.response_started"}:
            phase = "model_response"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type == "tool.started":
            phase = "tool"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type == "tool.finished":
            phase = "tool"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type == "session.compacted":
            phase = "compacting"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type == "worker.progress":
            # Heartbeat between turns — keep prior phase if active, else idle_progress.
            if phase in {"idle", "idle_progress"}:
                phase = "idle_progress"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
        elif event_type in {"worker.error", "tool.error", "worker.cancelled"}:
            phase = "failed"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
            failure = event.get("failure_class")
            return {
                "io_phase": phase,
                "last_io_at": last_io_at,
                "last_event": last_event,
                "failure_class": (
                    failure
                    if isinstance(failure, str)
                    else ("cancelled" if event_type == "worker.cancelled" else "process")
                ),
            }
        elif event_type == "worker.completed":
            status = str(event.get("status") or "")
            phase = "completed" if status == "completed" else "failed"
            last_event = event_type
            if stamp is not None:
                last_io_at = stamp
            elif now is not None:
                last_io_at = now
            failure = event.get("failure_class")
            return {
                "io_phase": phase,
                "last_io_at": last_io_at,
                "last_event": last_event,
                "failure_class": failure if isinstance(failure, str) else None,
            }
    return {
        "io_phase": phase,
        "last_io_at": last_io_at,
        "last_event": last_event,
        "failure_class": None,
    }
