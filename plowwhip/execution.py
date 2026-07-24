from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .intake import canonical_json
from .provider import (
    ACTIVE_HOST_JOB_STATUSES,
    HostBridgeError,
    PROVIDERS,
    PROBE_MARKER,
    PROBE_TOKEN_CAP,
    cancel_provider_job,
    parse_context_events,
    provider_job_output,
    provider_job_status,
    record_model_call,
    run_provider_probe,
    start_provider_job,
    workspace_snapshot,
)
from .store import Store, write_atomic as _write_atomic


@dataclass(frozen=True)
class ProviderStep:
    kind: str
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    project_path: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    access: str
    context_policy: dict[str, object]


@dataclass(frozen=True)
class ProbeStep:
    kind: str
    project_id: str
    task_id: str
    job_id: str
    provider_key: str
    mode: str
    project_path: str
    prompt: str
    session_id: str | None
    timeout_seconds: int
    context_policy: dict[str, object]


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
    create_task_session(
        connection,
        project_id,
        task_id,
        now,
        executor_role,
        executor_provider,
        False,
        (settings_overrides or {}).get(executor_role, {}),
    )
    create_task_session(
        connection,
        project_id,
        task_id,
        now,
        checker_role,
        checker_provider,
        True,
        (settings_overrides or {}).get(checker_role, {}),
    )


def create_task_session(
    connection: sqlite3.Connection,
    project_id: str,
    task_id: str,
    now: float,
    role_key: str,
    provider_key: str,
    checker_independent: bool,
    settings_override: dict | None = None,
) -> None:
    settings = effective_settings(connection, project_id, settings_override or {})
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
            canonical_json(
                _role_snapshot(
                    connection, project_id, role_key, checker_independent
                )
            ),
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
        (uuid4().hex, session_id, provider_key, now),
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
    roles = (
        (executor_role, executor_provider, False),
        (checker_role, checker_provider, True),
    )
    for role_key, provider_key, checker_independent in roles:
        if connection.execute(
            "SELECT 1 FROM task_sessions WHERE task_id = ? AND role_key = ?",
            (task_id, role_key),
        ).fetchone():
            continue
        create_task_session(
            connection,
            project_id,
            task_id,
            now,
            role_key,
            provider_key,
            checker_independent,
            (settings_overrides or {}).get(role_key, {}),
        )


def effective_settings(
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
    connection: sqlite3.Connection,
    project_id: str,
    role_key: str,
    checker_independent: bool,
) -> dict:
    template_key = {
        "provider_probe": "provider_probe",
        "fullstack": "code_change",
        "planner": "code_change",
        "independent_checker": "code_change",
        "git_publisher": "git_publish",
    }.get(role_key, "deterministic_write")
    keys = [role_key, "v1_hard_boundaries", template_key]
    rows = connection.execute(
        """
        SELECT kind, item_key, revision, path, sha256 FROM library_items
        WHERE (
            (scope = 'global' AND project_id IS NULL AND item_key IN (?, ?, ?))
            OR (
                scope = 'project' AND project_id = ?
                AND (kind = 'rule' OR item_key IN (?, ?))
            )
          )
          AND revision = (
              SELECT MAX(latest.revision) FROM library_items latest
              WHERE latest.scope = library_items.scope
                AND latest.project_id IS library_items.project_id
                AND latest.kind = library_items.kind
                AND latest.item_key = library_items.item_key
          )
        ORDER BY CASE scope WHEN 'global' THEN 0 ELSE 1 END, kind, item_key
        """,
        (*keys, project_id, role_key, template_key),
    ).fetchall()
    return {
        "role_key": role_key,
        "permission": (
            "external_effect"
            if role_key == "git_publisher"
            else (
                "read_only"
                if role_key
                in {
                    "planner",
                    "independent_checker",
                    "deterministic_checker",
                    "provider_probe",
                }
                else "recoverable_workspace_change"
            )
        ),
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
) -> str | ProviderStep | ProbeStep:
    if purpose not in {"execute", "repair"}:
        raise ValueError("execution purpose must be execute or repair")
    started_at = time.time()
    spec = json.loads(task["spec_json"])
    if spec["kind"] == "provider_probe":
        return _prepare_provider_probe(connection, task, spec, purpose)
    if spec["kind"] in {"provider_task", "git_publish"}:
        return _prepare_provider_task(store, connection, task, spec, purpose)
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
                next_action_kind = 'check', updated_at = ?
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


def _prepare_provider_task(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    purpose: str,
) -> str | ProviderStep:
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
        SELECT provider_key, external_session_id FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (task_session_id, session_generation),
    ).fetchone()
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?", (task_session_id,)
    ).fetchone()
    settings = json.loads(session["settings_json"]).get("values", {})
    from .continuity import compile_hot_context

    try:
        prompt = _provider_prompt(
            task,
            spec,
            purpose,
            compile_hot_context(store, connection, task, task["role_key"] or "fullstack"),
        )
    except ValueError as error:
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision',
                phase = 'provider_recovery', wait_reason = ?,
                fault_code = 'scope', next_action_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (str(error), started_at, task["id"]),
        )
        return "needs_decision"
    access = "write" if spec.get("workspace_change_required", True) else "read"
    job_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            purpose,
            started_at,
            canonical_json(
                {
                    "prompt": prompt,
                    "context_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "access": access,
                }
            ),
        ),
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'in_progress', phase = 'execute_snapshot',
            wait_reason = NULL, fault_code = NULL, next_action_at = ?,
            next_action_kind = 'snapshot', updated_at = ?
        WHERE id = ?
        """,
        (started_at, started_at, task["id"]),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'host_job_prepared', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json({"host_job_id": job_id, "sequence": sequence, "purpose": purpose}),
            started_at,
        ),
    )
    return ProviderStep(
        "snapshot",
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        str(spec["project_path"]),
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        access,
        _context_policy(settings),
    )


def pending_provider_step(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> ProviderStep:
    job = connection.execute(
        """
        SELECT * FROM host_jobs
        WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
        ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    if not job:
        raise RuntimeError(f"Task {task['id']} has no active HostJob")
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
    spec = json.loads(task["spec_json"])
    dispatch = json.loads(job["dispatch_json"])
    kind = {
        "execute_snapshot": "snapshot",
        "execute_dispatch": "start",
        "execute_wait": "poll",
        "stopping": "cancel",
    }.get(task["phase"])
    settings = json.loads(session["settings_json"])["values"]
    if task["phase"] == "stopping" and job["status"] == "cancelling":
        dispatch = json.loads(job["dispatch_json"])
        requested_at = float(dispatch.get("stop_requested_at") or time.time())
        if time.time() >= requested_at + int(settings.get("stop_grace_seconds", 10)):
            kind = "poll"
    if not kind:
        raise RuntimeError(f"Task {task['id']} is not waiting on a Provider")
    return ProviderStep(
        kind,
        task["project_id"],
        task["id"],
        job["id"],
        generation["provider_key"],
        str(spec["project_path"]),
        str(
            dispatch.get("prompt")
            or _provider_prompt(task, spec, job["purpose"])
        ),
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        str(dispatch.get("access") or "write"),
        _context_policy(settings),
    )


def perform_provider_step(step: ProviderStep) -> dict[str, object]:
    stage = step.kind
    try:
        if step.kind == "snapshot":
            return {"ok": True, "before": workspace_snapshot(step.project_path)}
        if step.kind == "start":
            state = start_provider_job(
                step.job_id,
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                timeout_seconds=step.timeout_seconds,
                context_policy=step.context_policy,
                access=step.access,
            )
            stage = "output"
        elif step.kind == "poll":
            state = provider_job_status(step.job_id)
            stage = "output"
        elif step.kind == "cancel":
            state = cancel_provider_job(step.job_id)
            stage = "output"
        else:
            raise ValueError("unknown Provider step")
        output = provider_job_output(step.job_id)
        facts: dict[str, object] = {"ok": True, "state": state, "output": output}
        if str(state.get("status")) not in ACTIVE_HOST_JOB_STATUSES:
            stage = "snapshot_after"
            facts["after"] = workspace_snapshot(step.project_path)
        return facts
    except HostBridgeError as error:
        return {
            "ok": False,
            "error": type(error).__name__,
            "failure_kind": (
                "rejected"
                if step.kind == "start" and stage == "start" and error.rejected
                else "transport"
            ),
            "failure_stage": stage,
            "error_status": error.status,
            "error_detail": error.detail,
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}


def apply_provider_step(
    store: Store,
    connection: sqlite3.Connection,
    step: ProviderStep,
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
        return "stale_provider_fact"
    now = time.time()
    if not facts.get("ok"):
        if step.kind == "start" and facts.get("failure_kind") == "rejected":
            return _reject_provider_start(connection, task, job, step, facts, now)
        dispatch = json.loads(job["dispatch_json"])
        failures = int(dispatch.get("reconcile_failures") or 0) + 1
        dispatch["reconcile_failures"] = failures
        settings = connection.execute(
            "SELECT settings_json FROM task_sessions WHERE id = ?",
            (job["task_session_id"],),
        ).fetchone()
        values = json.loads(settings["settings_json"]).get("values", {})
        max_retries = int(values.get("retry_count", 0))
        exhausted = failures > max_retries
        if exhausted and step.kind == "snapshot":
            connection.execute(
                """
                UPDATE host_jobs SET status = 'failed', ended_at = ?, returncode = 1,
                    failure_code = 'transport', dispatch_json = ? WHERE id = ?
                """,
                (now, canonical_json(dispatch), job["id"]),
            )
        else:
            connection.execute(
                "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
                (canonical_json(dispatch), job["id"]),
            )
        connection.execute(
            """
            UPDATE tasks SET public_status = ?, phase = ?, wait_reason = ?,
                fault_code = ?, next_action_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "needs_decision" if exhausted else "in_progress",
                "provider_recovery" if exhausted else task["phase"],
                (
                    f"HostJob {job['id']} outcome is unknown after {failures} reconcile failures"
                    if exhausted and step.kind != "snapshot"
                    else (
                        f"Host Bridge snapshot unavailable after {failures} attempts"
                        if exhausted
                        else f"Host Bridge {step.kind} unavailable; idempotent reconcile scheduled"
                    )
                ),
                "unsafe_unknown" if exhausted and step.kind != "snapshot" else "transport",
                (
                    None
                    if exhausted
                    else now
                    + max(1, int(values.get("retry_backoff_seconds", 0)))
                ),
                now,
                task["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'host_job_reconcile_deferred', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json({"host_job_id": job["id"], "step": step.kind}),
                now,
            ),
        )
        return "needs_decision" if exhausted else step.kind
    if step.kind == "snapshot":
        before = facts.get("before")
        before_git = before.get("git") if isinstance(before, dict) else {}
        dispatch = json.loads(job["dispatch_json"])
        dispatch["before"] = before_git
        spec = json.loads(task["spec_json"])
        if spec["kind"] == "git_publish":
            operation = str(spec.get("operation") or "publish")
            observed_head = before_git.get("head")
            authorized_head = (
                spec.get("authorization", {}).get("expected_head")
                if operation == "publish"
                else None
            )
            if authorized_head and authorized_head != observed_head:
                connection.execute(
                    """
                    UPDATE host_jobs SET status = 'failed', ended_at = ?,
                        returncode = 1, failure_code = 'scope',
                        dispatch_json = ? WHERE id = ?
                    """,
                    (now, canonical_json(dispatch), job["id"]),
                )
                connection.execute(
                    """
                    UPDATE tasks SET public_status = 'needs_decision',
                        phase = 'provider_recovery', wait_reason = ?,
                        fault_code = 'scope', next_action_at = NULL,
                        next_action_kind = NULL, updated_at = ? WHERE id = ?
                    """,
                    (
                        "本地 HEAD 已在授权后变化；请刷新 Git 发布原因与证据",
                        now,
                        task["id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_events(
                        project_id, task_id, kind, detail_json, created_at
                    ) VALUES (?, ?, 'git_publish_context_stale', ?, ?)
                    """,
                    (
                        task["project_id"],
                        task["id"],
                        canonical_json(
                            {
                                "spec_revision": task["spec_revision"],
                                "authorized_local_head": authorized_head,
                                "observed_local_head": observed_head,
                                "host_job_id": job["id"],
                            }
                        ),
                        now,
                    ),
                )
                return "needs_decision"
            prompt = {
                "kind": "git_publish",
                "operation": operation,
                "remote_ssh": spec["remote_ssh"],
                "branch": spec["branch"],
                "expected_head": observed_head,
            }
            if operation == "publish":
                prompt.update(
                    {
                        "publish_mode": spec.get("publish_mode", "fast_forward"),
                        "expected_remote_head": spec.get("expected_remote_head"),
                        "authorization": {
                            **spec["authorization"],
                            "expected_head": observed_head,
                        },
                    }
                )
            dispatch["prompt"] = canonical_json(prompt)
        connection.execute(
            "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
            (canonical_json(dispatch), job["id"]),
        )
        connection.execute(
            """
            UPDATE tasks SET phase = 'execute_dispatch', wait_reason = NULL,
                fault_code = NULL, next_action_at = ?,
                next_action_kind = 'dispatch', updated_at = ? WHERE id = ?
            """,
            (now, now, task["id"]),
        )
        return "snapshot"

    state = facts["state"]
    bridge_status = str(state.get("status"))
    log_path, log_body = _provider_log(store, task, job, facts.get("output"))
    if log_body:
        _write_atomic(log_path, log_body)
    if bridge_status in ACTIVE_HOST_JOB_STATUSES:
        stopping = bool(
            task["phase"] == "stopping"
            or job["status"] == "cancelling"
            or step.kind == "cancel"
        )
        dispatch = json.loads(job["dispatch_json"])
        if stopping:
            dispatch.setdefault("stop_requested_at", now)
        connection.execute(
            """
            UPDATE host_jobs SET status = ?, output_ref = COALESCE(?, output_ref),
                dispatch_json = ?
            WHERE id = ?
            """,
            (
                "cancelling" if stopping else "running",
                store.relative_data_path(log_path) if log_body else None,
                canonical_json(dispatch),
                job["id"],
            ),
        )
        if state.get("session_id"):
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = ?
                WHERE task_session_id = ? AND generation = ?
                """,
                (state["session_id"], job["task_session_id"], job["session_generation"]),
            )
        connection.execute(
            """
            UPDATE tasks SET phase = ?, wait_reason = ?, fault_code = ?,
                next_action_at = ?, next_action_kind = ?, updated_at = ? WHERE id = ?
            """,
            (
                "stopping" if stopping else "execute_wait",
                task["wait_reason"] if stopping else None,
                task["fault_code"] if stopping else None,
                now + 1,
                "reconcile_stop" if stopping else "poll",
                now,
                task["id"],
            ),
        )
        return "cancel" if stopping else (
            "dispatch" if step.kind == "start" else "wait"
        )
    if task["phase"] == "stopping" or step.kind == "cancel":
        return _finalize_provider_cancel(store, connection, task, job, state, log_path, log_body)
    return _finalize_provider_job(
        store, connection, task, job, step, state, facts, log_path, log_body
    )


def _provider_log(
    store: Store,
    task: sqlite3.Row,
    job: sqlite3.Row,
    output: object,
) -> tuple[Path, bytes]:
    streams = {"stdout": [], "stderr": []}
    if isinstance(output, dict):
        for chunk in output.get("chunks", []):
            if isinstance(chunk, dict) and chunk.get("stream") in streams:
                streams[str(chunk["stream"])].append(str(chunk.get("text") or ""))
    body = (
        "".join(streams["stdout"])
        + ("\n" if streams["stdout"] and streams["stderr"] else "")
        + "".join(streams["stderr"])
    ).encode()
    path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "sessions"
        / (task["role_key"] or "fullstack")
        / f"generation-{job['session_generation']:06d}"
        / f"sequence-{job['sequence']:06d}.log"
    )
    return path, body


def _previous_git_publish_failure(
    store: Store,
    connection: sqlite3.Connection,
    task_id: str,
    sequence: int,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT id, returncode, output_ref FROM host_jobs
        WHERE task_id = ? AND sequence < ? AND status = 'failed'
        ORDER BY sequence DESC LIMIT 1
        """,
        (task_id, sequence),
    ).fetchone()
    if not row:
        return None
    reason = "failed_publish"
    if row["output_ref"]:
        try:
            path = store.resolve_data_path(row["output_ref"])
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 16_384))
                tail = handle.read().decode(errors="replace").lower()
            if any(
                marker in tail
                for marker in (
                    "non-fast-forward",
                    "fetch first",
                    "remote contains work that you do not have locally",
                )
            ):
                reason = "remote_history_conflict"
        except (OSError, ValueError):
            pass
    return {
        "host_job_id": row["id"],
        "returncode": row["returncode"],
        "reason": reason,
    }


def _git_publish_decision_context(
    task: sqlite3.Row,
    job: sqlite3.Row,
    result: dict[str, object],
    output_ref: str,
    *,
    prior_failure: dict[str, object] | None = None,
) -> dict[str, object]:
    spec = json.loads(task["spec_json"])
    local_head = str(result.get("local_head") or "")
    remote_head = str(result.get("remote_head") or "")
    reason = str(result.get("code") or "")
    if result.get("kind") == "git_publish_inspection":
        reason = str((prior_failure or {}).get("reason") or "failed_publish")
    complete = bool(
        reason == "remote_history_conflict"
        and len(local_head) == 40
        and len(remote_head) == 40
        and local_head != remote_head
    )
    branch = str(result.get("branch") or "目标分支")
    summary = (
        f"普通 push 已被远端以 non-fast-forward 拒绝；只读复核显示"
        f"本地 {local_head} 与远端 {branch} 的 {remote_head} 不同。"
        if complete
        else "尚未取得足够的只读事实，不能安全授权 Git 发布恢复。"
    )
    evidence = [
        {"kind": "host_job", "id": job["id"], "purpose": job["purpose"]},
        {"kind": "artifact", "path": output_ref},
    ]
    if prior_failure:
        evidence.append(
            {
                "kind": "host_job",
                "id": prior_failure["host_job_id"],
                "purpose": "failed_publish",
                "returncode": prior_failure["returncode"],
            }
        )
    return {
        "complete": complete,
        "spec_revision": task["spec_revision"],
        "reason_code": reason,
        "summary": summary,
        "question": (
            f"要保留远端 {branch} 并发布到新分支，还是以当前远端 SHA "
            f"{remote_head} 为 lease 改写 {branch}？"
            if complete
            else "请先刷新原因与证据。"
        ),
        "remote_ssh": result.get("remote_ssh") or spec.get("remote_ssh"),
        "branch": branch,
        "local_head": local_head,
        "remote_head": remote_head,
        "allowed_decisions": (
            ["publish_new_branch", "force_publish_with_lease"] if complete else []
        ),
        "options": (
            [
                {
                    "kind": "publish_new_branch",
                    "title": "保留远端历史，发布到新分支",
                    "effect": f"不改写 {branch}；创建一个指向本地 HEAD 的新分支。",
                    "risk": "低；会新增一个远端分支。",
                    "required_input": "一个不同且合法的新分支名",
                },
                {
                    "kind": "force_publish_with_lease",
                    "title": f"以 lease 改写 {branch}",
                    "effect": (
                        f"仅当远端仍为 {remote_head} 时，"
                        f"把 {branch} 改为本地 HEAD。"
                    ),
                    "risk": "高；会改写目标分支历史，远端已移动则安全拒绝。",
                    "required_input": f"完整远端 SHA：{remote_head}",
                },
            ]
            if complete
            else []
        ),
        "evidence": evidence,
        "external_write_performed": False,
    }


def _finalize_provider_job(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    step: ProviderStep,
    state: dict,
    facts: dict[str, object],
    log_path: Path,
    log_body: bytes,
) -> str:
    now = time.time()
    after = facts.get("after")
    after_git = after.get("git") if isinstance(after, dict) else {}
    before_git = json.loads(job["dispatch_json"]).get("before", {})
    workspace_changed = canonical_json(before_git) != canonical_json(after_git)
    returncode = state.get("returncode")
    returncode = int(returncode) if isinstance(returncode, int) else 1
    succeeded = str(state.get("status")) == "completed" and returncode == 0
    stdout, stderr = _provider_output_streams(facts.get("output"))
    context_events = parse_context_events(stdout)
    manifest = {
        "provider_key": step.provider_key,
        "project_path": step.project_path,
        "host_job_id": job["id"],
        "returncode": returncode,
        "failure_class": state.get("failure_class"),
        "duration_ms": max(0, int(state.get("duration_ms") or 0)),
        "workspace_changed": workspace_changed,
        "before": before_git,
        "after": after_git,
        "stdout_tail": _bounded_tail(stdout),
        "stderr_tail": _bounded_tail(stderr),
    }
    script_result = None
    if step.provider_key == "git_publish":
        script_result = _last_json_object(stdout) or _last_json_object(stderr)
        manifest["script_result"] = script_result
    body = canonical_json(manifest).encode()
    output_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"execution-{job['sequence']:06d}"
        / "output"
        / "provider-execution.json"
    )
    if not log_body:
        log_body = (
            f"provider={step.provider_key} returncode={returncode} "
            f"workspace_changed={str(workspace_changed).lower()}\n"
        ).encode()
    _write_atomic(output_path, body)
    _write_atomic(log_path, log_body)
    connection.execute(
        """
        UPDATE host_jobs SET status = ?, ended_at = ?, returncode = ?,
            output_ref = ?, failure_code = ? WHERE id = ?
        """,
        (
            "succeeded" if succeeded else "failed",
            now,
            returncode,
            store.relative_data_path(log_path),
            None if succeeded else "provider",
            job["id"],
        ),
    )
    if state.get("session_id"):
        connection.execute(
            """
            UPDATE session_generations SET external_session_id = ?
            WHERE task_session_id = ? AND generation = ?
            """,
            (state["session_id"], job["task_session_id"], job["session_generation"]),
        )
    input_tokens = max(0, int(state.get("input_tokens") or 0))
    cached_tokens = min(input_tokens, max(0, int(state.get("cached_input_tokens") or 0)))
    output_tokens = max(0, int(state.get("output_tokens") or 0))
    if step.provider_key != "git_publish":
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            step.provider_key,
            "single",
            input_tokens,
            cached_tokens,
            output_tokens,
            str(state.get("model") or step.provider_key),
        )
    for event in context_events:
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'provider_compacted', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "provider_key": step.provider_key,
                        "generation": job["session_generation"],
                        "event": event,
                    }
                ),
                now,
            ),
        )
    for kind, path, data in (("output", output_path, body), ("log", log_path, log_body)):
        connection.execute(
            """
            INSERT INTO artifacts(
                id, project_id, task_id, kind, path, sha256, bytes,
                acceptance_id, revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                uuid4().hex,
                task["project_id"],
                task["id"],
                kind,
                store.relative_data_path(path),
                hashlib.sha256(data).hexdigest(),
                len(data),
                task["spec_revision"],
                now,
            ),
        )
    inspection = bool(
        succeeded
        and isinstance(script_result, dict)
        and script_result.get("kind") == "git_publish_inspection"
    )
    if inspection:
        output_ref = store.relative_data_path(output_path)
        context = _git_publish_decision_context(
            task,
            job,
            script_result,
            output_ref,
            prior_failure=_previous_git_publish_failure(
                store, connection, task["id"], job["sequence"]
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'needs_decision',
                phase = 'provider_recovery', next_action_at = NULL,
                next_action_kind = NULL, wait_reason = ?, fault_code = 'scope',
                updated_at = ? WHERE id = ?
            """,
            (context["summary"], now, task["id"]),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'executed', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "host_job_sequence": job["sequence"],
                        "provider_key": step.provider_key,
                        "returncode": returncode,
                        "workspace_changed": workspace_changed,
                        "normalized_total": 0,
                        "operation": "inspect",
                    }
                ),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'git_publish_needs_decision', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(context),
                now,
            ),
        )
        return "inspect"
    fallback = (
        _fallback_provider_generation(connection, task, job, step.provider_key, now)
        if not succeeded
        else None
    )
    if succeeded and step.provider_key not in {"codex_cli", "git_publish"}:
        _rotate_context_generation(connection, task, job, step.provider_key, now)
    conflict = bool(
        isinstance(script_result, dict)
        and script_result.get("code")
        in {"remote_history_conflict", "lease_mismatch"}
    )
    wait_reason = None
    if not succeeded:
        if conflict:
            remote_head = str(script_result.get("remote_head") or "不存在")
            branch = str(script_result.get("branch") or "目标分支")
            wait_reason = (
                (
                    f"远端 {branch} 已移动到 {remote_head}；"
                    "原授权证据失效，请先刷新原因与证据"
                )
                if script_result.get("code") == "lease_mismatch"
                else (
                    f"远端 {branch} 当前为 {remote_head}；"
                    "请选择发布到新分支，或用该 SHA 明确授权 force-with-lease"
                )
            )
        elif fallback:
            wait_reason = f"Provider {step.provider_key} failed; falling back to {fallback}"
        else:
            wait_reason = "Provider execution did not exit successfully"
    connection.execute(
        """
        UPDATE tasks SET public_status = ?, phase = ?, next_action_at = ?,
            next_action_kind = ?, wait_reason = ?, fault_code = ?,
            updated_at = ? WHERE id = ?
        """,
        (
            "in_progress" if succeeded or fallback else "needs_decision",
            "verify" if succeeded else ("execute" if fallback else "provider_recovery"),
            now if succeeded or fallback else None,
            "check" if succeeded else ("execute" if fallback else None),
            wait_reason,
            None if succeeded else ("scope" if conflict else "provider"),
            now,
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
            "repaired" if job["purpose"] == "repair" else "executed",
            canonical_json(
                {
                    "host_job_id": job["id"],
                    "host_job_sequence": job["sequence"],
                    "provider_key": step.provider_key,
                    "returncode": returncode,
                    "workspace_changed": workspace_changed,
                    "normalized_total": input_tokens + output_tokens,
                }
            ),
            now,
        ),
    )
    if conflict:
        context = _git_publish_decision_context(
            task,
            job,
            script_result,
            store.relative_data_path(output_path),
        )
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'git_publish_needs_decision', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(context),
                now,
            ),
        )
    return "provider_fallback" if fallback else job["purpose"]


def _rotate_context_generation(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    provider_key: str,
    now: float,
) -> bool:
    session = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (job["task_session_id"],),
    ).fetchone()
    threshold = int(
        json.loads(session["settings_json"])["values"].get(
            "rotation_input_tokens", 180_000
        )
    )
    used = connection.execute(
        """
        SELECT COALESCE(SUM(normalized_total), 0) AS value FROM model_calls
        WHERE task_session_id = ? AND session_generation = ?
        """,
        (job["task_session_id"], job["session_generation"]),
    ).fetchone()["value"]
    generation = connection.execute(
        """
        SELECT handoff_ref FROM session_generations
        WHERE task_session_id = ? AND generation = ? AND status = 'active'
        """,
        (job["task_session_id"], job["session_generation"]),
    ).fetchone()
    if used < threshold or not generation or not generation["handoff_ref"]:
        return False
    connection.execute(
        """
        UPDATE session_generations SET status = 'archived', ended_at = ?
        WHERE task_session_id = ? AND generation = ?
        """,
        (now, job["task_session_id"], job["session_generation"]),
    )
    connection.execute(
        """
        INSERT INTO session_generations(
            id, task_session_id, generation, provider_key, status,
            handoff_ref, created_at
        ) VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            uuid4().hex,
            job["task_session_id"],
            job["session_generation"] + 1,
            provider_key,
            generation["handoff_ref"],
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'context_generation_rotated', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "from_generation": job["session_generation"],
                    "to_generation": job["session_generation"] + 1,
                    "provider_key": provider_key,
                    "normalized_total": used,
                    "threshold": threshold,
                    "handoff_ref": generation["handoff_ref"],
                }
            ),
            now,
        ),
    )
    return True


def _fallback_provider_generation(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    provider_key: str,
    now: float,
) -> str | None:
    session = connection.execute(
        """
        SELECT role_key, settings_json FROM task_sessions WHERE id = ?
        """,
        (job["task_session_id"],),
    ).fetchone()
    order = (
        json.loads(session["settings_json"])
        .get("values", {})
        .get("provider_order", {})
        .get(session["role_key"], [])
    )
    try:
        candidates = order[order.index(provider_key) + 1 :]
    except ValueError:
        candidates = order
    next_provider = next(
        (
            str(candidate)
            for candidate in candidates
            if candidate != provider_key and candidate in PROVIDERS
        ),
        None,
    )
    if not next_provider:
        return None
    current = connection.execute(
        """
        SELECT handoff_ref FROM session_generations
        WHERE task_session_id = ? AND generation = ?
        """,
        (job["task_session_id"], job["session_generation"]),
    ).fetchone()
    connection.execute(
        """
        UPDATE session_generations SET status = 'archived', ended_at = ?
        WHERE task_session_id = ? AND generation = ? AND status = 'active'
        """,
        (now, job["task_session_id"], job["session_generation"]),
    )
    connection.execute(
        """
        INSERT INTO session_generations(
            id, task_session_id, generation, provider_key, status,
            handoff_ref, created_at
        ) VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            uuid4().hex,
            job["task_session_id"],
            job["session_generation"] + 1,
            next_provider,
            current["handoff_ref"] if current else None,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'provider_fallback', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "from": provider_key,
                    "to": next_provider,
                    "generation": job["session_generation"] + 1,
                    "host_job_id": job["id"],
                }
            ),
            now,
        ),
    )
    return next_provider


def _finalize_provider_cancel(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    state: dict,
    log_path: Path,
    log_body: bytes,
) -> str:
    now = time.time()
    if log_body:
        _write_atomic(log_path, log_body)
    returncode = state.get("returncode")
    connection.execute(
        """
        UPDATE host_jobs SET status = 'cancelled', ended_at = ?, returncode = ?,
            output_ref = COALESCE(?, output_ref), failure_code = 'process'
        WHERE id = ?
        """,
        (
            now,
            int(returncode) if isinstance(returncode, int) else -15,
            store.relative_data_path(log_path) if log_body else None,
            job["id"],
        ),
    )
    deadline_stop = str(task["wait_reason"] or "").startswith("[deadline]")
    connection.execute(
        """
        UPDATE tasks SET public_status = ?, outcome = ?, phase = ?,
            next_action_at = NULL, next_action_kind = NULL,
            wait_reason = ?, fault_code = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            "needs_decision" if deadline_stop else "done",
            None if deadline_stop else "cancelled",
            "provider_recovery" if deadline_stop else "done",
            (
                "[deadline] HostJob stopped after reconcile; decide whether to resume, revise, or cancel"
                if deadline_stop
                else None
            ),
            "process" if deadline_stop else None,
            now,
            task["id"],
        ),
    )
    if not deadline_stop:
        archive_task_sessions(connection, task["id"], now)
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            "deadline_stopped" if deadline_stop else "cancelled",
            canonical_json({"host_job_id": job["id"]}),
            now,
        ),
    )
    return "needs_decision" if deadline_stop else "cancel"


def _provider_output_streams(output: object) -> tuple[str, str]:
    streams = {"stdout": [], "stderr": []}
    if isinstance(output, dict):
        for chunk in output.get("chunks", []):
            if isinstance(chunk, dict) and chunk.get("stream") in streams:
                streams[str(chunk["stream"])].append(str(chunk.get("text") or ""))
    return "".join(streams["stdout"]), "".join(streams["stderr"])


def _provider_prompt(
    task: sqlite3.Row, spec: dict, purpose: str, hot_context: str | None = None
) -> str:
    if spec["kind"] == "git_publish":
        operation = str(spec.get("operation") or "publish")
        prompt = {
            "kind": "git_publish",
            "operation": operation,
            "remote_ssh": spec["remote_ssh"],
            "branch": spec["branch"],
            "expected_head": "",
        }
        if operation == "publish":
            prompt.update(
                {
                    "publish_mode": spec.get("publish_mode", "fast_forward"),
                    "expected_remote_head": spec.get("expected_remote_head"),
                    "authorization": spec["authorization"],
                }
            )
        return canonical_json(prompt)
    repair = (
        f"\nRepair context: {task['wait_reason']}"
        if purpose == "repair" and task["wait_reason"]
        else ""
    )
    access = (
        "This is a read-only analysis Task: inspect and report bounded findings; "
        "do not modify any workspace file.\n"
        if not spec.get("workspace_change_required", True)
        else ""
    )
    return (
        f"Complete this bounded code Task in the current workspace:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.{repair}\n"
        + (f"Bounded context capsule:\n{hot_context}\n" if hot_context else "")
        + access
        + "Stay inside the workspace. Make only recoverable changes. Do not commit, deploy, "
        "delete permanently, expose secrets, send external messages, make purchases, or "
        "change permissions. A scoped deletion must move the target to "
        "original-name.rm.YYYYMMDDHHMMSS instead of unlinking it. Run the smallest relevant "
        "checks and leave the workspace ready "
        "for an independent read-only checker."
    )


def _last_json_object(value: str) -> dict[str, object] | None:
    for line in reversed(value.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _reject_provider_start(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    step: ProviderStep,
    facts: dict[str, object],
    now: float,
) -> str:
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
    fallback = (
        None
        if step.provider_key == "git_publish"
        else _fallback_provider_generation(
            connection, task, job, step.provider_key, now
        )
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = ?, phase = ?, wait_reason = ?,
            fault_code = 'provider', next_action_at = ?, next_action_kind = ?,
            updated_at = ? WHERE id = ?
        """,
        (
            "in_progress" if fallback else "needs_decision",
            "execute" if fallback else "provider_recovery",
            (
                f"Provider start was rejected; falling back to {fallback}"
                if fallback
                else "Host Bridge rejected the job before acceptance"
            ),
            now if fallback else None,
            "execute" if fallback else None,
            now,
            task["id"],
        ),
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
                    "provider_key": step.provider_key,
                    "http_status": facts.get("error_status"),
                    "fallback": fallback,
                }
            ),
            now,
        ),
    )
    return "provider_fallback" if fallback else "needs_decision"


def _bounded_tail(value: str, byte_cap: int = 16_384) -> str:
    body = value.encode()
    return body[-byte_cap:].decode(errors="replace")


def _context_policy(settings: dict) -> dict[str, object]:
    return {
        "hot_max_bytes": int(settings.get("context_max_bytes", 16_384)),
        "warm_max_bytes": int(settings.get("handoff_max_bytes", 8_192)),
        "rotation_max_bytes": int(
            settings.get("session_segment_max_bytes", 65_536)
        ),
        "provider_compaction_token_limit": int(
            settings.get("native_compact_input_tokens", 120_000)
        ),
        "provider_compaction_scope": "body_after_prefix",
        "sources": {
            key: "task_session"
            for key in (
                "hot_max_bytes",
                "warm_max_bytes",
                "rotation_max_bytes",
                "provider_compaction_token_limit",
            )
        },
    }


def _prepare_provider_probe(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    spec: dict,
    purpose: str,
) -> ProbeStep:
    started_at = time.time()
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM host_jobs WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["value"]
    task_session_id, session_generation = current_session(
        connection, task["id"], task["role_key"] or "provider_probe"
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
    settings = json.loads(session["settings_json"])["values"]
    job_id = uuid4().hex
    project_path = (
        os.environ.get("PLOW_WHIP_PROBE_PROJECT_PATH", "")
        if spec["mode"] == "minimal"
        else ""
    )
    prompt = (
        f"Reply with exactly {PROBE_MARKER}. "
        "Do not inspect or modify files. Do not call tools."
        if spec["mode"] == "minimal"
        else ""
    )
    dispatch = {
        "mode": spec["mode"],
        "project_path": project_path,
        "prompt": prompt,
        "access": "read",
        "reconcile_failures": 0,
    }
    connection.execute(
        """
        INSERT INTO host_jobs(
            id, task_id, task_session_id, session_generation,
            spec_revision, sequence, purpose, status, started_at, dispatch_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?)
        """,
        (
            job_id,
            task["id"],
            task_session_id,
            session_generation,
            task["spec_revision"],
            sequence,
            purpose,
            started_at,
            canonical_json(dispatch),
        ),
    )
    phase = "probe_call" if spec["mode"] == "zero" else "probe_dispatch"
    connection.execute(
        """
        UPDATE tasks SET public_status = 'in_progress', phase = ?,
            wait_reason = NULL, fault_code = NULL, next_action_at = ?,
            next_action_kind = ?, updated_at = ? WHERE id = ?
        """,
        (
            phase,
            started_at,
            "probe" if spec["mode"] == "zero" else "probe_start",
            started_at,
            task["id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, 'host_job_prepared', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "host_job_id": job_id,
                    "sequence": sequence,
                    "purpose": purpose,
                    "probe_mode": spec["mode"],
                }
            ),
            started_at,
        ),
    )
    return ProbeStep(
        "zero" if spec["mode"] == "zero" else "start",
        task["project_id"],
        task["id"],
        job_id,
        generation["provider_key"],
        spec["mode"],
        project_path,
        prompt,
        generation["external_session_id"],
        min(int(settings.get("max_runtime_seconds", 600)), 60),
        _context_policy(settings)
        | {"max_turns": 1, "tool_no_progress_limit": 1},
    )


def pending_probe_step(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> ProbeStep:
    job = connection.execute(
        """
        SELECT * FROM host_jobs
        WHERE task_id = ? AND status IN ('dispatching', 'running', 'cancelling')
        ORDER BY sequence DESC LIMIT 1
        """,
        (task["id"],),
    ).fetchone()
    if not job:
        raise RuntimeError(f"Probe Task {task['id']} has no active HostJob")
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
    dispatch = json.loads(job["dispatch_json"])
    kind = {
        "probe_call": "zero",
        "probe_dispatch": "start",
        "probe_wait": "poll",
    }.get(task["phase"])
    if task["phase"] == "stopping":
        kind = "poll" if job["status"] == "cancelling" else "cancel"
    if not kind:
        raise RuntimeError(f"Probe Task {task['id']} has no pending action")
    spec = json.loads(task["spec_json"])
    return ProbeStep(
        kind,
        task["project_id"],
        task["id"],
        job["id"],
        generation["provider_key"],
        spec["mode"],
        str(dispatch.get("project_path") or ""),
        str(dispatch.get("prompt") or ""),
        generation["external_session_id"],
        min(int(settings.get("max_runtime_seconds", 600)), 60),
        _context_policy(settings)
        | {"max_turns": 1, "tool_no_progress_limit": 1},
    )


def perform_probe_step(step: ProbeStep) -> dict[str, object]:
    try:
        if step.kind == "zero":
            return {
                "ok": True,
                "result": run_provider_probe(step.provider_key, "zero"),
            }
        if not step.project_path:
            raise ValueError("PLOW_WHIP_PROBE_PROJECT_PATH is not configured")
        if step.kind == "start":
            state = start_provider_job(
                step.job_id,
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                timeout_seconds=step.timeout_seconds,
                context_policy=step.context_policy,
                access="read",
            )
        elif step.kind == "poll":
            state = provider_job_status(step.job_id)
        elif step.kind == "cancel":
            state = cancel_provider_job(step.job_id)
        else:
            raise ValueError("unknown Probe step")
        return {
            "ok": True,
            "state": state,
            "output": provider_job_output(step.job_id),
        }
    except (OSError, RuntimeError, ValueError) as error:
        return {"ok": False, "error": type(error).__name__}


def apply_probe_step(
    store: Store,
    connection: sqlite3.Connection,
    step: ProbeStep,
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
        return "stale_probe_fact"
    now = time.time()
    if not facts.get("ok"):
        return _defer_probe_reconcile(connection, task, job, step, now)
    if step.kind == "zero":
        return _persist_probe_result(
            store, connection, task, job, facts["result"], now
        )

    state = facts["state"]
    status = str(state.get("status"))
    if state.get("session_id"):
        connection.execute(
            """
            UPDATE session_generations SET external_session_id = ?
            WHERE task_session_id = ? AND generation = ?
            """,
            (state["session_id"], job["task_session_id"], job["session_generation"]),
        )
    if status in ACTIVE_HOST_JOB_STATUSES:
        stopping = task["phase"] == "stopping" or step.kind == "cancel"
        connection.execute(
            "UPDATE host_jobs SET status = ? WHERE id = ?",
            (
                "cancelling"
                if stopping
                else ("running" if status != "dispatching" else "dispatching"),
                job["id"],
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET phase = ?, next_action_at = ?,
                next_action_kind = ?, updated_at = ? WHERE id = ?
            """,
            (
                "stopping" if stopping else "probe_wait",
                now + 1,
                "probe_poll",
                now,
                task["id"],
            ),
        )
        return "probe_wait"
    if status == "cancelled":
        connection.execute(
            """
            UPDATE host_jobs SET status = 'cancelled', ended_at = ?,
                returncode = 130, failure_code = 'process' WHERE id = ?
            """,
            (now, job["id"]),
        )
        connection.execute(
            """
            UPDATE tasks SET public_status = 'done', phase = 'done',
                outcome = 'cancelled', next_action_at = NULL,
                next_action_kind = NULL, updated_at = ? WHERE id = ?
            """,
            (now, task["id"]),
        )
        archive_task_sessions(connection, task["id"], now)
        return "cancel"

    stdout, stderr = _provider_output_streams(facts.get("output"))
    input_tokens = max(0, int(state.get("input_tokens") or 0))
    cached_tokens = min(
        input_tokens, max(0, int(state.get("cached_input_tokens") or 0))
    )
    output_tokens = max(0, int(state.get("output_tokens") or 0))
    total_tokens = input_tokens + output_tokens
    raw_returncode = state.get("returncode")
    returncode = int(raw_returncode) if isinstance(raw_returncode, int) else 1
    marker_found = PROBE_MARKER in stdout
    within_cap = 0 < total_tokens <= PROBE_TOKEN_CAP
    result = {
        "provider_key": step.provider_key,
        "display_name": PROVIDERS[step.provider_key]["display_name"],
        "mode": "minimal",
        "configured": True,
        "available": (
            status == "completed"
            and returncode == 0
            and marker_found
            and within_cap
        ),
        "detail": (
            "minimal terminal probe returned the expected marker"
            if marker_found and within_cap
            else (
                "minimal terminal probe exceeded the Token cap"
                if total_tokens > PROBE_TOKEN_CAP
                else "minimal terminal probe did not return valid bounded evidence"
            )
        ),
        "model_invoked": True,
        "returncode": returncode,
        "marker_found": marker_found,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "token_cap": PROBE_TOKEN_CAP,
        "model": str(state.get("model") or step.provider_key),
        "checked_at": now,
        "stderr_present": bool(stderr),
    }
    return _persist_probe_result(store, connection, task, job, result, now)


def _defer_probe_reconcile(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    step: ProbeStep,
    now: float,
) -> str:
    dispatch = json.loads(job["dispatch_json"])
    failures = int(dispatch.get("reconcile_failures") or 0) + 1
    dispatch["reconcile_failures"] = failures
    settings = connection.execute(
        "SELECT settings_json FROM task_sessions WHERE id = ?",
        (job["task_session_id"],),
    ).fetchone()
    values = json.loads(settings["settings_json"])["values"]
    exhausted = failures > int(values.get("retry_count", 0))
    connection.execute(
        "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
        (canonical_json(dispatch), job["id"]),
    )
    connection.execute(
        """
        UPDATE tasks SET public_status = ?, wait_reason = ?, fault_code = ?,
            next_action_at = ?, next_action_kind = ?, updated_at = ? WHERE id = ?
        """,
        (
            "needs_decision" if exhausted else "in_progress",
            (
                "Provider probe could not produce bounded diagnostic facts"
                if exhausted
                else "Host Bridge probe reconcile scheduled"
            ),
            (
                "unsafe_unknown"
                if exhausted and step.mode == "minimal"
                else ("provider" if exhausted else "transport")
            ),
            (
                None
                if exhausted
                else now + max(1, int(values.get("retry_backoff_seconds", 0)))
            ),
            None if exhausted else task["next_action_kind"],
            now,
            task["id"],
        ),
    )
    return "needs_decision" if exhausted else f"probe_{step.kind}_retry"


def _persist_probe_result(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    result: dict[str, object],
    now: float,
) -> str:
    base = store.data_root / "projects" / task["project_id"] / "tasks" / task["id"]
    output_path = (
        base
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"execution-{job['sequence']:06d}"
        / "output"
        / "provider-probe.json"
    )
    log_path = (
        base
        / "sessions"
        / (task["role_key"] or "provider_probe")
        / f"generation-{job['session_generation']:06d}"
        / f"sequence-{job['sequence']:06d}.log"
    )
    body = canonical_json(result).encode()
    log_body = (
        f"provider={result['provider_key']} mode={result['mode']} "
        f"available={result['available']} detail={result['detail']}\n"
    ).encode()
    _write_atomic(output_path, body)
    _write_atomic(log_path, log_body)
    if result["model_invoked"]:
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            str(result["provider_key"]),
            "single",
            int(result["input_tokens"]),
            int(result["cached_input_tokens"]),
            int(result["output_tokens"]),
            str(result["model"] or result["provider_key"]),
        )
    returncode = result.get("returncode")
    returncode = int(returncode) if isinstance(returncode, int) else 0
    connection.execute(
        """
        UPDATE host_jobs SET status = ?, ended_at = ?, returncode = ?,
            output_ref = ?, failure_code = ? WHERE id = ?
        """,
        (
            "succeeded" if returncode == 0 else "failed",
            now,
            returncode,
            store.relative_data_path(log_path),
            None if returncode == 0 else "provider",
            job["id"],
        ),
    )
    for kind, path, data in (
        ("output", output_path, body),
        ("log", log_path, log_body),
    ):
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
                    f"provider_{result['mode']}_probe"
                    if kind == "output"
                    else None
                ),
                task["spec_revision"],
                now,
            ),
        )
    connection.execute(
        """
        UPDATE tasks SET public_status = 'in_progress', phase = 'verify',
            next_action_at = ?, next_action_kind = 'check', updated_at = ?
        WHERE id = ?
        """,
        (now, now, task["id"]),
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
                    "provider_key": result["provider_key"],
                    "mode": result["mode"],
                    "available": result["available"],
                    "model_invoked": result["model_invoked"],
                    "total_tokens": result["total_tokens"],
                    "host_job_id": job["id"],
                }
            ),
            now,
        ),
    )
    return job["purpose"]
