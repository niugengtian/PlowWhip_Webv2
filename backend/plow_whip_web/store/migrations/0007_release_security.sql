ALTER TABLE tasks ADD COLUMN provider TEXT NOT NULL DEFAULT 'generic-command';
ALTER TABLE tasks ADD COLUMN quality_profile TEXT NOT NULL DEFAULT 'balanced';

CREATE TABLE provider_configs (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    model_invoked INTEGER NOT NULL,
    capabilities_json TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO provider_configs(name, status, model_invoked, capabilities_json, reason)
VALUES
    ('generic-command', 'available', 0, '["execute","verify"]', NULL),
    ('codex', 'unavailable', 1, '["resume_session"]', 'adapter not configured'),
    ('cursor', 'unavailable', 1, '["resume_session"]', 'adapter not configured'),
    ('claude', 'unavailable', 1, '["resume_session"]', 'adapter not configured');

CREATE TABLE permission_grants (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    capability TEXT NOT NULL,
    resource TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
);

CREATE TABLE audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_created ON audit_log(sequence DESC);
CREATE INDEX idx_permissions_project ON permission_grants(project_id, capability, revoked_at);
