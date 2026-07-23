from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from .execution import _write_atomic, archive_task_sessions, current_session
from .intake import canonical_json
from .provider import PROBE_TOKEN_CAP
from .store import Store


def verify_task(store: Store, connection: sqlite3.Connection, task: sqlite3.Row) -> str:
    started_at = time.time()
    spec = json.loads(task["spec_json"])
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
