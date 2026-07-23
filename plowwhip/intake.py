from __future__ import annotations

import json
import re
import time
from pathlib import PurePosixPath
from uuid import uuid4

from .store import Store


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_ID = re.compile(r"^[0-9a-f]{32}$")
WRITE_INSTRUCTION = re.compile(
    r"^(?:write|写入)\s+([^:\s]+)\s*:\s*([\s\S]*)$", re.IGNORECASE
)
PROVIDER_PROBE_INSTRUCTION = re.compile(
    r"^(?:probe\s+provider|探测\s*Provider)\s+"
    r"(codex_cli|cursor_cli|deepseek|kimi)\s*:\s*"
    r"(0token|minimal)(?:\s+确认\s+([a-z0-9_]+))?$",
    re.IGNORECASE,
)


def submit_message(
    store: Store, project_id: str, content: str, idempotency_key: str
) -> str:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must be 1-64 safe identifier characters")
    if not content or len(content.encode()) > 65_536:
        raise ValueError("message must contain 1-65536 UTF-8 bytes")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")

    now = time.time()
    message_id = uuid4().hex
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO projects(id, created_at) VALUES (?, ?)",
            (project_id, now),
        )
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project["archived_at"] is not None:
            raise ValueError("project is archived; restore it before sending messages")
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, project_id, role, content, idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?)
            """,
            (message_id, project_id, content, idempotency_key, now),
        )
        row = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
    return str(row["id"])


def create_project(
    store: Store,
    project_id: str,
    idempotency_key: str,
    host_path: str | None = None,
) -> str:
    _validate_project_action(project_id, idempotency_key)
    workspace = _normalize_host_path(host_path)
    now = time.time()
    action_id = uuid4().hex
    with store.transaction() as connection:
        duplicate = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if duplicate:
            return str(duplicate["id"])
        existing = connection.execute(
            "SELECT id, host_path, archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if existing and existing["archived_at"] is None:
            if workspace and workspace != existing["host_path"]:
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE project_id = ? AND outcome IS NULL LIMIT 1",
                    (project_id,),
                ).fetchone():
                    raise ValueError("active project workspace cannot be changed")
                connection.execute(
                    "UPDATE projects SET host_path = ? WHERE id = ?",
                    (workspace, project_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        id, project_id, role, content, action_json,
                        idempotency_key, created_at, processed_at
                    ) VALUES (?, ?, 'owner', 'bind_project_workspace', ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        project_id,
                        canonical_json({"kind": "bind_project_workspace"}),
                        idempotency_key,
                        now,
                        now,
                    ),
                )
            return project_id
        kind = "restore_project" if existing else "create_project"
        if existing:
            connection.execute(
                """
                UPDATE projects
                SET archived_at = NULL, host_path = COALESCE(?, host_path)
                WHERE id = ?
                """,
                (workspace, project_id),
            )
        else:
            connection.execute(
                "INSERT INTO projects(id, host_path, created_at) VALUES (?, ?, ?)",
                (project_id, workspace, now),
            )
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at, processed_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                project_id,
                kind,
                canonical_json({"kind": kind}),
                idempotency_key,
                now,
                now,
            ),
        )
    return action_id


def archive_project(
    store: Store, project_id: str, confirmation: str, idempotency_key: str
) -> str:
    _validate_project_action(project_id, idempotency_key)
    if confirmation != project_id:
        raise ValueError("confirmation must exactly match project_id")
    now = time.time()
    action_id = uuid4().hex
    with store.transaction() as connection:
        project = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            raise ValueError("project not found")
        if project["archived_at"] is not None:
            return project_id
        if connection.execute(
            "SELECT 1 FROM tasks WHERE project_id = ? AND outcome IS NULL LIMIT 1",
            (project_id,),
        ).fetchone():
            raise ValueError("project with an active task cannot be archived")
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at, processed_at
            ) VALUES (?, ?, 'owner', 'archive_project', ?, ?, ?, ?)
            """,
            (
                action_id,
                project_id,
                canonical_json({"kind": "archive_project"}),
                idempotency_key,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?", (now, project_id)
        )
    return action_id


def _validate_project_action(project_id: str, idempotency_key: str) -> None:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must be 1-64 safe identifier characters")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")


def _normalize_host_path(value: str | None) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("host_path must be a string")
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    if "\0" in candidate or len(candidate.encode()) > 4096:
        raise ValueError("host_path must contain at most 4096 safe UTF-8 bytes")
    path = PurePosixPath(candidate)
    if not path.is_absolute() or any(part in ("", ".", "..") for part in path.parts[1:]):
        raise ValueError("host_path must be an absolute path without traversal")
    return path.as_posix()


def submit_action(
    store: Store,
    project_id: str,
    task_id: str,
    kind: str,
    instruction: str,
    idempotency_key: str,
    plan: dict | None = None,
) -> str:
    if not PROJECT_ID.fullmatch(project_id) or not TASK_ID.fullmatch(task_id):
        raise ValueError("invalid project_id or task_id")
    if kind not in {"provide_decision", "provide_plan", "cancel", "rerun", "wake"}:
        raise ValueError(
            "supported actions: provide_decision, provide_plan, cancel, rerun, wake"
        )
    if kind == "provide_decision" and not instruction:
        raise ValueError("provide_decision requires instruction")
    if len(instruction.encode()) > 65_536:
        raise ValueError("instruction must contain at most 65536 UTF-8 bytes")
    if kind == "provide_plan" and not isinstance(plan, dict):
        raise ValueError("provide_plan requires plan")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters")

    now = time.time()
    message_id = uuid4().hex
    action = {"kind": kind, "task_id": task_id, "instruction": instruction}
    if plan is not None:
        action["plan"] = plan
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM messages WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        ).fetchone()
        if existing:
            return str(existing["id"])
        task = connection.execute(
            "SELECT public_status, outcome FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        ).fetchone()
        if not task:
            raise ValueError("task not found")
        allowed = (
            kind == "provide_decision"
            and task["public_status"] == "needs_decision"
            and task["outcome"] != "cancelled"
        ) or (
            kind == "provide_plan"
            and task["public_status"] == "needs_decision"
            and task["outcome"] is None
        ) or (kind == "cancel" and task["outcome"] is None) or (
            kind == "rerun" and task["outcome"] == "cancelled"
        ) or (
            kind == "wake"
            and task["outcome"] is None
            and task["public_status"] in {"pending", "in_progress"}
        )
        if not allowed:
            raise ValueError(f"action {kind} is not allowed for current task")
        connection.execute(
            """
            INSERT INTO messages(
                id, project_id, role, content, action_json,
                idempotency_key, created_at
            ) VALUES (?, ?, 'owner', ?, ?, ?, ?)
            """,
            (
                message_id,
                project_id,
                instruction or kind,
                canonical_json(action),
                idempotency_key,
                now,
            ),
        )
    return message_id


def normalize_instruction(content: str) -> tuple[dict[str, object], list[dict[str, str]]]:
    probe = PROVIDER_PROBE_INSTRUCTION.fullmatch(content.strip())
    if probe:
        provider_key = probe.group(1).lower()
        mode = "zero" if probe.group(2).lower() == "0token" else "minimal"
        if mode == "minimal" and (probe.group(3) or "").lower() != provider_key:
            return (
                {
                    "kind": "authorization_required",
                    "instruction": content,
                    "wait_reason": (
                        f"minimal Token probe requires exact confirmation: {provider_key}"
                    ),
                },
                [],
            )
        return (
            {
                "kind": "provider_probe",
                "provider_key": provider_key,
                "mode": mode,
            },
            [
                {
                    "id": f"provider_{mode}_probe",
                    "kind": "provider_probe_contract",
                }
            ],
        )

    match = WRITE_INSTRUCTION.fullmatch(content.strip())
    if not match:
        return (
            {
                "kind": "provider_task",
                "provider_key": "codex_cli",
                "instruction": content,
            },
            [{"id": "independent_checker_pass", "kind": "checker_verdict"}],
        )

    target = PurePosixPath(match.group(1))
    if (
        target.is_absolute()
        or not target.parts
        or any(part in ("", ".", "..") for part in target.parts)
    ):
        return (
            {"kind": "unsafe_path", "instruction": content},
            [],
        )

    spec = {"kind": "write_text", "target": target.as_posix(), "content": match.group(2)}
    acceptance = [{"id": "artifact_content_sha256", "kind": "sha256_matches_spec"}]
    return spec, acceptance


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
