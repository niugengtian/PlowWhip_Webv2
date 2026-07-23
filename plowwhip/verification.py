from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from .execution import _write_atomic, archive_task_sessions, current_session
from .intake import canonical_json
from .provider import (
    CHECKER_PASS_MARKER,
    PROBE_TOKEN_CAP,
    record_model_call,
    run_provider_task,
)
from .store import Store


def verify_task(store: Store, connection: sqlite3.Connection, task: sqlite3.Row) -> str:
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    if spec["kind"] == "provider_task":
        return _verify_provider_task(store, connection, task, spec, started_at)
    acceptance_id = (
        f"provider_{spec['mode']}_probe"
        if spec["kind"] == "provider_probe"
        else "artifact_content_sha256"
    )
    artifact = connection.execute(
        """
        SELECT path FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND revision = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task["id"], task["spec_revision"]),
    ).fetchone()
    output_path = store.resolve_data_path(artifact["path"]) if artifact else None
    exists = bool(output_path and output_path.is_file())
    if spec["kind"] == "provider_probe":
        try:
            result = json.loads(output_path.read_text()) if output_path else {}
        except (OSError, json.JSONDecodeError):
            result = {}
        contract = (
            isinstance(result, dict)
            and result.get("provider_key") == spec["provider_key"]
            and result.get("mode") == spec["mode"]
        )
        if spec["mode"] == "zero":
            passed = bool(
                exists
                and contract
                and result.get("model_invoked") is False
                and all(
                    int(result.get(name) or 0) == 0
                    for name in (
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                )
            )
        else:
            total_tokens = int(result.get("total_tokens") or 0)
            passed = bool(
                exists
                and contract
                and result.get("model_invoked") is True
                and result.get("available") is True
                and result.get("marker_found") is True
                and result.get("returncode") == 0
                and 0 < total_tokens <= PROBE_TOKEN_CAP
            )
        evidence = {
            "acceptance_id": acceptance_id,
            "provider_key": spec["provider_key"],
            "probe_mode": spec["mode"],
            "configured": result.get("configured"),
            "available": result.get("available"),
            "model_invoked": result.get("model_invoked"),
            "input_tokens": int(result.get("input_tokens") or 0),
            "cached_input_tokens": int(result.get("cached_input_tokens") or 0),
            "output_tokens": int(result.get("output_tokens") or 0),
            "total_tokens": int(result.get("total_tokens") or 0),
            "token_cap": 0 if spec["mode"] == "zero" else PROBE_TOKEN_CAP,
            "marker_found": result.get("marker_found"),
            "passed": passed,
            "status": "PASS" if passed else "CHANGES_REQUIRED",
            "allowed_scope": "Host Bridge diagnostic only",
            "recheck": "provider_probe_contract",
        }
    else:
        expected = hashlib.sha256(spec["content"].encode()).hexdigest()
        observed = hashlib.sha256(output_path.read_bytes()).hexdigest() if exists else None
        passed = bool(exists and observed == expected)
        evidence = {
            "acceptance_id": acceptance_id,
            "expected_sha256": expected,
            "observed_sha256": observed,
            "output_exists": exists,
            "passed": passed,
            "status": "PASS" if passed else "CHANGES_REQUIRED",
            "allowed_scope": spec["target"],
            "recheck": "sha256_matches_spec",
        }
    now = time.time()
    executor = connection.execute(
        """
        SELECT settings_json FROM task_sessions
        WHERE task_id = ? AND role_key = ?
        """,
        (task["id"], task["role_key"] or "deterministic"),
    ).fetchone()
    settings = json.loads(executor["settings_json"]) if executor else {"values": {}}
    max_retries = int(settings.get("values", {}).get("retry_count", 0))
    will_repair = not passed and task["retry_count"] < max_retries
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    task_session_id, session_generation = current_session(
        connection, task["id"], task["checker_role_key"] or "deterministic_checker"
    )
    evidence["next"] = "repair" if will_repair else ("done" if passed else "needs_decision")
    evidence["verified_at"] = now
    evidence_body = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
    evidence_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"check-{sequence:06d}"
        / "evidence"
        / f"{acceptance_id}.json"
    )
    _write_atomic(evidence_path, evidence_body)
    connection.execute(
        """
        INSERT INTO artifacts(
            id, project_id, task_id, kind, path, sha256, bytes,
            acceptance_id, revision, created_at
        ) VALUES (?, ?, ?, 'evidence', ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task["project_id"],
            task["id"],
            store.relative_data_path(evidence_path),
            hashlib.sha256(evidence_body).hexdigest(),
            len(evidence_body),
            acceptance_id,
            task["spec_revision"],
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status,
            started_at, ended_at, returncode, output_ref
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'succeeded', ?, ?, 0, ?)
        """,
        (
            uuid4().hex,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            started_at,
            now,
            store.relative_data_path(evidence_path),
        ),
    )
    if passed:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'done', phase = 'done', wait_reason = NULL,
                fault_code = NULL, next_action_at = NULL, outcome = 'done', updated_at = ?
            WHERE id = ?
            """,
            (now, task["id"]),
        )
        archive_task_sessions(connection, task["id"], now)
    elif will_repair:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'in_progress', phase = 'repair',
                wait_reason = ?,
                fault_code = 'verification', retry_count = retry_count + 1,
                next_action_at = ?, updated_at = ? WHERE id = ?
            """,
            (
                (
                    "Provider probe contract failed; bounded retry scheduled"
                    if spec["kind"] == "provider_probe"
                    else "output hash mismatch; deterministic repair scheduled"
                ),
                now,
                now,
                task["id"],
            ),
        )
    else:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'verify',
                wait_reason = ?,
                fault_code = 'verification', next_action_at = NULL,
                outcome = NULL, updated_at = ? WHERE id = ?
            """,
            (
                (
                    "minimal Token probe did not produce verified terminal evidence"
                    if spec["kind"] == "provider_probe"
                    else "output hash does not satisfy acceptance"
                ),
                now,
                task["id"],
            ),
        )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'verified', ?, ?)
        """,
        (task["project_id"], task["id"], canonical_json(evidence), now),
    )
    return "verify"


def _verify_provider_task(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    started_at: float,
) -> str:
    acceptance_id = "independent_checker_pass"
    artifact = connection.execute(
        """
        SELECT path FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND revision = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task["id"], task["spec_revision"]),
    ).fetchone()
    output_path = store.resolve_data_path(artifact["path"]) if artifact else None
    try:
        execution = json.loads(output_path.read_text()) if output_path else {}
    except (OSError, json.JSONDecodeError):
        execution = {}
    task_session_id, session_generation = current_session(
        connection, task["id"], task["checker_role_key"] or "independent_checker"
    )
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?", (task_session_id,)
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    try:
        checker = run_provider_task(
            generation["provider_key"],
            str(spec["project_path"]),
            _checker_prompt(task, spec, execution),
            session_id=generation["external_session_id"],
            access="read",
            timeout_seconds=int(settings.get("max_runtime_seconds", 600)),
        )
        if checker["session_id"]:
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (checker["session_id"], task_session_id, session_generation),
            )
        record_model_call(
            connection,
            task["id"],
            task_session_id,
            session_generation,
            generation["provider_key"],
            "single",
            int(checker["input_tokens"]),
            int(checker["cached_input_tokens"]),
            int(checker["output_tokens"]),
            str(checker["model"]),
        )
    except (OSError, RuntimeError, ValueError) as error:
        checker = {
            "returncode": 1,
            "stdout": "",
            "stderr": f"checker unavailable: {type(error).__name__}",
            "session_id": generation["external_session_id"],
        }
    checker_stdout = str(checker["stdout"])
    workspace_changed = bool(execution.get("workspace_changed"))
    passed = bool(
        execution
        and execution.get("returncode") == 0
        and workspace_changed
        and checker["returncode"] == 0
        and CHECKER_PASS_MARKER in checker_stdout
    )
    executor = connection.execute(
        """
        SELECT settings_json FROM task_sessions
        WHERE task_id = ? AND role_key = ?
        """,
        (task["id"], task["role_key"] or "fullstack"),
    ).fetchone()
    executor_settings = json.loads(executor["settings_json"]) if executor else {"values": {}}
    max_retries = int(executor_settings.get("values", {}).get("retry_count", 0))
    will_repair = not passed and task["retry_count"] < max_retries
    now = time.time()
    evidence = {
        "acceptance_id": acceptance_id,
        "provider_key": spec["provider_key"],
        "executor_returncode": execution.get("returncode"),
        "workspace_changed": workspace_changed,
        "before": execution.get("before"),
        "after": execution.get("after"),
        "checker_returncode": checker["returncode"],
        "checker_output_sha256": hashlib.sha256(checker_stdout.encode()).hexdigest(),
        "checker_output_tail": checker_stdout.encode()[-4096:].decode(errors="replace"),
        "passed": passed,
        "status": "PASS" if passed else "CHANGES_REQUIRED",
        "allowed_scope": spec["project_path"],
        "recheck": "workspace_delta_and_independent_checker",
        "next": "repair" if will_repair else ("done" if passed else "needs_decision"),
        "verified_at": now,
    }
    evidence_body = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    evidence_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"check-{sequence:06d}"
        / "evidence"
        / f"{acceptance_id}.json"
    )
    _write_atomic(evidence_path, evidence_body)
    connection.execute(
        """
        INSERT INTO artifacts(
            id, project_id, task_id, kind, path, sha256, bytes,
            acceptance_id, revision, created_at
        ) VALUES (?, ?, ?, 'evidence', ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task["project_id"],
            task["id"],
            store.relative_data_path(evidence_path),
            hashlib.sha256(evidence_body).hexdigest(),
            len(evidence_body),
            acceptance_id,
            task["spec_revision"],
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status,
            started_at, ended_at, returncode, output_ref, failure_code
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            "succeeded" if checker["returncode"] == 0 else "failed",
            started_at,
            now,
            int(checker["returncode"]),
            store.relative_data_path(evidence_path),
            None if checker["returncode"] == 0 else "provider",
        ),
    )
    if passed:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'done', phase = 'done', wait_reason = NULL,
                fault_code = NULL, next_action_at = NULL, outcome = 'done', updated_at = ?
            WHERE id = ?
            """,
            (now, task["id"]),
        )
        archive_task_sessions(connection, task["id"], now)
    elif will_repair:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'in_progress', phase = 'repair',
                wait_reason = ?, fault_code = 'verification',
                retry_count = retry_count + 1, next_action_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                (
                    "No workspace delta was proven"
                    if not workspace_changed
                    else f"Independent checker requested changes: {evidence['checker_output_tail']}"
                ),
                now,
                now,
                task["id"],
            ),
        )
    else:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'verify',
                wait_reason = ?, fault_code = 'verification', next_action_at = NULL,
                outcome = NULL, updated_at = ? WHERE id = ?
            """,
            (
                (
                    "code task produced no workspace delta"
                    if not workspace_changed
                    else "independent checker did not return PASS"
                ),
                now,
                task["id"],
            ),
        )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'verified', ?, ?)
        """,
        (task["project_id"], task["id"], canonical_json(evidence), now),
    )
    return "verify"


def _checker_prompt(task: sqlite3.Row, spec: dict, execution: dict) -> str:
    return (
        "Independently inspect the current workspace read-only. Verify this Task against "
        f"the actual files and smallest relevant checks:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.\n"
        f"Control-plane workspace delta recorded: {bool(execution.get('workspace_changed'))}.\n"
        f"Executor tail:\n{str(execution.get('stdout_tail') or '')[-4000:]}\n"
        f"If the Task is complete, finish with exactly {CHECKER_PASS_MARKER}. "
        "Otherwise finish with PLOWWHIP_CHECKER_CHANGES_REQUIRED: <concise reason>. "
        "Do not modify files, commit, deploy, send messages, or create external effects."
    )
