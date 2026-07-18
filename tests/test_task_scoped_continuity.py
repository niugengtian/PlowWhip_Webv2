from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError
from plow_whip_web.store import database as database_module
from plow_whip_web.store.database import Database


def _task(app, project: dict[str, object], role_id: str, title: str):
    return app.state.task_repository.create(
        title=title,
        objective=f"complete {title}",
        project_path=str(project["path"]),
        project_id=str(project["id"]),
        role_id=role_id,
        provider="codex",
        command={"argv": ["true"]},
        verification=[{"kind": "exit_code", "expected": 0}],
        max_attempts=2,
        idempotency_key=f"task:{title}",
    )


def test_physical_session_is_reused_only_inside_same_project_role_task() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="session-scope", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            str(project["id"]), "fullstack"
        )["role_id"]
        first = _task(app, project, role_id, "first")
        first_claim = app.state.task_repository.claim(
            first.id, expected_revision=first.revision, idempotency_key="claim:first"
        )
        assert first_claim.task.worker_id
        first_context = app.state.task_repository.worker_execution_context(
            first_claim.task.worker_id, task_id=first.id
        )
        assert first_context["external_session_id"] is None
        app.state.task_repository.record_worker_result(
            first_claim.task.worker_id,
            task_id=first.id,
            external_session_id="codex-first",
            error=None,
        )
        resumed = app.state.task_repository.worker_execution_context(
            first_claim.task.worker_id, task_id=first.id
        )
        assert resumed["external_session_id"] == "codex-first"

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET status = 'completed', worker_id = NULL WHERE id = ?",
                (first.id,),
            )
            connection.execute(
                "DELETE FROM task_leases WHERE task_id = ?", (first.id,)
            )
            connection.execute(
                "DELETE FROM resource_locks WHERE task_id = ?", (first.id,)
            )
            connection.execute(
                """
                UPDATE workers SET status = 'idle', active_task_id = NULL
                WHERE id = ?
                """,
                (first_claim.task.worker_id,),
            )

        second = _task(app, project, role_id, "second")
        second_claim = app.state.task_repository.claim(
            second.id, expected_revision=second.revision, idempotency_key="claim:second"
        )
        assert second_claim.task.worker_id == first_claim.task.worker_id
        second_context = app.state.task_repository.worker_execution_context(
            second_claim.task.worker_id, task_id=second.id
        )
        assert second_context["external_session_id"] is None
        assert second_context["session_generation"] == 1


def test_model_call_ledger_deltas_provider_cumulative_snapshots() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        ledger = app.state.model_calls
        first = ledger.prepare(
            idempotency_key="call:first",
            call_kind="executor",
            provider="codex",
            project_id=None,
            session_generation=1,
        )
        ledger.settle(
            first["call_id"],
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
            },
            session_id="physical-session",
        )
        second = ledger.prepare(
            idempotency_key="call:second",
            call_kind="executor",
            provider="codex",
            project_id=None,
            session_id="physical-session",
            session_generation=1,
        )
        settled = ledger.settle(
            second["call_id"],
            {
                "input_tokens": 160,
                "cached_input_tokens": 140,
                "output_tokens": 15,
            },
            session_id="physical-session",
        )
        assert (
            settled["input_tokens"],
            settled["cached_input_tokens"],
            settled["output_tokens"],
        ) == (60, 60, 5)
        summary = ledger.summary()
        assert summary["input_tokens"] == 160
        assert summary["cached_input_tokens"] == 140
        assert summary["output_tokens"] == 15
        assert summary["raw_snapshot_totals"]["total_tokens"] == 285


def test_continuity_settings_precedence_and_conflict_validation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="settings", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            str(project["id"]), "fullstack"
        )["role_id"]
        task = _task(app, project, role_id, "settings-task")
        settings = app.state.runtime_settings
        settings.update_override(
            scope="project",
            scope_id=str(project["id"]),
            values={"checkpoint_max_bytes": 3072},
            expected_revision=0,
        )
        settings.update_override(
            scope="task_role",
            scope_id=task.id,
            values={"checkpoint_max_bytes": 3584, "handoff_max_bytes": 1024},
            expected_revision=0,
        )
        effective = settings.effective(task_id=task.id)
        assert effective["values"]["checkpoint_max_bytes"] == 3584
        assert effective["sources"]["checkpoint_max_bytes"] == "task_role"
        assert effective["sources"]["context_max_bytes"] == "global_default"

        with pytest.raises(DomainError, match="exceeds context_max_bytes"):
            settings.update_override(
                scope="task_role",
                scope_id=task.id,
                values={
                    "checkpoint_max_bytes": 30_000,
                    "handoff_max_bytes": 4_000,
                },
                expected_revision=1,
            )


def test_0027_normalizes_all_preexisting_cumulative_snapshots() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        names = [item.name for item in migrations]
        start = names.index("0027_task_sessions_bounded_continuity.sql")
        connection = Database(db_path).connect()
        try:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for migration in migrations[:start]:
                for statement in database_module._split_statements(
                    migration.read_text(encoding="utf-8")
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (migration.name,),
                )
            connection.execute(
                "INSERT INTO projects(id, name, path) VALUES ('p', 'p', '/p')"
            )
            connection.execute(
                "INSERT INTO roles(id, project_id, kind) VALUES ('r', 'p', 'fullstack')"
            )
            connection.execute(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id
                ) VALUES ('w', 'p', 'r', 'codex', 'logical')
                """
            )
            for call_id, input_tokens, cached_tokens, output_tokens in (
                ("old-1", 100, 80, 10),
                ("old-2", 160, 140, 15),
            ):
                connection.execute(
                    """
                    INSERT INTO model_calls(
                        call_id, idempotency_key, project_id, worker_id,
                        provider, model, call_kind, session_generation, status,
                        input_tokens, cached_input_tokens, output_tokens,
                        normalized_usage_json, settled_at
                    ) VALUES (?, ?, 'p', 'w', 'codex', 'provider-managed',
                              'executor', 1, 'completed', ?, ?, ?, ?,
                              CURRENT_TIMESTAMP)
                    """,
                    (
                        call_id,
                        f"key:{call_id}",
                        input_tokens,
                        cached_tokens,
                        output_tokens,
                        '{"source":"provider_normalized"}',
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        assert Database(db_path).migrate() == names[start:]
        connection = Database(db_path).connect()
        try:
            rows = connection.execute(
                """
                SELECT input_tokens, cached_input_tokens, output_tokens,
                       raw_input_tokens, usage_semantics
                FROM model_calls ORDER BY call_id
                """
            ).fetchall()
        finally:
            connection.close()
        assert [tuple(row) for row in rows] == [
            (100, 80, 10, 100, "legacy_inferred_delta"),
            (60, 60, 5, 160, "legacy_inferred_delta"),
        ]
