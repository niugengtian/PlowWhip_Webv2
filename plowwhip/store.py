from __future__ import annotations

import fcntl
import json
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote
from uuid import uuid4


HOST_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_session_id TEXT,
    session_generation INTEGER,
    spec_revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('execute', 'check', 'repair', 'command')),
    status TEXT NOT NULL CHECK (
        status IN (
            'dispatching', 'running', 'cancelling',
            'succeeded', 'failed', 'cancelled', 'interrupted'
        )
    ),
    started_at REAL NOT NULL,
    ended_at REAL,
    returncode INTEGER,
    output_ref TEXT,
    failure_code TEXT,
    dispatch_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (task_id, sequence)
);
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    host_path TEXT,
    lease_token TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    lease_until REAL,
    archived_at REAL,
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

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    revision INTEGER NOT NULL,
    selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
    summary_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (goal_id, revision)
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
    next_action_kind TEXT,
    deadline_at REAL,
    outcome TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

DROP INDEX IF EXISTS one_active_task_per_project;
CREATE UNIQUE INDEX one_active_task_per_project
ON tasks(project_id)
WHERE outcome IS NULL
  AND phase <> 'queued'
  AND public_status IN ('pending', 'in_progress', 'needs_decision');

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role_key TEXT NOT NULL,
    template_revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (project_id, role_key)
);

CREATE TABLE IF NOT EXISTS task_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    worker_id TEXT NOT NULL REFERENCES workers(id),
    role_key TEXT NOT NULL,
    role_snapshot_json TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (task_id, role_key)
);

CREATE TABLE IF NOT EXISTS session_generations (
    id TEXT PRIMARY KEY,
    task_session_id TEXT NOT NULL REFERENCES task_sessions(id),
    generation INTEGER NOT NULL,
    provider_key TEXT NOT NULL,
    external_session_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived', 'broken')),
    handoff_ref TEXT,
    created_at REAL NOT NULL,
    ended_at REAL,
    UNIQUE (task_session_id, generation)
);

{HOST_JOBS_SCHEMA}

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('output', 'evidence', 'handoff', 'log')),
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

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    task_session_id TEXT NOT NULL REFERENCES task_sessions(id),
    session_generation INTEGER NOT NULL,
    provider_key TEXT NOT NULL,
    model TEXT NOT NULL,
    usage_kind TEXT NOT NULL CHECK (usage_kind IN ('single', 'cumulative')),
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    normalized_total INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_items (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id TEXT REFERENCES projects(id),
    kind TEXT NOT NULL CHECK (kind IN ('role', 'rule', 'worker_template', 'script')),
    item_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (scope, project_id, kind, item_key, revision)
);

CREATE TABLE IF NOT EXISTS settings (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id TEXT REFERENCES projects(id),
    setting_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (scope, project_id, setting_key)
);
"""


DEFAULT_SETTINGS = {
    "provider_order": {
        "planner": ["codex_cli", "cursor_cli", "deepseek", "kimi"],
        "fullstack": ["cursor_cli", "codex_cli", "deepseek", "kimi"],
        "independent_checker": ["codex_cli", "cursor_cli", "deepseek", "kimi"],
        "simple": ["deepseek", "kimi", "codex_cli"],
        "provider_probe": ["codex_cli", "cursor_cli", "deepseek", "kimi"],
        "deterministic": ["local"],
        "deterministic_checker": ["local"],
    },
    "max_runtime_seconds": 600,
    "stop_grace_seconds": 10,
    "handoff_max_bytes": 8192,
    "checkpoint_max_bytes": 8192,
    "context_max_bytes": 16_384,
    "session_segment_max_bytes": 65_536,
    "native_compact_input_tokens": 120_000,
    "rotation_input_tokens": 180_000,
    "monitor_tail_lines": 20,
    "monitor_tail_bytes": 8192,
    "retry_count": 1,
    "retry_backoff_seconds": 0,
}


DEFAULT_LIBRARY = {
    ("role", "deterministic"): (
        "roles/deterministic.md",
        "# Deterministic executor\n\nPerform only the bounded action in TaskSpec and report files and hashes.\n",
    ),
    ("role", "deterministic_checker"): (
        "roles/deterministic-checker.md",
        "# Deterministic checker\n\nRead TaskSpec, artifacts and evidence independently; never trust executor claims.\n",
    ),
    ("role", "provider_probe"): (
        "roles/provider-probe.md",
        "# Provider probe\n\nRun only the declared bounded probe; never turn a zero Token probe into model execution.\n",
    ),
    ("role", "fullstack"): (
        "roles/fullstack.md",
        "# Fullstack Worker\n\nOwn one bounded code Task inside its registered workspace and leave verifiable changes.\n",
    ),
    ("role", "planner"): (
        "roles/planner.md",
        "# Planner\n\nRead the frozen Goal and produce the smallest bounded alternatives and Task DAG without modifying the workspace.\n",
    ),
    ("role", "independent_checker"): (
        "roles/independent-checker.md",
        "# Independent checker\n\nInspect the current workspace read-only and return an evidence-backed verdict.\n",
    ),
    ("rule", "v1_hard_boundaries"): (
        "rules/v1-hard-boundaries.md",
        "# V1 hard boundaries\n\nNo paid Provider, Docker, production, old-data migration, destructive action or out-of-scope write.\n",
    ),
    ("worker_template", "deterministic_write"): (
        "worker-templates/deterministic-write.md",
        "# Deterministic write\n\nWrite the declared relative artifact, then verify its SHA-256.\n",
    ),
    ("worker_template", "provider_probe"): (
        "worker-templates/provider-probe.md",
        "# Provider probe\n\nUse the Host Bridge probe contract and record bounded result, Token facts and evidence.\n",
    ),
    ("worker_template", "code_change"): (
        "worker-templates/code-change.md",
        "# Code change\n\nUse the registered Host Bridge workspace; do not escape scope, commit, deploy or create external effects.\n",
    ),
}


def write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


ENVIRONMENT_FIELDS = {
    "code_root",
    "data_root",
    "db_path",
    "compose_project",
    "port",
    "host_bridge_namespace",
    "cronner_enabled",
}


def _environment_manifest(
    source: dict[str, object], name: str, cronner_enabled: bool
) -> dict[str, object]:
    if set(source) != ENVIRONMENT_FIELDS:
        raise ValueError(f"{name} must declare the exact isolation fields")
    if source["cronner_enabled"] is not cronner_enabled:
        state = "enabled" if cronner_enabled else "disabled"
        raise ValueError(f"{name} Cronner must be {state}")
    normalized: dict[str, object] = {}
    for field in ("code_root", "data_root", "db_path"):
        value = source[field]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{name}.{field} must be an absolute path")
        normalized[field] = str(Path(value).resolve())
    for field in ("compose_project", "host_bridge_namespace"):
        value = source[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name}.{field} must be a non-empty string")
        normalized[field] = value.strip()
    port = source["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ValueError(f"{name}.port must be a valid TCP port")
    normalized["port"] = port
    normalized["cronner_enabled"] = cronner_enabled
    try:
        db_inside_data = os.path.commonpath(
            [str(normalized["data_root"]), str(normalized["db_path"])]
        ) == normalized["data_root"]
    except ValueError:
        db_inside_data = False
    if not db_inside_data:
        raise ValueError(f"{name}.db_path must be inside {name}.data_root")
    return normalized


def candidate_preflight(
    production: dict[str, object], candidate: dict[str, object]
) -> dict[str, object]:
    normalized = {
        "production": _environment_manifest(production, "production", True),
        "candidate": _environment_manifest(candidate, "candidate", False),
    }
    isolated_fields = (
        "code_root",
        "data_root",
        "db_path",
        "compose_project",
        "port",
        "host_bridge_namespace",
    )
    collisions = [
        field
        for field in isolated_fields
        if normalized["production"][field] == normalized["candidate"][field]
    ]
    if collisions:
        raise ValueError(
            "candidate isolation collision: " + ", ".join(collisions)
        )
    return {
        "isolated": True,
        "backup_required": "sqlite_backup_api",
        "candidate_cronner_enabled": False,
        "cutover_approved": False,
        "next_gate": "owner_explicit_cutover_approval",
        "production": normalized["production"],
        "candidate": normalized["candidate"],
    }


def rollback_preflight(candidate: dict[str, object]) -> dict[str, object]:
    normalized = _environment_manifest(candidate, "candidate", False)
    data_root = Path(str(normalized["data_root"]))
    db_path = Path(str(normalized["db_path"]))
    if not data_root.is_dir() or not db_path.is_file():
        raise ValueError("candidate data_root and db_path must exist")
    lock = (data_root / ".cronner.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("candidate scheduler lock is still owned") from None
        connection = Store(db_path, data_root).connect_readonly()
        try:
            quick_check = [
                row[0] for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            active_leases = connection.execute(
                """
                SELECT COUNT(*) FROM projects
                WHERE lease_token IS NOT NULL AND lease_until >= ?
                """,
                (time.time(),),
            ).fetchone()[0]
        finally:
            connection.close()
        if quick_check != ["ok"]:
            raise ValueError("candidate database quick_check failed")
        if active_leases:
            raise ValueError("candidate still has an active project lease")
        return {
            "rollback_ready": True,
            "candidate_cronner_enabled": False,
            "scheduler_lock_released": True,
            "active_leases": 0,
            "quick_check": quick_check,
            "candidate": normalized,
        }
    finally:
        lock.close()


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
            self._ensure_host_job_schema(connection)
            self._ensure_project_columns(connection)
            self._ensure_task_columns(connection)
            self._ensure_model_call_columns(connection)
            connection.execute(
                """
                UPDATE tasks SET outcome = NULL
                WHERE public_status = 'needs_decision' AND outcome = 'needs_decision'
                """
            )
            now = time.time()
            for key, value in DEFAULT_SETTINGS.items():
                value_json = json.dumps(value, sort_keys=True)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO settings(
                        id, scope, project_id, setting_key, value_json, source, updated_at
                    ) VALUES (?, 'global', NULL, ?, ?, 'v1_default', ?)
                    """,
                    (f"global:{key}", key, value_json, now),
                )
                connection.execute(
                    """
                    UPDATE settings SET value_json = ?, updated_at = ?
                    WHERE scope = 'global' AND project_id IS NULL
                      AND setting_key = ? AND source = 'v1_default'
                      AND value_json != ?
                    """,
                    (value_json, now, key, value_json),
                )
            self._sync_default_library(connection, now)
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
        finally:
            connection.close()

    def backup_to(self, destination: str | Path) -> dict[str, object]:
        target_path = Path(destination).resolve()
        if target_path == self.db_path:
            raise ValueError("backup destination must differ from the source database")
        if target_path.exists():
            raise ValueError("backup destination already exists")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect_readonly()
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            quick_check = [
                row[0] for row in target.execute("PRAGMA quick_check").fetchall()
            ]
            if quick_check != ["ok"]:
                raise RuntimeError("backup quick_check failed")
            target.commit()
        except Exception:
            target.close()
            target_path.unlink(missing_ok=True)
            raise
        else:
            target.close()
        finally:
            source.close()
        return {
            "method": "sqlite_backup_api",
            "source": str(self.db_path),
            "destination": str(target_path),
            "bytes": target_path.stat().st_size,
            "quick_check": quick_check,
        }

    @staticmethod
    def _ensure_host_job_schema(connection: sqlite3.Connection) -> None:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'host_jobs'"
        ).fetchone()["sql"]
        if "dispatching" in definition and "dispatch_json" in definition:
            return
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE host_jobs RENAME TO host_jobs_v3")
            connection.execute(HOST_JOBS_SCHEMA.strip().removesuffix(";"))
            connection.execute(
                """
                INSERT INTO host_jobs(
                    id, task_id, task_session_id, session_generation,
                    spec_revision, sequence, purpose, status,
                    started_at, ended_at, returncode, output_ref, failure_code
                )
                SELECT id, task_id, task_session_id, session_generation,
                       spec_revision, sequence, purpose, status,
                       started_at, ended_at, returncode, output_ref, failure_code
                FROM host_jobs_v3
                """
            )
            connection.execute("DROP TABLE host_jobs_v3")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _ensure_project_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(projects)")
        }
        if "archived_at" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN archived_at REAL")
        if "host_path" not in columns:
            connection.execute("ALTER TABLE projects ADD COLUMN host_path TEXT")

    @staticmethod
    def _ensure_task_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        additions = {
            "plan_id": "TEXT REFERENCES plans(id)",
            "sprint": "INTEGER",
            "role_key": "TEXT",
            "checker_role_key": "TEXT",
            "next_action_kind": "TEXT",
            "deadline_at": "REAL",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_model_call_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(model_calls)")
        }
        if "model" not in columns:
            connection.execute("ALTER TABLE model_calls ADD COLUMN model TEXT")
            connection.execute("UPDATE model_calls SET model = provider_key")

    def _sync_default_library(self, connection: sqlite3.Connection, now: float) -> None:
        for (kind, item_key), (relative, default_body) in DEFAULT_LIBRARY.items():
            path = self.data_root / "library" / relative
            path.resolve().relative_to(self.data_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(default_body)
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            latest = connection.execute(
                """
                SELECT revision, sha256 FROM library_items
                WHERE scope = 'global' AND project_id IS NULL
                  AND kind = ? AND item_key = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (kind, item_key),
            ).fetchone()
            if latest and latest["sha256"] == digest:
                continue
            revision = 1 if not latest else latest["revision"] + 1
            connection.execute(
                """
                INSERT INTO library_items(
                    id, scope, project_id, kind, item_key, revision,
                    path, sha256, created_at
                ) VALUES (?, 'global', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    kind,
                    item_key,
                    revision,
                    self.relative_data_path(path),
                    digest,
                    now,
                ),
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def connect_readonly(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{quote(str(self.db_path))}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
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
