from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "sha256",
            1,
            lambda value: hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
            deterministic=True,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> list[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migration_dir = Path(__file__).with_name("migrations")
        manifest = migration_manifest(migration_dir)
        applied_now: list[str] = []
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(schema_migrations)")
            }
            if "checksum" not in columns:
                connection.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
            applied = {
                row["version"]: row["checksum"]
                for row in connection.execute(
                    "SELECT version, checksum FROM schema_migrations"
                )
            }
            expected = dict(manifest)
            unknown = sorted(set(applied) - set(expected))
            if unknown:
                raise RuntimeError(f"database has unknown migrations: {unknown}")
            for migration in sorted(migration_dir.glob("*.sql")):
                checksum = expected[migration.name]
                if migration.name in applied:
                    recorded = applied[migration.name]
                    if recorded and recorded != checksum:
                        raise RuntimeError(
                            f"migration checksum mismatch: {migration.name}"
                        )
                    if not recorded:
                        connection.execute(
                            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                            (checksum, migration.name),
                        )
                    continue
                for statement in _split_statements(migration.read_text(encoding="utf-8")):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)",
                    (migration.name, checksum),
                )
                applied_now.append(migration.name)
        return applied_now

    def health(self) -> dict[str, object]:
        connection = self.connect()
        try:
            connection.execute("SELECT 1").fetchone()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            migration_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            contract = migration_contract()
        finally:
            connection.close()
        return {
            "status": "ok",
            "journal_mode": journal_mode,
            "migration_count": migration_count,
            "schema_head": contract["schema_head"],
            "schema_checksum": contract["schema_checksum"],
        }


def migration_manifest(migration_dir: Path | None = None) -> list[tuple[str, str]]:
    directory = migration_dir or Path(__file__).with_name("migrations")
    return [
        (migration.name, hashlib.sha256(migration.read_bytes()).hexdigest())
        for migration in sorted(directory.glob("*.sql"))
    ]


def manifest_checksum(manifest: list[tuple[str, str]]) -> str:
    payload = "\n".join(f"{name}:{checksum}" for name, checksum in manifest)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def migration_contract(migration_dir: Path | None = None) -> dict[str, object]:
    manifest = migration_manifest(migration_dir)
    return {
        "migration_count": len(manifest),
        "schema_head": manifest[-1][0] if manifest else None,
        "schema_checksum": manifest_checksum(manifest),
    }


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer = f"{buffer}\n{line}".strip()
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer:
        raise ValueError("incomplete SQL migration")
    return statements
