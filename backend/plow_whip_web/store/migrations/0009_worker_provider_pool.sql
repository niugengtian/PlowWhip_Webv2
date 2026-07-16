ALTER TABLE projects ADD COLUMN host_path TEXT;

ALTER TABLE workers ADD COLUMN external_session_id TEXT;
ALTER TABLE workers ADD COLUMN last_seen_at TEXT;
ALTER TABLE workers ADD COLUMN last_error TEXT;

ALTER TABLE provider_configs ADD COLUMN display_name TEXT;
ALTER TABLE provider_configs ADD COLUMN adapter TEXT NOT NULL DEFAULT 'generic-command';
ALTER TABLE provider_configs ADD COLUMN transport TEXT NOT NULL DEFAULT 'container';
ALTER TABLE provider_configs ADD COLUMN executable TEXT;
ALTER TABLE provider_configs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_configs ADD COLUMN credential_env TEXT;
ALTER TABLE provider_configs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE provider_configs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_configs ADD COLUMN last_probed_at TEXT;

UPDATE provider_configs SET
    display_name = '容器命令', adapter = 'generic-command', transport = 'container',
    executable = NULL, enabled = 1, status = 'available', reason = NULL
WHERE name = 'generic-command';

UPDATE provider_configs SET
    display_name = 'Codex CLI', adapter = 'codex', transport = 'host-bridge',
    executable = '/Applications/ChatGPT.app/Contents/Resources/codex', enabled = 1,
    status = 'unknown', reason = '等待 Host Bridge 探测'
WHERE name = 'codex';

UPDATE provider_configs SET
    display_name = 'Cursor CLI', adapter = 'cursor', transport = 'host-bridge',
    executable = '/Applications/Cursor.app/Contents/Resources/app/bin/cursor', enabled = 1,
    status = 'unknown', reason = '等待 Host Bridge 探测'
WHERE name = 'cursor';

UPDATE provider_configs SET
    display_name = 'Claude CLI', adapter = 'json-worker', transport = 'host-bridge',
    executable = 'claude', enabled = 0, status = 'disabled', reason = '尚未启用'
WHERE name = 'claude';

INSERT INTO provider_configs(
    name, display_name, status, model_invoked, capabilities_json, reason,
    adapter, transport, executable, enabled, credential_env, config_json
) VALUES (
    'simple-worker', 'Simple Worker · DeepSeek', 'unknown', 1,
    '["new_session","resume_session","refine_convention"]', '等待 Host Bridge 探测',
    'json-worker', 'host-bridge', 'simple-worker', 1, NULL, '{}'
);

CREATE INDEX idx_provider_configs_enabled ON provider_configs(enabled, status);

CREATE TABLE convention_refinements (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_convention_refinements_scope
ON convention_refinements(scope, scope_id, created_at DESC);
