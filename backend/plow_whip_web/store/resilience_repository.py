from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
    TaskStatus,
)
from plow_whip_web.store.database import Database


SUSPENDED_TASK_STATUSES = {
    TaskStatus.NETWORK_SUSPENDED.value,
    TaskStatus.PROVIDER_SUSPENDED.value,
}


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class ResilienceRepository:
    """Canonical suspension, continuation, health, and alert convergence state."""

    model_invoked = False

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_network(
        self,
        result: Any,
        *,
        failure_threshold: int,
        recovery_successes: int,
        debounce_seconds: int = 30,
    ) -> dict[str, Any]:
        states: dict[str, str] = {}
        changed: list[dict[str, str]] = []
        with self.database.transaction(immediate=True) as connection:
            for zone, ok in (
                ("domestic", bool(result.domestic_ok)),
                ("overseas", bool(result.overseas_ok)),
            ):
                row = connection.execute(
                    "SELECT * FROM network_zone_health WHERE zone = ?", (zone,)
                ).fetchone()
                assert row is not None
                failures = 0 if ok else int(row["consecutive_failures"]) + 1
                successes = int(row["consecutive_successes"]) + 1 if ok else 0
                prior = str(row["state"])
                state = prior
                if not ok and failures >= max(1, failure_threshold):
                    state = "unavailable"
                elif ok and successes >= max(1, recovery_successes):
                    state = "available"
                connection.execute(
                    """
                    UPDATE network_zone_health
                    SET state = ?, consecutive_failures = ?,
                        consecutive_successes = ?, evidence_json = ?,
                        last_checked_at = CURRENT_TIMESTAMP,
                        changed_at = CASE WHEN state != ? THEN CURRENT_TIMESTAMP
                                          ELSE changed_at END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE zone = ?
                    """,
                    (
                        state,
                        failures,
                        successes,
                        _json(
                            {
                                "zone": zone,
                                "ok": ok,
                                "checks": list(
                                    getattr(result, f"{zone}_checks", ()) or ()
                                ),
                            }
                        ),
                        state,
                        zone,
                    ),
                )
                states[zone] = state
                if state != prior:
                    changed.append({"zone": zone, "from": prior, "to": state})

            global_offline = all(
                states[zone] == "unavailable" for zone in ("domestic", "overseas")
            )
            if global_offline:
                incident = self._upsert_incident(
                    connection,
                    fingerprint="network:global",
                    root_kind="global_network",
                    scope_key="global",
                    severity="critical",
                    title="全局网络不可用",
                    detail={"zones": states},
                    debounce_seconds=debounce_seconds,
                )
                self._resolve_incident(
                    connection, fingerprint="network:domestic", suppressed_by=incident
                )
                self._resolve_incident(
                    connection, fingerprint="network:overseas", suppressed_by=incident
                )
            else:
                self._resolve_incident(connection, fingerprint="network:global")
                for zone in ("domestic", "overseas"):
                    fingerprint = f"network:{zone}"
                    if states[zone] == "unavailable":
                        self._upsert_incident(
                            connection,
                            fingerprint=fingerprint,
                            root_kind="network_zone",
                            scope_key=zone,
                            severity="error",
                            title=f"{zone} 网络区域不可用",
                            detail={"zone": zone},
                            debounce_seconds=debounce_seconds,
                        )
                    else:
                        self._resolve_incident(
                            connection, fingerprint=fingerprint
                        )
        return {
            "zones": states,
            "global_offline": all(
                states.get(zone) == "unavailable"
                for zone in ("domestic", "overseas")
            ),
            "changed": changed,
            "model_invoked": False,
        }

    def record_provider_health(
        self,
        providers: list[dict[str, Any]],
        *,
        debounce_seconds: int = 30,
    ) -> list[dict[str, str]]:
        changed: list[dict[str, str]] = []
        with self.database.transaction(immediate=True) as connection:
            for provider in providers:
                name = str(provider["name"])
                fingerprint = f"provider:{name}"
                unavailable = (
                    not provider.get("probe_skipped")
                    and (
                        provider.get("status") != "available"
                        or provider.get("circuit_state") == "open"
                    )
                )
                if unavailable:
                    incident_id = self._upsert_incident(
                        connection,
                        fingerprint=fingerprint,
                        root_kind="provider",
                        scope_key=name,
                        severity="error",
                        title=f"Provider {name} 不可用",
                        detail={
                            "provider": name,
                            "status": provider.get("status"),
                            "circuit_state": provider.get("circuit_state"),
                            "reason": provider.get("reason"),
                            "network_zone": provider.get("network_zone"),
                        },
                        debounce_seconds=debounce_seconds,
                    )
                    changed.append(
                        {"provider": name, "state": "open", "incident_id": incident_id}
                    )
                else:
                    self._resolve_incident(
                        connection, fingerprint=fingerprint
                    )
        return changed

    def network_state(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            zones = {
                row["zone"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM network_zone_health ORDER BY zone"
                ).fetchall()
            }
        finally:
            connection.close()
        return {
            "zones": zones,
            "global_offline": bool(zones)
            and all(
                zones.get(zone, {}).get("state") == "unavailable"
                for zone in ("domestic", "overseas")
            ),
        }

    def suspend_task(
        self,
        task_id: str,
        *,
        kind: str,
        reason: str,
        incident_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if kind not in SUSPENDED_TASK_STATUSES:
            raise ValueError(f"unsupported suspension kind: {kind}")
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return dict(
                    connection.execute(
                        "SELECT * FROM tasks WHERE id = ?", (duplicate["task_id"],)
                    ).fetchone()
                )
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if task["status"] in SUSPENDED_TASK_STATUSES:
                return dict(task)
            if task["status"] not in {"ready", "running", "verifying"}:
                raise InvalidTransitionError(
                    f"cannot suspend task in state {task['status']}"
                )
            revision = int(task["revision"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, suspended_from_status = ?, suspension_reason = ?,
                    suspension_incident_id = ?, revision = ?,
                    next_eligible_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    kind,
                    task["status"],
                    reason[:1000],
                    incident_id,
                    revision,
                    task_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, payload_json, state_revision,
                    idempotency_key
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    f"task.{kind}",
                    _json(
                        {
                            "reason": reason[:1000],
                            "incident_id": incident_id,
                            "attempt_consumed": False,
                        }
                    ),
                    revision,
                    idempotency_key,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            )

    def resume_suspended(
        self,
        *,
        limit: int,
        zone_availability: dict[str, bool],
        available_providers: set[str],
    ) -> list[str]:
        resumed: list[str] = []
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('network_suspended', 'provider_suspended')
                ORDER BY updated_at, id LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
            for task in rows:
                if task["status"] == "network_suspended":
                    requirement = str(task["network_requirement"])
                    ready = (
                        requirement == "none"
                        or (
                            requirement == "any"
                            and any(zone_availability.values())
                        )
                        or zone_availability.get(requirement, False)
                    )
                else:
                    ready = str(task["provider"]) in available_providers
                if not ready:
                    continue
                revision = int(task["revision"]) + 1
                target = (
                    str(task["suspended_from_status"])
                    if task["suspended_from_status"] == "ready"
                    else "ready"
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, revision = ?, suspended_from_status = NULL,
                        suspension_reason = NULL, suspension_incident_id = NULL,
                        next_eligible_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (target, revision, task["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO task_events(
                        task_id, event_type, payload_json, state_revision,
                        idempotency_key
                    ) VALUES (?, 'task.suspension_recovered', ?, ?, ?)
                    """,
                    (
                        task["id"],
                        _json({"from": task["status"], "target": target}),
                        revision,
                        f"suspension-recovery:{task['id']}:{revision}",
                    ),
                )
                resumed.append(str(task["id"]))
        return resumed

    def grant_continuation(
        self,
        task_id: str,
        *,
        action: str,
        operator: str,
        reason: str,
        expected_revision: int,
        budget_delta: dict[str, Any],
        target_provider: str | None,
        expires_at: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM operator_continuation_grants
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                return dict(existing)
            task = connection.execute(
                "SELECT revision, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if int(task["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {task['revision']}"
                )
            grant_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO operator_continuation_grants(
                    id, task_id, action, operator, reason, task_revision,
                    budget_delta_json, target_provider, expires_at, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    task_id,
                    action,
                    operator,
                    reason,
                    expected_revision,
                    _json(budget_delta),
                    target_provider,
                    expires_at,
                    idempotency_key,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM operator_continuation_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
            )

    def operator_resume(
        self,
        task_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return dict(
                    connection.execute(
                        "SELECT * FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                )
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if int(task["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {task['revision']}"
                )
            if task["status"] not in {
                "needs_human", "network_suspended", "provider_suspended", "paused",
            }:
                raise InvalidTransitionError(
                    f"cannot continue task in state {task['status']}"
                )
            revision = int(task["revision"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', revision = ?, suspended_from_status = NULL,
                    suspension_reason = NULL, suspension_incident_id = NULL,
                    next_eligible_at = CURRENT_TIMESTAMP, manual_override = 1,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (revision, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, payload_json, state_revision,
                    idempotency_key
                ) VALUES (?, 'task.operator_continued', ?, ?, ?)
                """,
                (
                    task_id,
                    _json(
                        {
                            "from": task["status"],
                            "reason": reason,
                            "attempt_incremented": False,
                        }
                    ),
                    revision,
                    idempotency_key,
                ),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            )

    def operator_cancel(
        self,
        task_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT task_id FROM task_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return dict(
                    connection.execute(
                        "SELECT * FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                )
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if int(task["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {task['revision']}"
                )
            if task["status"] not in {
                "ready", "paused", "needs_human",
                "network_suspended", "provider_suspended",
            }:
                raise InvalidTransitionError(
                    f"cannot cancel task in state {task['status']}"
                )
            revision = int(task["revision"]) + 1
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', revision = ?, next_eligible_at = NULL,
                    last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (revision, reason[:1000], task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, payload_json, state_revision,
                    idempotency_key
                ) VALUES (?, 'task.cancelled', ?, ?, ?)
                """,
                (task_id, _json({"reason": reason}), revision, idempotency_key),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            )

    def mark_grant_applied(self, grant_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE operator_continuation_grants
                SET applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP)
                WHERE id = ?
                """,
                (grant_id,),
            )
            row = connection.execute(
                "SELECT * FROM operator_continuation_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"continuation grant not found: {grant_id}")
            return dict(row)
            return dict(
                connection.execute(
                    "SELECT * FROM operator_continuation_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
            )

    def incidents(self, *, status: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            clauses = "WHERE status = ?" if status else ""
            params = (status,) if status else ()
            rows = connection.execute(
                f"""
                SELECT * FROM incidents {clauses}
                ORDER BY CASE severity
                    WHEN 'critical' THEN 0 WHEN 'error' THEN 1
                    WHEN 'warning' THEN 2 ELSE 3 END,
                    last_seen_at DESC
                """,
                params,
            ).fetchall()
            return [
                {
                    **dict(row),
                    "detail": json.loads(row["detail_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def incident(self, incident_id: str, *, limit: int = 100) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"incident not found: {incident_id}")
            events = connection.execute(
                """
                SELECT id, event_type, source_kind, source_id,
                       detail_json, created_at
                FROM incident_events
                WHERE incident_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (incident_id, min(max(1, limit), 500)),
            ).fetchall()
            return {
                **dict(row),
                "detail": json.loads(row["detail_json"]),
                "events": [
                    {
                        **dict(event),
                        "detail": json.loads(event["detail_json"]),
                    }
                    for event in events
                ],
            }
        finally:
            connection.close()

    @staticmethod
    def _upsert_incident(
        connection: Any,
        *,
        fingerprint: str,
        root_kind: str,
        scope_key: str,
        severity: str,
        title: str,
        detail: dict[str, Any],
        debounce_seconds: int = 30,
    ) -> str:
        row = connection.execute(
            """
            SELECT * FROM incidents
            WHERE fingerprint = ? AND status != 'resolved'
            """,
            (fingerprint,),
        ).fetchone()
        if row is None:
            incident_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO incidents(
                    id, fingerprint, root_kind, scope_key, severity,
                    title, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    fingerprint,
                    root_kind,
                    scope_key,
                    severity,
                    title,
                    _json(detail),
                ),
            )
            event_type = "opened"
        else:
            incident_id = str(row["id"])
            should_emit = connection.execute(
                """
                SELECT (
                    strftime('%s', 'now') - strftime('%s', last_seen_at)
                ) >= ?
                FROM incidents WHERE id = ?
                """,
                (max(0, debounce_seconds), incident_id),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE incidents
                SET occurrence_count = occurrence_count + 1,
                    last_seen_at = CURRENT_TIMESTAMP, detail_json = ?
                WHERE id = ?
                """,
                (_json(detail), incident_id),
            )
            event_type = "observed" if should_emit else None
        if event_type:
            connection.execute(
                """
                INSERT INTO incident_events(
                    incident_id, event_type, source_kind, source_id, detail_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (incident_id, event_type, root_kind, scope_key, _json(detail)),
            )
        return incident_id

    @staticmethod
    def _resolve_incident(
        connection: Any,
        *,
        fingerprint: str,
        suppressed_by: str | None = None,
    ) -> None:
        row = connection.execute(
            """
            SELECT id FROM incidents
            WHERE fingerprint = ? AND status != 'resolved'
            """,
            (fingerprint,),
        ).fetchone()
        if row is None:
            return
        connection.execute(
            """
            UPDATE incidents
            SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )
        connection.execute(
            """
            INSERT INTO incident_events(
                incident_id, event_type, source_kind, source_id, detail_json
            ) VALUES (?, ?, 'correlator', ?, ?)
            """,
            (
                row["id"],
                "suppressed" if suppressed_by else "resolved",
                suppressed_by,
                _json({"suppressed_by": suppressed_by}),
            ),
        )


def checkpoint_hash(checkpoint: dict[str, Any]) -> str:
    return hashlib.sha256(_json(checkpoint).encode("utf-8")).hexdigest()
