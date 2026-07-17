CREATE TABLE host_jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    fencing_token INTEGER,
    session_generation INTEGER,
    host_pid INTEGER,
    status TEXT NOT NULL DEFAULT 'dispatching',
    external_session_id TEXT,
    heartbeat_at TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    result_json TEXT,
    last_error TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, attempt_id)
);

CREATE INDEX idx_host_jobs_reconcile ON host_jobs(consumed_at, status, updated_at);
CREATE INDEX idx_host_jobs_task ON host_jobs(task_id, created_at DESC);
