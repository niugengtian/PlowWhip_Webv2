from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from uuid import uuid4

from .artifact_contract import (
    register_artifact,
    result_coverage,
    task_data_scope,
    verified_artifact,
)
from .execution import (
    _context_policy,
    _fallback_provider_generation,
    current_session,
)
from .intake import canonical_json
from .lifecycle_state import (
    apply_model_budget_fact,
    finalize_task_terminal,
    increment_task_retry,
    write_task_fields,
)
from .provider import (
    CHECKER_RESULT_PREFIX,
    ACTIVE_HOST_JOB_STATUSES,
    HostBridgeError,
    PROBE_TOKEN_CAP,
    provider_agent_text,
    provider_job_output,
    provider_job_status,
    model_budget_fact,
    record_model_call,
    selected_model,
    start_provider_job,
    workspace_snapshot,
)
from .store import Store, write_atomic as _write_atomic


@dataclass(frozen=True)
class CheckerStep:
    kind: str
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    project_path: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    execution: dict
    context_policy: dict[str, object]
    workspace_kind: str = "project"
    workspace_key: str | None = None
    model: str | None = None


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
        else "git_publish_contract"
        if spec["kind"] == "git_publish"
        else "artifact_content_sha256"
    )
    artifact = connection.execute(
        """
        SELECT * FROM artifacts
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
        if spec["mode"] == "minimal" and passed and artifact is not None:
            return _prepare_model_probe_checker_step(
                store,
                connection,
                task,
                spec,
                artifact,
                result,
                started_at,
            )
    elif spec["kind"] == "git_publish":
        try:
            manifest = json.loads(output_path.read_text()) if output_path else {}
        except (OSError, json.JSONDecodeError):
            manifest = {}
        result = (
            manifest.get("script_result")
            if isinstance(manifest, dict)
            else None
        )
        target_scope = (
            f"{spec['remote_ssh']}#refs/heads/{spec['branch']}"
        )
        passed = bool(
            exists
            and isinstance(result, dict)
            and manifest.get("provider_key") == "git_publish"
            and manifest.get("returncode") == 0
            and result.get("kind") == "git_publish"
            and result.get("remote_ssh") == spec["remote_ssh"]
            and result.get("branch") == spec["branch"]
            and result.get("secret_scan_passed") is True
            and result.get("pushed") is True
            and result.get("local_head")
            and result.get("local_head") == result.get("remote_head")
        )
        evidence = {
            "acceptance_id": acceptance_id,
            "target_scope": target_scope,
            "local_head": result.get("local_head") if isinstance(result, dict) else None,
            "remote_head": result.get("remote_head") if isinstance(result, dict) else None,
            "secret_scan_passed": (
                result.get("secret_scan_passed")
                if isinstance(result, dict)
                else False
            ),
            "files_scanned": (
                int(result.get("files_scanned") or 0)
                if isinstance(result, dict)
                else 0
            ),
            "passed": passed,
            "status": "PASS" if passed else "CHANGES_REQUIRED",
            "allowed_scope": target_scope,
            "recheck": "secret_scan_and_remote_sha",
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
    register_artifact(
        store,
        connection,
        project_id=task["project_id"],
        task_id=task["id"],
        kind="evidence",
        path=evidence_path,
        stored_path=store.relative_data_path(evidence_path),
        acceptance_id=acceptance_id,
        revision=task["spec_revision"],
        scope=task_data_scope(
            result_coverage(json.loads(task["spec_json"]))
        ),
        source_task_id=task["id"],
        created_at=now,
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
        finalize_task_terminal(connection, task["id"], "done", now)
    elif will_repair:
        increment_task_retry(connection, task["id"], 1)
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "in_progress",
                "phase": "repair",
                "wait_reason": (
                    "Provider probe contract failed; bounded retry scheduled"
                    if spec["kind"] == "provider_probe"
                    else (
                        "Git publish proof failed; bounded retry scheduled"
                        if spec["kind"] == "git_publish"
                        else "output hash mismatch; deterministic repair scheduled"
                    )
                ),
                "fault_code": "verification",
                "next_action_at": now,
                "next_action_kind": "repair",
                "updated_at": now,
            },
        )
    else:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "verify",
                "wait_reason": (
                    "minimal Token probe did not produce verified terminal evidence"
                    if spec["kind"] == "provider_probe"
                    else (
                        "Git publish evidence did not prove secret scan and remote SHA"
                        if spec["kind"] == "git_publish"
                        else "output hash does not satisfy acceptance"
                    )
                ),
                "fault_code": "verification",
                "next_action_at": None,
                "next_action_kind": None,
                "outcome": None,
                "updated_at": now,
            },
        )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'verified', ?, ?)
        """,
        (task["project_id"], task["id"], canonical_json(evidence), now),
    )
    return "verify"


def _prepare_model_probe_checker_step(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    artifact: sqlite3.Row,
    result: dict,
    started_at: float,
) -> CheckerStep:
    artifact_ref, artifact_body = verified_artifact(
        store,
        artifact,
        expected_source_task_id=task["id"],
        expected_revision=task["spec_revision"],
    )
    try:
        complete_result = json.loads(artifact_body)
    except json.JSONDecodeError as error:
        raise ValueError("minimal probe Artifact is not valid JSON") from error
    if complete_result != result:
        raise ValueError("minimal probe Artifact changed before Checker dispatch")
    task_session_id, session_generation = current_session(
        connection,
        task["id"],
        task["checker_role_key"] or "independent_checker",
    )
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (task_session_id,),
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    sequence = connection.execute(
        """
        SELECT COALESCE(MAX(sequence), 0) + 1 AS value
        FROM host_jobs WHERE task_id = ?
        """,
        (task["id"],),
    ).fetchone()["value"]
    job_id = str(uuid4())
    workspace_key = f"probe-{task['id']}"
    execution = {
        "subject": "model_probe",
        "source_task_id": task["id"],
        "source_revision": task["spec_revision"],
        "artifact_ref": artifact_ref,
        "artifact_sha256": artifact_ref["sha256"],
        "artifact_bytes": artifact_ref["bytes"],
        "acceptance_id": "provider_minimal_probe",
        "probe_result": complete_result,
    }
    prompt = canonical_json(
        {
            "role": "Independent Checker",
            "subject": "model_probe",
            "task_spec": spec,
            "acceptance": json.loads(task["acceptance_json"]),
            "source_artifact": execution,
            "rules": [
                "Do not trust or consume Worker free chat.",
                "Check the complete structured probe Artifact independently.",
                "Confirm provider identity, model invocation, terminal success, marker, and token cap.",
                "Do not modify files or create external effects.",
            ],
            "required_output": (
                CHECKER_RESULT_PREFIX
                + '{"verdict":"PASS|CHANGES_REQUIRED",'
                '"acceptances":[{"acceptance_id":"provider_minimal_probe",'
                '"passed":true,"actual_evidence":"bounded independent fact",'
                '"recheck_command":"bounded recheck"}],'
                '"decision_reason":null}'
            ),
        }
    )
    dispatch = {
        "prompt": prompt,
        "access": "read",
        "workspace_kind": "planner",
        "workspace_key": workspace_key,
        "check_subject": "model_probe",
        "execution": execution,
    }
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'dispatching', ?, ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            started_at,
            canonical_json(dispatch),
        ),
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress",
            "phase": "check_call",
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": started_at,
            "next_action_kind": "check",
            "updated_at": started_at,
        },
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'model_probe_checker_prepared', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "host_job_id": job_id,
                    "artifact_ref": artifact_ref["path"],
                    "artifact_sha256": artifact_ref["sha256"],
                }
            ),
            started_at,
        ),
    )
    return CheckerStep(
        "start",
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        "",
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        execution,
        _context_policy(settings),
        "planner",
        workspace_key,
        selected_model(settings, generation["provider_key"]),
    )


def _prepare_checker_step(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    started_at: float,
) -> str | CheckerStep:
    execution = _execution_manifest(store, connection, task)
    report, report_error = _provider_report(
        store, connection, task, execution
    )
    if report_error:
        now = time.time()
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "verify",
                "wait_reason": report_error,
                "fault_code": "verification",
                "next_action_at": None,
                "next_action_kind": None,
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'provider_report_invalid', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "report_ref": execution.get("provider_report_ref"),
                        "report_sha256": execution.get("provider_report_sha256"),
                        "reason": report_error,
                    }
                ),
                now,
            ),
        )
        return "needs_decision"
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
    prompt = _checker_prompt(task, spec, execution, report)
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'dispatching', ?, ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            started_at,
            canonical_json(
                {
                    "prompt": prompt,
                    "access": "read",
                    "workspace_kind": (
                        "planner"
                        if spec.get("butler_semantic_query")
                        else "project"
                    ),
                    "workspace_key": (
                        spec.get("butler_workspace_key")
                        if spec.get("butler_semantic_query")
                        else None
                    ),
                }
            ),
        ),
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress",
            "phase": "check_call",
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": started_at,
            "next_action_kind": "check",
            "updated_at": started_at,
        },
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
        "start",
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        str(spec["project_path"]),
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        execution,
        _context_policy(settings),
        (
            "planner"
            if spec.get("butler_semantic_query")
            else "project"
        ),
        (
            str(spec["butler_workspace_key"])
            if spec.get("butler_semantic_query")
            else None
        ),
        model=selected_model(settings, generation["provider_key"]),
    )


def pending_checker_step(
    store: Store, connection: sqlite3.Connection, task: sqlite3.Row
) -> CheckerStep:
    job = connection.execute(
        """
        SELECT * FROM host_jobs
        WHERE task_id = ? AND purpose = 'check'
          AND status IN ('dispatching', 'running')
        ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    if not job:
        raise RuntimeError(f"Task {task['id']} has no active Checker HostJob")
    generation = connection.execute(
        """
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (job["task_session_id"], job["session_generation"]),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (job["task_session_id"],),
    ).fetchone()
    settings = json.loads(session["settings_json"])["values"]
    spec = json.loads(task["spec_json"])
    dispatch = json.loads(job["dispatch_json"])
    if dispatch.get("check_subject"):
        execution = dict(dispatch.get("execution") or {})
        prompt = str(dispatch["prompt"])
    else:
        execution = _execution_manifest(store, connection, task)
        prompt = str(
            dispatch.get("prompt")
            or _checker_prompt(
                task,
                spec,
                execution,
                _provider_report(store, connection, task, execution)[0],
            )
        )
    return CheckerStep(
        "start" if job["status"] == "dispatching" else "poll",
        task["project_id"],
        task["id"],
        job["id"],
        generation["provider_key"],
        str(spec["project_path"]),
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        execution,
        _context_policy(settings),
        str(dispatch.get("workspace_kind") or "project"),
        (
            str(dispatch["workspace_key"])
            if dispatch.get("workspace_key")
            else None
        ),
        selected_model(settings, generation["provider_key"]),
    )


def perform_checker_step(step: CheckerStep) -> dict[str, object]:
    try:
        state = (
            start_provider_job(
                step.job_id,
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                timeout_seconds=step.timeout_seconds,
                context_policy=step.context_policy,
                access="read",
                workspace_kind=step.workspace_kind,
                workspace_key=step.workspace_key,
                model=step.model,
            )
            if step.kind == "start"
            else provider_job_status(step.job_id)
        )
        return {
            "ok": True,
            "state": state,
            "output": provider_job_output(
                step.job_id,
                complete=str(state.get("status"))
                not in ACTIVE_HOST_JOB_STATUSES,
            ),
        }
    except HostBridgeError as error:
        return {
            "ok": False,
            "error": type(error).__name__,
            "failure_kind": (
                "rejected"
                if step.kind == "start" and error.rejected
                else "transport"
            ),
            "failure_stage": step.kind,
            "error_status": error.status,
            "error_detail": error.detail,
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
    if not facts.get("ok"):
        now = time.time()
        if step.kind == "start" and facts.get("failure_kind") == "rejected":
            dispatch = json.loads(job["dispatch_json"])
            dispatch["rejection"] = {
                "status": facts.get("error_status"),
                "detail": str(facts.get("error_detail") or "")[:500],
            }
            connection.execute(
                """
                UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = 125,
                    failure_code = 'rejected', dispatch_json = ? WHERE id = ?
                """,
                (now, canonical_json(dispatch), job["id"]),
            )
            fallback = _fallback_provider_generation(
                store, connection, task, job, step.provider_key, now
            )
            retry_job_id = (
                _enqueue_checker_fallback(connection, task, job, now)
                if fallback
                else None
            )
            write_task_fields(
                connection,
                task["id"],
                {
                    "public_status": (
                        "in_progress" if fallback else "needs_decision"
                    ),
                    "phase": "check_call" if fallback else "provider_recovery",
                    "wait_reason": (
                        f"Checker start was rejected; falling back to {fallback}"
                        if fallback
                        else "Host Bridge rejected the Checker before acceptance"
                    ),
                    "fault_code": "provider",
                    "next_action_at": now if fallback else None,
                    "next_action_kind": "check" if fallback else None,
                    "updated_at": now,
                },
            )
            connection.execute(
                """
                INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
                VALUES (?, ?, 'host_job_rejected', ?, ?)
                """,
                (
                    task["project_id"],
                    task["id"],
                    canonical_json(
                        {
                            "host_job_id": job["id"],
                            "purpose": "check",
                            "provider_key": step.provider_key,
                            "http_status": facts.get("error_status"),
                            "fallback": fallback,
                            "retry_host_job_id": retry_job_id,
                        }
                    ),
                    now,
                ),
            )
            return "checker_fallback" if fallback else "needs_decision"
        dispatch = json.loads(job["dispatch_json"])
        failures = int(dispatch.get("reconcile_failures") or 0) + 1
        dispatch["reconcile_failures"] = failures
        session = connection.execute(
            "SELECT settings_json FROM task_sessions WHERE id = ?",
            (job["task_session_id"],),
        ).fetchone()
        values = json.loads(session["settings_json"]).get("values", {})
        exhausted = failures > int(values.get("retry_count", 0))
        connection.execute(
            "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
            (canonical_json(dispatch), job["id"]),
        )
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": (
                    "needs_decision" if exhausted else "in_progress"
                ),
                "phase": (
                    "provider_recovery" if exhausted else task["phase"]
                ),
                "wait_reason": (
                    "independent Checker outcome is unknown; reconcile or cancel the HostJob"
                    if exhausted
                    else "Checker HostJob unavailable; idempotent reconcile scheduled"
                ),
                "fault_code": (
                    "unsafe_unknown" if exhausted else "transport"
                ),
                "next_action_at": None if exhausted else now + max(
                    1, int(values.get("retry_backoff_seconds", 0))
                ),
                "next_action_kind": None if exhausted else (
                    "check" if job["status"] == "dispatching" else "check_poll"
                ),
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'host_job_reconcile_deferred', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "purpose": "check",
                        "failures": failures,
                    }
                ),
                now,
            ),
        )
        return "needs_decision" if exhausted else "check_reconcile"
    if facts.get("ok") and str(facts["state"].get("status")) in ACTIVE_HOST_JOB_STATUSES:
        state = facts["state"]
        now = time.time()
        connection.execute(
            "UPDATE host_jobs SET status = 'running' WHERE id = ?",
            (job["id"],),
        )
        if state.get("session_id"):
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (state["session_id"], job["task_session_id"], job["session_generation"]),
            )
        write_task_fields(
            connection,
            task["id"],
            {
                "phase": "check_wait",
                "next_action_at": now + 1,
                "next_action_kind": "check_poll",
                "updated_at": now,
            },
        )
        return "check_wait"
    spec = json.loads(task["spec_json"])
    checker = (
        _checker_result(step, facts["state"], facts.get("output"))
        if facts.get("ok")
        else {}
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
            str(checker.get("usage_kind") or "cumulative"),
            int(checker["input_tokens"]),
            int(checker["cached_input_tokens"]),
            int(checker["output_tokens"]),
            str(checker["model"]),
        )
        budget_fact = model_budget_fact(connection, task["id"])
        if budget_fact:
            apply_model_budget_fact(connection, task["id"], budget_fact)
            connection.execute(
                """
                UPDATE host_jobs SET status = 'succeeded', ended_at = ?,
                    returncode = ? WHERE id = ?
                """,
                (time.time(), checker["returncode"], job["id"]),
            )
            return "needs_decision"
    checker_completed = bool(
        str(facts["state"].get("status")) == "completed"
        and checker["returncode"] == 0
    )
    if not checker_completed:
        now = time.time()
        connection.execute(
            """
            UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = ?,
                failure_code = 'provider' WHERE id = ?
            """,
            (now, checker["returncode"], job["id"]),
        )
        fallback = _fallback_provider_generation(
            store, connection, task, job, step.provider_key, now
        )
        retry_job_id = (
            _enqueue_checker_fallback(connection, task, job, now)
            if fallback
            else None
        )
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": (
                    "in_progress" if fallback else "needs_decision"
                ),
                "phase": "check_call" if fallback else "provider_recovery",
                "wait_reason": (
                    f"Checker Provider failed; falling back to {fallback}"
                    if fallback
                    else "independent Checker exhausted all frozen Provider candidates"
                ),
                "fault_code": "provider",
                "next_action_at": now if fallback else None,
                "next_action_kind": "check" if fallback else None,
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'checker_provider_failed', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "provider_key": step.provider_key,
                        "fallback": fallback,
                        "retry_host_job_id": retry_job_id,
                    }
                ),
                now,
            ),
        )
        return "checker_fallback" if fallback else "needs_decision"
    subject = str(step.execution.get("subject") or "")
    if subject in {"planner", "model_probe"}:
        return _finalize_special_model_checker(
            store,
            connection,
            task,
            job,
            step,
            checker,
            subject,
        )
    checker_stdout = str(checker["stdout"])
    workspace_changed = bool(step.execution.get("workspace_changed"))
    workspace_change_required = bool(
        spec.get("workspace_change_required", True)
    )
    expected_acceptance = json.loads(task["acceptance_json"])
    verdict = _parse_checker_verdict(
        provider_agent_text(checker_stdout),
        expected_acceptance,
        spec["project_path"],
    )
    if workspace_change_required and not workspace_changed:
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
        and (workspace_changed or not workspace_change_required)
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
        "workspace_change_required": workspace_change_required,
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
    checker_succeeded = bool(
        facts.get("ok")
        and str(facts["state"].get("status")) == "completed"
        and checker["returncode"] == 0
    )
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
        finalize_task_terminal(connection, task["id"], "done", now)
    elif will_repair:
        increment_task_retry(connection, task["id"], 1)
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "in_progress",
                "phase": "repair",
                "wait_reason": (
                    "No required workspace delta was proven"
                    if workspace_change_required and not workspace_changed
                    else (
                        "Independent checker requested changes: "
                        + canonical_json(verdict["repair_package"])[:4000]
                    )
                ),
                "fault_code": "verification",
                "next_action_at": now,
                "next_action_kind": "repair",
                "updated_at": now,
            },
        )
    else:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "verify",
                "wait_reason": (
                    "code task produced no required workspace delta"
                    if workspace_change_required and not workspace_changed
                    else (
                        verdict["decision_reason"]
                        or "independent checker contract did not prove every acceptance"
                    )
                ),
                "fault_code": "verification",
                "next_action_at": None,
                "next_action_kind": None,
                "outcome": None,
                "updated_at": now,
            },
        )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'verified', ?, ?)
        """,
        (task["project_id"], task["id"], canonical_json(evidence), now),
    )
    return "verify"


def _enqueue_checker_fallback(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    failed_job: sqlite3.Row,
    now: float,
) -> str:
    generation = connection.execute(
        """
        SELECT generation FROM session_generations
        WHERE task_session_id = ? AND status = 'active'
        ORDER BY generation DESC LIMIT 1
        """,
        (failed_job["task_session_id"],),
    ).fetchone()
    if not generation:
        raise RuntimeError("Checker fallback has no active SessionGeneration")
    sequence = connection.execute(
        """
        SELECT COALESCE(MAX(sequence), 0) + 1 AS value
        FROM host_jobs WHERE task_id = ?
        """,
        (task["id"],),
    ).fetchone()["value"]
    dispatch = json.loads(
        connection.execute(
            "SELECT dispatch_json FROM host_jobs WHERE id = ?",
            (failed_job["id"],),
        ).fetchone()["dispatch_json"]
    )
    dispatch.pop("reconcile_failures", None)
    dispatch["fallback_from_host_job_id"] = failed_job["id"]
    retry_job_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, 'check', 'dispatching', ?, ?)
        """,
        (
            retry_job_id,
            task["id"],
            failed_job["task_session_id"],
            generation["generation"],
            task["spec_revision"],
            sequence,
            now,
            canonical_json(dispatch),
        ),
    )
    return retry_job_id


def _finalize_special_model_checker(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    step: CheckerStep,
    checker: dict[str, object],
    subject: str,
) -> str:
    acceptance_id = (
        "planner_contract"
        if subject == "planner"
        else "provider_minimal_probe"
    )
    checker_stdout = str(checker["stdout"])
    verdict = _parse_checker_verdict(
        provider_agent_text(checker_stdout),
        [{"id": acceptance_id, "expected": f"{subject} result contract"}],
        "Planner Artifact" if subject == "planner" else "Provider probe Evidence",
    )
    passed = bool(
        checker["returncode"] == 0
        and verdict["valid"]
        and verdict["passed"]
    )
    now = time.time()
    evidence = {
        "subject": subject,
        "source_task_id": task["id"],
        "source_revision": task["spec_revision"],
        "source_artifact": step.execution,
        "checker_provider": step.provider_key,
        "checker_returncode": checker["returncode"],
        "checker_output_sha256": hashlib.sha256(
            checker_stdout.encode()
        ).hexdigest(),
        "checker_contract_valid": verdict["valid"],
        "checker_verdict": verdict["verdict"],
        "acceptances": verdict["acceptances"],
        "decision_reason": verdict["decision_reason"],
        "passed": passed,
        "status": "PASS" if passed else "CHANGES_REQUIRED",
        "verified_at": now,
    }
    evidence_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"check-{job['sequence']:06d}"
        / "evidence"
        / f"{subject}-checker-verdict.json"
    )
    _record_checker_evidence(
        store,
        connection,
        task,
        evidence_path,
        evidence,
        acceptance_id,
        now,
    )
    connection.execute(
        """
        UPDATE host_jobs SET status = 'succeeded', ended_at = ?, returncode = 0,
            output_ref = ?, failure_code = NULL WHERE id = ?
        """,
        (now, store.relative_data_path(evidence_path), job["id"]),
    )
    if passed:
        return (
            "planner_checker_pass"
            if subject == "planner"
            else "probe_checker_pass"
        )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "needs_decision",
            "phase": "plan" if subject == "planner" else "verify",
            "wait_reason": (
                verdict["decision_reason"]
                or f"independent Checker rejected the {subject} result"
            ),
            "fault_code": "verification",
            "next_action_at": None,
            "next_action_kind": None,
            "updated_at": now,
        },
    )
    return "needs_decision"


def _execution_manifest(
    store: Store, connection: sqlite3.Connection, task: sqlite3.Row
) -> dict:
    artifact = connection.execute(
        """
        SELECT kind, path, sha256, bytes, acceptance_id, revision,
               scope_json, source_task_id
        FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND revision = ?
          AND path LIKE '%/provider-execution.json'
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (task["id"], task["spec_revision"]),
    ).fetchone()
    if not artifact:
        return {"manifest_error": "Provider execution manifest is missing"}
    try:
        _entry, body = verified_artifact(
            store,
            artifact,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
        value = json.loads(body)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            "manifest_error": (
                f"Provider execution manifest failed verification: "
                f"{type(error).__name__}"
            )
        }
    if not isinstance(value, dict) or value.get("manifest_version") != 2:
        return {"manifest_error": "Provider execution manifest version is invalid"}
    if value.get("formal_result_error"):
        return {"manifest_error": str(value["formal_result_error"])[:500]}
    spec = json.loads(task["spec_json"])
    expected_contract = spec.get("task_contract", {}).get("result", {})
    if value.get("result_contract") != expected_contract:
        return {"manifest_error": "formal result contract does not match TaskSpec"}
    result_entries = value.get("result_artifacts")
    if not isinstance(result_entries, list) or not result_entries:
        return {"manifest_error": "formal result manifest is empty"}
    for entry in result_entries:
        if not isinstance(entry, dict):
            return {"manifest_error": "formal result entry is invalid"}
        row = connection.execute(
            """
            SELECT kind, path, sha256, bytes, acceptance_id, revision,
                   scope_json, source_task_id
            FROM artifacts
            WHERE task_id = ? AND kind = ? AND path = ? AND revision = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (
                task["id"],
                entry.get("kind"),
                entry.get("path"),
                task["spec_revision"],
            ),
        ).fetchone()
        if not row:
            return {"manifest_error": "formal result Artifact index is missing"}
        try:
            scope = json.loads(row["scope_json"])
        except json.JSONDecodeError:
            return {"manifest_error": "formal result Artifact scope is invalid"}
        indexed = {
            "kind": row["kind"],
            "path": row["path"],
            "sha256": row["sha256"],
            "bytes": row["bytes"],
            "acceptance_id": row["acceptance_id"],
            "revision": row["revision"],
            "scope": scope,
            "source_task_id": row["source_task_id"],
        }
        if indexed != entry:
            return {
                "manifest_error": (
                    "formal result Artifact path/hash/revision/scope/source mismatch"
                )
            }
        if scope.get("kind") == "task_data":
            try:
                verified_artifact(
                    store,
                    row,
                    expected_source_task_id=task["id"],
                    expected_revision=task["spec_revision"],
                )
            except (OSError, ValueError):
                return {
                    "manifest_error": (
                        "formal result Artifact content failed "
                        "path/bytes/SHA-256 verification"
                    )
                }
        elif scope.get("kind") == "workspace":
            snapshot = workspace_snapshot(
                str(scope["workspace_root"]), [str(row["path"])]
            )
            current = next(
                (
                    item
                    for item in snapshot.get("requested", [])
                    if isinstance(item, dict)
                    and item.get("path") == row["path"]
                ),
                None,
            )
            if (
                not isinstance(current, dict)
                or current.get("sha256") != row["sha256"]
                or current.get("bytes") != row["bytes"]
            ):
                return {
                    "manifest_error": (
                        "workspace Artifact changed after execution snapshot"
                    )
                }
    return value


def _provider_report(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    execution: dict,
) -> tuple[str, str | None]:
    if execution.get("manifest_error"):
        return "", str(execution["manifest_error"])
    result_type = execution.get("result_contract", {}).get("type")
    if result_type != "evidence":
        return "", None
    report_ref = str(execution.get("provider_report_ref") or "")
    expected_sha256 = str(execution.get("provider_report_sha256") or "")
    if not report_ref or not expected_sha256:
        if any(
            isinstance(entry, dict)
            and entry.get("acceptance_id") == "structured_result"
            for entry in execution.get("result_artifacts", [])
        ):
            return "", None
        return "", "Provider report artifact is missing; Checker was not started"
    if execution.get("provider_report_truncated"):
        return "", "Provider report artifact exceeded its frozen bound"
    artifact = connection.execute(
        """
        SELECT kind, path, sha256, bytes, acceptance_id, revision,
               scope_json, source_task_id
        FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND path = ? AND revision = ?
        ORDER BY rowid DESC LIMIT 1
        """,
        (task["id"], report_ref, task["spec_revision"]),
    ).fetchone()
    if not artifact:
        return "", "Provider report Artifact index is missing"
    try:
        _entry, body = verified_artifact(
            store,
            artifact,
            expected_source_task_id=task["id"],
            expected_revision=task["spec_revision"],
        )
    except (OSError, ValueError):
        return "", "Provider report artifact cannot be read; Checker was not started"
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        return "", "Provider report artifact SHA-256 does not match its manifest"
    if len(body) != int(execution.get("provider_report_bytes") or -1):
        return "", "Provider report artifact byte count does not match its manifest"
    report = body.decode(errors="replace").strip()
    return (
        (report, None)
        if report
        else ("", "Provider report artifact is empty; Checker was not started")
    )


def _checker_result(
    step: CheckerStep, state: object, output: object
) -> dict[str, object]:
    state = state if isinstance(state, dict) else {}
    streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
    if isinstance(output, dict):
        for chunk in output.get("chunks", []):
            if isinstance(chunk, dict) and chunk.get("stream") in streams:
                streams[str(chunk["stream"])].append(str(chunk.get("text") or ""))
    input_tokens = max(0, int(state.get("input_tokens") or 0))
    cached_tokens = min(
        input_tokens, max(0, int(state.get("cached_input_tokens") or 0))
    )
    return {
        "returncode": (
            int(state["returncode"])
            if isinstance(state.get("returncode"), int)
            else 1
        ),
        "stdout": "".join(streams["stdout"]),
        "stderr": "".join(streams["stderr"]),
        "session_id": str(state.get("session_id") or "") or step.session_id,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": max(0, int(state.get("output_tokens") or 0)),
        "usage_kind": str(state.get("usage_kind") or "cumulative"),
        "model": str(state.get("model") or step.provider_key),
    }


def _checker_prompt(
    task: sqlite3.Row, spec: dict, execution: dict, report: str
) -> str:
    acceptance = json.loads(task["acceptance_json"])
    before = execution.get("before")
    after = execution.get("after")
    frozen_head = (
        str(before.get("head") or "")
        if isinstance(before, dict)
        and isinstance(after, dict)
        and before.get("head")
        and before.get("head") == after.get("head")
        else ""
    )
    return (
        "Independently inspect the Task target read-only. Verify this Task against "
        f"the actual files and smallest relevant checks:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.\n"
        f"Frozen acceptance contract: {canonical_json(acceptance)}\n"
        f"Control-plane workspace delta recorded: {bool(execution.get('workspace_changed'))}.\n"
        f"Control-plane executor provider: {execution.get('provider_key')}.\n"
        f"Frozen execution target HEAD: {frozen_head or 'unavailable'}.\n"
        + (
            "The before/after execution evidence binds this read-only Task to that "
            "frozen HEAD. If the live checkout moved later, inspect the frozen target "
            "with git show/diff; do not fail solely because live HEAD differs.\n"
            if frozen_head and not spec.get("workspace_change_required", True)
            else ""
        )
        +
        "Frozen formal result manifest (references only; never Worker chat): "
        f"{canonical_json({'result_contract': execution.get('result_contract'), 'result_artifacts': execution.get('result_artifacts')})}\n"
        "Open and verify every declared workspace Artifact/diff against its "
        "path, SHA-256, revision, scope, source Task and coverage. For internal "
        "Evidence, use only the verified manifest reference; independently "
        "recheck the Task against the workspace and frozen TaskSpec.\n"
        f"Finish with one line beginning {CHECKER_RESULT_PREFIX!r} followed by one JSON object. "
        'Use {"verdict":"PASS|CHANGES_REQUIRED|NEEDS_DECISION",'
        '"acceptances":[{"acceptance_id":"...","passed":true,'
        '"actual_evidence":"bounded fact","recheck_command":"bounded command"}],'
        '"decision_reason":null}. Include every frozen acceptance_id exactly once. '
        "Never ask the owner directly; use NEEDS_DECISION plus decision_reason as the "
        "only structured blocker fact. Do not modify files, commit, deploy, send messages, "
        "or create external effects."
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
    register_artifact(
        store,
        connection,
        project_id=task["project_id"],
        task_id=task["id"],
        kind="evidence",
        path=path,
        stored_path=store.relative_data_path(path),
        acceptance_id=acceptance_id,
        revision=task["spec_revision"],
        scope=task_data_scope(
            result_coverage(json.loads(task["spec_json"]))
        ),
        source_task_id=task["id"],
        created_at=now,
    )
