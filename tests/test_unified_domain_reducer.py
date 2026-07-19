from __future__ import annotations

import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import InvalidTransitionError, RevisionConflictError
from plow_whip_web.runtime.model_call_ledger import ModelCallLedger
from plow_whip_web.store.host_job_repository import HostJobRepository


def _app(root: Path):
    return create_app(Settings(data_dir=root / "runtime"))


def _task(
    app, project: Path, key: str, *, project_id=None, role_id=None,
    provider="generic-command",
):
    return app.state.task_repository.create(
        title=key,
        objective="deterministic work",
        project_path=str(project),
        project_id=project_id,
        role_id=role_id,
        provider=provider,
        command={"argv": ["python3", "-c", "print('ok')"]},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2,
        token_budget=1,
        idempotency_key=f"create-{key}",
    )


def test_model_calls_is_only_stored_usage_truth() -> None:
    with TemporaryDirectory() as directory:
        app = _app(Path(directory))
        connection = app.state.database.connect()
        try:
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE name IN ('model_calls','token_usage','token_reservations')"
                )
            }
        finally:
            connection.close()
        assert ("model_calls", "table") in objects
        assert ("token_usage", "view") in objects
        assert not any(name == "token_reservations" for name, _ in objects)


def test_model_call_has_direct_goal_task_worker_provider_session_attribution() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="attribution", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app, project_path, "attribution",
            project_id=project["id"], role_id=role, provider="codex",
        )
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO goals(id,title,objective,project_id,provider,status,plan_json)
                VALUES ('goal-attribution','goal','goal',?,'codex','running','{}')
                """,
                (project["id"],),
            )
            connection.execute(
                "UPDATE tasks SET goal_id = 'goal-attribution' WHERE id = ?",
                (task.id,),
            )
        task = app.state.task_repository.get(task.id)
        claim = app.state.task_repository.claim(
            task.id, expected_revision=task.revision,
            idempotency_key="claim-attribution",
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id, external_session_id="session-attribution", error=None
        )
        ModelCallLedger(app.state.database).record(
            claim.task, {"input_tokens": 5, "output_tokens": 2},
            run_id="call-attribution", provider="codex",
        )
        summary = ModelCallLedger(app.state.database).summary()
        call = next(
            item for item in summary["calls"]
            if item["call_id"] == "call-attribution"
        )
        assert call["goal_id"] == "goal-attribution"
        assert len(call["goal_id_hash"]) == 64
        assert call["task_id"] == task.id
        assert call["worker_id"] == claim.task.worker_id
        assert call["provider"] == "codex"
        assert call["status"] == "completed"
        assert call["settled_at"] is not None
        assert call["physical_session_id"] == "session-attribution"
        assert call["attribution_granularity"] == "turn"
        assert call["value_classification"] == "unknown"
        project_rollup = next(
            item for item in summary["projects"]
            if item["project_id"] == project["id"]
        )
        task_rollup = next(
            item for item in summary["tasks"] if item["task_id"] == task.id
        )
        assert project_rollup["tokens"] == 7
        assert task_rollup["tokens"] == 7
        rollup = next(
            item for item in summary["attribution"]
            if item["goal_id"] == "goal-attribution"
        )
        assert rollup["task_id"] == task.id
        assert rollup["worker_id"] == claim.task.worker_id
        assert rollup["provider"] == "codex"
        assert rollup["physical_session_id"] == "session-attribution"


def test_large_usage_never_blocks_verified_terminal_state() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = _app(root)
        repository = app.state.task_repository
        task = _task(app, project, "large-usage", provider="codex")
        claim = repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-large-usage"
        )
        verifying = repository.mark_verifying(
            task.id,
            expected_revision=claim.task.revision,
            idempotency_key="verify-large-usage",
        )
        finished = repository.finish(
            task.id,
            expected_revision=verifying.revision,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            execution={
                "returncode": 0,
                "input_tokens": 3_589_597,
                "cached_input_tokens": 3_423_488,
                "output_tokens": 17_304,
            },
            verification={
                "passed": True,
                "checks": [{"kind": "exit_code", "passed": True}],
                "evidence_hash": "large-usage-proof",
                "summary": "passed",
            },
            idempotency_key="finish-large-usage",
        )
        assert finished.status.value == "completed"
        assert app.state.budget.summary()["total_tokens"] == 3_606_901


def test_cumulative_usage_is_normalized_within_one_physical_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="session", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app,
            project_path,
            "cumulative",
            project_id=project["id"],
            role_id=role,
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-cumulative"
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id,
            external_session_id="physical-cumulative",
            error=None,
        )
        ledger = ModelCallLedger(app.state.database)
        ledger.record(
            claim.task,
            {"snapshot_kind": "cumulative", "input_tokens": 100,
             "cached_input_tokens": 60, "output_tokens": 10},
            run_id="cumulative-1", provider="codex",
        )
        ledger.record(
            claim.task,
            {"snapshot_kind": "cumulative", "input_tokens": 145,
             "cached_input_tokens": 75, "output_tokens": 18},
            run_id="cumulative-2", provider="codex",
        )
        connection = app.state.database.connect()
        try:
            second = connection.execute(
                "SELECT normalized_input_tokens, normalized_cached_input_tokens, "
                "normalized_output_tokens, previous_call_id FROM model_calls "
                "WHERE call_id = 'cumulative-2'"
            ).fetchone()
        finally:
            connection.close()
        assert tuple(second) == (45, 15, 8, "cumulative-1")


def test_cumulative_usage_uses_settlement_order_not_call_id_order() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="settlement-order", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app, project_path, "ordered-cumulative",
            project_id=project["id"], role_id=role,
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-ordered-cumulative"
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id, external_session_id="ordered-session", error=None
        )
        ledger = ModelCallLedger(app.state.database)
        for call_id, input_tokens in (
            ("z-first", 100), ("a-second", 150), ("m-third", 180)
        ):
            ledger.record(
                claim.task,
                {
                    "snapshot_kind": "cumulative",
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": input_tokens // 10,
                },
                run_id=call_id,
                provider="codex",
            )
        connection = app.state.database.connect()
        try:
            third = connection.execute(
                "SELECT previous_call_id, normalized_input_tokens, "
                "normalized_output_tokens FROM model_calls "
                "WHERE call_id = 'm-third'"
            ).fetchone()
        finally:
            connection.close()
        assert tuple(third) == ("a-second", 30, 3)


def test_cumulative_usage_normalizes_independent_counter_reset_and_cache_shift() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="counter-reset", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app, project_path, "counter-reset",
            project_id=project["id"], role_id=role,
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-counter-reset"
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id, external_session_id="counter-reset-session", error=None
        )
        ledger = ModelCallLedger(app.state.database)
        ledger.record(
            claim.task,
            {
                "snapshot_kind": "cumulative",
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 30,
            },
            run_id="counter-reset-1",
            provider="codex",
        )
        ledger.record(
            claim.task,
            {
                "snapshot_kind": "cumulative",
                "input_tokens": 110,
                "cached_input_tokens": 70,
                "output_tokens": 5,
            },
            run_id="counter-reset-2",
            provider="codex",
        )
        connection = app.state.database.connect()
        try:
            normalized = connection.execute(
                "SELECT normalized_input_tokens, normalized_cached_input_tokens, "
                "normalized_output_tokens FROM model_calls "
                "WHERE call_id = 'counter-reset-2'"
            ).fetchone()
        finally:
            connection.close()
        assert tuple(normalized) == (10, 10, 5)


def test_host_call_keeps_prepared_session_identity_after_binding_rotation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="host-lineage", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app, project_path, "host-lineage",
            project_id=project["id"], role_id=role,
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-host-lineage"
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id, external_session_id="physical-old", error=None
        )
        job = HostJobRepository(app.state.database).prepare(
            task_id=task.id, attempt_id=claim.attempt_id,
            run_id=claim.run_id, provider=task.provider,
        )
        HostJobRepository(app.state.database).record(job["job_id"], {
            "status": "completed", "session_id": "physical-old",
            "snapshot_kind": "cumulative", "input_tokens": 10,
            "cached_input_tokens": 2, "output_tokens": 3,
        })
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE provider_sessions SET state = 'archived' WHERE task_id = ?",
                (task.id,),
            )
            connection.execute(
                """
                INSERT INTO provider_sessions(
                    id, project_id, role_id, task_id, worker_id, provider,
                    session_generation, external_session_id, state
                ) VALUES ('replacement-binding', ?, ?, ?, ?, ?, 2, 'physical-new', 'bound')
                """,
                (project["id"], role, task.id, claim.task.worker_id, task.provider),
            )
        ModelCallLedger(app.state.database).record(
            claim.task,
            {
                "snapshot_kind": "cumulative", "input_tokens": 10,
                "cached_input_tokens": 2, "output_tokens": 3,
                "external_session_id": "physical-old",
            },
            run_id=claim.run_id, provider=task.provider,
            attempt_id=claim.attempt_id, host_job_id=job["job_id"],
        )
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                """
                SELECT physical_session_id, session_generation, host_job_id
                FROM model_calls WHERE call_id = ?
                """,
                (claim.run_id,),
            ).fetchone()
        finally:
            connection.close()
        assert tuple(call) == ("physical-old", 1, job["job_id"])


def test_zero_usage_host_call_is_attributed_and_task_uses_normalized_delta() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(
            name="zero-call", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(
            app, project_path, "zero-call",
            project_id=project["id"], role_id=role, provider="codex",
        )
        claim = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-zero-call"
        )
        app.state.task_repository.record_worker_result(
            claim.task.worker_id, external_session_id="physical-zero", error=None
        )
        job = HostJobRepository(app.state.database).prepare(
            task_id=task.id,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            provider=task.provider,
        )
        app.state.task_repository.finalize_host_fault(
            task.id,
            job_id=job["job_id"],
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            action="resume",
            failure_class="interrupted",
            reason="zero usage still attributable",
            execution={
                "snapshot_kind": "cumulative",
                "input_tokens": 0,
                "output_tokens": 0,
            },
            external_session_id="physical-zero",
        )
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                """
                SELECT task_id, attempt_id, host_job_id, physical_session_id,
                       normalized_input_tokens, normalized_output_tokens, status
                FROM model_calls WHERE call_id = ?
                """,
                (claim.run_id,),
            ).fetchone()
            worker_session = connection.execute(
                "SELECT external_session_id FROM workers WHERE id = ?",
                (claim.task.worker_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert tuple(call) == (
            task.id,
            claim.attempt_id,
            job["job_id"],
            "physical-zero",
            0,
            0,
            "completed",
        )
        assert worker_session is None

        retry = app.state.task_repository.claim(
            task.id,
            expected_revision=app.state.task_repository.get(task.id).revision,
            idempotency_key="claim-normalized-retry",
        )
        app.state.task_repository.record_worker_result(
            retry.task.worker_id, external_session_id="physical-zero", error=None
        )
        retry_job = HostJobRepository(app.state.database).prepare(
            task_id=task.id,
            attempt_id=retry.attempt_id,
            run_id=retry.run_id,
            provider=task.provider,
        )
        app.state.task_repository.finalize_host_fault(
            task.id,
            job_id=retry_job["job_id"],
            attempt_id=retry.attempt_id,
            run_id=retry.run_id,
            action="resume",
            failure_class="interrupted",
            reason="first cumulative snapshot",
            execution={
                "snapshot_kind": "cumulative",
                "input_tokens": 6,
                "output_tokens": 1,
            },
            external_session_id="physical-zero",
        )
        final = app.state.task_repository.claim(
            task.id,
            expected_revision=app.state.task_repository.get(task.id).revision,
            idempotency_key="claim-normalized-final",
        )
        app.state.task_repository.record_worker_result(
            final.task.worker_id, external_session_id="physical-zero", error=None
        )
        verifying = app.state.task_repository.mark_verifying(
            task.id,
            expected_revision=final.task.revision,
            idempotency_key="verify-normalized-retry",
        )
        completed = app.state.task_repository.finish(
            task.id,
            expected_revision=verifying.revision,
            attempt_id=final.attempt_id,
            run_id=final.run_id,
            execution={
                "snapshot_kind": "cumulative",
                "input_tokens": 10,
                "output_tokens": 2,
            },
            verification={
                "passed": True,
                "checks": [{"kind": "exit_code", "passed": True}],
                "evidence_hash": "normalized-proof",
                "summary": "passed",
            },
            idempotency_key="finish-normalized-retry",
        )
        assert completed.tokens_used == 12


def test_provider_session_identity_includes_task_and_never_cross_resumes() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project"
        path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(name="p", path=str(path))
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        first = _task(app, path, "first", project_id=project["id"], role_id=role)
        first_claim = app.state.task_repository.claim(
            first.id, expected_revision=0, idempotency_key="claim-first-session"
        )
        app.state.task_repository.record_worker_result(
            first_claim.task.worker_id, external_session_id="session-first", error=None
        )
        job = HostJobRepository(app.state.database).prepare(
            task_id=first.id,
            attempt_id=first_claim.attempt_id,
            run_id=first_claim.run_id,
            provider=first.provider,
        )
        app.state.task_repository.finalize_host_fault(
            first.id,
            job_id=job["job_id"],
            attempt_id=first_claim.attempt_id,
            run_id=first_claim.run_id,
            action="resume",
            failure_class="interrupted",
            reason="test handoff",
            execution={"input_tokens": 0, "output_tokens": 0},
            external_session_id="session-first",
        )
        second = _task(app, path, "second", project_id=project["id"], role_id=role)
        second_claim = app.state.task_repository.claim(
            second.id, expected_revision=0, idempotency_key="claim-second-session"
        )
        context = app.state.task_repository.worker_execution_context(
            second_claim.task.worker_id
        )
        assert context["task_id"] == second.id
        assert context["external_session_id"] is None
        connection = app.state.database.connect()
        try:
            identities = [
                tuple(row)
                for row in connection.execute(
                    "SELECT project_id, role_id, task_id FROM provider_sessions "
                    "ORDER BY task_id"
                )
            ]
        finally:
            connection.close()
        assert {item[2] for item in identities} == {first.id, second.id}


def test_evidence_rewrite_is_cas_idempotent_and_keeps_lineage() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = _app(root)
        task = _task(app, project, "rewrite")
        first = app.state.reducer.rewrite_evidence(
            aggregate_type="task",
            aggregate_id=task.id,
            expected_revision=0,
            new_evidence_hash="replacement-proof",
            actor_type="owner",
            actor_id="owner-1",
            reason="authorized correction",
            idempotency_key="rewrite-evidence-001",
        )
        replay = app.state.reducer.rewrite_evidence(
            aggregate_type="task",
            aggregate_id=task.id,
            expected_revision=0,
            new_evidence_hash="replacement-proof",
            actor_type="owner",
            actor_id="owner-1",
            reason="authorized correction",
            idempotency_key="rewrite-evidence-001",
        )
        assert first["sequence"] == replay["sequence"]
        lineage = app.state.reducer.lineage("task", task.id)
        assert lineage[-1]["previous_evidence_hash"] is None
        assert lineage[-1]["new_evidence_hash"] == "replacement-proof"
        assert lineage[-1]["actor_id"] == "owner-1"
        with pytest.raises(InvalidTransitionError):
            app.state.reducer.rewrite_evidence(
                aggregate_type="task",
                aggregate_id=task.id,
                expected_revision=0,
                new_evidence_hash="different-proof",
                actor_type="owner",
                actor_id="owner-1",
                reason="authorized correction",
                idempotency_key="rewrite-evidence-001",
            )
        with pytest.raises(RevisionConflictError):
            app.state.reducer.rewrite_evidence(
                aggregate_type="task",
                aggregate_id=task.id,
                expected_revision=0,
                new_evidence_hash="stale",
                actor_type="owner",
                actor_id=None,
                reason="stale write",
                idempotency_key="rewrite-evidence-002",
            )


def test_reducer_rejects_different_command_for_existing_revision() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = _app(root)
        task = _task(app, project, "revision-collision")
        with app.state.database.transaction(immediate=True) as connection:
            with pytest.raises(RevisionConflictError):
                app.state.reducer.record(
                    connection,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    revision=0,
                    idempotency_key="different-command-same-revision",
                    actor_type="runtime",
                    actor_id=None,
                    reason="conflicting command",
                    previous_state={"status": "unknown", "revision": -1},
                    new_state={"status": "ready", "revision": 0},
                )


def test_task_delete_retains_anonymous_usage_audit_and_artifact_file() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        artifact = project / "result.txt"
        artifact.write_text("evidence", encoding="utf-8")
        app = _app(root)
        task = app.state.task_repository.create(
            title="delete",
            objective="delete control data",
            project_path=str(project),
            command={"argv": ["true"]},
            verification=[{"kind": "file_exists", "path": "result.txt"}],
            max_attempts=1,
            token_budget=0,
            idempotency_key="create-delete-retain",
        )
        ModelCallLedger(app.state.database).record(
            task,
            {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3},
            run_id="delete-usage", provider="codex",
        )
        deleted = app.state.deletions.task(
            task.id,
            expected_revision=0,
            idempotency_key="delete-retain-001",
            actor_type="owner",
            actor_id="owner-1",
            reason="owner requested deletion",
        )
        replay = app.state.deletions.task(
            task.id,
            expected_revision=99,
            idempotency_key="delete-retain-replay",
            actor_type="owner",
            actor_id="owner-1",
            reason="replay",
        )
        assert deleted["status"] == replay["status"] == "deleted"
        assert artifact.is_file()
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                "SELECT task_id, task_id_hash, goal_id, goal_id_hash, input_tokens "
                "FROM model_calls "
                "WHERE call_id = 'delete-usage'"
            ).fetchone()
            audit = connection.execute(
                "SELECT payload_json FROM audit_events "
                "WHERE event_type = 'aggregate.deleted'"
            ).fetchone()
            raw_lineage = connection.execute(
                "SELECT COUNT(*) FROM aggregate_transitions "
                "WHERE aggregate_type = 'task' AND aggregate_id = ?",
                (task.id,),
            ).fetchone()[0]
            anonymous_lineage = connection.execute(
                "SELECT COUNT(*) FROM aggregate_transitions "
                "WHERE aggregate_type = 'task' AND aggregate_id = ?",
                (hashlib.sha256(task.id.encode()).hexdigest(),),
            ).fetchone()[0]
        finally:
            connection.close()
        assert call["task_id"] is None and len(call["task_id_hash"]) == 64
        assert call["goal_id"] is None
        assert call["input_tokens"] == 7
        assert raw_lineage == 0 and anonymous_lineage > 0
        assert json.loads(audit["payload_json"])["artifact_files_deleted"] is False


def test_active_task_delete_stops_before_physical_delete() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project"
        path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(name="p", path=str(path))
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        task = _task(app, path, "active-delete", project_id=project["id"], role_id=role)
        running = app.state.task_repository.claim(
            task.id, expected_revision=0, idempotency_key="claim-active-delete"
        ).task
        result = app.state.deletions.task(
            task.id,
            expected_revision=running.revision,
            idempotency_key="delete-active-001",
            actor_type="owner",
            actor_id=None,
            reason="safe delete",
        )
        assert result["status"] == "stopping"
        assert app.state.task_repository.get(task.id).status.value == "stopping"


def test_goal_delete_cascades_control_rows_but_not_artifact_files() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project"
        path.mkdir()
        artifact = path / "goal-result.txt"
        artifact.write_text("kept", encoding="utf-8")
        app = _app(root)
        project = app.state.project_repository.create(name="p", path=str(path))
        goal_id = "goal-delete-001"
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO goals(id,title,objective,project_id,provider,status,"
                "plan_json,revision) VALUES (?,?,?,?,?,'cancelled','{}',0)",
                (goal_id, "goal", "delete", project["id"], "generic-command"),
            )
        task = app.state.task_repository.create(
            title="goal child",
            objective="child",
            project_path=str(path),
            command={"argv": ["true"]},
            verification=[{"kind": "file_exists", "path": "goal-result.txt"}],
            max_attempts=1,
            token_budget=0,
            idempotency_key="create-goal-child-delete",
        )
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET goal_id = ? WHERE id = ?", (goal_id, task.id)
            )
        task = app.state.task_repository.get(task.id)
        ModelCallLedger(app.state.database).record(
            task, {"input_tokens": 4, "output_tokens": 1},
            run_id="goal-delete-usage", provider="codex",
        )
        result = app.state.deletions.goal(
            goal_id,
            expected_revision=0,
            idempotency_key="delete-goal-001",
            actor_type="owner",
            actor_id=None,
            reason="remove goal",
        )
        assert result["status"] == "deleted"
        assert artifact.is_file()
        connection = app.state.database.connect()
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE goal_id = ?", (goal_id,)
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()[0] == 0
            child_deleted = connection.execute(
                """
                SELECT new_state_json FROM aggregate_transitions
                WHERE aggregate_type = 'task' AND aggregate_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (hashlib.sha256(task.id.encode()).hexdigest(),),
            ).fetchone()
            anonymous_usage = connection.execute(
                "SELECT goal_id, goal_id_hash FROM model_calls "
                "WHERE call_id = 'goal-delete-usage'"
            ).fetchone()
        finally:
            connection.close()
        assert json.loads(child_deleted["new_state_json"])["status"] == "deleted"
        assert anonymous_usage["goal_id"] is None
        assert anonymous_usage["goal_id_hash"] == hashlib.sha256(
            goal_id.encode()
        ).hexdigest()


def test_goal_delete_stops_active_and_cancels_undispatched_tasks_idempotently() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project"
        path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(name="mixed", path=str(path))
        role = app.state.project_repository.resolve_role(
            project["id"], "backend"
        )["role_id"]
        goal_id = "goal-delete-mixed"
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO goals(id,title,objective,project_id,provider,status,"
                "plan_json,revision) VALUES (?,?,?,?,?,'running','{}',0)",
                (goal_id, "mixed", "delete", project["id"], "generic-command"),
            )
        active = _task(
            app, path, "mixed-active", project_id=project["id"], role_id=role
        )
        ready = _task(app, path, "mixed-ready")
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET goal_id = ? WHERE id IN (?, ?)",
                (goal_id, active.id, ready.id),
            )
        running = app.state.task_repository.claim(
            active.id, expected_revision=0, idempotency_key="claim-mixed-delete"
        ).task
        first = app.state.deletions.goal(
            goal_id,
            expected_revision=0,
            idempotency_key="delete-goal-mixed",
            actor_type="owner",
            actor_id=None,
            reason="safe mixed delete",
        )
        replay = app.state.deletions.goal(
            goal_id,
            expected_revision=running.revision,
            idempotency_key="delete-goal-mixed-replay",
            actor_type="owner",
            actor_id=None,
            reason="safe mixed delete replay",
        )
        assert first["status"] == replay["status"] == "stopping"
        assert app.state.task_repository.get(active.id).status.value == "stopping"
        assert app.state.task_repository.get(ready.id).status.value == "cancelled"
        assert app.state.goal_repository.get(goal_id)["revision"] == 1


def test_goal_delete_anonymizes_goal_only_model_calls() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "project"
        path.mkdir()
        app = _app(root)
        project = app.state.project_repository.create(name="goal-call", path=str(path))
        goal_id = "goal-only-model-call"
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO goals(id,title,objective,project_id,provider,status,"
                "plan_json,revision) VALUES (?,?,?,?,?,'cancelled','{}',0)",
                (goal_id, "goal call", "delete", project["id"], "codex"),
            )
            connection.execute(
                "INSERT INTO model_calls(call_id,idempotency_key,project_id,goal_id,"
                "goal_id_hash,provider,model,status) VALUES (?,?,?,?,?,?,?,'completed')",
                (
                    "goal-only-call",
                    "goal-only-call-key",
                    project["id"],
                    goal_id,
                    hashlib.sha256(goal_id.encode()).hexdigest(),
                    "codex",
                    "codex",
                ),
            )
        result = app.state.deletions.goal(
            goal_id,
            expected_revision=0,
            idempotency_key="delete-goal-only-call",
            actor_type="owner",
            actor_id=None,
            reason="anonymize goal attribution",
        )
        assert result["status"] == "deleted"
        connection = app.state.database.connect()
        try:
            call = connection.execute(
                "SELECT project_id, goal_id, goal_id_hash FROM model_calls "
                "WHERE call_id = 'goal-only-call'"
            ).fetchone()
        finally:
            connection.close()
        assert call["project_id"] is None
        assert call["goal_id"] is None
        assert call["goal_id_hash"] == hashlib.sha256(goal_id.encode()).hexdigest()
