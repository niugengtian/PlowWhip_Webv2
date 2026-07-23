from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from .intake import canonical_json
from .store import Store


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_task(store: Store, connection: sqlite3.Connection, task: sqlite3.Row) -> str:
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    base = store.data_root / "projects" / task["project_id"] / "tasks" / task["id"]
    log_path = base / "sessions" / "deterministic" / f"sequence-{sequence:06d}.log"

    try:
        body = spec["content"].encode()
        output_root = (
            base
            / "artifacts"
            / f"revision-{task['spec_revision']:06d}"
            / f"execution-{sequence:06d}"
            / "output"
        )
        output_path = output_root / spec["target"]
        output_path.resolve().relative_to(output_root.resolve())
        _write_atomic(output_path, body)
        digest = hashlib.sha256(body).hexdigest()
        log_body = f"wrote {spec['target']} sha256={digest}\n".encode()
        _write_atomic(log_path, log_body)
        ended_at = time.time()
        output_ref = store.relative_data_path(log_path)
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref
            ) VALUES (?, ?, ?, ?, 'command', 'succeeded', ?, ?, 0, ?)
            """,
            (
                uuid4().hex,
                task["id"],
                task["spec_revision"],
                sequence,
                started_at,
                ended_at,
                output_ref,
            ),
        )
        for kind, path, data in (("output", output_path, body), ("log", log_path, log_body)):
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, task_id, kind, path, sha256, bytes,
                    acceptance_id, revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    task["project_id"],
                    task["id"],
                    kind,
                    store.relative_data_path(path),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    "artifact_content_sha256" if kind == "output" else None,
                    task["spec_revision"],
                    ended_at,
                ),
            )
        connection.execute(
            """
            UPDATE tasks
            SET public_status = 'in_progress', phase = 'verify', next_action_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (ended_at, ended_at, task["id"]),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'executed', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json({"host_job_sequence": sequence, "sha256": digest}),
                ended_at,
            ),
        )
        return "execute"
    except OSError as error:
        ended_at = time.time()
        log_body = f"execution failed: {type(error).__name__}\n".encode()
        _write_atomic(log_path, log_body)
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref, failure_code
            ) VALUES (?, ?, ?, ?, 'command', 'failed', ?, ?, 1, ?, 'process')
            """,
            (
                uuid4().hex,
                task["id"],
                task["spec_revision"],
                sequence,
                started_at,
                ended_at,
                store.relative_data_path(log_path),
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'execute',
                wait_reason = ?, fault_code = 'process', next_action_at = NULL,
                outcome = 'needs_decision', updated_at = ? WHERE id = ?
            """,
            ("deterministic write failed; automatic path exhausted", ended_at, task["id"]),
        )
        return "execute"
