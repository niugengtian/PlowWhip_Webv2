from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


TASK_FIELDS = {
    "acceptance_json",
    "checker_role_key",
    "deadline_at",
    "fault_code",
    "goal_id",
    "next_action_at",
    "next_action_kind",
    "next_retry_at",
    "outcome",
    "phase",
    "plan_id",
    "public_status",
    "retry_count",
    "role_key",
    "spec_json",
    "spec_revision",
    "sprint",
    "terminal_capabilities_revoked_at",
    "updated_at",
    "wait_reason",
}
_WRITE_ORIGIN: ContextVar[str | None] = ContextVar(
    "plowwhip_lifecycle_write_origin", default=None
)


@contextmanager
def lifecycle_write_scope(origin: str) -> Iterator[None]:
    if origin not in {"advance_project", "schema_migration"}:
        raise ValueError("invalid lifecycle write origin")
    token = _WRITE_ORIGIN.set(origin)
    try:
        yield
    finally:
        _WRITE_ORIGIN.reset(token)


def write_task_fields(
    connection: sqlite3.Connection,
    task_id: str,
    fields: dict[str, object],
) -> None:
    if _WRITE_ORIGIN.get() != "advance_project":
        raise RuntimeError(
            "Task lifecycle fields may only be written by advance_project"
        )
    if (
        not isinstance(task_id, str)
        or not task_id
        or not fields
        or any(field not in TASK_FIELDS for field in fields)
    ):
        raise ValueError("invalid Task lifecycle field fact")
    columns = list(fields)
    assignments = ", ".join(f"{column} = ?" for column in columns)
    cursor = connection.execute(
        f"UPDATE tasks SET {assignments} WHERE id = ?",
        (*[fields[column] for column in columns], task_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Task lifecycle target does not exist: {task_id}")


def increment_task_retry(
    connection: sqlite3.Connection, task_id: str, amount: int
) -> None:
    if _WRITE_ORIGIN.get() != "advance_project":
        raise RuntimeError(
            "Task lifecycle fields may only be written by advance_project"
        )
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("invalid Task retry increment")
    cursor = connection.execute(
        "UPDATE tasks SET retry_count = retry_count + ? WHERE id = ?",
        (amount, task_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Task lifecycle target does not exist: {task_id}")


def finalize_task_terminal(
    connection: sqlite3.Connection,
    task_id: str,
    outcome: str,
    now: float,
) -> dict[str, object]:
    if _WRITE_ORIGIN.get() != "advance_project":
        raise RuntimeError(
            "Task terminal state may only be written by advance_project"
        )
    if outcome not in {"done", "cancelled"}:
        raise ValueError("invalid terminal Task outcome")
    task = connection.execute(
        """
        SELECT project_id, spec_revision, spec_json, outcome
        FROM tasks WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if not task:
        raise RuntimeError(f"Task terminal target does not exist: {task_id}")
    active = connection.execute(
        """
        SELECT id FROM host_jobs
        WHERE task_id = ?
          AND status IN ('dispatching', 'running', 'cancelling')
        ORDER BY sequence
        """,
        (task_id,),
    ).fetchall()
    if active:
        raise RuntimeError(
            "Task cannot become terminal before every HostJob is terminal"
        )
    try:
        spec = json.loads(task["spec_json"])
    except json.JSONDecodeError:
        spec = {}
    authorization = (
        spec.get("authorization")
        if isinstance(spec, dict)
        else None
    )
    authorization_sha256 = (
        hashlib.sha256(
            json.dumps(
                authorization,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if isinstance(authorization, dict)
        else None
    )
    secret_reference_hashes = sorted(_secret_reference_hashes(spec))
    revocation = {
        "spec_revision": int(task["spec_revision"]),
        "authorization_sha256": authorization_sha256,
        "secret_reference_hashes": secret_reference_hashes,
        "revoked_at": now,
    }
    write_task_fields(
        connection,
        task_id,
        {
            "public_status": "done",
            "phase": "done",
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": None,
            "next_action_kind": None,
            "outcome": outcome,
            "terminal_capabilities_revoked_at": now,
            "updated_at": now,
        },
    )
    connection.execute(
        """
        UPDATE session_generations SET status = 'archived', ended_at = ?
        WHERE status = 'active' AND task_session_id IN (
            SELECT id FROM task_sessions WHERE task_id = ?
        )
        """,
        (now, task_id),
    )
    connection.execute(
        """
        INSERT INTO task_events(
            project_id, task_id, kind, detail_json, created_at
        ) VALUES (?, ?, 'temporary_capabilities_revoked', ?, ?)
        """,
        (
            task["project_id"],
            task_id,
            json.dumps(
                revocation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )
    return revocation


def _secret_reference_hashes(value: object, key: str = "") -> set[str]:
    hashes: set[str] = set()
    normalized = key.lower().replace("-", "_")
    if (
        "secret" in normalized
        and ("ref" in normalized or "reference" in normalized)
        and value not in (None, "", [], {})
    ):
        hashes.add(
            hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        return hashes
    if isinstance(value, dict):
        for child_key, child in value.items():
            hashes.update(_secret_reference_hashes(child, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            hashes.update(_secret_reference_hashes(child, key))
    return hashes


def apply_model_budget_fact(
    connection: sqlite3.Connection,
    task_id: str,
    fact: dict[str, object],
) -> None:
    task = connection.execute(
        "SELECT project_id, outcome FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not task or task["outcome"] is not None:
        return
    now = time.time()
    write_task_fields(
        connection,
        task_id,
        {
            "public_status": "needs_decision",
            "phase": "provider_recovery",
            "wait_reason": str(fact["detail"]),
            "fault_code": "scope",
            "next_action_at": None,
            "next_action_kind": None,
            "updated_at": now,
        },
    )
    connection.execute(
        """
        INSERT INTO task_events(
            project_id, task_id, kind, detail_json, created_at
        ) VALUES (?, ?, 'model_budget_reached', ?, ?)
        """,
        (
            task["project_id"],
            task_id,
            json.dumps(
                {
                    "calls": fact["calls"],
                    "max_model_calls": fact["max_model_calls"],
                    "normalized_tokens": fact["normalized_tokens"],
                    "max_total_tokens": fact["max_total_tokens"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            now,
        ),
    )


def migrate_legacy_needs_decision_outcomes(
    connection: sqlite3.Connection,
) -> None:
    if _WRITE_ORIGIN.get() != "schema_migration":
        raise RuntimeError("legacy Task repair requires schema migration scope")
    connection.execute(
        """
        UPDATE tasks SET outcome = NULL
        WHERE public_status = 'needs_decision' AND outcome = 'needs_decision'
        """
    )
