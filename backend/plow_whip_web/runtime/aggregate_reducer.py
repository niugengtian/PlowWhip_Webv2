from __future__ import annotations

import json
import uuid
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
)
from plow_whip_web.store.database import Database


class AggregateReducer:
    """The single versioned write protocol for state/evidence lineage."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def record(
        connection: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        revision: int,
        idempotency_key: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
        previous_state: dict[str, Any],
        new_state: dict[str, Any],
        previous_evidence_hash: str | None = None,
        new_evidence_hash: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        existing = connection.execute(
            """
            SELECT * FROM aggregate_transitions WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing["aggregate_type"] != aggregate_type
                or existing["aggregate_id"] != aggregate_id
                or int(existing["revision"]) != revision
                or existing["actor_type"] != actor_type
                or existing["actor_id"] != actor_id
                or existing["reason"] != reason
                or existing["previous_state_json"] != _dump(previous_state)
                or existing["new_state_json"] != _dump(new_state)
                or existing["previous_evidence_hash"] != previous_evidence_hash
                or existing["new_evidence_hash"] != new_evidence_hash
                or (command_id is not None and existing["command_id"] != command_id)
            ):
                raise InvalidTransitionError(
                    "transition idempotency key reused for a different command"
                )
            return dict(existing)
        existing = connection.execute(
            """
            SELECT * FROM aggregate_transitions
            WHERE aggregate_type = ? AND aggregate_id = ? AND revision = ?
            """,
            (aggregate_type, aggregate_id, revision),
        ).fetchone()
        if existing is not None:
            raise RevisionConflictError(
                f"{aggregate_type} revision {revision} already has a transition"
            )
        command_id = command_id or str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"plow-whip:{aggregate_type}:{aggregate_id}:{idempotency_key}",
        ))
        connection.execute(
            """
            INSERT INTO aggregate_transitions(
                aggregate_type, aggregate_id, revision, command_id,
                idempotency_key, actor_type, actor_id, reason,
                previous_state_json, new_state_json,
                previous_evidence_hash, new_evidence_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aggregate_type,
                aggregate_id,
                revision,
                command_id,
                idempotency_key,
                actor_type,
                actor_id,
                reason,
                _dump(previous_state),
                _dump(new_state),
                previous_evidence_hash,
                new_evidence_hash,
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM aggregate_transitions WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        )

    def rewrite_evidence(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        expected_revision: int,
        new_evidence_hash: str,
        actor_type: str,
        actor_id: str | None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise InvalidTransitionError("evidence rewrite reason is required")
        if aggregate_type not in {"task", "goal"}:
            raise InvalidTransitionError("evidence rewrite supports task or goal")
        table = "tasks" if aggregate_type == "task" else "goals"
        id_column = "id"
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                """
                SELECT * FROM aggregate_transitions WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                if (
                    duplicate["aggregate_type"] != aggregate_type
                    or duplicate["aggregate_id"] != aggregate_id
                    or int(duplicate["revision"]) != expected_revision + 1
                    or duplicate["actor_type"] != actor_type
                    or duplicate["actor_id"] != actor_id
                    or duplicate["reason"] != reason
                    or duplicate["new_evidence_hash"] != new_evidence_hash
                ):
                    raise InvalidTransitionError(
                        "transition idempotency key reused for a different command"
                    )
                return dict(duplicate)
            row = connection.execute(
                f"""
                SELECT {id_column} id, status, revision, last_evidence_hash
                FROM {table} WHERE {id_column} = ?
                """,
                (aggregate_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"{aggregate_type} not found: {aggregate_id}")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current revision {row['revision']}"
                )
            next_revision = expected_revision + 1
            updated = connection.execute(
                f"""
                UPDATE {table}
                SET last_evidence_hash = ?, revision = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE {id_column} = ? AND revision = ?
                """,
                (
                    new_evidence_hash,
                    next_revision,
                    aggregate_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise RevisionConflictError(f"{aggregate_type} changed during rewrite")
            return self.record(
                connection,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                revision=next_revision,
                idempotency_key=idempotency_key,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                previous_state={
                    "status": row["status"],
                    "revision": expected_revision,
                },
                new_state={
                    "status": row["status"],
                    "revision": next_revision,
                },
                previous_evidence_hash=row["last_evidence_hash"],
                new_evidence_hash=new_evidence_hash,
            )

    def lineage(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        limit: int = 20,
        connection: Any | None = None,
    ) -> list[dict[str, Any]]:
        owns_connection = connection is None
        connection = connection or self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM aggregate_transitions
                WHERE aggregate_type = ? AND aggregate_id = ?
                ORDER BY revision DESC, sequence DESC LIMIT ?
                """,
                (aggregate_type, aggregate_id, max(1, min(int(limit), 100))),
            ).fetchall()
            return [_view(row) for row in reversed(rows)]
        finally:
            if owns_connection:
                connection.close()


def state_snapshot(row: Any) -> dict[str, Any]:
    return {
        "status": row.status.value if hasattr(row.status, "value") else row["status"],
        "revision": int(row.revision if hasattr(row, "revision") else row["revision"]),
    }


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _view(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["previous_state"] = json.loads(item.pop("previous_state_json"))
    item["new_state"] = json.loads(item.pop("new_state_json"))
    return item
