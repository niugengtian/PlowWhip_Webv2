from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.store import database as database_module
from plow_whip_web.store.database import Database
from plow_whip_web.store.task_repository import TaskRepository


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
        assert database.health() == {
            "status": "ok",
            "journal_mode": "wal",
            **database_module.migration_contract(),
        }
        connection = database.connect()
        try:
            recorded = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        assert [tuple(row) for row in recorded] == database_module.migration_manifest()


def test_migration_checksum_drift_is_rejected() -> None:
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "drift.sqlite3")
        database.migrate()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                ("0" * 64, _migration_names()[0]),
            )
        with pytest.raises(RuntimeError, match="migration checksum mismatch"):
            database.migrate()


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
        names = [item.name for item in migrations]
        start = names.index("0020_provider_context_pressure.sql")
        upgrade = migrations[start:]
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
        assert database.migrate() == [item.name for item in upgrade]
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


def test_0022_upgrades_0021_and_preserves_terminal_task() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade-0021.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        names = [item.name for item in migrations]
        start = names.index("0022_task_spec_continuity.sql")
        upgrade = migrations[start:]
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
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, command_json,
                    verification_json, max_attempts, token_budget, sizing_json
                ) VALUES (
                    'failed-task', 'failed', 'do not rerun', '/tmp',
                    'terminal_failed', '{"timeout_seconds":321}',
                    '[{"kind":"file_exists","path":"evidence.txt"}]',
                    1, 0, '{"status":"legacy_fallback"}'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        database = Database(db_path)
        assert database.migrate() == [item.name for item in upgrade]
        connection = database.connect()
        try:
            task = connection.execute(
                "SELECT status, current_spec_revision FROM tasks WHERE id = 'failed-task'"
            ).fetchone()
            spec = connection.execute(
                "SELECT spec_json, spec_hash FROM task_specs WHERE task_id = 'failed-task'"
            ).fetchone()
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE task_specs SET spec_json = '{}' WHERE task_id = 'failed-task'"
                )
        finally:
            connection.close()
        payload = json.loads(spec["spec_json"])
        assert tuple(task) == ("terminal_failed", 1)
        assert payload["objective"] == "do not rerun"
        assert payload["artifacts"] == ["evidence.txt"]
        assert payload["deadline"] == {"hard_seconds": 321}


def test_0023_retires_legacy_coordination_parent_without_failing_goal() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade-0022.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        names = [item.name for item in migrations]
        start = names.index("0023_butler_execution_policy.sql")
        upgrade = migrations[start:]
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
                "INSERT INTO projects(id, name, path) VALUES ('p', 'legacy', '/projects/p')"
            )
            connection.execute(
                "INSERT INTO roles(id, project_id, kind) VALUES ('r', 'p', 'coordination')"
            )
            connection.execute(
                "INSERT INTO roles(id, project_id, kind) VALUES ('rb', 'p', 'backend')"
            )
            connection.execute(
                "INSERT INTO roles(id, project_id, kind) VALUES ('rf', 'p', 'fullstack')"
            )
            connection.executemany(
                """
                INSERT INTO workers(
                    id, project_id, role_id, provider, session_id,
                    status, active_task_id
                ) VALUES (?, 'p', ?, 'codex', ?, ?, ?)
                """,
                [
                    ("wc", "r", "session-c", "idle", None),
                    ("wb", "rb", "session-b", "idle", None),
                    ("wf", "rf", "session-f", "busy", "child"),
                ],
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, title, objective, project_id, provider, status,
                    plan_json, parent_task_id
                ) VALUES ('g', 'same title', 'ship', 'p', 'codex', 'running', '{}', NULL)
                """
            )
            base = """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, revision,
                    command_json, verification_json, max_attempts, token_budget,
                    project_id, role_id, provider, quality_profile, goal_id,
                    parent_task_id, depends_on_json, work_item_kind, ordinal
                ) VALUES (?, 'same title', 'ship', '/projects/p', ?, 0,
                    '{}', '[]', 1, 0, 'p', 'r', 'codex', 'deterministic',
                    'g', ?, '[]', ?, ?)
            """
            connection.execute(base, ("parent", "paused", None, "coordination", 0))
            connection.execute(base, ("child", "paused", "parent", "implementation", 1))
            connection.execute(
                "UPDATE tasks SET role_id = 'rf', worker_id = 'wf' WHERE id = 'child'"
            )
            connection.execute(
                "UPDATE goals SET parent_task_id = 'parent' WHERE id = 'g'"
            )
            connection.commit()
        finally:
            connection.close()

        assert Database(db_path).migrate() == [item.name for item in upgrade]
        connection = Database(db_path).connect()
        try:
            parent = connection.execute(
                "SELECT status, blocked_reason FROM tasks WHERE id = 'parent'"
            ).fetchone()
            goal = connection.execute(
                "SELECT status, parent_task_id FROM goals WHERE id = 'g'"
            ).fetchone()
            child = connection.execute(
                "SELECT status, parent_task_id FROM tasks WHERE id = 'child'"
            ).fetchone()
            roles = connection.execute(
                "SELECT kind, status FROM roles ORDER BY kind"
            ).fetchall()
            workers = connection.execute(
                "SELECT id, status, active_task_id, released_at FROM workers ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        assert tuple(parent) == ("cancelled", "retired_to_goal_aggregate")
        assert tuple(goal) == ("running", None)
        assert tuple(child) == ("paused", None)
        assert [tuple(row) for row in roles] == [
            ("backend", "released"),
            ("butler", "available"),
            ("coordination", "released"),
            ("fullstack", "draining"),
        ]
        assert [tuple(row)[:3] for row in workers] == [
            ("wb", "released", None),
            ("wc", "released", None),
            ("wf", "busy", "child"),
        ]
        assert workers[0]["released_at"] and workers[1]["released_at"]
        assert workers[2]["released_at"] is None

        database = Database(db_path)
        with database.transaction(immediate=True) as connection:
            connection.execute("UPDATE tasks SET status = 'completed' WHERE id = 'child'")
            TaskRepository._release_worker_and_lock(
                connection, "child", "wf"
            )
        connection = database.connect()
        try:
            drained = connection.execute(
                """
                SELECT w.status, w.released_at, r.status role_status
                FROM workers w JOIN roles r ON r.id = w.role_id
                WHERE w.id = 'wf'
                """
            ).fetchone()
        finally:
            connection.close()
        assert tuple(drained)[0] == "released"
        assert drained["released_at"] is not None
        assert drained["role_status"] == "released"


def test_0024_upgrades_0023_and_backfills_immutable_goal_spec() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade-0023.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        names = [item.name for item in migrations]
        start = names.index("0024_goal_specs_evidence_manifests.sql")
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
                "INSERT INTO projects(id, name, path) VALUES ('p24', 'upgrade', '/projects/p24')"
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, title, objective, project_id, provider, status, plan_json
                ) VALUES (
                    'g24', 'upgrade goal', 'goal objective', 'p24',
                    'generic-command', 'running', '{}'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, command_json,
                    verification_json, max_attempts, token_budget, project_id,
                    provider, quality_profile, goal_id, work_item_kind, ordinal
                ) VALUES (
                    't24', 'child', 'child objective', '/projects/p24',
                    'terminal_failed', '{}', '[]', 1, 0, 'p24',
                    'generic-command', 'deterministic', 'g24', 'implementation', 1
                )
                """
            )
            spec = {
                "objective": "child objective",
                "scope": ["backend"],
                "acceptance": ["upgrade_preserves_contract"],
                "verification": [{"kind": "exit_code", "expected": 0}],
                "artifacts": ["release.json"],
                "constraints": ["no_rerun"],
                "deadline": {"hard_seconds": 321},
            }
            spec_json = json.dumps(
                spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                """
                INSERT INTO task_specs(
                    task_id, spec_revision, spec_json, spec_hash
                ) VALUES ('t24', 1, ?, sha256(?))
                """,
                (spec_json, spec_json),
            )
            connection.commit()
        finally:
            connection.close()

        assert Database(db_path).migrate() == names[start:]
        connection = Database(db_path).connect()
        try:
            goal = connection.execute(
                "SELECT current_spec_revision FROM goals WHERE id = 'g24'"
            ).fetchone()
            goal_spec = connection.execute(
                "SELECT spec_json, spec_hash FROM goal_specs WHERE goal_id = 'g24'"
            ).fetchone()
            task = connection.execute(
                "SELECT status FROM tasks WHERE id = 't24'"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'goal_specs', 'run_evidence_baselines', 'evidence_manifests'
                    )
                    """
                )
            }
        finally:
            connection.close()
        payload = json.loads(goal_spec["spec_json"])
        assert goal["current_spec_revision"] == 1
        assert task["status"] == "terminal_failed"
        assert payload == {**spec, "objective": "goal objective"}
        assert len(goal_spec["spec_hash"]) == 64
        assert tables == {
            "goal_specs",
            "run_evidence_baselines",
            "evidence_manifests",
        }


def test_0025_upgrades_0024_and_binds_existing_host_job_to_episode() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "upgrade-0024.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        names = [item.name for item in migrations]
        start = names.index("0025_execution_episodes.sql")
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
            connection.commit()
        finally:
            connection.close()

        task_id = "upgrade-episode-task"
        spec_json = json.dumps({
            "objective": "retain running Host work",
            "scope": [],
            "acceptance": [],
            "verification": [{"kind": "exit_code", "expected": 0}],
            "artifacts": [],
            "constraints": [],
            "deadline": {"hard_seconds": 321},
        }, sort_keys=True, separators=(",", ":"))
        connection = Database(db_path).connect()
        try:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, command_json,
                    verification_json, max_attempts, token_budget, provider,
                    quality_profile, sizing_json, current_spec_revision
                ) VALUES (
                    ?, 'upgrade episode', 'retain running Host work',
                    '/projects/upgrade-episode', 'running',
                    '{"argv":["true"],"timeout_seconds":321}',
                    '[{"expected":0,"kind":"exit_code"}]', 1, 0, 'codex',
                    'deterministic', '{"status":"legacy_fallback"}', 1
                )
                """,
                (task_id,),
            )
            connection.execute(
                """
                INSERT INTO task_specs(
                    task_id, spec_revision, spec_json, spec_hash
                ) VALUES (?, 1, ?, sha256(?))
                """,
                (task_id, spec_json, spec_json),
            )
            connection.execute(
                """
                INSERT INTO task_attempts(
                    id, task_id, attempt_number, status, spec_revision
                ) VALUES ('upgrade-attempt', ?, 1, 'running', 1)
                """,
                (task_id,),
            )
            connection.execute(
                """
                INSERT INTO task_runs(
                    id, task_id, attempt_id, run_type, provider, status,
                    spec_revision
                ) VALUES (
                    'upgrade-run', ?, 'upgrade-attempt', 'execute',
                    'codex', 'running', 1
                )
                """,
                (task_id,),
            )
            connection.execute(
                """
                INSERT INTO host_jobs(
                    job_id, task_id, attempt_id, run_id, provider, spec_revision
                ) VALUES (
                    'upgrade-host-job', ?, 'upgrade-attempt', 'upgrade-run',
                    'codex', 1
                )
                """,
                (task_id,),
            )
            connection.commit()
        finally:
            connection.close()

        assert Database(db_path).migrate() == [names[start]]
        connection = Database(db_path).connect()
        try:
            row = connection.execute(
                """
                SELECT h.episode_process_number, e.status, e.host_process_count,
                       CAST(
                           (julianday(e.deadline_at) - julianday(e.started_at))
                           * 86400 AS INTEGER
                       ) AS deadline_seconds
                FROM host_jobs h
                JOIN execution_episodes e ON e.id = h.episode_id
                WHERE h.job_id = 'upgrade-host-job'
                """
            ).fetchone()
        finally:
            connection.close()

        assert row["episode_process_number"] == 1
        assert row["status"] == "active"
        assert row["host_process_count"] == 1
        assert row["deadline_seconds"] in {320, 321}
