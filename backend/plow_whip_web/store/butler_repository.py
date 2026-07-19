from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
)
from plow_whip_web.store.database import Database


_QUESTIONS = {
    "objective": "你最终要得到什么可验证的结果？",
    "boundaries": "这个目标允许修改什么、明确不应改变什么？",
    "acceptance": "用哪些可验证结果判断目标已经完成？",
}


class ButlerRepository:
    """Durable, model-free intake state for global and project Butlers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_project_conversation(
        self,
        *,
        project_id: str,
        source_type: str,
        source_id: str | None,
        instruction: str,
        draft: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        conversation_id = str(uuid.uuid4())
        normalized = _normalize_draft(instruction, draft)
        expected = _next_missing_field(normalized)
        confidence = _confidence(normalized)
        status = "clarifying" if expected else "awaiting_confirmation"
        proposal_hash = _proposal_hash(normalized) if not expected else None
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT id FROM butler_conversations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._get_with_connection(connection, duplicate["id"])
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND status = 'active'",
                (project_id,),
            ).fetchone() is None:
                raise NotFoundError(f"active project not found: {project_id}")
            connection.execute(
                """
                INSERT INTO butler_conversations(
                    id, scope, project_id, source_type, source_id, status,
                    confidence, expected_field, spec_json, proposal_hash,
                    idempotency_key
                ) VALUES (?, 'project', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    project_id,
                    source_type,
                    source_id,
                    status,
                    confidence,
                    expected,
                    _json(normalized),
                    proposal_hash,
                    idempotency_key,
                ),
            )
            self._append(
                connection,
                conversation_id,
                source_type,
                "instruction",
                instruction,
                {"source_id": source_id},
            )
            if expected:
                self._append(
                    connection,
                    conversation_id,
                    "project_butler",
                    "question",
                    _QUESTIONS[expected],
                    {"field": expected},
                )
            else:
                self._append_proposal(connection, conversation_id, normalized, proposal_hash)
            return self._get_with_connection(connection, conversation_id)

    def answer(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        field: str,
        values: list[str],
        sender_type: str,
    ) -> dict[str, Any]:
        clean_values = [value.strip() for value in values if value.strip()]
        if not clean_values:
            raise InvalidTransitionError("answer must contain at least one non-empty value")
        with self.database.transaction(immediate=True) as connection:
            row = self._row(connection, conversation_id)
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {row['revision']}"
                )
            if row["status"] != "clarifying":
                raise InvalidTransitionError("conversation is not accepting answers")
            if row["expected_field"] != field:
                raise InvalidTransitionError(
                    f"answer must address the one active question: {row['expected_field']}"
                )
            draft = json.loads(row["spec_json"])
            draft[field] = clean_values[0] if field == "objective" else clean_values
            expected = _next_missing_field(draft)
            confidence = _confidence(draft)
            status = "clarifying" if expected else "awaiting_confirmation"
            proposal_hash = _proposal_hash(draft) if not expected else None
            connection.execute(
                """
                UPDATE butler_conversations
                SET status = ?, revision = revision + 1, confidence = ?,
                    expected_field = ?, spec_json = ?, proposal_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    confidence,
                    expected,
                    _json(draft),
                    proposal_hash,
                    conversation_id,
                ),
            )
            self._append(
                connection,
                conversation_id,
                sender_type,
                "answer",
                "\n".join(clean_values),
                {"field": field, "values": clean_values},
            )
            if expected:
                self._append(
                    connection,
                    conversation_id,
                    "project_butler",
                    "question",
                    _QUESTIONS[expected],
                    {"field": expected},
                )
            else:
                self._append_proposal(connection, conversation_id, draft, proposal_hash)
            return self._get_with_connection(connection, conversation_id)

    def mark_dispatched(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        proposal_hash: str,
        goal_id: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = self._row(connection, conversation_id)
            if row["status"] == "dispatched" and row["goal_id"] == goal_id:
                return self._get_with_connection(connection, conversation_id)
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {row['revision']}"
                )
            if row["status"] != "awaiting_confirmation":
                raise InvalidTransitionError("conversation has no proposal to confirm")
            if row["proposal_hash"] != proposal_hash:
                raise RevisionConflictError("proposal changed; review the latest proposal")
            connection.execute(
                """
                UPDATE butler_conversations
                SET status = 'dispatched', revision = revision + 1, goal_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (goal_id, conversation_id),
            )
            self._append(
                connection,
                conversation_id,
                "human",
                "confirmation",
                "已确认目标、边界、验收标准与拆分方案",
                {"proposal_hash": proposal_hash, "goal_id": goal_id},
            )
            return self._get_with_connection(connection, conversation_id)

    def get(self, conversation_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            return self._get_with_connection(connection, conversation_id)
        finally:
            connection.close()

    def list_project(self, project_id: str) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM butler_conversations
                    WHERE project_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (project_id,),
                )
            ]
            return [self._get_with_connection(connection, item) for item in ids]
        finally:
            connection.close()

    def global_overview(self, *, workspace_root: str | None = None) -> dict[str, Any]:
        root = Path(workspace_root).expanduser().resolve() if workspace_root else None
        connection = self.database.connect()
        try:
            projects = connection.execute(
                """
                SELECT p.id, p.name, p.path, p.host_path, p.status,
                       COUNT(DISTINCT g.id) goal_count,
                       COUNT(DISTINCT CASE WHEN g.status = 'running' THEN g.id END)
                           running_goals,
                       COUNT(DISTINCT CASE WHEN t.status IN (
                           'ready', 'running', 'stopping', 'verifying', 'paused'
                       ) THEN t.id END) active_tasks,
                       COUNT(DISTINCT CASE WHEN w.released_at IS NULL THEN w.id END)
                           active_workers
                FROM projects p
                LEFT JOIN goals g ON g.project_id = p.id
                LEFT JOIN tasks t ON t.project_id = p.id
                LEFT JOIN workers w ON w.project_id = p.id
                GROUP BY p.id
                ORDER BY p.created_at DESC, p.id DESC
                """
            ).fetchall()
            items = []
            for row in projects:
                item = dict(row)
                resource_path = Path(item["host_path"] or item["path"]).expanduser().resolve()
                if root is not None and not resource_path.is_relative_to(root):
                    continue
                item["resource_path"] = str(resource_path)
                items.append(item)
            return {
                "scope": "global",
                "workspace_root": str(root) if root else None,
                "projects": items,
                "totals": {
                    "projects": len(items),
                    "running_goals": sum(int(item["running_goals"]) for item in items),
                    "active_tasks": sum(int(item["active_tasks"]) for item in items),
                    "active_workers": sum(int(item["active_workers"]) for item in items),
                },
                "canonical_sources": [
                    "projects",
                    "goals",
                    "tasks",
                    "workers",
                ],
                "model_invoked": False,
            }
        finally:
            connection.close()

    def _row(self, connection: Any, conversation_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM butler_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"butler conversation not found: {conversation_id}")
        return row

    def _get_with_connection(
        self, connection: Any, conversation_id: str
    ) -> dict[str, Any]:
        row = self._row(connection, conversation_id)
        messages = connection.execute(
            """
            SELECT id, ordinal, sender_type, kind, content, payload_json, created_at
            FROM butler_messages WHERE conversation_id = ? ORDER BY ordinal
            """,
            (conversation_id,),
        ).fetchall()
        result = dict(row)
        result["spec"] = json.loads(result.pop("spec_json"))
        result["messages"] = [
            {**dict(message), "payload": json.loads(message["payload_json"])}
            for message in messages
        ]
        for message in result["messages"]:
            message.pop("payload_json", None)
        result["direct_project_butler_url"] = (
            f"/api/projects/{row['project_id']}/butler/conversations/{conversation_id}"
            if row["project_id"]
            else None
        )
        return result

    def _append(
        self,
        connection: Any,
        conversation_id: str,
        sender_type: str,
        kind: str,
        content: str,
        payload: dict[str, Any],
    ) -> None:
        ordinal = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1
                FROM butler_messages WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO butler_messages(
                id, conversation_id, ordinal, sender_type, kind, content, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                conversation_id,
                ordinal,
                sender_type,
                kind,
                content,
                _json(payload),
            ),
        )

    def _append_proposal(
        self,
        connection: Any,
        conversation_id: str,
        draft: dict[str, Any],
        proposal_hash: str | None,
    ) -> None:
        self._append(
            connection,
            conversation_id,
            "project_butler",
            "proposal",
            "目标、边界和验收标准已明确，请由人类直接确认后再执行。",
            {"proposal_hash": proposal_hash, "spec": draft},
        )


def _normalize_draft(instruction: str, draft: dict[str, Any]) -> dict[str, Any]:
    objective = str(draft.get("objective") or instruction).strip()
    title = str(draft.get("title") or objective[:120]).strip()
    return {
        **draft,
        "title": title,
        "objective": objective,
        "boundaries": _strings(draft.get("boundaries")),
        "acceptance": _strings(draft.get("acceptance")),
        "scope": _strings(draft.get("scope")),
        "artifacts": _strings(draft.get("artifacts")),
        "constraints": _strings(draft.get("constraints")),
        "role_providers": dict(draft.get("role_providers") or {}),
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _next_missing_field(draft: dict[str, Any]) -> str | None:
    for field in ("objective", "boundaries", "acceptance"):
        value = draft.get(field)
        if not value:
            return field
    return None


def _confidence(draft: dict[str, Any]) -> int:
    return (
        (35 if draft.get("objective") else 0)
        + (30 if draft.get("boundaries") else 0)
        + (30 if draft.get("acceptance") else 0)
    )


def _proposal_hash(draft: dict[str, Any]) -> str:
    return hashlib.sha256(_json(draft).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
