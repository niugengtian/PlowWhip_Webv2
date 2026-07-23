from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from uuid import uuid4

from .execution import _write_atomic, archive_task_sessions, current_session
from .intake import canonical_json
from .provider import (
    CHECKER_RESULT_PREFIX,
    PROBE_TOKEN_CAP,
    record_model_call,
    run_provider_task,
)
from .store import Store


@dataclass(frozen=True)
class CheckerStep:
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    project_path: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    execution: dict


def verify_task(
    store: Store, connection: sqlite3.Connection, task: sqlite3.Row
) -> str | CheckerStep:
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    if spec["kind"] == "provider_task":
        return _prepare_checker_step(store, connection, task, spec, started_at)
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


def _prepare_checker_step(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    started_at: float,
) -> CheckerStep:
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
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    job_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'dispatching', ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            started_at,
        ),
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'in_progress', phase = 'check_call',
            wait_reason = NULL, fault_code = NULL, next_action_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (started_at, started_at, task["id"]),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'checker_prepared', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json({"host_job_id": job_id, "sequence": sequence}),
            started_at,
        ),
    )
    return CheckerStep(
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        str(spec["project_path"]),
        _checker_prompt(task, spec, execution),
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        execution,
    )


def perform_checker_step(step: CheckerStep) -> dict[str, object]:
    try:
        return {
            "ok": True,
            "checker": run_provider_task(
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                access="read",
                timeout_seconds=step.timeout_seconds,
            ),
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}


def apply_checker_step(
    store: Store,
    connection: sqlite3.Connection,
    step: CheckerStep,
    facts: dict[str, object],
) -> str:
    task = connection.execute(
        "SELECT * FROM tasks WHERE id = ? AND project_id = ?",
        (step.task_id, step.project_id),
    ).fetchone()
    job = connection.execute(
        "SELECT * FROM host_jobs WHERE id = ? AND task_id = ?",
        (step.job_id, step.task_id),
    ).fetchone()
    if not task or not job or task["outcome"] is not None:
        return "stale_checker_fact"
    spec = json.loads(task["spec_json"])
    checker = (
        facts["checker"]
        if facts.get("ok")
        else {
            "returncode": 1,
            "stdout": "",
            "stderr": f"checker unavailable: {facts.get('error', 'unknown')}",
            "session_id": step.session_id,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "model": step.provider_key,
        }
    )
    if facts.get("ok"):
        if checker["session_id"]:
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (checker["session_id"], job["task_session_id"], job["session_generation"]),
            )
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            step.provider_key,
            "single",
            int(checker["input_tokens"]),
            int(checker["cached_input_tokens"]),
            int(checker["output_tokens"]),
            str(checker["model"]),
        )
    checker_stdout = str(checker["stdout"])
    workspace_changed = bool(step.execution.get("workspace_changed"))
    expected_acceptance = json.loads(task["acceptance_json"])
    verdict = _parse_checker_verdict(
        checker_stdout, expected_acceptance, spec["project_path"]
    )
    if not workspace_changed:
        for item in verdict["acceptances"]:
            if item["acceptance_id"] == "relevant_checks":
                item.update(
                    {
                        "passed": False,
                        "actual_evidence": "no workspace revision or effective file hash changed",
                        "recheck_command": "git status --short",
                    }
                )
        verdict["verdict"] = "CHANGES_REQUIRED"
        verdict["passed"] = False
        verdict["repair_package"] = [
            item for item in verdict["acceptances"] if not item["passed"]
        ]
    passed = bool(
        step.execution
        and step.execution.get("returncode") == 0
        and workspace_changed
        and checker["returncode"] == 0
        and verdict["passed"]
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
    will_repair = bool(
        facts.get("ok")
        and verdict["valid"]
        and verdict["verdict"] == "CHANGES_REQUIRED"
        and verdict["repair_package"]
        and task["retry_count"] < max_retries
    )
    now = time.time()
    evidence = {
        "provider_key": step.provider_key,
        "executor_returncode": step.execution.get("returncode"),
        "workspace_changed": workspace_changed,
        "before": step.execution.get("before"),
        "after": step.execution.get("after"),
        "checker_returncode": checker["returncode"],
        "checker_output_sha256": hashlib.sha256(checker_stdout.encode()).hexdigest(),
        "checker_output_tail": checker_stdout.encode()[-4096:].decode(errors="replace"),
        "checker_call_confirmed": bool(facts.get("ok")),
        "checker_contract_valid": verdict["valid"],
        "checker_verdict": verdict["verdict"],
        "acceptances": verdict["acceptances"],
        "repair_package": verdict["repair_package"],
        "decision_reason": verdict["decision_reason"],
        "passed": passed,
        "status": "PASS" if passed else "CHANGES_REQUIRED",
        "allowed_scope": spec["project_path"],
        "recheck": "workspace_delta_and_independent_checker",
        "next": (
            "repair"
            if will_repair
            else ("done" if passed else "needs_decision")
        ),
        "verified_at": now,
    }
    evidence_root = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"check-{job['sequence']:06d}"
        / "evidence"
    )
    evidence_path = evidence_root / "checker-verdict.json"
    _record_checker_evidence(
        store, connection, task, evidence_path, evidence, None, now
    )
    for acceptance in verdict["acceptances"]:
        _record_checker_evidence(
            store,
            connection,
            task,
            evidence_root / f"{acceptance['acceptance_id']}.json",
            {
                **acceptance,
                "checker_output_sha256": evidence["checker_output_sha256"],
                "verified_at": now,
            },
            acceptance["acceptance_id"],
            now,
        )
    checker_succeeded = bool(facts.get("ok") and checker["returncode"] == 0)
    connection.execute(
        """
        UPDATE host_jobs SET status = ?, ended_at = ?, returncode = ?,
            output_ref = ?, failure_code = ? WHERE id = ?
        """,
        (
            "succeeded" if checker_succeeded else "failed",
            now,
            int(checker["returncode"]),
            store.relative_data_path(evidence_path),
            None if checker_succeeded else "provider",
            job["id"],
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
                    else (
                        "Independent checker requested changes: "
                        + canonical_json(verdict["repair_package"])[:4000]
                    )
                ),
                now,
                now,
                task["id"],
            ),
        )
    elif not facts.get("ok"):
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision', phase = 'provider_recovery',
                wait_reason = 'independent Checker result is unknown; automatic replay is unsafe',
                fault_code = 'unsafe_unknown', next_action_at = NULL,
                outcome = NULL, updated_at = ? WHERE id = ?
            """,
            (now, task["id"]),
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
                    else (
                        verdict["decision_reason"]
                        or "independent checker contract did not prove every acceptance"
                    )
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
    acceptance = json.loads(task["acceptance_json"])
    return (
        "Independently inspect the current workspace read-only. Verify this Task against "
        f"the actual files and smallest relevant checks:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.\n"
        f"Frozen acceptance contract: {canonical_json(acceptance)}\n"
        f"Control-plane workspace delta recorded: {bool(execution.get('workspace_changed'))}.\n"
        f"Executor tail:\n{str(execution.get('stdout_tail') or '')[-4000:]}\n"
        f"Finish with one line beginning {CHECKER_RESULT_PREFIX!r} followed by one JSON object. "
        'Use {"verdict":"PASS|CHANGES_REQUIRED|NEEDS_DECISION",'
        '"acceptances":[{"acceptance_id":"...","passed":true,'
        '"actual_evidence":"bounded fact","recheck_command":"bounded command"}],'
        '"decision_reason":null}. Include every frozen acceptance_id exactly once. '
        "Do not modify files, commit, deploy, send messages, or create external effects."
    )


def _parse_checker_verdict(
    output: str, expected: list[dict], allowed_scope: str
) -> dict[str, object]:
    line = next(
        (
            value
            for value in reversed(output.splitlines())
            if value.startswith(CHECKER_RESULT_PREFIX)
        ),
        "",
    )
    payload: object = {}
    try:
        payload = json.loads(line[len(CHECKER_RESULT_PREFIX) :]) if line else {}
    except json.JSONDecodeError:
        payload = {}
    expected_by_id = {
        str(item["id"]): str(item.get("expected") or item.get("kind") or item["id"])
        for item in expected
        if isinstance(item, dict) and item.get("id")
    }
    raw_items = payload.get("acceptances", []) if isinstance(payload, dict) else []
    raw_by_id = {
        str(item.get("acceptance_id")): item
        for item in raw_items
        if isinstance(item, dict) and item.get("acceptance_id")
    }
    acceptances = []
    valid = (
        isinstance(payload, dict)
        and payload.get("verdict") in {"PASS", "CHANGES_REQUIRED", "NEEDS_DECISION"}
        and isinstance(raw_items, list)
        and len(raw_by_id) == len(raw_items)
        and set(raw_by_id) == set(expected_by_id)
    )
    for acceptance_id, expected_result in expected_by_id.items():
        raw = raw_by_id.get(acceptance_id, {})
        actual = str(raw.get("actual_evidence") or "").strip()
        recheck = str(raw.get("recheck_command") or "").strip()
        item_valid = (
            isinstance(raw.get("passed"), bool)
            and bool(actual)
            and bool(recheck)
            and len(actual.encode()) <= 4096
            and len(recheck.encode()) <= 1024
        )
        valid = valid and item_valid
        acceptances.append(
            {
                "acceptance_id": acceptance_id,
                "passed": bool(raw.get("passed")) if item_valid else False,
                "actual_evidence": actual[:4096] if actual else "missing checker evidence",
                "expected_result": expected_result[:4096],
                "allowed_scope": allowed_scope,
                "recheck_command": recheck[:1024] if recheck else "manual bounded recheck required",
            }
        )
    verdict = str(payload.get("verdict") or "CHANGES_REQUIRED") if isinstance(payload, dict) else "CHANGES_REQUIRED"
    passed = bool(valid and verdict == "PASS" and all(item["passed"] for item in acceptances))
    repair_package = [item for item in acceptances if not item["passed"]]
    if verdict == "CHANGES_REQUIRED" and not repair_package:
        valid = False
    return {
        "valid": valid,
        "verdict": verdict,
        "passed": passed,
        "acceptances": acceptances,
        "repair_package": repair_package,
        "decision_reason": (
            str(payload.get("decision_reason") or "")[:1000]
            if isinstance(payload, dict)
            else ""
        ),
    }


def _record_checker_evidence(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    path,
    evidence: dict,
    acceptance_id: str | None,
    now: float,
) -> None:
    body = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()
    _write_atomic(path, body)
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
            store.relative_data_path(path),
            hashlib.sha256(body).hexdigest(),
            len(body),
            acceptance_id,
            task["spec_revision"],
            now,
        ),
    )
