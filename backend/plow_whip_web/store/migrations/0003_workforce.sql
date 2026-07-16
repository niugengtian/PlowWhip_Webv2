CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    next_fencing_token INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE roles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, kind)
);

CREATE TABLE workers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'idle',
    active_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at TEXT,
    UNIQUE(project_id, role_id)
);

CREATE TABLE worker_session_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_leases (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    lease_token TEXT NOT NULL UNIQUE,
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE resource_locks (
    resource_key TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT,
    lease_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE tasks ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE tasks ADD COLUMN role_id TEXT REFERENCES roles(id);
ALTER TABLE tasks ADD COLUMN worker_id TEXT REFERENCES workers(id);
ALTER TABLE tasks ADD COLUMN resource_key TEXT;

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX idx_tasks_role_status ON tasks(role_id, status);
CREATE INDEX idx_workers_project_status ON workers(project_id, status);
