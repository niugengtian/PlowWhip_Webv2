from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.store.database import Database


def test_migrations_are_idempotent() -> None:
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "test.sqlite3")
        assert database.migrate() == [
            "0001_initial.sql", "0002_tasks.sql", "0003_workforce.sql", "0004_scheduler.sql",
            "0005_context_usage.sql",
            "0006_resilience.sql",
            "0007_release_security.sql",
            "0008_embedded_cron.sql",
            "0009_worker_provider_pool.sql",
            "0010_cli_capabilities.sql",
            "0011_host_jobs.sql",
            "0012_token_usage_idempotency.sql",
            "0013_simple_worker.sql",
        ]
        assert database.migrate() == []
        assert database.health()["migration_count"] == 13
