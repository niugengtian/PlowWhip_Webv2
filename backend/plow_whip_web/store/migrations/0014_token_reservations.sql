CREATE TABLE token_reservations (
    run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT,
    worker_id TEXT,
    provider TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens > 0),
    actual_tokens INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'settled', 'released')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TEXT
);

CREATE INDEX idx_token_reservations_active
ON token_reservations(status, created_at);
