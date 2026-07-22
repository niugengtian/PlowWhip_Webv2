from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    lease_token TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'butler')),
    content TEXT NOT NULL,
    action_json TEXT,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    processed_at REAL,
    UNIQUE (project_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id),
    objective TEXT NOT NULL,
    boundary_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    spec_revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    goal_id TEXT NOT NULL REFERENCES goals(id),
    spec_revision INTEGER NOT NULL DEFAULT 1,
    spec_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    public_status TEXT NOT NULL CHECK (
        public_status IN ('pending', 'in_progress', 'done', 'needs_decision')
    ),
    phase TEXT NOT NULL,
    wait_reason TEXT,
    fault_code TEXT CHECK (
        fault_code IS NULL OR fault_code IN (
            'transport', 'provider', 'process', 'verification',
            'credential', 'unsafe_unknown', 'scope'
        )
    ),
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    next_action_at REAL,
    outcome TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_per_project
ON tasks(project_id)
WHERE public_status IN ('pending', 'in_progress', 'needs_decision');

CREATE TABLE IF NOT EXISTS host_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_session_id TEXT,
    session_generation INTEGER,
    spec_revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('execute', 'check', 'repair', 'command')),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    returncode INTEGER NOT NULL,
    output_ref TEXT,
    failure_code TEXT,
    UNIQUE (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('output', 'evidence', 'log')),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    acceptance_id TEXT,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (task_id, kind, path, revision)
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path, data_root: str | Path):
        self.db_path = Path(db_path).resolve()
        self.data_root = Path(data_root).resolve()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def relative_data_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.data_root).as_posix()

    def resolve_data_path(self, relative: str) -> Path:
        path = (self.data_root / relative).resolve()
        path.relative_to(self.data_root)
        return path
