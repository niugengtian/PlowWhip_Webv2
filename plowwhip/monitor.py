from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .provider import provider_facts
from .store import Store


SHANGHAI = timezone(timedelta(hours=8))


def snapshot(db_path: str | Path, data_root: str | Path, project_id: str) -> dict:
    """Read canonical state and a bounded log tail without opening a write path."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        task = connection.execute(
            """
            SELECT * FROM tasks WHERE project_id = ?
            ORDER BY
              CASE
                WHEN outcome IS NULL AND phase <> 'queued' THEN 0
                WHEN outcome IS NULL THEN 1
                ELSE 2
              END,
              created_at DESC, rowid DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if not task:
            return {
                "project_id": project_id,
                "task": None,
                "events": [],
                "artifacts": [],
                "last_output": [],
            }
        return _task_view(connection, store, task)
    finally:
        connection.close()


def projects_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT p.id AS project_id, p.created_at,
                   t.id AS task_id, t.public_status, t.phase,
                   t.spec_revision, t.outcome, t.updated_at
            FROM projects p
            LEFT JOIN tasks t ON t.rowid = (
                SELECT latest.rowid FROM tasks latest
                WHERE latest.project_id = p.id
                ORDER BY
                  CASE
                    WHEN latest.outcome IS NULL AND latest.phase <> 'queued' THEN 0
                    WHEN latest.outcome IS NULL THEN 1
                    ELSE 2
                  END,
                  latest.created_at DESC, latest.rowid DESC LIMIT 1
            )
            WHERE p.archived_at IS NULL
            ORDER BY p.created_at, p.rowid
            """
        ).fetchall()
        return {"projects": [dict(row) for row in rows]}
    finally:
        connection.close()


def task_snapshot(db_path: str | Path, data_root: str | Path, task_id: str) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return {"task": None, "events": [], "artifacts": [], "last_output": []}
        return _task_view(connection, store, task)
    finally:
        connection.close()


def settings_library_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        settings = connection.execute(
            """
            SELECT scope, project_id, setting_key, value_json, source, updated_at
            FROM settings ORDER BY scope, project_id, setting_key
            """
        ).fetchall()
        items = connection.execute(
            """
            SELECT scope, project_id, kind, item_key, revision, path, sha256, created_at
            FROM library_items current
            WHERE revision = (
                SELECT MAX(latest.revision) FROM library_items latest
                WHERE latest.scope = current.scope
                  AND latest.project_id IS current.project_id
                  AND latest.kind = current.kind
                  AND latest.item_key = current.item_key
            )
            ORDER BY scope, project_id, kind, item_key
            """
        ).fetchall()
        library = []
        for item in items:
            path = store.resolve_data_path(item["path"])
            body = path.read_bytes() if path.is_file() else b""
            library.append(
                {
                    **dict(item),
                    "content": body[:65_536].decode(errors="replace"),
                    "truncated": len(body) > 65_536,
                    "sha256_matches": hashlib.sha256(body).hexdigest() == item["sha256"],
                }
            )
        return {
            "settings": [
                {
                    "scope": row["scope"],
                    "project_id": row["project_id"],
                    "setting_key": row["setting_key"],
                    "value": json.loads(row["value_json"]),
                    "source": row["source"],
                    "updated_at": row["updated_at"],
                }
                for row in settings
            ],
            "library": library,
        }
    finally:
        connection.close()


def token_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    """Aggregate normalized physical-session deltas from ModelCallLedger."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        rows = connection.execute(
            """
            SELECT call.rowid AS ledger_rowid, call.id AS call_id,
                   call.task_id, call.task_session_id,
                   call.session_generation, call.provider_key,
                   COALESCE(call.model, call.provider_key) AS model,
                   call.usage_kind, call.input_tokens,
                   call.cached_input_tokens, call.output_tokens, call.created_at,
                   task.project_id, session.worker_id, worker.role_key AS worker_role
            FROM model_calls call
            JOIN tasks task ON task.id = call.task_id
            JOIN task_sessions session ON session.id = call.task_session_id
            JOIN workers worker ON worker.id = session.worker_id
            ORDER BY call.created_at, call.rowid
            """
        ).fetchall()
    finally:
        connection.close()
    calls = _normalized_calls(rows)
    today = datetime.now(SHANGHAI).date()
    today_calls = [
        call
        for call in calls
        if datetime.fromtimestamp(call["created_at"], SHANGHAI).date() == today
    ]
    daily = {}
    for call in calls:
        day = datetime.fromtimestamp(call["created_at"], SHANGHAI).date().isoformat()
        daily.setdefault(day, []).append(call)
    trend = []
    for offset in range(29, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        trend.append({"date": day, **_usage_totals(daily.get(day, []))})
    return {
        "timezone": "Asia/Shanghai",
        "usage_semantics": "physical_session_delta",
        "all_history": _usage_totals(calls),
        "today": {"date": today.isoformat(), **_usage_totals(today_calls)},
        "trend": trend,
        "projects": _group_calls(calls, ("project_id",)),
        "tasks": _group_calls(calls, ("task_id", "project_id")),
        "models": _group_calls(calls, ("model", "provider_key")),
        "sessions": _group_calls(
            calls,
            ("task_session_id", "worker_id", "worker_role", "project_id"),
        ),
    }


def monitor_snapshot(db_path: str | Path, data_root: str | Path) -> dict:
    """Read the current control-plane facts without creating observations."""
    store = Store(db_path, data_root)
    connection = store.connect_readonly()
    try:
        now = time.time()
        statuses = {"pending": 0, "in_progress": 0, "done": 0, "needs_decision": 0}
        for row in connection.execute(
            "SELECT public_status, COUNT(*) AS count FROM tasks GROUP BY public_status"
        ):
            statuses[row["public_status"]] = row["count"]
        projects = connection.execute(
            """
            SELECT project.id AS project_id, project.archived_at,
                   task.id AS task_id, task.public_status,
                   task.phase, task.updated_at
            FROM projects project
            LEFT JOIN tasks task ON task.rowid = (
                SELECT latest.rowid FROM tasks latest
                WHERE latest.project_id = project.id
                ORDER BY
                  CASE WHEN latest.outcome IS NULL
                            AND latest.phase <> 'queued' THEN 0
                       WHEN latest.outcome IS NULL THEN 1 ELSE 2 END,
                  latest.created_at DESC, latest.rowid DESC LIMIT 1
            )
            ORDER BY project.created_at, project.rowid
            """
        ).fetchall()
        recent = connection.execute(
            """
            SELECT project_id, task_id, kind, detail_json, created_at
            FROM task_events ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
        leases = connection.execute(
            """
            SELECT id AS project_id, lease_fence, lease_until
            FROM projects WHERE lease_until > ? ORDER BY lease_until
            """,
            (now,),
        ).fetchall()
        due = connection.execute(
            """
            SELECT id AS task_id, project_id, public_status, phase, next_action_at
            FROM tasks
            WHERE outcome IS NULL AND next_action_at IS NOT NULL
              AND next_action_at <= ?
            ORDER BY next_action_at LIMIT 50
            """,
            (now,),
        ).fetchall()
        jobs = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM host_jobs GROUP BY status"
            )
        }
        sessions = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM session_generations GROUP BY status
                """
            )
        }
        artifacts = {
            row["kind"]: row["count"]
            for row in connection.execute(
                "SELECT kind, COUNT(*) AS count FROM artifacts GROUP BY kind"
            )
        }
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        task_sessions = connection.execute(
            "SELECT COUNT(*) FROM task_sessions"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "read_only": True,
        "captured_at": now,
        "database": {
            "journal_mode": journal_mode,
            "schema_version": schema_version,
            "quick_check": quick_check,
            "tables": 15,
        },
        "cronner": {"mode": "in_process", "entry": "advance_project"},
        "summary": {
            "projects": sum(row["archived_at"] is None for row in projects),
            "archived_projects": sum(
                row["archived_at"] is not None for row in projects
            ),
            "tasks": sum(statuses.values()),
            "task_sessions": task_sessions,
            "due_actions": len(due),
            "active_leases": len(leases),
        },
        "task_statuses": statuses,
        "projects": [dict(row) for row in projects],
        "due_actions": [dict(row) for row in due],
        "leases": [dict(row) for row in leases],
        "host_jobs": jobs,
        "session_generations": sessions,
        "artifacts": artifacts,
        "recent_events": [dict(row) for row in recent],
    }


def _normalized_calls(rows) -> list[dict]:
    previous = {}
    calls = []
    for row in rows:
        item = dict(row)
        values = (
            item["input_tokens"],
            item["cached_input_tokens"],
            item["output_tokens"],
        )
        if item["usage_kind"] == "cumulative":
            key = (
                item["task_session_id"],
                item["session_generation"],
                item["provider_key"],
            )
            prior = previous.get(key)
            previous[key] = values
            if prior:
                values = tuple(current - old for current, old in zip(values, prior))
        item["input_tokens"], item["cached_input_tokens"], item["output_tokens"] = values
        item["uncached_input_tokens"] = values[0] - values[1]
        item["total_tokens"] = values[0] + values[2]
        calls.append(item)
    return calls


def _usage_totals(calls: list[dict]) -> dict:
    totals = {
        "total_tokens": sum(call["total_tokens"] for call in calls),
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "cached_input_tokens": sum(call["cached_input_tokens"] for call in calls),
        "uncached_input_tokens": sum(
            call["uncached_input_tokens"] for call in calls
        ),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "calls": len(calls),
    }
    totals["ratios"] = {
        "input_per_output": _ratio(
            totals["input_tokens"], totals["output_tokens"]
        ),
        "cached_per_uncached": _ratio(
            totals["cached_input_tokens"], totals["uncached_input_tokens"]
        ),
    }
    return totals


def _group_calls(calls: list[dict], fields: tuple[str, ...]) -> list[dict]:
    grouped = {}
    for call in calls:
        key = tuple(call[field] for field in fields)
        grouped.setdefault(key, []).append(call)
    result = [
        {
            **dict(zip(fields, key)),
            **_usage_totals(items),
        }
        for key, items in grouped.items()
    ]
    return sorted(result, key=lambda item: item["total_tokens"], reverse=True)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _task_view(connection, store: Store, task) -> dict:
    events = connection.execute(
        """
        SELECT kind, detail_json, created_at FROM task_events
        WHERE task_id = ? ORDER BY id DESC LIMIT 20
        """,
        (task["id"],),
    ).fetchall()
    job = connection.execute(
        """
        SELECT output_ref FROM host_jobs
        WHERE task_id = ? ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    artifacts = connection.execute(
        """
        SELECT kind, path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind IN ('output', 'evidence')
        ORDER BY revision, CASE kind WHEN 'output' THEN 0 ELSE 1 END
        """,
        (task["id"],),
    ).fetchall()
    handoffs = connection.execute(
        """
        SELECT path, sha256, acceptance_id, revision FROM artifacts
        WHERE task_id = ? AND kind = 'handoff'
        ORDER BY created_at DESC, rowid DESC LIMIT 20
        """,
        (task["id"],),
    ).fetchall()
    sessions = connection.execute(
        """
        SELECT session.id AS task_session_id, session.role_key,
               session.role_snapshot_json, session.settings_json,
               generation.generation, generation.provider_key,
               generation.status, generation.handoff_ref
        FROM task_sessions session
        JOIN session_generations generation ON generation.task_session_id = session.id
        WHERE session.task_id = ?
        ORDER BY session.role_key, generation.generation
        """,
        (task["id"],),
    ).fetchall()
    model_usage = connection.execute(
        """
        SELECT task_session_id, SUM(normalized_total) AS normalized_total
        FROM model_calls WHERE task_id = ? GROUP BY task_session_id
        """,
        (task["id"],),
    ).fetchall()
    tail_values = {}
    for session in sessions:
        if session["role_key"] == (task["role_key"] or "deterministic"):
            tail_values = json.loads(session["settings_json"]).get("values", {})
            break
    last_output = (
        _tail(
            store.data_root,
            job["output_ref"],
            int(tail_values.get("monitor_tail_lines", 20)),
            int(tail_values.get("monitor_tail_bytes", 8192)),
        )
        if job and job["output_ref"]
        else []
    )
    return {
        "project_id": task["project_id"],
        "task": dict(task),
        "events": [dict(event) for event in events],
        "artifacts": [
            {
                **dict(artifact),
                "kind": "artifact" if artifact["kind"] == "output" else "evidence",
                "path": str(store.resolve_data_path(artifact["path"])),
            }
            for artifact in artifacts
        ],
        "handoffs": [
            {**dict(handoff), "path": str(store.resolve_data_path(handoff["path"]))}
            for handoff in handoffs
        ],
        "sessions": [
            {
                "task_session_id": session["task_session_id"],
                "role_key": session["role_key"],
                "generation": session["generation"],
                "provider_key": session["provider_key"],
                "status": session["status"],
                "handoff_ref": session["handoff_ref"],
                "model": "deterministic" if session["provider_key"] == "local" else None,
                "role_snapshot": json.loads(session["role_snapshot_json"]),
                "settings": json.loads(session["settings_json"]),
                "provider_candidates": provider_facts(session["role_key"]),
            }
            for session in sessions
        ],
        "model_usage": [dict(row) for row in model_usage],
        "session_files": _session_files(store, task),
        "last_output": last_output,
    }


def _session_files(store: Store, task) -> list[dict]:
    root = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "sessions"
    )
    if not root.is_dir():
        return []
    files = sorted((path for path in root.rglob("*") if path.is_file()), reverse=True)[:100]
    return [
        {
            "path": str(path),
            "bytes": path.stat().st_size,
        }
        for path in files
    ]


def _tail(data_root: Path, relative: str, lines: int = 20, byte_cap: int = 8192) -> list[str]:
    path = (data_root / relative).resolve()
    path.relative_to(data_root)
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - byte_cap))
        return handle.read(byte_cap).decode(errors="replace").splitlines()[-lines:]
