from __future__ import annotations

import json
import re
import time
from pathlib import PurePosixPath
from uuid import uuid4

from .store import Store


PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WRITE_INSTRUCTION = re.compile(
    r"^(?:write|写入)\s+([^:\s]+)\s*:\s*([\s\S]*)$", re.IGNORECASE
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


def normalize_instruction(content: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    match = WRITE_INSTRUCTION.fullmatch(content.strip())
    if not match:
        return (
            {"kind": "unsupported", "instruction": content},
            [],
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
