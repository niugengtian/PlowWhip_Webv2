from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from plow_whip_web.domain.model import (
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
)
from plow_whip_web.store.database import Database
from plow_whip_web.runtime.continuity import (
    bounded_same_task_object,
    resolve_continuity_limits,
)
from plow_whip_web.store.settings_repository import DEFAULT_SETTINGS


SIZE_ORDER = {"small": 0, "medium": 1, "large": 2}


class ButlerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        project_id: str,
        source: str,
        instruction: str,
        structured_input: dict[str, Any] | None,
        model_size: str | None,
        confidence: int,
        proposal: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if source not in {"structured", "natural_language"}:
            raise InvalidTransitionError("unsupported Butler intake source")
        payload = structured_input or {}
        deterministic = deterministic_size(instruction, payload)
        assessed = _max_size(deterministic, model_size)
        confidence = max(0, min(100, int(confidence)))
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT id FROM butler_intakes WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                return self._view(connection, duplicate["id"])
            if connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone() is None:
                raise NotFoundError(f"project not found: {project_id}")
            intake_id = str(uuid.uuid4())
            question = None
            if assessed == "large" and confidence < 95:
                status = "clarifying"
                question = "请补充当前方案中最关键的未决约束，以便达到 95% 把握。"
            elif assessed == "large":
                if proposal is None:
                    proposal = _default_proposal(instruction, payload)
                status = "awaiting_confirmation"
            else:
                proposal = proposal or _default_proposal(instruction, payload)
                status = "dispatching"
            proposal_hash = _hash(proposal) if proposal is not None else None
            question_id = str(uuid.uuid4()) if question else None
            connection.execute(
                """
                INSERT INTO butler_intakes(
                    id, project_id, source, instruction, input_json, status,
                    deterministic_size, assessed_size, confidence,
                    current_question_id, proposal_json, proposal_hash,
                    idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intake_id, project_id, source, instruction, _dump(payload),
                    status, deterministic, assessed, confidence, question_id,
                    _dump(proposal) if proposal is not None else None,
                    proposal_hash, idempotency_key,
                ),
            )
            if question:
                connection.execute(
                    "INSERT INTO butler_questions(id,intake_id,question) VALUES (?,?,?)",
                    (question_id, intake_id, question),
                )
            self._event(
                connection, intake_id, 0, "intake.created", "owner", None,
                "unified intake", {"source": source, "question": question},
                f"{idempotency_key}:created",
            )
            return self._view(connection, intake_id)

    def answer(
        self,
        intake_id: str,
        *,
        expected_revision: int,
        answer: str,
        confidence: int,
        proposal: dict[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = self._duplicate(connection, idempotency_key)
            if duplicate:
                return self._view(connection, duplicate)
            row = self._row(connection, intake_id)
            self._cas(row, expected_revision)
            if row["status"] != "clarifying" or not row["current_question_id"]:
                raise InvalidTransitionError("intake has no unanswered question")
            connection.execute(
                """
                UPDATE butler_questions SET answer = ?, answered_at = CURRENT_TIMESTAMP
                WHERE id = ? AND answered_at IS NULL
                """,
                (answer, row["current_question_id"]),
            )
            confidence = max(0, min(100, int(confidence)))
            next_revision = int(row["revision"]) + 1
            question_id = None
            if confidence < 95:
                status = "clarifying"
                question_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO butler_questions(id,intake_id,question)
                    VALUES (?,?,?)
                    """,
                    (question_id, intake_id, "仍有一个关键约束未确认，请继续补充。"),
                )
            else:
                status = "awaiting_confirmation"
                proposal = proposal or _default_proposal(
                    row["instruction"], json.loads(row["input_json"])
                )
            proposal_json = _dump(proposal) if proposal is not None else row["proposal_json"]
            proposal_hash = _hash(json.loads(proposal_json)) if proposal_json else None
            connection.execute(
                """
                UPDATE butler_intakes SET status = ?, revision = ?, confidence = ?,
                    current_question_id = ?, proposal_json = ?, proposal_hash = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND revision = ?
                """,
                (
                    status, next_revision, confidence, question_id, proposal_json,
                    proposal_hash, intake_id, expected_revision,
                ),
            )
            self._event(
                connection, intake_id, next_revision, "intake.answer_recorded",
                "owner", None, "clarification answer", {"confidence": confidence},
                idempotency_key,
            )
            return self._view(connection, intake_id)

    def confirm(
        self,
        intake_id: str,
        *,
        expected_revision: int,
        proposal_hash: str,
        approved: bool,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = self._duplicate(connection, idempotency_key)
            if duplicate:
                return self._view(connection, duplicate)
            row = self._row(connection, intake_id)
            self._cas(row, expected_revision)
            if row["status"] != "awaiting_confirmation":
                raise InvalidTransitionError("intake is not awaiting owner confirmation")
            if int(row["confidence"]) < 95:
                raise InvalidTransitionError("large intake cannot dispatch below 95 confidence")
            if proposal_hash != row["proposal_hash"]:
                raise RevisionConflictError("proposal hash changed before confirmation")
            next_revision = expected_revision + 1
            status = "dispatching" if approved else "interrupted"
            connection.execute(
                """
                UPDATE butler_intakes SET status = ?, revision = ?,
                    confirmed_proposal_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (
                    status, next_revision, proposal_hash if approved else None,
                    intake_id, expected_revision,
                ),
            )
            self._event(
                connection, intake_id, next_revision, "intake.confirmed" if approved
                else "intake.rejected", "owner", None, reason,
                {"approved": approved, "proposal_hash": proposal_hash},
                idempotency_key,
            )
            return self._view(connection, intake_id)

    def dispatched(
        self,
        intake_id: str,
        *,
        expected_revision: int,
        goal_id: str,
        provider: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = self._duplicate(connection, idempotency_key)
            if duplicate:
                return self._view(connection, duplicate)
            row = self._row(connection, intake_id)
            self._cas(row, expected_revision)
            if row["status"] != "dispatching":
                raise InvalidTransitionError("intake is not dispatchable")
            if row["assessed_size"] == "large" and (
                int(row["confidence"]) < 95
                or row["confirmed_proposal_hash"] != row["proposal_hash"]
            ):
                raise InvalidTransitionError("large intake lacks confirmed proposal")
            revision = expected_revision + 1
            connection.execute(
                """
                UPDATE butler_intakes SET status = 'dispatched', revision = ?,
                    goal_id = ?, selected_provider = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revision = ?
                """,
                (revision, goal_id, provider, intake_id, expected_revision),
            )
            tasks = connection.execute(
                """
                SELECT id, provider FROM tasks
                WHERE goal_id = ? AND status = 'ready'
                  AND work_item_kind != 'coordination'
                ORDER BY ordinal, id
                """,
                (goal_id,),
            ).fetchall()
            for task in tasks:
                connection.execute(
                    """
                    INSERT INTO outbox_events(
                        topic, aggregate_id, event_type, payload_json
                    ) VALUES ('task', ?, 'worker.wake_requested', ?)
                    """,
                    (
                        task["id"],
                        _dump({
                            "intake_id": intake_id,
                            "goal_id": goal_id,
                            "task_id": task["id"],
                            "provider": task["provider"],
                            "proof": "request_only_not_execution_completion",
                        }),
                    ),
                )
            self._event(
                connection, intake_id, revision, "intake.dispatched", "butler",
                None, "confirmed dispatch", {"goal_id": goal_id, "provider": provider},
                idempotency_key,
            )
            return self._view(connection, intake_id)

    def interrupt(
        self, intake_id: str, *, expected_revision: int, reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = self._duplicate(connection, idempotency_key)
            if duplicate:
                return self._view(connection, duplicate)
            row = self._row(connection, intake_id)
            self._cas(row, expected_revision)
            if row["status"] in {"interrupted", "failed"}:
                return self._view(connection, intake_id)
            revision = expected_revision + 1
            connection.execute(
                """
                UPDATE butler_intakes SET status = 'interrupted', revision = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND revision = ?
                """,
                (revision, intake_id, expected_revision),
            )
            self._event(
                connection, intake_id, revision, "intake.interrupted", "owner",
                None, reason, {}, idempotency_key,
            )
            return self._view(connection, intake_id)

    def get(self, intake_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            return self._view(connection, intake_id)
        finally:
            connection.close()

    def create_help(
        self, *, project_id: str, task_id: str, worker_id: str | None,
        category: str, severity: str, question: str,
        checkpoint: dict[str, Any], idempotency_key: str,
    ) -> dict[str, Any]:
        if severity not in {"normal", "blocking", "extreme"}:
            raise InvalidTransitionError("invalid help severity")
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT id FROM worker_help_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._help_view(connection, duplicate["id"])
            task = connection.execute(
                "SELECT goal_id, worker_id FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if worker_id is not None and worker_id != task["worker_id"]:
                raise InvalidTransitionError("help worker is not bound to this task")
            limits = _continuity_limits(connection, project_id, task_id)
            checkpoint = bounded_same_task_object(
                checkpoint, task_id,
                maximum_bytes=limits["values"]["checkpoint_max_bytes"],
                label="help checkpoint",
            )
            help_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO worker_help_requests(
                    id,project_id,goal_id,task_id,worker_id,category,severity,
                    status,question,checkpoint_json,idempotency_key
                ) VALUES (?,?,?,?,?,?,?,'open',?,?,?)
                """,
                (help_id, project_id, task["goal_id"], task_id, worker_id,
                 category, severity, question, _dump(checkpoint), idempotency_key),
            )
            connection.execute(
                """
                INSERT INTO outbox_events(topic,aggregate_id,event_type,payload_json)
                VALUES ('help',?,'worker.help_requested',?)
                """,
                (help_id, _dump({"task_id": task_id, "severity": severity})),
            )
            return self._help_view(connection, help_id)

    def get_help(self, help_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            return self._help_view(connection, help_id)
        finally:
            connection.close()

    def list_help(
        self,
        *,
        project_id: str | None = None,
        goal_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        connection: Any | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("goal_id", goal_id),
            ("task_id", task_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        owns_connection = connection is None
        connection = connection or self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT id FROM worker_help_requests {where}
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (*values, max(1, min(int(limit), 100))),
            ).fetchall()
            return [self._help_view(connection, row["id"]) for row in rows]
        finally:
            if owns_connection:
                connection.close()

    def reply_help(
        self, help_id: str, *, expected_revision: int, sender: str,
        content: str, bounded_context: dict[str, Any], escalate: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            duplicate = connection.execute(
                "SELECT help_id FROM worker_help_replies WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                return self._help_view(connection, duplicate["help_id"])
            row = connection.execute(
                "SELECT * FROM worker_help_requests WHERE id = ?", (help_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"help request not found: {help_id}")
            self._cas(row, expected_revision)
            owner_resolution = (
                row["status"] == "owner_escalated"
                and sender == "owner"
                and not escalate
            )
            if escalate and sender != "butler":
                raise InvalidTransitionError("only Butler can request owner escalation")
            if sender == "owner" and not owner_resolution:
                raise InvalidTransitionError("owner can only resolve an escalated request")
            if row["status"] != "open" and not owner_resolution:
                raise InvalidTransitionError("help request is not open")
            if escalate and connection.execute(
                """
                SELECT 1 FROM worker_help_requests
                WHERE task_id = ? AND status = 'owner_escalated' AND id != ?
                """,
                (row["task_id"], help_id),
            ).fetchone():
                raise InvalidTransitionError(
                    "task already has one open owner escalation"
                )
            limits = _continuity_limits(
                connection, row["project_id"], row["task_id"]
            )
            bounded_context = bounded_same_task_object(
                bounded_context, row["task_id"],
                maximum_bytes=limits["values"]["handoff_max_bytes"],
                label="Butler reply context",
            )
            revision = expected_revision + 1
            status = "owner_escalated" if escalate else "answered"
            event_type = (
                "owner.escalation_requested"
                if escalate
                else "owner.escalation_resolved"
                if owner_resolution
                else "butler.help_replied"
                if sender == "butler"
                else "system.help_replied"
            )
            connection.execute(
                """
                UPDATE worker_help_requests SET status = ?, revision = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    resolved_at = CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END
                WHERE id = ? AND revision = ?
                """,
                (status, revision, 1 if escalate else 0, help_id, expected_revision),
            )
            connection.execute(
                """
                INSERT INTO worker_help_replies(
                    id,help_id,revision,sender,content,bounded_context_json,
                    idempotency_key
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (str(uuid.uuid4()), help_id, revision, sender, content,
                 _dump(bounded_context), idempotency_key),
            )
            connection.execute(
                """
                INSERT INTO outbox_events(topic,aggregate_id,event_type,payload_json)
                VALUES ('help',?,?,?)
                """,
                (help_id, event_type,
                 _dump({"task_id": row["task_id"], "revision": revision,
                        "bounded_context": bounded_context})),
            )
            return self._help_view(connection, help_id)

    @staticmethod
    def _event(connection: Any, intake_id: str, revision: int, event_type: str,
               actor_type: str, actor_id: str | None, reason: str,
               payload: dict[str, Any], idempotency_key: str) -> None:
        connection.execute(
            """
            INSERT INTO butler_events(
                intake_id,revision,event_type,actor_type,actor_id,reason,
                payload_json,idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (intake_id, revision, event_type, actor_type, actor_id, reason,
             _dump(payload), idempotency_key),
        )

    @staticmethod
    def _cas(row: Any, expected_revision: int) -> None:
        if int(row["revision"]) != expected_revision:
            raise RevisionConflictError(
                f"expected revision {expected_revision}, current revision {row['revision']}"
            )

    @staticmethod
    def _duplicate(connection: Any, key: str) -> str | None:
        row = connection.execute(
            "SELECT intake_id FROM butler_events WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row["intake_id"] if row else None

    @staticmethod
    def _row(connection: Any, intake_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM butler_intakes WHERE id = ?", (intake_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Butler intake not found: {intake_id}")
        return row

    def _view(self, connection: Any, intake_id: str) -> dict[str, Any]:
        row = self._row(connection, intake_id)
        item = dict(row)
        item["input"] = json.loads(item.pop("input_json"))
        item["proposal"] = json.loads(item.pop("proposal_json")) if item["proposal_json"] else None
        item["questions"] = [dict(value) for value in connection.execute(
            "SELECT * FROM butler_questions WHERE intake_id = ? ORDER BY asked_at,id",
            (intake_id,),
        ).fetchall()]
        item["events"] = [
            {**dict(value), "payload": json.loads(value["payload_json"])}
            for value in connection.execute(
                "SELECT * FROM butler_events WHERE intake_id = ? ORDER BY sequence",
                (intake_id,),
            ).fetchall()
        ]
        for event in item["events"]:
            event.pop("payload_json", None)
        return item

    @staticmethod
    def _help_view(connection: Any, help_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM worker_help_requests WHERE id = ?", (help_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"help request not found: {help_id}")
        item = dict(row)
        item["checkpoint"] = json.loads(item.pop("checkpoint_json"))
        item["replies"] = [
            {**dict(reply), "bounded_context": json.loads(reply["bounded_context_json"])}
            for reply in connection.execute(
                "SELECT * FROM worker_help_replies WHERE help_id = ? ORDER BY revision",
                (help_id,),
            ).fetchall()
        ]
        for reply in item["replies"]:
            reply.pop("bounded_context_json", None)
        return item


def deterministic_size(instruction: str, payload: dict[str, Any]) -> str:
    plan = payload.get("plan_items") or payload.get("tasks") or []
    risk = str(payload.get("risk_level") or "").lower()
    lowered = instruction.lower()
    high_risk = risk == "high" or any(
        marker in lowered for marker in (
            "deploy", "migration", "security", "删除", "部署", "迁移", "生产"
        )
    )
    if len(plan) >= 4 or high_risk or len(instruction) > 1600:
        return "large"
    if len(plan) >= 2 or len(instruction) > 400:
        return "medium"
    return "small"


def _max_size(deterministic: str, model_size: str | None) -> str:
    if model_size not in SIZE_ORDER:
        return deterministic
    return max((deterministic, model_size), key=SIZE_ORDER.__getitem__)


def _default_proposal(instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"objective": instruction, "plan_items": payload.get("plan_items") or []}


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _continuity_limits(
    connection: Any, project_id: str | None, task_id: str
) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    row = connection.execute(
        "SELECT settings_json FROM system_settings WHERE id = 1"
    ).fetchone()
    if row is not None:
        settings.update({
            key: value for key, value in json.loads(row["settings_json"]).items()
            if key in DEFAULT_SETTINGS
        })
    conventions: list[dict[str, Any]] = []
    scopes = [("global", "global")]
    if project_id:
        scopes.append(("project", project_id))
    scopes.append(("task", task_id))
    for scope, scope_id in scopes:
        convention = connection.execute(
            "SELECT scope,scope_id,content,revision FROM conventions "
            "WHERE scope = ? AND scope_id = ?",
            (scope, scope_id),
        ).fetchone()
        if convention is not None:
            conventions.append(dict(convention))
    return resolve_continuity_limits(settings, conventions)
