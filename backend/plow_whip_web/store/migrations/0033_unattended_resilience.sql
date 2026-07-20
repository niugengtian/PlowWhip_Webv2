-- One canonical resilience model for network zones, Provider circuits,
-- Task suspension/continuation, bounded execution progress, and alert
-- convergence. Historical rows remain append-only.

ALTER TABLE provider_configs ADD COLUMN network_zone TEXT NOT NULL DEFAULT 'overseas'
    CHECK (network_zone IN ('local', 'domestic', 'overseas'));
ALTER TABLE provider_configs ADD COLUMN priority INTEGER NOT NULL DEFAULT 100;
ALTER TABLE provider_configs ADD COLUMN circuit_state TEXT NOT NULL DEFAULT 'closed'
    CHECK (circuit_state IN ('closed', 'open', 'half_open'));
ALTER TABLE provider_configs ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_configs ADD COLUMN consecutive_successes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_configs ADD COLUMN circuit_opened_at TEXT;
ALTER TABLE provider_configs ADD COLUMN next_probe_at TEXT;
ALTER TABLE provider_configs ADD COLUMN last_failure_class TEXT;

UPDATE provider_configs SET network_zone = 'local', priority = 0
WHERE name = 'generic-command';
UPDATE provider_configs SET network_zone = 'overseas', priority = 10
WHERE name = 'codex';
UPDATE provider_configs SET network_zone = 'overseas', priority = 20
WHERE name = 'cursor';
UPDATE provider_configs SET network_zone = 'domestic', priority = 30
WHERE name = 'simple-worker';

UPDATE projects
SET execution_policy_json = json_set(
    execution_policy_json,
    '$.version',
    'butler-v2',
    '$.routing.XS',
    'ephemeral-fullstack'
)
WHERE json_extract(execution_policy_json, '$.version') = 'butler-v1'
   OR json_extract(execution_policy_json, '$.routing.XS') = 'simple-worker';

INSERT OR IGNORE INTO provider_configs(
    name, display_name, status, model_invoked, capabilities_json, reason,
    adapter, transport, executable, enabled, credential_env, config_json,
    revision, network_zone, priority
) SELECT
    'deepseek', 'DeepSeek', status, 1, capabilities_json,
    '由 legacy simple-worker 迁移；等待 Host Bridge 探测',
    'json-worker', transport, executable, enabled, credential_env,
    json_set(config_json, '$.legacy_alias', 'simple-worker'),
    1, 'domestic', 30
FROM provider_configs WHERE name = 'simple-worker';

UPDATE provider_configs
SET display_name = 'Simple Worker（兼容别名）', enabled = 0,
    status = 'disabled', reason = '兼容旧记录；新任务使用 deepseek',
    revision = revision + 1
WHERE name = 'simple-worker';

INSERT OR IGNORE INTO provider_configs(
    name, display_name, status, model_invoked, capabilities_json, reason,
    adapter, transport, executable, enabled, credential_env, config_json,
    revision, network_zone, priority
) VALUES (
    'kimi', 'Kimi（待接入）', 'disabled', 1,
    '["new_session","resume_session","refine_convention"]',
    '仅注册扩展能力，未配置真实适配器或凭据',
    'json-worker', 'host-bridge', 'kimi', 0, NULL,
    '{"schema_only":true}', 1, 'domestic', 40
);

ALTER TABLE tasks ADD COLUMN provider_policy TEXT NOT NULL DEFAULT 'auto'
    CHECK (provider_policy IN ('auto', 'preferred', 'pinned'));
ALTER TABLE tasks ADD COLUMN fallback_enabled INTEGER NOT NULL DEFAULT 1
    CHECK (fallback_enabled IN (0, 1));
ALTER TABLE tasks ADD COLUMN provider_order_json TEXT NOT NULL
    DEFAULT '["codex","cursor","deepseek","kimi"]'
    CHECK (json_valid(provider_order_json));
ALTER TABLE tasks ADD COLUMN suspended_from_status TEXT;
ALTER TABLE tasks ADD COLUMN suspension_reason TEXT;
ALTER TABLE tasks ADD COLUMN suspension_incident_id TEXT;

CREATE TABLE task_provider_policies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    spec_revision INTEGER NOT NULL,
    provider_policy TEXT NOT NULL CHECK (
        provider_policy IN ('auto', 'preferred', 'pinned')
    ),
    fallback_enabled INTEGER NOT NULL CHECK (fallback_enabled IN (0, 1)),
    provider_order_json TEXT NOT NULL CHECK (json_valid(provider_order_json)),
    initial_provider TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(task_id, spec_revision),
    FOREIGN KEY(task_id, spec_revision)
        REFERENCES task_specs(task_id, spec_revision) ON DELETE CASCADE
);
INSERT INTO task_provider_policies(
    task_id, spec_revision, provider_policy, fallback_enabled,
    provider_order_json, initial_provider
)
SELECT s.task_id, s.spec_revision, t.provider_policy, t.fallback_enabled,
       t.provider_order_json, t.provider
FROM task_specs s
JOIN tasks t ON t.id = s.task_id;
CREATE TRIGGER task_provider_policies_no_update
BEFORE UPDATE ON task_provider_policies
BEGIN
    SELECT RAISE(ABORT, 'task provider policy snapshots are immutable');
END;
CREATE TRIGGER task_provider_policies_no_delete
BEFORE DELETE ON task_provider_policies
WHEN NOT EXISTS (
    SELECT 1 FROM task_deletion_permits p
    WHERE p.task_id = OLD.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'task provider policy snapshots are immutable');
END;

ALTER TABLE butler_conversations ADD COLUMN provider TEXT NOT NULL DEFAULT 'codex';
ALTER TABLE butler_conversations ADD COLUMN external_session_id TEXT;
ALTER TABLE butler_conversations ADD COLUMN session_generation INTEGER NOT NULL DEFAULT 1;
ALTER TABLE butler_conversations ADD COLUMN archived_at TEXT;

CREATE TABLE network_zone_health (
    zone TEXT PRIMARY KEY CHECK (zone IN ('domestic', 'overseas')),
    state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (state IN ('unknown', 'available', 'unavailable')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    last_checked_at TEXT,
    changed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO network_zone_health(zone) VALUES ('domestic');
INSERT INTO network_zone_health(zone) VALUES ('overseas');

CREATE TABLE task_recovery_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
    session_generation INTEGER NOT NULL,
    spec_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'consumed', 'invalidated')),
    checkpoint_json TEXT NOT NULL CHECK (json_valid(checkpoint_json)),
    checkpoint_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    consumed_at TEXT
);
CREATE UNIQUE INDEX idx_task_recovery_one_pending
ON task_recovery_checkpoints(task_id)
WHERE status = 'pending';

ALTER TABLE execution_episodes ADD COLUMN effective_limits_json TEXT
    NOT NULL DEFAULT '{}' CHECK (json_valid(effective_limits_json));
ALTER TABLE execution_episodes ADD COLUMN progress_evidence_json TEXT
    NOT NULL DEFAULT '{}' CHECK (json_valid(progress_evidence_json));
ALTER TABLE execution_episodes ADD COLUMN last_progress_at TEXT;
ALTER TABLE execution_episodes ADD COLUMN extension_seconds INTEGER NOT NULL DEFAULT 0;

-- Databases upgraded through the legacy migration may carry the old universal
-- 900-second wall deadline. Active Episodes converge to their Task hard
-- deadline; subsequent starts resolve inherited effective limits in runtime.
UPDATE execution_episodes
SET wall_deadline_at = deadline_at
WHERE status = 'active' AND id LIKE 'legacy-%';

-- The legacy host_jobs table permits only one row per Task attempt. Keep that
-- hot table as the current physical process and move completed Episode rows to
-- an append-only archive before the same attempt resumes with a new run/job.
CREATE TABLE host_job_archives (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    episode_id TEXT REFERENCES execution_episodes(id) ON DELETE SET NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_host_job_archives_task_attempt
ON host_job_archives(task_id, attempt_id, archived_at DESC);

CREATE TABLE operator_continuation_grants (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (
        action IN ('continue_once', 'switch_provider', 'replace_session', 'cancel')
    ),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    budget_delta_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(budget_delta_json)),
    target_provider TEXT,
    expires_at TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    root_kind TEXT NOT NULL CHECK (
        root_kind IN (
            'global_network', 'network_zone', 'provider', 'host_bridge',
            'task', 'watchdog', 'security'
        )
    ),
    scope_key TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (
        severity IN ('info', 'warning', 'error', 'critical')
    ),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'recovering', 'resolved')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);
CREATE UNIQUE INDEX idx_incidents_one_open_fingerprint
ON incidents(fingerprint)
WHERE status != 'resolved';

CREATE TABLE incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('opened', 'observed', 'suppressed', 'recovering', 'resolved')
    ),
    source_kind TEXT NOT NULL,
    source_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_incident_events_incident
ON incident_events(incident_id, id DESC);
