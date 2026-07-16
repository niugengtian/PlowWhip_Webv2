ALTER TABLE tasks ADD COLUMN network_requirement TEXT NOT NULL DEFAULT 'none';
ALTER TABLE tasks ADD COLUMN same_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN no_progress_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_failure_fingerprint TEXT;
ALTER TABLE tasks ADD COLUMN next_eligible_at TEXT;

CREATE TABLE task_controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE outbox_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivered_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE runtime_health (
    id TEXT PRIMARY KEY CHECK (id = 'global'),
    connectivity TEXT NOT NULL DEFAULT 'unknown',
    domestic_ok INTEGER,
    overseas_ok INTEGER,
    last_tick_at TEXT,
    last_resume_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO runtime_health(id) VALUES ('global');

CREATE INDEX idx_tasks_ready_eligible ON tasks(status, next_eligible_at);
CREATE INDEX idx_outbox_undelivered ON outbox_events(delivered_at, sequence);
