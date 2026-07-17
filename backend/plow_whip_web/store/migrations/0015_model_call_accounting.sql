DROP INDEX idx_token_usage_run;
DROP INDEX idx_token_usage_project_time;

ALTER TABLE token_usage RENAME TO token_usage_legacy;

CREATE TABLE token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT,
    worker_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL,
    run_id TEXT REFERENCES task_runs(id),
    call_id TEXT NOT NULL,
    call_kind TEXT NOT NULL DEFAULT 'task_execution'
        CHECK (call_kind IN ('task_execution', 'convention_refinement')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO token_usage(
    id, task_id, project_id, worker_id, input_tokens, output_tokens,
    provider, run_id, call_id, call_kind, created_at
)
SELECT id, task_id, project_id, worker_id, input_tokens, output_tokens,
       provider, run_id, COALESCE(run_id, 'legacy-usage-' || id),
       'task_execution', created_at
FROM token_usage_legacy;

DROP TABLE token_usage_legacy;

CREATE UNIQUE INDEX idx_token_usage_call ON token_usage(call_id);
CREATE INDEX idx_token_usage_project_time ON token_usage(project_id, created_at);

DROP INDEX idx_token_reservations_active;

ALTER TABLE token_reservations RENAME TO token_reservations_legacy;

CREATE TABLE token_reservations (
    call_id TEXT PRIMARY KEY,
    call_kind TEXT NOT NULL
        CHECK (call_kind IN ('task_execution', 'convention_refinement')),
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT UNIQUE REFERENCES task_runs(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
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

INSERT INTO token_reservations(
    call_id, call_kind, idempotency_key, run_id, task_id, project_id,
    worker_id, provider, reserved_tokens, actual_tokens, status,
    created_at, settled_at
)
SELECT run_id, 'task_execution', 'task-run:' || run_id, run_id, task_id,
       project_id, worker_id, provider, reserved_tokens, actual_tokens,
       status, created_at, settled_at
FROM token_reservations_legacy;

DROP TABLE token_reservations_legacy;

CREATE INDEX idx_token_reservations_active
ON token_reservations(status, created_at);

ALTER TABLE convention_refinements ADD COLUMN project_id TEXT;
ALTER TABLE convention_refinements ADD COLUMN call_id TEXT;
ALTER TABLE convention_refinements ADD COLUMN idempotency_key TEXT;
ALTER TABLE convention_refinements ADD COLUMN suggestion TEXT;
ALTER TABLE convention_refinements ADD COLUMN error TEXT;

CREATE UNIQUE INDEX idx_convention_refinements_idempotency
ON convention_refinements(idempotency_key) WHERE idempotency_key IS NOT NULL;
