from __future__ import annotations

import json
import hashlib
from typing import Any

from plow_whip_web.domain.model import TaskRecord
from plow_whip_web.store.database import Database


class ModelCallLedger:
    """Observe-only, idempotent model-call accounting."""

    def __init__(self, database: Database, settings: object | None = None) -> None:
        self.database = database
        self.settings = settings

    @staticmethod
    def prepare_in_transaction(
        connection: Any,
        *,
        call_id: str,
        call_kind: str,
        idempotency_key: str,
        provider: str,
        reserved_tokens: int = 0,
        task: TaskRecord | None = None,
        project_id: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        del reserved_tokens, run_id
        connection.execute(
            """
            INSERT OR IGNORE INTO model_calls(
                call_id, idempotency_key, project_id, goal_id, goal_id_hash,
                role_id, task_id, task_id_hash, worker_id, provider, model,
                call_kind, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
            """,
            (
                call_id,
                idempotency_key,
                task.project_id if task else project_id,
                task.goal_id if task else None,
                _identity_hash(task.goal_id if task else None),
                task.role_id if task else None,
                task.id if task else None,
                _identity_hash(task.id if task else None),
                (task.worker_id if task else None) or worker_id,
                provider,
                provider,
                _call_kind(call_kind),
            ),
        )

    @staticmethod
    def record_in_transaction(
        connection: Any,
        *,
        call_id: str,
        execution: dict[str, Any],
        task: TaskRecord | None = None,
        provider: str | None = None,
        call_kind: str = "task_execution",
        project_id: str | None = None,
        worker_id: str | None = None,
        run_id: str | None = None,
        add_to_task: bool = False,
        idempotency_key: str | None = None,
        attempt_id: str | None = None,
        episode_id: str | None = None,
        host_job_id: str | None = None,
    ) -> bool:
        del run_id
        input_tokens = max(0, int(execution.get("input_tokens") or 0))
        cached_tokens = min(
            input_tokens, max(0, int(execution.get("cached_input_tokens") or 0))
        )
        output_tokens = max(0, int(execution.get("output_tokens") or 0))
        snapshot_kind = str(execution.get("snapshot_kind") or "per_call")
        if snapshot_kind not in {"per_call", "cumulative"}:
            raise ValueError("snapshot_kind must be per_call or cumulative")
        resolved_worker = worker_id or (task.worker_id if task else None)
        session = _physical_session(
            connection,
            task,
            host_job_id=host_job_id,
            execution_session_id=(
                str(execution.get("external_session_id"))
                if execution.get("external_session_id") else None
            ),
        )
        previous = None
        if snapshot_kind == "cumulative" and session is not None:
            previous = connection.execute(
                """
                SELECT call_id, input_tokens, cached_input_tokens, output_tokens
                FROM model_calls
                WHERE physical_session_id = ? AND session_generation = ?
                  AND snapshot_kind = 'cumulative' AND status = 'completed'
                  AND call_id != ?
                ORDER BY settled_sequence DESC LIMIT 1
                """,
                (session["external_session_id"], session["session_generation"], call_id),
            ).fetchone()
        normalized = _normalized_delta(
            snapshot_kind,
            input_tokens,
            cached_tokens,
            output_tokens,
            previous,
        )
        settled_sequence = int(connection.execute(
            "SELECT COALESCE(MAX(settled_sequence), 0) + 1 FROM model_calls"
        ).fetchone()[0])
        existed = connection.execute(
            "SELECT status FROM model_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        values = (
            task.project_id if task else project_id,
            task.goal_id if task else None,
            _identity_hash(task.goal_id if task else None),
            task.role_id if task else None,
            task.id if task else None,
            _identity_hash(task.id if task else None),
            attempt_id,
            episode_id,
            resolved_worker,
            host_job_id,
            provider or (task.provider if task else "unknown"),
            provider or (task.provider if task else "unknown"),
            _call_kind(call_kind),
            session["external_session_id"] if session else None,
            session["session_generation"] if session else None,
            snapshot_kind,
            previous["call_id"] if previous else None,
            json.dumps(execution, sort_keys=True, separators=(",", ":")),
            input_tokens,
            cached_tokens,
            output_tokens,
            *normalized,
            str(execution.get("attribution_granularity") or "turn"),
            str(execution.get("value_classification") or "unknown"),
            execution.get("rotation_reason"),
            call_id,
        )
        if existed is None:
            connection.execute(
                """
                INSERT INTO model_calls(
                    project_id, goal_id, goal_id_hash, role_id, task_id,
                    task_id_hash, attempt_id, episode_id,
                    worker_id, host_job_id, provider, model, call_kind,
                    physical_session_id, session_generation, snapshot_kind,
                    previous_call_id, raw_usage_json, input_tokens,
                    cached_input_tokens, output_tokens, normalized_input_tokens,
                    normalized_cached_input_tokens, normalized_output_tokens,
                    attribution_granularity, value_classification,
                    rotation_reason, call_id, idempotency_key, status,
                    settled_sequence, settled_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, CURRENT_TIMESTAMP
                )
                """,
                (
                    *values,
                    idempotency_key or f"model-call:{call_id}",
                    settled_sequence,
                ),
            )
            inserted = True
        elif existed["status"] not in {"completed", "failed"}:
            connection.execute(
                """
                UPDATE model_calls SET
                    project_id = ?, goal_id = ?, goal_id_hash = ?, role_id = ?,
                    task_id = ?, task_id_hash = ?, attempt_id = ?, episode_id = ?,
                    worker_id = ?, host_job_id = ?,
                    provider = ?, model = ?, call_kind = ?,
                    physical_session_id = ?, session_generation = ?,
                    snapshot_kind = ?, previous_call_id = ?, raw_usage_json = ?,
                    input_tokens = ?, cached_input_tokens = ?, output_tokens = ?,
                    normalized_input_tokens = ?,
                    normalized_cached_input_tokens = ?,
                    normalized_output_tokens = ?, attribution_granularity = ?,
                    value_classification = ?, rotation_reason = ?,
                    status = 'completed', settled_sequence = ?,
                    settled_at = CURRENT_TIMESTAMP
                WHERE call_id = ?
                """,
                (*values[:-1], settled_sequence, values[-1]),
            )
            inserted = True
        else:
            inserted = False
        if add_to_task and task is not None and inserted:
            connection.execute(
                """
                UPDATE tasks SET tokens_used = tokens_used + ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (normalized[0] + normalized[2], task.id),
            )
        return inserted

    settle_in_transaction = record_in_transaction

    def settle(
        self,
        task: TaskRecord | None,
        execution: dict[str, Any],
        *,
        call_id: str,
        provider: str | None = None,
        add_to_task: bool = False,
        attempt_id: str | None = None,
        episode_id: str | None = None,
        host_job_id: str | None = None,
    ) -> bool:
        with self.database.transaction(immediate=True) as connection:
            return self.record_in_transaction(
                connection,
                call_id=call_id,
                execution=execution,
                task=task,
                provider=provider,
                add_to_task=add_to_task,
                attempt_id=attempt_id,
                episode_id=episode_id,
                host_job_id=host_job_id,
            )

    def record(
        self,
        task: TaskRecord,
        execution: dict[str, Any],
        *,
        provider: str,
        run_id: str | None = None,
        add_to_task: bool = False,
        attempt_id: str | None = None,
        episode_id: str | None = None,
        host_job_id: str | None = None,
    ) -> None:
        self.settle(
            task,
            execution,
            call_id=run_id or f"task-usage:{task.id}",
            provider=provider,
            add_to_task=add_to_task,
            attempt_id=attempt_id,
            episode_id=episode_id,
            host_job_id=host_job_id,
        )

    def summary(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                """
                SELECT COALESCE(SUM(normalized_input_tokens), 0) input_tokens,
                       COALESCE(SUM(normalized_cached_input_tokens), 0)
                           cached_input_tokens,
                       COALESCE(SUM(normalized_output_tokens), 0) output_tokens
                FROM model_calls WHERE status = 'completed'
                """
            ).fetchone()
            projects = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT project_id,
                           SUM(normalized_input_tokens) AS input_tokens,
                           SUM(normalized_cached_input_tokens) AS cached_input_tokens,
                           SUM(normalized_input_tokens - normalized_cached_input_tokens)
                               AS uncached_input_tokens,
                           SUM(normalized_output_tokens) AS output_tokens,
                           SUM(normalized_input_tokens + normalized_output_tokens)
                               AS tokens
                    FROM model_calls WHERE status = 'completed'
                    GROUP BY project_id ORDER BY tokens DESC LIMIT 100
                    """
                ).fetchall()
            ]
            tasks = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT task_id,
                           SUM(normalized_input_tokens) AS input_tokens,
                           SUM(normalized_cached_input_tokens) AS cached_input_tokens,
                           SUM(normalized_input_tokens - normalized_cached_input_tokens)
                               AS uncached_input_tokens,
                           SUM(normalized_output_tokens) AS output_tokens,
                           SUM(normalized_input_tokens + normalized_output_tokens)
                               AS tokens
                    FROM model_calls
                    WHERE status = 'completed' AND task_id IS NOT NULL
                    GROUP BY task_id ORDER BY tokens DESC LIMIT 100
                    """
                ).fetchall()
            ]
            calls = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT call_id, call_kind, status, project_id, goal_id, goal_id_hash,
                           role_id, task_id, task_id_hash,
                           attempt_id, episode_id, worker_id, host_job_id, provider,
                           physical_session_id, session_generation, snapshot_kind,
                           previous_call_id, input_tokens, cached_input_tokens,
                           input_tokens - cached_input_tokens AS uncached_input_tokens,
                           output_tokens, normalized_input_tokens,
                           normalized_cached_input_tokens,
                           normalized_output_tokens, attribution_granularity,
                           value_classification, rotation_reason, created_at, settled_at
                    FROM model_calls ORDER BY created_at DESC, call_id DESC LIMIT 100
                    """
                ).fetchall()
            ]
            attribution = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT project_id, goal_id, goal_id_hash, role_id, task_id,
                           task_id_hash, worker_id, provider, physical_session_id,
                           session_generation,
                           SUM(normalized_input_tokens) AS input_tokens,
                           SUM(normalized_cached_input_tokens) AS cached_input_tokens,
                           SUM(normalized_output_tokens) AS output_tokens,
                           COUNT(*) AS calls
                    FROM model_calls WHERE status = 'completed'
                    GROUP BY project_id, goal_id, goal_id_hash, role_id, task_id,
                             task_id_hash, worker_id, provider,
                             physical_session_id, session_generation
                    ORDER BY input_tokens + output_tokens DESC
                    LIMIT 100
                    """
                ).fetchall()
            ]
            return {
                "input_tokens": int(total["input_tokens"]),
                "cached_input_tokens": int(total["cached_input_tokens"]),
                "cached_input_tokens_in_total": True,
                "output_tokens": int(total["output_tokens"]),
                "total_tokens": int(total["input_tokens"]) + int(total["output_tokens"]),
                "total_formula": "input_tokens + output_tokens",
                "control_gate": "disabled",
                "control_tokens": 0,
                "projects": projects,
                "tasks": tasks,
                "attribution": attribution,
                "calls": calls,
            }
        finally:
            connection.close()


def _call_kind(value: str) -> str:
    return {
        "task_execution": "executor",
        "convention_refinement": "convention_refinement",
    }.get(value, value)


def _identity_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _physical_session(
    connection: Any,
    task: TaskRecord | None,
    *,
    host_job_id: str | None = None,
    execution_session_id: str | None = None,
) -> Any:
    if host_job_id is not None:
        row = connection.execute(
            """
            SELECT
                COALESCE(?, hj.external_session_id, ps.external_session_id)
                    AS external_session_id,
                hj.session_generation AS session_generation
            FROM host_jobs hj
            LEFT JOIN provider_sessions ps
              ON ps.task_id = hj.task_id
             AND ps.session_generation = hj.session_generation
            WHERE hj.job_id = ?
            """,
            (execution_session_id, host_job_id),
        ).fetchone()
        if row is not None:
            return row
    if task is not None:
        row = connection.execute(
            """
            SELECT external_session_id, session_generation
            FROM provider_sessions
            WHERE task_id = ?
            ORDER BY session_generation DESC LIMIT 1
            """,
            (task.id,),
        ).fetchone()
        if row is not None:
            return row
    if execution_session_id:
        # Non-Task control calls are one-shot and never resumable, but the
        # provider-issued physical id remains attributable.
        return {
            "external_session_id": execution_session_id,
            "session_generation": 1,
        }
    return None


def _normalized_delta(
    snapshot_kind: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    previous: Any,
) -> tuple[int, int, int]:
    if snapshot_kind == "per_call" or previous is None:
        return input_tokens, cached_tokens, output_tokens
    # Provider counters can reset or be reclassified independently. Normalize
    # each component instead of charging the whole snapshot again when only one
    # component decreases. Cached input remains a subset after normalization.
    normalized_input = _counter_delta(input_tokens, int(previous["input_tokens"]))
    normalized_cached = min(
        normalized_input,
        _counter_delta(cached_tokens, int(previous["cached_input_tokens"])),
    )
    normalized_output = _counter_delta(output_tokens, int(previous["output_tokens"]))
    return normalized_input, normalized_cached, normalized_output


def _counter_delta(current: int, previous: int) -> int:
    return current - previous if current >= previous else current
