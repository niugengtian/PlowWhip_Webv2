CREATE TABLE system_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE scheduler_state (
    id TEXT PRIMARY KEY CHECK (id = 'global'),
    lease_owner TEXT,
    lease_token TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_tick_at TEXT,
    last_result_json TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO scheduler_state(id) VALUES ('global');
