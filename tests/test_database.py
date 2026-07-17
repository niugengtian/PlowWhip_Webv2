from __future__ import annotations

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
