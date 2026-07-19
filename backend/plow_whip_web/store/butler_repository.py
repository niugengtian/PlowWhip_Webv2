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
from plow_whip_web.runtime.goal_semantics import (
    assess_goal_semantics,
    gap_question,
    next_semantic_gap,
    structured_fields_provided,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.store.database import Database


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
        structured = structured_fields_provided(draft)
        assessment = assess_goal_semantics(normalized)
        expected = None if assessment["ready"] else next_semantic_gap(normalized)
        confidence = int(assessment["confidence"])
        status = "clarifying" if expected else "awaiting_confirmation"
        proposal_hash = _proposal_hash(normalized) if not expected else None
        auto_dispatch = _auto_dispatch_eligible(
            structured=structured,
            assessment=assessment,
            draft=normalized,
            asked_questions=0,
        )
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
                    _json({**normalized, "_intake": {
                        "structured": structured,
                        "auto_dispatch": auto_dispatch,
                        "semantic": assessment,
                        "questions_asked": 0 if expected is None else 1,
                    }}),
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
                {"source_id": source_id, "structured": structured},
            )
            if expected:
                self._append(
                    connection,
                    conversation_id,
                    "project_butler",
                    "question",
                    gap_question(expected, normalized),
                    {"field": expected, "gap": assessment["gaps"][0] if assessment["gaps"] else None},
                )
            else:
                self._append_proposal(
                    connection,
                    conversation_id,
                    normalized,
                    proposal_hash,
                    auto_dispatch=auto_dispatch,
                    structured=structured,
                )
            result = self._get_with_connection(connection, conversation_id)
            result["auto_dispatch"] = auto_dispatch
            result["structured_goal_spec"] = structured
            return result

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
            intake = dict(draft.pop("_intake", {}) or {})
            draft[field] = clean_values[0] if field == "objective" else clean_values
            assessment = assess_goal_semantics(draft)
            expected = None if assessment["ready"] else next_semantic_gap(draft)
            confidence = int(assessment["confidence"])
            status = "clarifying" if expected else "awaiting_confirmation"
            proposal_hash = _proposal_hash(draft) if not expected else None
            questions_asked = int(intake.get("questions_asked") or 0) + (1 if expected else 0)
            structured = bool(intake.get("structured"))
            auto_dispatch = False
            if not expected:
                auto_dispatch = _auto_dispatch_eligible(
                    structured=structured,
                    assessment=assessment,
                    draft=draft,
                    asked_questions=int(intake.get("questions_asked") or 0),
                )
            intake.update({
                "semantic": assessment,
                "questions_asked": questions_asked,
                "auto_dispatch": auto_dispatch,
            })
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
                    _json({**draft, "_intake": intake}),
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
                    gap_question(expected, draft),
                    {"field": expected, "gap": assessment["gaps"][0] if assessment["gaps"] else None},
                )
            else:
                self._append_proposal(
                    connection,
                    conversation_id,
                    draft,
                    proposal_hash,
                    auto_dispatch=auto_dispatch,
                    structured=structured,
                )
            result = self._get_with_connection(connection, conversation_id)
            result["auto_dispatch"] = auto_dispatch
            result["structured_goal_spec"] = structured
            return result

    def post_message(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        content: str,
        sender_type: str,
        field: str | None = None,
    ) -> dict[str, Any]:
        """Accept one conversational turn and emit the Butler's next turn."""
        clean_content = content.strip()
        if not clean_content:
            raise InvalidTransitionError("message must not be empty")
        with self.database.transaction(immediate=True) as connection:
            row = self._row(connection, conversation_id)
            if int(row["revision"]) != expected_revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision}, current {row['revision']}"
                )
            if row["status"] not in {"clarifying", "awaiting_confirmation"}:
                raise InvalidTransitionError("conversation is no longer accepting messages")

            active_field = row["expected_field"]
            if row["status"] == "clarifying":
                if field is not None and field != active_field:
                    raise InvalidTransitionError(
                        f"message must address the one active question: {active_field}"
                    )
                target_field = active_field
            else:
                if field is None:
                    raise InvalidTransitionError(
                        "select objective, boundaries, or acceptance before revising a proposal"
                    )
                target_field = field

            draft = json.loads(row["spec_json"])
            intake = dict(draft.pop("_intake", {}) or {})
            values = _message_values(clean_content)
            if target_field != "objective" and not values:
                raise InvalidTransitionError(
                    "message must contain at least one non-empty value"
                )
            draft[target_field] = clean_content if target_field == "objective" else values
            assessment = assess_goal_semantics(draft)
            expected = None if assessment["ready"] else next_semantic_gap(draft)
            confidence = int(assessment["confidence"])
            status = "clarifying" if expected else "awaiting_confirmation"
            proposal_hash = _proposal_hash(draft) if not expected else None
            questions_asked = int(intake.get("questions_asked") or 0)
            if row["status"] == "clarifying" and expected:
                questions_asked += 1
            structured = bool(intake.get("structured"))
            auto_dispatch = False
            if not expected:
                auto_dispatch = _auto_dispatch_eligible(
                    structured=structured,
                    assessment=assessment,
                    draft=draft,
                    asked_questions=int(intake.get("questions_asked") or 0),
                )
            intake.update({
                "semantic": assessment,
                "questions_asked": questions_asked,
                "auto_dispatch": auto_dispatch,
            })
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
                    _json({**draft, "_intake": intake}),
                    proposal_hash,
                    conversation_id,
                ),
            )
            self._append(
                connection,
                conversation_id,
                sender_type,
                "answer",
                clean_content,
                {
                    "field": target_field,
                    "values": values,
                    "proposal_revision": row["status"] == "awaiting_confirmation",
                },
            )
            if expected:
                self._append(
                    connection,
                    conversation_id,
                    "project_butler",
                    "question",
                    gap_question(expected, draft),
                    {"field": expected, "gap": assessment["gaps"][0] if assessment["gaps"] else None},
                )
            else:
                self._append_proposal(
                    connection,
                    conversation_id,
                    draft,
                    proposal_hash,
                    auto_dispatch=auto_dispatch,
                    structured=structured,
                )
            result = self._get_with_connection(connection, conversation_id)
            result["auto_dispatch"] = auto_dispatch
            result["structured_goal_spec"] = structured
            return result

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
        raw_spec = json.loads(result.pop("spec_json"))
        intake = dict(raw_spec.pop("_intake", {}) or {})
        result["spec"] = raw_spec
        result["auto_dispatch"] = bool(intake.get("auto_dispatch"))
        result["structured_goal_spec"] = bool(intake.get("structured"))
        result["semantic"] = intake.get("semantic")
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
        *,
        auto_dispatch: bool,
        structured: bool,
    ) -> None:
        if structured:
            message = "已收到完整结构化 GoalSpec，视为主人确认。"
        elif auto_dispatch:
            message = "中小型目标要素已充分，无需代主人推断，将自动选择 Provider 并指派。"
        else:
            message = "目标、边界和验收标准已达到语义可信度门槛，请由人类直接确认后再执行。"
        self._append(
            connection,
            conversation_id,
            "project_butler",
            "proposal",
            message,
            {
                "proposal_hash": proposal_hash,
                "spec": draft,
                "auto_dispatch": auto_dispatch,
                "structured": structured,
            },
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


def _message_values(content: str) -> list[str]:
    values = [line.strip(" \t-•") for line in content.splitlines()]
    return [value for value in values if value]


def _auto_dispatch_eligible(
    *,
    structured: bool,
    assessment: dict[str, Any],
    draft: dict[str, Any],
    asked_questions: int,
) -> bool:
    if not assessment.get("ready"):
        return False
    if structured:
        return True
    if asked_questions > 0:
        return False
    sizing = draft.get("sizing_inputs")
    if not isinstance(sizing, dict):
        return False
    try:
        preview = estimate_task_sizing(TaskSizingInputs(**{
            key: sizing[key]
            for key in TaskSizingInputs.__dataclass_fields__
            if key in sizing
        }))
    except (TypeError, KeyError, ValueError):
        return False
    return str(preview.get("size_class")) in {"XS", "S", "M"}


def _proposal_hash(draft: dict[str, Any]) -> str:
    clean = {key: value for key, value in draft.items() if not str(key).startswith("_")}
    return hashlib.sha256(_json(clean).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
