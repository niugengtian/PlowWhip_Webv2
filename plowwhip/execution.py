from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from .intake import canonical_json
from .provider import record_model_call, run_provider_probe, run_provider_task, workspace_snapshot
from .store import Store


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_task_sessions(
    connection: sqlite3.Connection,
    project_id: str,
    task_id: str,
    now: float,
    executor_role: str = "deterministic",
    checker_role: str = "deterministic_checker",
    executor_provider: str = "local",
    checker_provider: str = "local",
    settings_overrides: dict | None = None,
) -> None:
    for role_key in (executor_role, checker_role):
        settings = _effective_settings(
            connection, project_id, (settings_overrides or {}).get(role_key, {})
        )
        worker = connection.execute(
            "SELECT id FROM workers WHERE project_id = ? AND role_key = ?",
            (project_id, role_key),
        ).fetchone()
        worker_id = worker["id"] if worker else uuid4().hex
        if not worker:
            connection.execute(
                "INSERT INTO workers(id, project_id, role_key, created_at) VALUES (?, ?, ?, ?)",
                (worker_id, project_id, role_key, now),
            )
        session_id = uuid4().hex
        connection.execute(
            """
            INSERT INTO task_sessions(
                id, task_id, worker_id, role_key, role_snapshot_json, settings_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                worker_id,
                role_key,
                canonical_json(_role_snapshot(connection, role_key, role_key == checker_role)),
                canonical_json(settings),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO session_generations(
                id, task_session_id, generation, provider_key, status, created_at
            ) VALUES (?, ?, 1, ?, 'active', ?)
            """,
            (
                uuid4().hex,
                session_id,
                executor_provider if role_key == executor_role else checker_provider,
                now,
            ),
        )


def ensure_task_sessions(
    connection: sqlite3.Connection,
    project_id: str,
    task_id: str,
    now: float,
    executor_role: str = "deterministic",
    checker_role: str = "deterministic_checker",
    executor_provider: str = "local",
    checker_provider: str = "local",
    settings_overrides: dict | None = None,
) -> None:
    count = connection.execute(
        "SELECT COUNT(*) AS count FROM task_sessions WHERE task_id = ?", (task_id,)
    ).fetchone()["count"]
    if count == 0:
        create_task_sessions(
            connection,
            project_id,
            task_id,
            now,
            executor_role,
            checker_role,
            executor_provider,
            checker_provider,
            settings_overrides,
        )
    elif count != 2:
        raise RuntimeError(f"Task {task_id} has incomplete role ownership")


def _effective_settings(
    connection: sqlite3.Connection, project_id: str, task_role: dict
) -> dict:
    rows = connection.execute(
        """
        SELECT scope, setting_key, value_json, source FROM settings
        WHERE scope = 'global' OR (scope = 'project' AND project_id = ?)
        ORDER BY CASE scope WHEN 'global' THEN 0 ELSE 1 END, rowid
        """,
        (project_id,),
    ).fetchall()
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for row in rows:
        values[row["setting_key"]] = json.loads(row["value_json"])
        sources[row["setting_key"]] = (
            f"project:{project_id}:{row['source']}"
            if row["scope"] == "project"
            else row["source"]
        )
    for key, value in task_role.items():
        values[key] = value
        sources[key] = "task_role"
    return {"values": values, "sources": sources}


def _role_snapshot(
    connection: sqlite3.Connection, role_key: str, checker_independent: bool
) -> dict:
    template_key = {
        "provider_probe": "provider_probe",
        "fullstack": "code_change",
        "independent_checker": "code_change",
    }.get(role_key, "deterministic_write")
    keys = [role_key, "v1_hard_boundaries", template_key]
    rows = connection.execute(
        """
        SELECT kind, item_key, revision, path, sha256 FROM library_items
        WHERE scope = 'global' AND project_id IS NULL
          AND item_key IN (?, ?, ?)
          AND revision = (
              SELECT MAX(latest.revision) FROM library_items latest
              WHERE latest.scope = library_items.scope
                AND latest.project_id IS library_items.project_id
                AND latest.kind = library_items.kind
                AND latest.item_key = library_items.item_key
          )
        ORDER BY kind, item_key
        """,
        keys,
    ).fetchall()
    return {
        "role_key": role_key,
        "permission": "recoverable_workspace_change",
        "checker_independent": checker_independent,
        "library": [dict(row) for row in rows],
    }


def current_session(
    connection: sqlite3.Connection, task_id: str, role_key: str
) -> tuple[str, int]:
    row = connection.execute(
        """
        SELECT task_session.id, generation.generation
        FROM task_sessions task_session
        JOIN session_generations generation
          ON generation.task_session_id = task_session.id
        WHERE task_session.task_id = ? AND task_session.role_key = ?
          AND generation.status = 'active'
        ORDER BY generation.generation DESC LIMIT 1
        """,
        (task_id, role_key),
    ).fetchone()
    if not row:
        raise RuntimeError(f"missing active TaskSession for {task_id}:{role_key}")
    return row["id"], row["generation"]


def archive_task_sessions(connection: sqlite3.Connection, task_id: str, now: float) -> None:
    connection.execute(
        """
        UPDATE session_generations SET status = 'archived', ended_at = ?
        WHERE status = 'active' AND task_session_id IN (
            SELECT id FROM task_sessions WHERE task_id = ?
        )
        """,
        (now, task_id),
    )


def rotate_task_sessions(connection: sqlite3.Connection, task_id: str, now: float) -> None:
    archive_task_sessions(connection, task_id, now)
    sessions = connection.execute(
        "SELECT id FROM task_sessions WHERE task_id = ?", (task_id,)
    ).fetchall()
    for session in sessions:
        previous = connection.execute(
            """
            SELECT generation, provider_key, handoff_ref FROM session_generations
            WHERE task_session_id = ? ORDER BY generation DESC LIMIT 1
            """,
            (session["id"],),
        ).fetchone()
        generation = previous["generation"] + 1
        connection.execute(
            """
            INSERT INTO session_generations(
                id, task_session_id, generation, provider_key, status,
                handoff_ref, created_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                uuid4().hex,
                session["id"],
                generation,
                previous["provider_key"],
                previous["handoff_ref"],
                now,
            ),
        )


def execute_task(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    purpose: str = "execute",
) -> str:
    if purpose not in {"execute", "repair"}:
        raise ValueError("execution purpose must be execute or repair")
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    if spec["kind"] == "provider_probe":
        return _execute_provider_probe(store, connection, task, spec, purpose)
    if spec["kind"] == "provider_task":
        return _execute_provider_task(store, connection, task, spec, purpose)
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    base = store.data_root / "projects" / task["project_id"] / "tasks" / task["id"]
    task_session_id, session_generation = current_session(
        connection, task["id"], task["role_key"] or "deterministic"
    )
    log_path = (
        base
        / "sessions"
        / (task["role_key"] or "deterministic")
        / f"generation-{session_generation:06d}"
        / f"sequence-{sequence:06d}.log"
    )

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
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, 0, ?)
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
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
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                "repaired" if purpose == "repair" else "executed",
                canonical_json({"host_job_sequence": sequence, "sha256": digest}),
                ended_at,
            ),
        )
        return purpose
    except OSError as error:
        ended_at = time.time()
        log_body = f"execution failed: {type(error).__name__}\n".encode()
        _write_atomic(log_path, log_body)
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, 1, ?, 'process')
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
                started_at,
                ended_at,
                store.relative_data_path(log_path),
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'execute',
                wait_reason = ?, fault_code = 'process', next_action_at = NULL,
                outcome = NULL, updated_at = ? WHERE id = ?
            """,
            ("deterministic write failed; automatic path exhausted", ended_at, task["id"]),
        )
        return purpose


def _execute_provider_task(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    purpose: str,
) -> str:
    started_at = time.time()
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    task_session_id, session_generation = current_session(
        connection, task["id"], task["role_key"] or "fullstack"
    )
    generation = connection.execute(
        """
        SELECT external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?", (task_session_id,)
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    base = store.data_root / "projects" / task["project_id"] / "tasks" / task["id"]
    output_path = (
        base
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"execution-{sequence:06d}"
        / "output"
        / "provider-execution.json"
    )
    log_path = (
        base
        / "sessions"
        / (task["role_key"] or "fullstack")
        / f"generation-{session_generation:06d}"
        / f"sequence-{sequence:06d}.log"
    )
    try:
        before = workspace_snapshot(str(spec["project_path"]))
        prompt = _provider_prompt(task, spec, purpose)
        # ponytail: synchronous V1 path; use durable Bridge jobs when concurrent code Tasks are required.
        result = run_provider_task(
            str(spec["provider_key"]),
            str(spec["project_path"]),
            prompt,
            session_id=generation["external_session_id"],
            timeout_seconds=int(settings.get("max_runtime_seconds", 600)),
        )
        after = workspace_snapshot(str(spec["project_path"]))
        before_git = before.get("git") if isinstance(before.get("git"), dict) else {}
        after_git = after.get("git") if isinstance(after.get("git"), dict) else {}
        workspace_changed = canonical_json(before_git) != canonical_json(after_git)
        manifest = {
            "provider_key": spec["provider_key"],
            "project_path": spec["project_path"],
            "returncode": result["returncode"],
            "failure_class": result["failure_class"],
            "duration_ms": result["duration_ms"],
            "workspace_changed": workspace_changed,
            "before": before_git,
            "after": after_git,
            "stdout_tail": _bounded_tail(str(result["stdout"])),
            "stderr_tail": _bounded_tail(str(result["stderr"])),
        }
        body = canonical_json(manifest).encode()
        log_body = (
            f"provider={spec['provider_key']} returncode={result['returncode']} "
            f"workspace_changed={str(workspace_changed).lower()}\n"
            f"{manifest['stdout_tail']}\n{manifest['stderr_tail']}\n"
        ).encode()
        _write_atomic(output_path, body)
        _write_atomic(log_path, log_body)
        ended_at = time.time()
        if result["session_id"]:
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (result["session_id"], task_session_id, session_generation),
            )
        record_model_call(
            connection,
            task["id"],
            task_session_id,
            session_generation,
            str(spec["provider_key"]),
            "single",
            int(result["input_tokens"]),
            int(result["cached_input_tokens"]),
            int(result["output_tokens"]),
            str(result["model"]),
        )
        succeeded = int(result["returncode"]) == 0
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
                "succeeded" if succeeded else "failed",
                started_at,
                ended_at,
                int(result["returncode"]),
                store.relative_data_path(log_path),
                None if succeeded else "provider",
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
                    "independent_checker_pass" if kind == "output" else None,
                    task["spec_revision"],
                    ended_at,
                ),
            )
        connection.execute(
            """
            UPDATE tasks SET public_status = ?, phase = ?, next_action_at = ?,
                wait_reason = ?, fault_code = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "in_progress" if succeeded else "needs_decision",
                "verify" if succeeded else "execute",
                ended_at if succeeded else None,
                None if succeeded else "Provider execution did not exit successfully",
                None if succeeded else "provider",
                ended_at,
                task["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                "repaired" if purpose == "repair" else "executed",
                canonical_json(
                    {
                        "host_job_sequence": sequence,
                        "provider_key": spec["provider_key"],
                        "returncode": result["returncode"],
                        "workspace_changed": workspace_changed,
                        "normalized_total": result["total_tokens"],
                    }
                ),
                ended_at,
            ),
        )
        return purpose
    except (OSError, RuntimeError, ValueError) as error:
        ended_at = time.time()
        log_body = f"Provider execution failed: {type(error).__name__}\n".encode()
        _write_atomic(log_path, log_body)
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, 1, ?, 'provider')
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
                started_at,
                ended_at,
                store.relative_data_path(log_path),
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'execute',
                wait_reason = ?, fault_code = 'provider', next_action_at = NULL,
                updated_at = ? WHERE id = ?
            """,
            ("Host Bridge Provider execution is unavailable", ended_at, task["id"]),
        )
        return purpose


def _provider_prompt(task: sqlite3.Row, spec: dict, purpose: str) -> str:
    repair = (
        f"\nRepair context: {task['wait_reason']}"
        if purpose == "repair" and task["wait_reason"]
        else ""
    )
    return (
        f"Complete this bounded code Task in the current workspace:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.{repair}\n"
        "Stay inside the workspace. Make only recoverable changes. Do not commit, deploy, "
        "delete permanently, expose secrets, send external messages, make purchases, or "
        "change permissions. Run the smallest relevant checks and leave the workspace ready "
        "for an independent read-only checker."
    )


def _bounded_tail(value: str, byte_cap: int = 16_384) -> str:
    body = value.encode()
    return body[-byte_cap:].decode(errors="replace")


def _execute_provider_probe(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    purpose: str,
) -> str:
    started_at = time.time()
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    task_session_id, session_generation = current_session(
        connection, task["id"], task["role_key"] or "provider_probe"
    )
    base = store.data_root / "projects" / task["project_id"] / "tasks" / task["id"]
    output_path = (
        base
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"execution-{sequence:06d}"
        / "output"
        / "provider-probe.json"
    )
    log_path = (
        base
        / "sessions"
        / (task["role_key"] or "provider_probe")
        / f"generation-{session_generation:06d}"
        / f"sequence-{sequence:06d}.log"
    )
    try:
        result = run_provider_probe(spec["provider_key"], spec["mode"])
        body = canonical_json(result).encode()
        log_body = (
            f"provider={spec['provider_key']} mode={spec['mode']} "
            f"available={result['available']} detail={result['detail']}\n"
        ).encode()
        _write_atomic(output_path, body)
        _write_atomic(log_path, log_body)
        ended_at = time.time()
        if result["model_invoked"]:
            record_model_call(
                connection,
                task["id"],
                task_session_id,
                session_generation,
                spec["provider_key"],
                "single",
                int(result["input_tokens"]),
                int(result["cached_input_tokens"]),
                int(result["output_tokens"]),
                str(result["model"] or spec["provider_key"]),
            )
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, 0, ?)
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
                started_at,
                ended_at,
                store.relative_data_path(log_path),
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
                    (
                        f"provider_{spec['mode']}_probe"
                        if kind == "output"
                        else None
                    ),
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
            VALUES (?, ?, 'provider_probed', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "provider_key": spec["provider_key"],
                        "mode": spec["mode"],
                        "available": result["available"],
                        "model_invoked": result["model_invoked"],
                        "total_tokens": result["total_tokens"],
                    }
                ),
                ended_at,
            ),
        )
        return purpose
    except (OSError, RuntimeError, ValueError) as error:
        ended_at = time.time()
        log_body = f"provider probe failed: {type(error).__name__}\n".encode()
        _write_atomic(log_path, log_body)
        connection.execute(
            """
            INSERT INTO host_jobs(
                id, task_id, task_session_id, session_generation,
                spec_revision, sequence, purpose, status,
                started_at, ended_at, returncode, output_ref, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, 1, ?, 'provider')
            """,
            (
                uuid4().hex,
                task["id"],
                task_session_id,
                session_generation,
                task["spec_revision"],
                sequence,
                purpose,
                started_at,
                ended_at,
                store.relative_data_path(log_path),
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'execute',
                wait_reason = ?, fault_code = 'provider', next_action_at = NULL,
                outcome = NULL, updated_at = ? WHERE id = ?
            """,
            ("Provider probe could not produce bounded diagnostic facts", ended_at, task["id"]),
        )
        return purpose
