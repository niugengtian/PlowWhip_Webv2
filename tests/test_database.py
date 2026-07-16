from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.store.database import Database


def test_migrations_are_idempotent() -> None:
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "test.sqlite3")
        assert database.migrate() == ["0001_initial.sql", "0002_tasks.sql"]
        assert database.migrate() == []
        assert database.health()["migration_count"] == 2
