-- One versioned mutation ledger, task-bound physical sessions, observe-only usage,
-- and durable deletion tombstones.

ALTER TABLE goals ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE goals ADD COLUMN last_evidence_hash TEXT;

CREATE TABLE aggregate_transitions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL CHECK (
        aggregate_type IN ('task', 'goal', 'provider_session')
    ),
    aggregate_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT NOT NULL,
    previous_state_json TEXT NOT NULL CHECK (json_valid(previous_state_json)),
    new_state_json TEXT NOT NULL CHECK (json_valid(new_state_json)),
    previous_evidence_hash TEXT,
    new_evidence_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(aggregate_type, aggregate_id, revision)
);

CREATE INDEX idx_aggregate_transitions_lineage
ON aggregate_transitions(aggregate_type, aggregate_id, revision);

CREATE TABLE provider_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    session_generation INTEGER NOT NULL DEFAULT 1 CHECK (session_generation > 0),
    external_session_id TEXT,
    state TEXT NOT NULL DEFAULT 'bound' CHECK (
        state IN ('bound', 'idle', 'terminating', 'archived')
    ),
    revision INTEGER NOT NULL DEFAULT 0,
    bound_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    unbound_at TEXT,
    archived_at TEXT,
    archive_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, role_id, task_id, session_generation)
);

INSERT INTO provider_sessions(
    id, project_id, role_id, task_id, worker_id, provider,
    session_generation, external_session_id, state
)
SELECT
    lower(hex(randomblob(16))), w.project_id, w.role_id, w.active_task_id,
    w.id, w.provider, w.session_generation, w.external_session_id, 'bound'
FROM workers w
WHERE w.active_task_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = w.active_task_id);

-- Physical Provider session ids have one source of truth after the backfill.
UPDATE workers SET external_session_id = NULL;

CREATE UNIQUE INDEX idx_provider_sessions_external_active
ON provider_sessions(external_session_id)
WHERE external_session_id IS NOT NULL AND state IN ('bound', 'idle');

CREATE UNIQUE INDEX idx_provider_sessions_task_current
ON provider_sessions(project_id, role_id, task_id)
WHERE state IN ('bound', 'idle', 'terminating');

CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    project_id TEXT,
    goal_id TEXT,
    goal_id_hash TEXT,
    role_id TEXT,
    task_id TEXT,
    task_id_hash TEXT,
    attempt_id TEXT,
    episode_id TEXT,
    worker_id TEXT,
    host_job_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'unknown',
    call_kind TEXT NOT NULL DEFAULT 'executor',
    physical_session_id TEXT,
    session_generation INTEGER,
    snapshot_kind TEXT NOT NULL DEFAULT 'per_call' CHECK (
        snapshot_kind IN ('per_call', 'cumulative')
    ),
    previous_call_id TEXT REFERENCES model_calls(call_id),
    raw_usage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(raw_usage_json)),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
    ),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    normalized_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        normalized_input_tokens >= 0
    ),
    normalized_cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        normalized_cached_input_tokens >= 0
        AND normalized_cached_input_tokens <= normalized_input_tokens
    ),
    normalized_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        normalized_output_tokens >= 0
    ),
    attribution_granularity TEXT NOT NULL DEFAULT 'turn',
    value_classification TEXT NOT NULL DEFAULT 'unknown',
    rotation_reason TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    error_class TEXT,
    settled_sequence INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TEXT
);

INSERT INTO model_calls(
    call_id, idempotency_key, project_id, goal_id, task_id, worker_id, provider, model,
    call_kind, session_generation, snapshot_kind, raw_usage_json,
    input_tokens, cached_input_tokens, output_tokens,
    normalized_input_tokens, normalized_cached_input_tokens,
    normalized_output_tokens, attribution_granularity, value_classification,
    rotation_reason, status, created_at, settled_at
)
SELECT
    tu.call_id, 'legacy-model-call:' || tu.call_id, tu.project_id,
    (SELECT t.goal_id FROM tasks t WHERE t.id = tu.task_id),
    tu.task_id, tu.worker_id,
    provider, provider,
    CASE WHEN call_kind = 'convention_refinement'
         THEN 'convention_refinement' ELSE 'executor' END,
    session_generation, 'per_call',
    json_object(
        'input_tokens', input_tokens,
        'cached_input_tokens', cached_input_tokens,
        'output_tokens', output_tokens,
        'source', 'legacy_token_usage'
    ),
    input_tokens, cached_input_tokens, output_tokens,
    input_tokens, cached_input_tokens, output_tokens,
    attribution_granularity, value_classification, rotation_reason,
    'completed', created_at, created_at
FROM token_usage tu;

UPDATE model_calls SET settled_sequence = rowid WHERE status = 'completed';

CREATE UNIQUE INDEX idx_model_calls_settled_sequence
ON model_calls(settled_sequence) WHERE settled_sequence IS NOT NULL;

DROP TABLE token_reservations;
DROP TABLE token_usage;

CREATE INDEX idx_model_calls_project_time
ON model_calls(project_id, created_at DESC);
CREATE INDEX idx_model_calls_goal_time
ON model_calls(goal_id, created_at DESC);
CREATE INDEX idx_model_calls_task_time
ON model_calls(task_id, created_at DESC);
CREATE INDEX idx_model_calls_session_time
ON model_calls(physical_session_id, session_generation, created_at);

-- Read-only compatibility projection. model_calls remains the only stored truth.
CREATE VIEW token_usage AS
SELECT
    rowid AS id,
    task_id,
    project_id,
    worker_id,
    normalized_input_tokens AS input_tokens,
    normalized_cached_input_tokens AS cached_input_tokens,
    normalized_output_tokens AS output_tokens,
    provider,
    call_id AS run_id,
    call_id,
    CASE WHEN call_kind = 'convention_refinement'
         THEN 'convention_refinement' ELSE 'task_execution' END AS call_kind,
    session_generation,
    attribution_granularity,
    value_classification,
    rotation_reason,
    created_at
FROM model_calls
WHERE status = 'completed';

CREATE TABLE deletion_tombstones (
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN ('task', 'goal')),
    aggregate_id TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    requested_revision INTEGER NOT NULL,
    final_revision INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('stopping', 'deleted')
    ),
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT NOT NULL,
    anonymous_usage_calls INTEGER NOT NULL DEFAULT 0,
    retained_artifacts_json TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(retained_artifacts_json)
    ),
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TEXT,
    PRIMARY KEY(aggregate_type, aggregate_id)
);
