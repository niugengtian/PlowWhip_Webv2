from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .artifact_contract import (
    register_artifact,
    register_indexed_artifact,
    task_data_scope,
    workspace_scope,
)
from .continuity import checkpoint_task_session
from .intake import canonical_json
from .lifecycle_state import (
    apply_model_budget_fact,
    finalize_task_terminal,
    increment_task_retry,
    write_task_fields,
)
from .planner import audit_delivery_intent
from .provider import (
    ACTIVE_HOST_JOB_STATUSES,
    HostBridgeError,
    PROVIDERS,
    PROBE_MARKER,
    PROBE_TOKEN_CAP,
    cancel_provider_job,
    parse_context_events,
    provider_agent_text,
    provider_job_output,
    provider_job_status,
    model_budget_fact,
    record_model_call,
    run_provider_probe,
    selected_model,
    start_provider_job,
    workspace_snapshot,
)
from .recovery_policy import (
    MAX_SAME_PROBLEM_RETRIES,
    clamp_retry_count,
    count_recovery_attempts,
    failure_signature,
    recovery_cap_reached,
    recovery_cap_wait_reason,
)
from .secret_policy import redact_secret
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
    source_job_id: str | None = None
    source_before: dict | None = None
    source_after: dict | None = None
    result_artifact_path: str | None = None
    capability: dict[str, object] | None = None
    model: str | None = None
    workspace_kind: str = "project"
    workspace_key: str | None = None


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
    model: str | None = None


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
    budget_contract = _context_budget_contract(
        connection, task_id, role_key, settings
    )
    settings["budget_contract"] = budget_contract
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


def _context_budget_contract(
    connection: sqlite3.Connection,
    task_id: str,
    role_key: str,
    settings: dict,
) -> dict[str, object]:
    values = settings["values"]
    sources = settings["sources"]
    keys = (
        "context_max_bytes",
        "handoff_max_bytes",
        "checkpoint_max_bytes",
        "session_segment_max_bytes",
        "monitor_tail_bytes",
    )
    effective = {
        key: {"value": int(values[key]), "source": sources[key]}
        for key in keys
    }
    task = connection.execute(
        "SELECT spec_json, acceptance_json FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not task:
        raise ValueError("Task is missing while freezing context budgets")
    mandatory_bytes = len(
        canonical_json(
            {
                "task_id": task_id,
                "role_key": role_key,
                "task_spec": json.loads(task["spec_json"]),
                "acceptance": json.loads(task["acceptance_json"]),
            }
        ).encode()
    )
    if mandatory_bytes > int(values["context_max_bytes"]):
        raise ValueError(
            "mandatory TaskSpec and acceptance exceed effective "
            f"context_max_bytes={values['context_max_bytes']} "
            f"from {sources['context_max_bytes']}"
        )
    warnings = []
    warm_cap = min(
        int(values["handoff_max_bytes"]),
        int(values["checkpoint_max_bytes"]),
    )
    if int(values["handoff_max_bytes"]) != int(values["checkpoint_max_bytes"]):
        warnings.append(
            "Warm handoff uses the smaller handoff/checkpoint cap "
            f"({warm_cap} bytes)"
        )
    if warm_cap + mandatory_bytes > int(values["context_max_bytes"]):
        warnings.append(
            "full Warm handoff plus mandatory Context may not fit; "
            "the verified handoff reference will be retained instead of truncating "
            "mandatory TaskSpec content"
        )
    if int(values["monitor_tail_bytes"]) > int(
        values["session_segment_max_bytes"]
    ):
        warnings.append(
            "observation byte cap exceeds one Cold segment; observation remains "
            "non-evidentiary and segment history remains canonical"
        )
    return {
        "mandatory_context_bytes": mandatory_bytes,
        "effective": effective,
        "warnings": warnings,
    }


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
        "local_script_runner": "local_script",
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


def activate_task_session_roles(
    connection: sqlite3.Connection,
    task_id: str,
    role_keys: set[str],
    now: float,
) -> None:
    """Keep only the selected Task roles active and restore any archived selection."""
    if not role_keys:
        raise ValueError("at least one TaskSession role is required")
    sessions = connection.execute(
        "SELECT id, role_key FROM task_sessions WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    for session in sessions:
        if session["role_key"] not in role_keys:
            connection.execute(
                """
                UPDATE session_generations SET status = 'archived', ended_at = ?
                WHERE task_session_id = ? AND status = 'active'
                """,
                (now, session["id"]),
            )
            continue
        active = connection.execute(
            """
            SELECT 1 FROM session_generations
            WHERE task_session_id = ? AND status = 'active'
            """,
            (session["id"],),
        ).fetchone()
        if active:
            continue
        previous = connection.execute(
            """
            SELECT generation, provider_key, handoff_ref
            FROM session_generations
            WHERE task_session_id = ?
            ORDER BY generation DESC LIMIT 1
            """,
            (session["id"],),
        ).fetchone()
        if not previous:
            raise RuntimeError(f"TaskSession {session['id']} has no generation")
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
                previous["generation"] + 1,
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
    if spec["kind"] in {"provider_task", "git_publish", "local_script"}:
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
        coverage = _task_result_coverage(spec)
        for kind, path, _data in (
            ("output", output_path, body),
            ("log", log_path, log_body),
        ):
            register_artifact(
                store,
                connection,
                project_id=task["project_id"],
                task_id=task["id"],
                kind=kind,
                path=path,
                stored_path=store.relative_data_path(path),
                acceptance_id=(
                    "artifact_content_sha256" if kind == "output" else None
                ),
                revision=task["spec_revision"],
                scope=task_data_scope(coverage if kind == "output" else []),
                source_task_id=task["id"],
                created_at=ended_at,
            )
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "in_progress",
                "phase": "verify",
                "next_action_at": ended_at,
                "next_action_kind": "check",
                "updated_at": ended_at,
            },
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
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "execute",
                "wait_reason": "deterministic write failed; automatic path exhausted",
                "fault_code": "process",
                "next_action_at": None,
                "outcome": None,
                "updated_at": ended_at,
            },
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
        hot_context = None
        if spec["kind"] not in {"git_publish", "local_script"}:
            hot_context = compile_hot_context(
                store,
                connection,
                task,
                task["role_key"] or "fullstack",
            )
        prompt = _provider_prompt(
            task,
            spec,
            purpose,
            hot_context,
            store=store,
            connection=connection,
        )
    except ValueError as error:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "provider_recovery",
                "wait_reason": str(error),
                "fault_code": "scope",
                "next_action_at": None,
                "updated_at": started_at,
            },
        )
        return "needs_decision"
    access = "write" if spec.get("workspace_change_required", True) else "read"
    reusable = None
    if (
        purpose == "execute"
        and spec.get("kind") == "provider_task"
        and access == "read"
    ):
        reusable = connection.execute(
            """
            SELECT job.id FROM host_jobs job
            JOIN session_generations generation
              ON generation.task_session_id = job.task_session_id
             AND generation.generation = job.session_generation
            WHERE job.task_id = ? AND job.spec_revision = ?
              AND job.purpose = 'execute' AND job.status = 'succeeded'
              AND job.returncode = 0 AND generation.provider_key = ?
            ORDER BY job.sequence DESC LIMIT 1
            """,
            (task["id"], task["spec_revision"], generation["provider_key"]),
        ).fetchone()
        reusable_root = (
            _root_provider_execution(
                store, connection, task["id"], task["spec_revision"], reusable["id"]
            )
            if reusable
            else None
        )
        if reusable_root:
            reusable = {"id": reusable_root[0]}
            reusable_execution = reusable_root[1]
        else:
            reusable = None
            reusable_execution = None
    else:
        reusable_execution = None
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
                    "reuse_from_host_job_id": reusable["id"] if reusable else None,
                    "reuse_before": (
                        reusable_execution["before"] if reusable_execution else None
                    ),
                    "reuse_after": (
                        reusable_execution["after"] if reusable_execution else None
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
            "phase": "execute_snapshot",
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": started_at,
            "next_action_kind": "snapshot",
            "updated_at": started_at,
        },
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
        str(spec.get("project_path") or ""),
        prompt,
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        access,
        _execution_context_policy(settings, spec),
        reusable["id"] if reusable else None,
        reusable_execution["before"] if reusable_execution else None,
        reusable_execution["after"] if reusable_execution else None,
        _declared_artifact_path(spec),
        _task_capability(task, spec, access),
        selected_model(settings, generation["provider_key"]),
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
    )


def _provider_execution_for_job(
    store: Store,
    connection: sqlite3.Connection,
    task_id: str,
    spec_revision: int,
    job_id: str,
) -> dict | None:
    rows = connection.execute(
        """
        SELECT path FROM artifacts
        WHERE task_id = ? AND kind = 'output' AND revision = ?
          AND path LIKE '%/provider-execution.json'
        ORDER BY created_at DESC, rowid DESC LIMIT 20
        """,
        (task_id, spec_revision),
    ).fetchall()
    for row in rows:
        try:
            manifest = json.loads(store.resolve_data_path(row["path"]).read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(manifest, dict)
            and manifest.get("host_job_id") == job_id
            and isinstance(manifest.get("before"), dict)
            and isinstance(manifest.get("after"), dict)
        ):
            return manifest
    return None


def _root_provider_execution(
    store: Store,
    connection: sqlite3.Connection,
    task_id: str,
    spec_revision: int,
    job_id: str,
) -> tuple[str, dict] | None:
    seen = set()
    while job_id not in seen and len(seen) < 20:
        seen.add(job_id)
        manifest = _provider_execution_for_job(
            store, connection, task_id, spec_revision, job_id
        )
        if not manifest:
            return None
        source_job_id = str(manifest.get("reused_from_host_job_id") or "")
        if not source_job_id:
            return job_id, manifest
        job_id = source_job_id
    return None


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
        "execute_dispatch": (
            "recover" if dispatch.get("reuse_from_host_job_id") else "start"
        ),
        "execute_wait": "poll",
        "timeout_reconcile": "poll",
        "stopping": "cancel",
    }.get(task["phase"])
    settings = json.loads(session["settings_json"])["values"]
    if task["phase"] == "stopping":
        cancel_sent_at = dispatch.get("cancel_sent_at")
        force_sent_at = dispatch.get("force_cancel_sent_at")
        if cancel_sent_at is None:
            kind = "cancel"
        elif force_sent_at is not None:
            # Force was already issued. Poll the Bridge; it owns process truth
            # and has its own bounded force-cancel convergence watchdog.
            kind = "poll"
        elif time.time() >= float(cancel_sent_at) + int(
            settings.get("stop_grace_seconds", 10)
        ):
            kind = "force_cancel"
        else:
            kind = "poll"
    if not kind:
        raise RuntimeError(f"Task {task['id']} is not waiting on a Provider")
    return ProviderStep(
        kind,
        task["project_id"],
        task["id"],
        job["id"],
        generation["provider_key"],
        str(spec.get("project_path") or ""),
        str(
            dispatch.get("prompt")
            or _provider_prompt(task, spec, job["purpose"])
        ),
        generation["external_session_id"],
        int(settings.get("max_runtime_seconds", 600)),
        str(dispatch.get("access") or "write"),
        _execution_context_policy(settings, spec),
        str(dispatch.get("reuse_from_host_job_id") or "") or None,
        dispatch.get("reuse_before"),
        dispatch.get("reuse_after"),
        _declared_artifact_path(spec),
        _task_capability(
            task, spec, str(dispatch.get("access") or "write")
        ),
        selected_model(settings, generation["provider_key"]),
        str(dispatch.get("workspace_kind") or "project"),
        (
            str(dispatch["workspace_key"])
            if dispatch.get("workspace_key")
            else None
        ),
    )


def _task_capability(
    task: sqlite3.Row, spec: dict, access: str
) -> dict[str, object]:
    base = {
        "project_id": task["project_id"],
        "task_id": task["id"],
        "spec_revision": task["spec_revision"],
    }
    contract = spec.get("task_contract")
    boundary = (
        contract.get("authorization_boundary", {})
        if isinstance(contract, dict)
        else {}
    )
    if access == "read":
        return {
            "tier": "read_only",
            **base,
            "allowed_actions": list(
                boundary.get("allowed_actions") or ["read_workspace"]
            ),
            "target_scope": str(
                boundary.get("target_scope") or "project_workspace"
            ),
        }
    if spec.get("kind") != "git_publish":
        return {
            "tier": "recoverable_workspace_write",
            **base,
            "allowed_actions": list(
                boundary.get("allowed_actions")
                or ["write_workspace", "run_checks"]
            ),
            "target_scope": str(
                boundary.get("target_scope") or "project_workspace"
            ),
        }
    authorization = spec.get("authorization")
    if (
        not isinstance(authorization, dict)
        or task["terminal_capabilities_revoked_at"] is not None
    ):
        raise ValueError("Git publish capability is missing or revoked")
    return {
        "tier": "authorized_external_effect",
        **base,
        "authorization": authorization,
    }


def perform_provider_step(step: ProviderStep) -> dict[str, object]:
    stage = step.kind
    try:
        if step.kind == "snapshot":
            return {
                "ok": True,
                "before": (
                    {"git": step.source_before}
                    if step.source_job_id
                    else {
                        "kind": "butler_canonical_refs",
                        "sha256": hashlib.sha256(step.prompt.encode()).hexdigest(),
                    }
                    if step.workspace_kind == "planner"
                    else workspace_snapshot(step.project_path)
                ),
            }
        if step.kind == "recover":
            state = provider_job_status(str(step.source_job_id))
            stage = "output"
        elif step.kind == "start":
            state = start_provider_job(
                step.job_id,
                step.provider_key,
                step.project_path,
                step.prompt,
                session_id=step.session_id,
                timeout_seconds=step.timeout_seconds,
                context_policy=step.context_policy,
                access=step.access,
                capability=step.capability,
                model=step.model,
                workspace_kind=step.workspace_kind,
                workspace_key=step.workspace_key,
            )
            stage = "output"
        elif step.kind == "poll":
            state = provider_job_status(step.job_id)
            stage = "output"
        elif step.kind in {"cancel", "force_cancel"}:
            state = cancel_provider_job(
                step.job_id,
                force=step.kind == "force_cancel",
            )
            stage = "output"
        else:
            raise ValueError("unknown Provider step")
        output = provider_job_output(
            str(step.source_job_id) if step.kind == "recover" else step.job_id,
            complete=str(state.get("status"))
            not in ACTIVE_HOST_JOB_STATUSES,
        )
        facts: dict[str, object] = {"ok": True, "state": state, "output": output}
        if (
            str(state.get("status")) not in ACTIVE_HOST_JOB_STATUSES
            and (step.project_path or step.workspace_kind == "planner")
        ):
            stage = "snapshot_after"
            facts["after"] = (
                {"git": step.source_after}
                if step.kind == "recover"
                else {
                    "kind": "butler_canonical_refs",
                    "sha256": hashlib.sha256(step.prompt.encode()).hexdigest(),
                }
                if step.workspace_kind == "planner"
                else workspace_snapshot(
                    step.project_path,
                    [step.result_artifact_path]
                    if step.result_artifact_path
                    else [],
                )
            )
        return facts
    except HostBridgeError as error:
        return {
            "ok": False,
            "error": type(error).__name__,
            "failure_kind": (
                "rejected"
                if step.kind == "start" and stage == "start" and error.rejected
                else error.failure_kind
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
            return _reject_provider_start(
                store, connection, task, job, step, facts, now
            )
        dispatch = json.loads(job["dispatch_json"])
        failures = int(dispatch.get("reconcile_failures") or 0) + 1
        dispatch["reconcile_failures"] = failures
        bridge_failure = str(facts.get("failure_kind") or "transient")
        settings = connection.execute(
            "SELECT settings_json FROM task_sessions WHERE id = ?",
            (job["task_session_id"],),
        ).fetchone()
        values = json.loads(settings["settings_json"]).get("values", {})
        max_retries = int(values.get("retry_count", 0))
        exhausted = failures > max_retries
        safe_reconcile = step.kind in {"snapshot", "recover"}
        if exhausted and safe_reconcile:
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
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision" if exhausted else "in_progress",
                "phase": "provider_recovery" if exhausted else task["phase"],
                "wait_reason": (
                    f"HostJob {job['id']} outcome is unknown after {failures} reconcile failures"
                    if exhausted and not safe_reconcile
                    else (
                        (
                            "previous successful Provider report is unavailable "
                            f"after {failures} attempts"
                        )
                        if exhausted and step.kind == "recover"
                        else f"Host Bridge snapshot unavailable after {failures} attempts"
                        if exhausted
                        else (
                            f"Host Bridge {bridge_failure} during {step.kind}; "
                            "idempotent reconcile scheduled"
                        )
                    )
                ),
                "fault_code": (
                    "unsafe_unknown"
                    if exhausted and not safe_reconcile
                    else (
                        "credential"
                        if bridge_failure == "not_configured"
                        else "scope"
                        if bridge_failure == "task_missing"
                        else "transport"
                    )
                ),
                "next_action_at": (
                    None
                    if exhausted
                    else now
                    + max(1, int(values.get("retry_backoff_seconds", 0)))
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
                        "step": step.kind,
                        "failure_kind": bridge_failure,
                    }
                ),
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
                write_task_fields(
                    connection,
                    task["id"],
                    {
                        "public_status": "needs_decision",
                        "phase": "provider_recovery",
                        "wait_reason": "本地 HEAD 已在授权后变化；请刷新 Git 发布原因与证据",
                        "fault_code": "scope",
                        "next_action_at": None,
                        "next_action_kind": None,
                        "updated_at": now,
                    },
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
        write_task_fields(
            connection,
            task["id"],
            {
                "phase": "execute_dispatch",
                "wait_reason": None,
                "fault_code": None,
                "next_action_at": now,
                "next_action_kind": "dispatch",
                "updated_at": now,
            },
        )
        return "snapshot"

    state = facts["state"]
    bridge_status = str(state.get("status"))
    log_path, log_body = _provider_log(store, task, job, facts.get("output"))
    if log_body:
        _write_atomic(log_path, log_body)
        log_path.chmod(0o600)
    if task["phase"] == "timeout_reconcile":
        if bridge_status in ACTIVE_HOST_JOB_STATUSES:
            return _record_timeout_reconciliation(
                store,
                connection,
                task,
                job,
                facts,
                log_path,
                log_body,
                now,
            )
        write_task_fields(
            connection,
            task["id"],
            {
                "deadline_at": None,
                "wait_reason": (
                    "[deadline] terminal result arrived during reconcile"
                ),
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(
                project_id, task_id, kind, detail_json, created_at
            ) VALUES (?, ?, 'deadline_terminal_reconciled', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "status": bridge_status,
                        "returncode": state.get("returncode"),
                        "next": "evaluate_real_result",
                    }
                ),
                now,
            ),
        )
    if bridge_status in ACTIVE_HOST_JOB_STATUSES:
        stopping = bool(
            task["phase"] == "stopping"
            or job["status"] == "cancelling"
            or step.kind == "cancel"
        )
        dispatch = json.loads(job["dispatch_json"])
        if (
            not stopping
            and state.get("deadline_reached_at") is not None
            and task["phase"] in {"execute_wait", "execute_dispatch", "plan_wait", "check_wait"}
        ):
            from .soft_timeout import try_soft_timeout_extension

            soft_outcome = try_soft_timeout_extension(
                connection, task, job, state, now=now
            )
            if soft_outcome == "extended":
                return "wait"
            if soft_outcome in {
                "hard_idle",
                "soft_cap_reached",
                "stall_no_increment",
            }:
                dispatch.setdefault("deadline_detected_at", now)
                dispatch["timeout_stage"] = "reconcile"
                dispatch["timeout_class"] = (
                    "hard"
                    if soft_outcome == "hard_idle"
                    else "soft_stall"
                    if soft_outcome == "stall_no_increment"
                    else "soft_cap"
                )
                connection.execute(
                    """
                    UPDATE host_jobs SET status = 'running',
                        output_ref = COALESCE(?, output_ref), dispatch_json = ?
                    WHERE id = ?
                    """,
                    (
                        store.relative_data_path(log_path) if log_body else None,
                        canonical_json(dispatch),
                        job["id"],
                    ),
                )
                write_task_fields(
                    connection,
                    task["id"],
                    {
                        "phase": "timeout_reconcile",
                        "wait_reason": (
                            "[deadline] reconcile and checkpoint required before stop"
                        ),
                        "fault_code": "process",
                        "next_action_at": now,
                        "next_action_kind": "reconcile_stop",
                        "updated_at": now,
                    },
                )
                return "deadline_reconcile"
        if stopping:
            dispatch.setdefault("stop_requested_at", now)
            if step.kind == "cancel":
                dispatch.setdefault("cancel_sent_at", now)
            elif step.kind == "force_cancel":
                dispatch["force_cancel_sent_at"] = now
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
        # Bind Task deadline to HostJob wall budget when missing (LIVE-DS-22).
        deadline_fields: dict[str, object] = {
            "phase": "stopping" if stopping else "execute_wait",
            "wait_reason": task["wait_reason"] if stopping else None,
            "fault_code": task["fault_code"] if stopping else None,
            "next_action_at": now + 1,
            "next_action_kind": (
                "reconcile_stop" if stopping else "poll"
            ),
            "updated_at": now,
        }
        if (
            not stopping
            and task["deadline_at"] is None
            and step.kind == "start"
        ):
            session_row = connection.execute(
                """
                SELECT settings_json FROM task_sessions WHERE id = ?
                """,
                (job["task_session_id"],),
            ).fetchone()
            session_values = (
                json.loads(session_row["settings_json"]).get("values", {})
                if session_row
                else {}
            )
            budget = int(
                state.get("timeout_seconds")
                or dispatch.get("timeout_seconds")
                or session_values.get("max_runtime_seconds")
                or 600
            )
            deadline_fields["deadline_at"] = now + budget
            dispatch["timeout_seconds"] = budget
            connection.execute(
                "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
                (canonical_json(dispatch), job["id"]),
            )
        write_task_fields(
            connection,
            task["id"],
            deadline_fields,
        )
        return "cancel" if stopping else (
            "dispatch" if step.kind == "start" else "wait"
        )
    if task["phase"] == "stopping" or step.kind == "cancel":
        return _finalize_provider_cancel(store, connection, task, job, state, log_path, log_body)
    return _finalize_provider_job(
        store, connection, task, job, step, state, facts, log_path, log_body
    )


def _record_timeout_reconciliation(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    facts: dict[str, object],
    log_path: Path,
    log_body: bytes,
    now: float,
) -> str:
    output = facts.get("output")
    stream_refs = (
        output.get("stream_refs")
        if isinstance(output, dict)
        and isinstance(output.get("stream_refs"), dict)
        else {}
    )
    evidence = {
        "status": "RECONCILED_BEFORE_STOP",
        "source_task_id": task["id"],
        "source_revision": task["spec_revision"],
        "host_job_id": job["id"],
        "host_job_sequence": job["sequence"],
        "deadline_at": task["deadline_at"],
        "provider_state": {
            key: facts["state"].get(key)
            for key in (
                "status",
                "session_id",
                "deadline_reached_at",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "model",
            )
            if isinstance(facts.get("state"), dict)
        },
        "stream_refs": stream_refs,
        "handoff_checkpoint_required": True,
        "reconciled_at": now,
    }
    base = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"timeout-{job['sequence']:06d}"
    )
    evidence_path = base / "evidence" / "timeout-reconcile.json"
    evidence_body = canonical_json(evidence).encode()
    _write_atomic(evidence_path, evidence_body)
    if log_body:
        existing_log = connection.execute(
            """
            SELECT 1 FROM artifacts
            WHERE task_id = ? AND kind = 'log' AND path = ?
              AND revision = ?
            """,
            (
                task["id"],
                store.relative_data_path(log_path),
                task["spec_revision"],
            ),
        ).fetchone()
        if not existing_log:
            register_artifact(
                store,
                connection,
                project_id=task["project_id"],
                task_id=task["id"],
                kind="log",
                path=log_path,
                stored_path=store.relative_data_path(log_path),
                acceptance_id="timeout_reconcile_log",
                revision=task["spec_revision"],
                scope=task_data_scope([]),
                source_task_id=task["id"],
                created_at=now,
            )
    evidence_ref = register_artifact(
        store,
        connection,
        project_id=task["project_id"],
        task_id=task["id"],
        kind="evidence",
        path=evidence_path,
        stored_path=store.relative_data_path(evidence_path),
        acceptance_id="timeout_reconcile",
        revision=task["spec_revision"],
        scope=task_data_scope([]),
        source_task_id=task["id"],
        created_at=now,
    )
    dispatch = json.loads(job["dispatch_json"])
    dispatch.update(
        {
            "timeout_stage": "graceful_stop",
            "timeout_reconciled_at": now,
            "stop_requested_at": now,
            "stop_reason": "deadline",
            "timeout_evidence_ref": evidence_ref["path"],
            "timeout_evidence_sha256": evidence_ref["sha256"],
        }
    )
    connection.execute(
        """
        UPDATE host_jobs SET status = 'cancelling',
            output_ref = COALESCE(?, output_ref), dispatch_json = ?
        WHERE id = ?
        """,
        (
            store.relative_data_path(log_path) if log_body else None,
            canonical_json(dispatch),
            job["id"],
        ),
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress",
            "phase": "stopping",
            "wait_reason": (
                "[deadline] output Artifact/Evidence reconciled; "
                "checkpoint then graceful stop"
            ),
            "fault_code": "process",
            "next_action_at": now,
            "next_action_kind": "cancel",
            "updated_at": now,
        },
    )
    connection.execute(
        """
        INSERT INTO task_events(
            project_id, task_id, kind, detail_json, created_at
        ) VALUES (?, ?, 'deadline_reconciled', ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            canonical_json(
                {
                    "host_job_id": job["id"],
                    "evidence_ref": evidence_ref["path"],
                    "evidence_sha256": evidence_ref["sha256"],
                    "next": "checkpoint_then_graceful_stop",
                }
            ),
            now,
        ),
    )
    return "deadline_stop"


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
    text = (
        "".join(streams["stdout"])
        + ("\n" if streams["stdout"] and streams["stderr"] else "")
        + "".join(streams["stderr"])
    )
    body = redact_secret(text).encode()
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
    rows = connection.execute(
        """
        SELECT id, returncode, output_ref FROM host_jobs
        WHERE task_id = ? AND sequence < ? AND status = 'failed'
        ORDER BY sequence DESC LIMIT 20
        """,
        (task_id, sequence),
    ).fetchall()
    if not rows:
        return None
    fallback = {
        "host_job_id": rows[0]["id"],
        "returncode": rows[0]["returncode"],
        "reason": "failed_publish",
    }
    for row in rows:
        if not row["output_ref"]:
            continue
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
                    "一个仓库已向该引用进行了推送",
                )
            ):
                return {
                    "host_job_id": row["id"],
                    "returncode": row["returncode"],
                    "reason": "remote_history_conflict",
                }
        except (OSError, ValueError):
            pass
    return fallback


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
    spec = json.loads(task["spec_json"])
    result_contract = (
        spec.get("task_contract", {}).get("result", {})
        if isinstance(spec.get("task_contract"), dict)
        else {}
    )
    coverage = _task_result_coverage(spec)
    after = facts.get("after")
    after_git = after.get("git") if isinstance(after, dict) else {}
    before_git = json.loads(job["dispatch_json"]).get("before", {})
    workspace_changed = canonical_json(before_git) != canonical_json(after_git)
    returncode = state.get("returncode")
    returncode = int(returncode) if isinstance(returncode, int) else 1
    workspace_apply = state.get("workspace_apply")
    applied_delta = (
        isinstance(workspace_apply, dict)
        and bool(workspace_apply.get("applied"))
        and (
            int(workspace_apply.get("created") or 0)
            + int(workspace_apply.get("modified") or 0)
        )
        > 0
    )
    # LIVE-DS-26: nonzero exit with applied workspace payload still counts as
    # HostJob success so formal Artifact finalize is not skipped.
    succeeded = str(state.get("status")) == "completed" and (
        returncode == 0 or applied_delta or workspace_changed
    )
    stdout, stderr = _provider_output_streams(facts.get("output"))
    context_events = parse_context_events(stdout)
    script_result = (
        _last_json_object(stdout) or _last_json_object(stderr)
        if step.provider_key in {"git_publish", "local_script"}
        else None
    )
    report, report_truncated = (
        (
            redact_secret(provider_agent_text(stdout)),
            False,
        )
        if step.provider_key not in {"git_publish", "local_script"}
        else ("", False)
    )
    report_body = report.encode()
    report_path = (
        store.data_root
        / "projects"
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "artifacts"
        / f"revision-{task['spec_revision']:06d}"
        / f"execution-{job['sequence']:06d}"
        / "output"
        / "provider-report.md"
    )
    report_ref = store.relative_data_path(report_path) if report else None
    report_sha256 = hashlib.sha256(report_body).hexdigest() if report else None
    provider_result = (
        {
            "kind": "output",
            "path": report_ref,
            "sha256": report_sha256,
            "bytes": len(report_body),
            "acceptance_id": "provider_report",
            "revision": task["spec_revision"],
            "scope": task_data_scope(
                coverage
                if result_contract.get("type") == "evidence"
                else []
            ),
            "source_task_id": task["id"],
        }
        if report_ref and report_sha256
        else None
    )
    formal_result_error = None
    result_artifacts: list[dict[str, object]] = []
    if result_contract.get("type") == "artifact":
        artifact_path = _declared_artifact_path(spec)
        requested = (
            after.get("requested", []) if isinstance(after, dict) else []
        )
        indexed = next(
            (
                item
                for item in requested
                if isinstance(item, dict)
                and item.get("path") == artifact_path
            ),
            None,
        )
        if (
            not artifact_path
            or not isinstance(indexed, dict)
            or not isinstance(indexed.get("sha256"), str)
            or not isinstance(indexed.get("bytes"), int)
        ):
            formal_result_error = (
                "declared workspace Artifact is missing from the complete "
                "post-execution snapshot"
            )
        else:
            result_artifacts.append(
                register_indexed_artifact(
                    connection,
                    project_id=task["project_id"],
                    task_id=task["id"],
                    kind="output",
                    stored_path=artifact_path,
                    sha256=str(indexed["sha256"]),
                    bytes_count=int(indexed["bytes"]),
                    acceptance_id="task_result_artifact",
                    revision=task["spec_revision"],
                    scope=workspace_scope(step.project_path, coverage),
                    source_task_id=task["id"],
                    created_at=now,
                )
            )
    elif result_contract.get("type") == "workspace_change":
        if not workspace_changed:
            formal_result_error = (
                "declared workspace-change result produced no workspace revision"
            )
        else:
            revision_path = (
                store.data_root
                / "projects"
                / task["project_id"]
                / "tasks"
                / task["id"]
                / "artifacts"
                / f"revision-{task['spec_revision']:06d}"
                / f"execution-{job['sequence']:06d}"
                / "output"
                / "workspace-revision.json"
            )
            revision_body = canonical_json(
                {
                    "version": 1,
                    "project_path": step.project_path,
                    "source_task_id": task["id"],
                    "spec_revision": task["spec_revision"],
                    "before": before_git,
                    "after": after_git,
                    "coverage": coverage,
                }
            ).encode()
            _write_atomic(revision_path, revision_body)
            result_artifacts.append(
                register_artifact(
                    store,
                    connection,
                    project_id=task["project_id"],
                    task_id=task["id"],
                    kind="output",
                    path=revision_path,
                    stored_path=store.relative_data_path(revision_path),
                    acceptance_id="workspace_revision",
                    revision=task["spec_revision"],
                    scope=task_data_scope(coverage),
                    source_task_id=task["id"],
                    created_at=now,
                )
            )
    elif result_contract.get("type") == "evidence":
        if isinstance(script_result, dict):
            result_path = (
                store.data_root
                / "projects"
                / task["project_id"]
                / "tasks"
                / task["id"]
                / "artifacts"
                / f"revision-{task['spec_revision']:06d}"
                / f"execution-{job['sequence']:06d}"
                / "output"
                / "structured-result.json"
            )
            _write_atomic(
                result_path, (canonical_json(script_result) + "\n").encode()
            )
            result_artifacts.append(
                register_artifact(
                    store,
                    connection,
                    project_id=task["project_id"],
                    task_id=task["id"],
                    kind="output",
                    path=result_path,
                    stored_path=store.relative_data_path(result_path),
                    acceptance_id="structured_result",
                    revision=task["spec_revision"],
                    scope=task_data_scope(coverage),
                    source_task_id=task["id"],
                    created_at=now,
                )
            )
        elif provider_result:
            result_artifacts.append(provider_result)
        else:
            formal_result_error = (
                "declared Evidence result produced no complete structured result"
            )
    succeeded = succeeded and formal_result_error is None
    manifest = {
        "manifest_version": 2,
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
        "tail_observation_only": True,
        "provider_report_ref": report_ref,
        "provider_report_sha256": report_sha256,
        "provider_report_bytes": len(report_body),
        "provider_report_truncated": report_truncated,
        "provider_statement": provider_result,
        "result_contract": result_contract,
        "result_artifacts": result_artifacts,
        "formal_result_error": formal_result_error,
        "reused_from_host_job_id": step.source_job_id,
    }
    if step.provider_key in {"git_publish", "local_script"}:
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
    if report:
        _write_atomic(report_path, report_body)
    _write_atomic(log_path, log_body)
    log_path.chmod(0o600)
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
    if step.provider_key != "git_publish" and not step.source_job_id:
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            step.provider_key,
            str(state.get("usage_kind") or "cumulative"),
            input_tokens,
            cached_tokens,
            output_tokens,
            str(state.get("model") or step.provider_key),
        )
    budget_fact = model_budget_fact(connection, task["id"])
    if budget_fact:
        apply_model_budget_fact(connection, task["id"], budget_fact)
    budget_reached = budget_fact is not None
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
    artifacts = [
        ("output", output_path, body, None),
        ("log", log_path, log_body, None),
    ]
    if report:
        artifacts.append(("output", report_path, report_body, "provider_report"))
    for kind, path, _data, acceptance_id in artifacts:
        register_artifact(
            store,
            connection,
            project_id=task["project_id"],
            task_id=task["id"],
            kind=kind,
            path=path,
            stored_path=store.relative_data_path(path),
            acceptance_id=acceptance_id,
            revision=task["spec_revision"],
            scope=task_data_scope(
                coverage
                if (
                    kind == "output"
                    and acceptance_id == "provider_report"
                    and result_contract.get("type") == "evidence"
                )
                else []
            ),
            source_task_id=task["id"],
            created_at=now,
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
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "phase": "provider_recovery",
                "next_action_at": None,
                "next_action_kind": None,
                "wait_reason": context["summary"],
                "fault_code": "scope",
                "updated_at": now,
            },
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
    unsafe_interruption = _write_interruption_is_unsafe(step.access, state)
    fallback = (
        _fallback_provider_generation(
            store,
            connection,
            task,
            job,
            step.provider_key,
            now,
            retry_same_provider=(
                step.provider_key != "git_publish"
                and str(state.get("failure_class") or "")
                in {"process", "provider_unavailable", "transport"}
                # LIVE-DS-22: tool-loop / no-progress is not a process blip —
                # do not burn same-provider retries on identical shape.
                and str(state.get("failure_class") or "")
                != "internal_tool_no_progress"
            ),
        )
        if not succeeded and not unsafe_interruption and not budget_reached
        else None
    )
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
            wait_reason = (
                f"Provider {step.provider_key} failed; scheduled bounded retry"
                if fallback == step.provider_key
                else f"Provider {step.provider_key} failed; falling back to {fallback}"
            )
        elif recovery_cap_reached(connection, task["id"]):
            wait_reason = recovery_cap_wait_reason(
                count_recovery_attempts(connection, task["id"]),
                formal_result_error,
            )
        elif unsafe_interruption:
            wait_reason = (
                "Provider process was externally interrupted after a write-capable "
                "dispatch; workspace side effects are unknown and automatic fallback "
                "is fail-closed"
            )
        else:
            wait_reason = (
                formal_result_error
                or "Provider execution did not exit successfully"
            )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": (
                "needs_decision"
                if budget_reached
                else ("in_progress" if succeeded or fallback else "needs_decision")
            ),
            "phase": (
                "provider_recovery"
                if budget_reached
                else ("verify" if succeeded else ("execute" if fallback else "provider_recovery"))
            ),
            "next_action_at": (
                None if budget_reached else (now if succeeded or fallback else None)
            ),
            "next_action_kind": (
                None
                if budget_reached
                else ("check" if succeeded else ("execute" if fallback else None))
            ),
            "wait_reason": (
                connection.execute(
                    "SELECT wait_reason FROM tasks WHERE id = ?", (task["id"],)
                ).fetchone()["wait_reason"]
                if budget_reached
                else wait_reason
            ),
            "fault_code": (
                None
                if succeeded and not budget_reached
                else (
                    "scope"
                    if budget_reached
                    else (
                    "scope"
                    if conflict
                    else ("unsafe_unknown" if unsafe_interruption else "provider")
                    )
                )
            ),
            "updated_at": now,
        },
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
                    "normalized_total": (
                        0 if step.source_job_id else input_tokens + output_tokens
                    ),
                    "reused_from_host_job_id": step.source_job_id,
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


def _write_interruption_is_unsafe(access: str, state: dict[str, object]) -> bool:
    return bool(
        access == "write"
        and str(state.get("failure_class") or "") == "external_interruption"
    )


def _fallback_provider_generation(
    store: Store,
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    job: sqlite3.Row,
    provider_key: str,
    now: float,
    retry_same_provider: bool = False,
) -> str | None:
    from .provider_policy import provider_order_for_role

    attempts = count_recovery_attempts(connection, task["id"])
    if attempts >= MAX_SAME_PROBLEM_RETRIES:
        connection.execute(
            """
            INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
            VALUES (?, ?, 'recovery_cap_reached', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "attempts": attempts,
                        "max": MAX_SAME_PROBLEM_RETRIES,
                        "from": provider_key,
                        "host_job_id": job["id"],
                    }
                ),
                now,
            ),
        )
        write_task_fields(
            connection,
            task["id"],
            {
                "wait_reason": recovery_cap_wait_reason(
                    attempts, task["wait_reason"]
                ),
                "fault_code": "scope",
                "updated_at": now,
            },
        )
        return None

    session = connection.execute(
        """
        SELECT role_key, settings_json FROM task_sessions WHERE id = ?
        """,
        (job["task_session_id"],),
    ).fetchone()
    values = json.loads(session["settings_json"]).get("values", {})
    order = provider_order_for_role(values, session["role_key"])
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
    allowed_retries = clamp_retry_count(values.get("retry_count", 0))
    retrying = bool(
        not next_provider
        and retry_same_provider
        and provider_key in PROVIDERS
        and task["retry_count"] < allowed_retries
    )
    if retrying:
        next_provider = provider_key
    if not next_provider:
        return None
    handoff = checkpoint_task_session(
        store, connection, job["task_session_id"]
    )
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
            handoff["path"],
            now,
        ),
    )
    increment_task_retry(connection, task["id"], 1 if retrying else 0)
    connection.execute(
        """
        INSERT INTO task_events(project_id, task_id, kind, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            task["project_id"],
            task["id"],
            "provider_retry" if retrying else "provider_fallback",
            canonical_json(
                {
                    "failure_signature": failure_signature(
                        task["spec_revision"],
                        task["phase"],
                        task["fault_code"] or task["phase"],
                    ),
                    "spec_revision": task["spec_revision"],
                    "phase": task["phase"],
                    "failure_class": task["fault_code"] or task["phase"],
                    "from": provider_key,
                    "to": next_provider,
                    "generation": job["session_generation"] + 1,
                    "host_job_id": job["id"],
                    "handoff_ref": handoff["path"],
                    "handoff_sha256": handoff["sha256"],
                    "handoff_revision": handoff["revision"],
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
    remaining = connection.execute(
        """
        SELECT id FROM host_jobs
        WHERE task_id = ?
          AND status IN ('dispatching', 'running', 'cancelling')
        ORDER BY sequence DESC
        """,
        (task["id"],),
    ).fetchall()
    if remaining:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "in_progress",
                "phase": "stopping",
                "next_action_at": now,
                "next_action_kind": "cancel",
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(
                project_id, task_id, kind, detail_json, created_at
            ) VALUES (?, ?, 'host_job_stop_reconciled', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "remaining_host_job_ids": [
                            row["id"] for row in remaining
                        ],
                    }
                ),
                now,
            ),
        )
        return "cancel"
    deadline_stop = str(task["wait_reason"] or "").startswith("[deadline]")
    if deadline_stop:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "needs_decision",
                "outcome": None,
                "phase": "provider_recovery",
                "next_action_at": None,
                "next_action_kind": None,
                "wait_reason": (
                    "[deadline] HostJob stopped after reconcile; decide whether to resume, revise, or cancel"
                ),
                "fault_code": "process",
                "updated_at": now,
            },
        )
    else:
        finalize_task_terminal(
            connection, task["id"], "cancelled", now
        )
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
    task: sqlite3.Row,
    spec: dict,
    purpose: str,
    hot_context: str | None = None,
    *,
    store: Store | None = None,
    connection: sqlite3.Connection | None = None,
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
    if spec["kind"] == "local_script":
        script = dict(spec.get("script") or {})
        if script.get("origin") == "library":
            if store is None or connection is None:
                raise ValueError("local_script library resolution requires store")
            from .script_library import resolve_library_script

            resolved = resolve_library_script(
                connection,
                store,
                task["project_id"],
                str(script["item_key"]),
                int(script["revision"]) if script.get("revision") is not None else None,
            )
            script = {
                "origin": "library",
                "item_key": resolved["item_key"],
                "revision": resolved["revision"],
                "sha256": resolved["sha256"],
                "body": resolved["body"],
            }
        return canonical_json(
            {
                "kind": "local_script",
                "script": script,
                "argv": list(spec.get("argv") or []),
                "timeout_seconds": int(spec.get("timeout_seconds") or 120),
            }
        )
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
    task_contract = spec.get("task_contract", {})
    result_contract = (
        task_contract.get("result", {})
        if isinstance(task_contract, dict)
        else {}
    )
    formal_result = (
        "Frozen formal result contract: "
        f"{canonical_json(result_contract)}\n"
        "The final chat/stdout is only a model statement. "
        "If the result type is artifact, write the complete deliverable to the "
        "exact declared relative workspace path; do not substitute a summary, "
        "tail, handoff, or chat response.\n"
    )
    return (
        f"Complete this bounded code Task in the current workspace:\n{spec['instruction']}\n"
        f"Task ID: {task['id']} · spec revision {task['spec_revision']}.{repair}\n"
        + (f"Bounded context capsule:\n{hot_context}\n" if hot_context else "")
        + access
        + formal_result
        + "Stay inside the workspace. Make only recoverable changes. Do not commit, deploy, "
        "delete permanently, expose secrets, send external messages, make purchases, or "
        "change permissions. A scoped deletion must move the target to "
        "original-name.rm.YYYYMMDDHHMMSS instead of unlinking it. Run the smallest relevant "
        "checks. Never ask the owner directly; return only bounded blocker or decision "
        "facts for the lifecycle and Butler to route. Leave the workspace ready "
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
    store: Store,
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
            store, connection, task, job, step.provider_key, now
        )
    )
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress" if fallback else "needs_decision",
            "phase": "execute" if fallback else "provider_recovery",
            "wait_reason": (
                f"Provider start was rejected; falling back to {fallback}"
                if fallback
                else "Host Bridge rejected the job before acceptance"
            ),
            "fault_code": "provider",
            "next_action_at": now if fallback else None,
            "next_action_kind": "execute" if fallback else None,
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


def _task_result_coverage(spec: dict[str, object]) -> list[str]:
    contract = spec.get("task_contract")
    result = contract.get("result") if isinstance(contract, dict) else None
    coverage = result.get("coverage") if isinstance(result, dict) else None
    return (
        list(coverage)
        if isinstance(coverage, list)
        and all(isinstance(value, str) for value in coverage)
        else []
    )


def _declared_artifact_path(spec: dict[str, object]) -> str | None:
    contract = spec.get("task_contract")
    result = contract.get("result") if isinstance(contract, dict) else None
    artifact = result.get("artifact") if isinstance(result, dict) else None
    path = artifact.get("path") if isinstance(artifact, dict) else None
    return str(path) if isinstance(path, str) and path else None


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


def _execution_context_policy(
    settings: dict, spec: dict[str, object]
) -> dict[str, object]:
    policy = _context_policy(settings)
    if audit_delivery_intent(spec):
        # Write access may still be required for the report Artifact; progress
        # stays exploration so multi-file reads are not treated as stall.
        return {**policy, "progress_delivery": "audit_report"}
    return policy


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
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "in_progress",
            "phase": phase,
            "wait_reason": None,
            "fault_code": None,
            "next_action_at": started_at,
            "next_action_kind": (
                "probe" if spec["mode"] == "zero" else "probe_start"
            ),
            "updated_at": started_at,
        },
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
        selected_model(settings, generation["provider_key"]),
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
        "timeout_reconcile": "poll",
    }.get(task["phase"])
    if task["phase"] == "stopping":
        cancel_sent_at = dispatch.get("cancel_sent_at")
        if cancel_sent_at is None:
            kind = "cancel"
        elif time.time() >= float(cancel_sent_at) + int(
            settings.get("stop_grace_seconds", 10)
        ):
            kind = "force_cancel"
        else:
            kind = "poll"
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
                model=step.model,
            )
        elif step.kind == "poll":
            state = provider_job_status(step.job_id)
        elif step.kind in {"cancel", "force_cancel"}:
            state = cancel_provider_job(
                step.job_id,
                force=step.kind == "force_cancel",
            )
        else:
            raise ValueError("unknown Probe step")
        return {
            "ok": True,
            "state": state,
            "output": provider_job_output(
                step.job_id,
                complete=str(state.get("status"))
                not in ACTIVE_HOST_JOB_STATUSES,
            ),
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
    if task["phase"] == "timeout_reconcile":
        log_path, log_body = _provider_log(
            store, task, job, facts.get("output")
        )
        if log_body:
            _write_atomic(log_path, log_body)
            log_path.chmod(0o600)
        if status in ACTIVE_HOST_JOB_STATUSES:
            return _record_timeout_reconciliation(
                store,
                connection,
                task,
                job,
                facts,
                log_path,
                log_body,
                now,
            )
        write_task_fields(
            connection,
            task["id"],
            {
                "deadline_at": None,
                "wait_reason": (
                    "[deadline] terminal probe result arrived during reconcile"
                ),
                "updated_at": now,
            },
        )
        connection.execute(
            """
            INSERT INTO task_events(
                project_id, task_id, kind, detail_json, created_at
            ) VALUES (?, ?, 'deadline_terminal_reconciled', ?, ?)
            """,
            (
                task["project_id"],
                task["id"],
                canonical_json(
                    {
                        "host_job_id": job["id"],
                        "status": status,
                        "returncode": state.get("returncode"),
                        "next": "evaluate_real_result",
                    }
                ),
                now,
            ),
        )
    if status in ACTIVE_HOST_JOB_STATUSES:
        stopping = task["phase"] == "stopping" or step.kind == "cancel"
        if stopping and step.kind in {"cancel", "force_cancel"}:
            dispatch = json.loads(job["dispatch_json"])
            if step.kind == "cancel":
                dispatch.setdefault("cancel_sent_at", now)
            else:
                dispatch["force_cancel_sent_at"] = now
            connection.execute(
                "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
                (canonical_json(dispatch), job["id"]),
            )
        connection.execute(
            "UPDATE host_jobs SET status = ? WHERE id = ?",
            (
                "cancelling"
                if stopping
                else ("running" if status != "dispatching" else "dispatching"),
                job["id"],
            ),
        )
        write_task_fields(
            connection,
            task["id"],
            {
                "phase": "stopping" if stopping else "probe_wait",
                "next_action_at": now + 1,
                "next_action_kind": "probe_poll",
                "updated_at": now,
            },
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
        remaining = connection.execute(
            """
            SELECT id FROM host_jobs
            WHERE task_id = ?
              AND status IN ('dispatching', 'running', 'cancelling')
            ORDER BY sequence DESC
            """,
            (task["id"],),
        ).fetchall()
        if remaining:
            write_task_fields(
                connection,
                task["id"],
                {
                    "public_status": "in_progress",
                    "phase": "stopping",
                    "next_action_at": now,
                    "next_action_kind": "cancel",
                    "updated_at": now,
                },
            )
            return "cancel"
        finalize_task_terminal(
            connection, task["id"], "cancelled", now
        )
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
        "usage_kind": str(state.get("usage_kind") or "cumulative"),
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
    write_task_fields(
        connection,
        task["id"],
        {
            "public_status": "needs_decision" if exhausted else "in_progress",
            "wait_reason": (
                "Provider probe could not produce bounded diagnostic facts"
                if exhausted
                else "Host Bridge probe reconcile scheduled"
            ),
            "fault_code": (
                "unsafe_unknown"
                if exhausted and step.mode == "minimal"
                else ("provider" if exhausted else "transport")
            ),
            "next_action_at": (
                None
                if exhausted
                else now + max(1, int(values.get("retry_backoff_seconds", 0)))
            ),
            "next_action_kind": (
                None if exhausted else task["next_action_kind"]
            ),
            "updated_at": now,
        },
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
        f"available={result['available']}\n"
    ).encode()
    _write_atomic(output_path, body)
    _write_atomic(log_path, log_body)
    log_path.chmod(0o600)
    if result["model_invoked"]:
        record_model_call(
            connection,
            task["id"],
            job["task_session_id"],
            job["session_generation"],
            str(result["provider_key"]),
            str(result.get("usage_kind") or "cumulative"),
            int(result["input_tokens"]),
            int(result["cached_input_tokens"]),
            int(result["output_tokens"]),
            str(result["model"] or result["provider_key"]),
        )
    budget_fact = model_budget_fact(connection, task["id"])
    if budget_fact:
        apply_model_budget_fact(connection, task["id"], budget_fact)
    budget_reached = budget_fact is not None
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
    coverage = _task_result_coverage(json.loads(task["spec_json"]))
    for kind, path, _data in (
        ("output", output_path, body),
        ("log", log_path, log_body),
    ):
        register_artifact(
            store,
            connection,
            project_id=task["project_id"],
            task_id=task["id"],
            kind=kind,
            path=path,
            stored_path=store.relative_data_path(path),
            acceptance_id=(
                f"provider_{result['mode']}_probe"
                if kind == "output"
                else None
            ),
            revision=task["spec_revision"],
            scope=task_data_scope(coverage if kind == "output" else []),
            source_task_id=task["id"],
            created_at=now,
        )
    if not budget_reached:
        write_task_fields(
            connection,
            task["id"],
            {
                "public_status": "in_progress",
                "phase": "verify",
                "next_action_at": now,
                "next_action_kind": "check",
                "updated_at": now,
            },
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
    return "needs_decision" if budget_reached else job["purpose"]
