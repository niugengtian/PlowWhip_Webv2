from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.store import database as database_module
from plow_whip_web.store.database import Database


def _migration_names() -> list[str]:
    migration_dir = Path(database_module.__file__).with_name("migrations")
    return [migration.name for migration in sorted(migration_dir.glob("*.sql"))]


def test_migrations_are_idempotent() -> None:
    migration_names = _migration_names()
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "test.sqlite3")
        applied_migrations = database.migrate()

        assert applied_migrations == migration_names
        assert len(applied_migrations) == len(set(applied_migrations))
        assert database.migrate() == []
        assert database.health()["migration_count"] == len(migration_names)


def test_upgrade_migration_scrubs_legacy_sqlite_bodies_once() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade.sqlite3"
        database = Database(db_path)
        database.migrate()
        legacy = json.dumps({
            "stdout": "legacy stdout",
            "stderr": "legacy stderr",
            "prompt": "secret prompt",
            "execution": {
                "stdout": "nested stdout",
                "stderr": "nested stderr",
                "output_ref": "job/output/",
            },
            "output_segments": [{
                "stream": "stdout",
                "index": 0,
                "ref": "job/stdout.000000.log",
                "bytes": 13,
                "sha256": "a" * 64,
                "offset": 0,
            }],
        })
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, command_json,
                    verification_json, max_attempts, token_budget,
                    sizing_json, execution_budget_json
                ) VALUES (
                    'legacy-task', 'legacy', 'upgrade', '/tmp', 'ready', '{}',
                    '[]', 1, 100, '{"status":"estimated"}',
                    '{"max_attempts":4,"total_token_hard_cap":100}'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt_id, run_type, provider, status, result_json
                ) VALUES ('legacy-run', 'legacy-task', 'legacy-attempt',
                          'execute', 'codex', 'failed', ?)
                """,
                (legacy,),
            )
            connection.execute(
                """
                INSERT INTO host_jobs(
                    job_id, task_id, attempt_id, run_id, provider, result_json
                ) VALUES ('legacy-job', 'legacy-task', 'legacy-attempt',
                          'legacy-run', 'codex', ?)
                """,
                (legacy,),
            )
            connection.execute(
                """
                DELETE FROM schema_migrations
                WHERE version IN (
                    '0018_p0_correction.sql',
                    '0019_backend_correction.sql'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        assert database.migrate() == [
            "0018_p0_correction.sql",
            "0019_backend_correction.sql",
        ]
        assert database.migrate() == []
        connection = database.connect()
        try:
            payloads = [
                json.loads(row[0])
                for row in connection.execute(
                    """
                    SELECT result_json FROM host_jobs WHERE job_id = 'legacy-job'
                    UNION ALL
                    SELECT result_json FROM task_runs WHERE id = 'legacy-run'
                    """
                )
            ]
            max_attempts = connection.execute(
                "SELECT max_attempts FROM tasks WHERE id = 'legacy-task'"
            ).fetchone()[0]
        finally:
            connection.close()
        for payload in payloads:
            assert {"stdout", "stderr", "prompt", "prompt_text"}.isdisjoint(payload)
            assert {"stdout", "stderr", "prompt", "prompt_text"}.isdisjoint(
                payload["execution"]
            )
            assert payload["execution"]["output_ref"] == "job/output/"
            assert payload["output_segments"][0]["offset"] == 0
        assert max_attempts == 4


def test_0021_upgrades_0019_database_and_removes_reservations() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade-0019.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        assert [item.name for item in migrations[-2:]] == [
            "0020_provider_context_pressure.sql",
            "0021_remove_token_budget.sql",
        ]
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for migration in migrations[:-2]:
                for statement in database_module._split_statements(
                    migration.read_text(encoding="utf-8")
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (migration.name,),
                )
            connection.execute(
                "INSERT INTO projects(id, name, path) VALUES ('p', 'old', '/tmp/old')"
            )
            connection.execute(
                "INSERT INTO roles(id, project_id, kind) VALUES ('r', 'p', 'backend')"
            )
            connection.execute(
                """
                INSERT INTO workers(id, project_id, role_id, provider, session_id)
                VALUES ('w', 'p', 'r', 'codex', 's')
                """
            )
            connection.execute(
                """
                INSERT INTO token_usage(
                    project_id, worker_id, input_tokens, output_tokens,
                    provider, call_id
                ) VALUES ('p', 'w', 7, 3, 'codex', 'legacy-call')
                """
            )
            connection.commit()
        finally:
            connection.close()

        database = Database(db_path)
        assert database.migrate() == [
            "0020_provider_context_pressure.sql",
            "0021_remove_token_budget.sql",
        ]
        assert database.migrate() == []
        connection = database.connect()
        try:
            usage = connection.execute(
                """
                SELECT cached_input_tokens, attribution_granularity,
                       value_classification, rotation_reason
                FROM token_usage WHERE call_id = 'legacy-call'
                """
            ).fetchone()
            worker = connection.execute(
                """
                SELECT last_input_tokens, last_cached_input_tokens,
                       last_output_tokens, last_context_pressure_tokens,
                       last_context_pressure_reason
                FROM workers WHERE id = 'w'
                """
            ).fetchone()
            versions = {
                row[0] for row in connection.execute(
                    """
                    SELECT version FROM schema_migrations
                    WHERE version IN (
                        '0017_goal_orchestration.sql',
                        '0018_p0_correction.sql',
                        '0019_backend_correction.sql'
                    )
                    """
                )
            }
            reservations = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'token_reservations'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert tuple(usage) == (0, "turn", "unknown", None)
        assert tuple(worker) == (0, 0, 0, 0, None)
        assert reservations == 0
        assert versions == {
            "0017_goal_orchestration.sql",
            "0018_p0_correction.sql",
            "0019_backend_correction.sql",
        }
