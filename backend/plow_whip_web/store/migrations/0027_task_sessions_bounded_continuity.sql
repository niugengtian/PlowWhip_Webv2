CREATE TABLE task_sessions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    role_id TEXT REFERENCES roles(id) ON DELETE SET NULL,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    session_generation INTEGER NOT NULL DEFAULT 1,
    external_session_id TEXT,
    replacement_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_session_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    role_id TEXT REFERENCES roles(id) ON DELETE SET NULL,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    external_session_id TEXT,
    session_generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    trigger_key TEXT UNIQUE,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_task_sessions_identity
ON task_sessions(project_id, role_id, task_id);

CREATE TABLE runtime_setting_overrides (
    scope TEXT NOT NULL CHECK (scope IN ('project', 'task_role')),
    scope_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    role_id TEXT REFERENCES roles(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    values_json TEXT NOT NULL CHECK (json_valid(values_json)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(scope, scope_id),
    CHECK (
        (scope = 'project' AND scope_id = project_id AND task_id IS NULL)
        OR
        (scope = 'task_role' AND scope_id = task_id
         AND task_id IS NOT NULL AND role_id IS NOT NULL)
    )
);

ALTER TABLE model_calls ADD COLUMN raw_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE model_calls ADD COLUMN raw_cached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE model_calls ADD COLUMN raw_output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE model_calls ADD COLUMN usage_semantics TEXT NOT NULL DEFAULT 'delta'
CHECK (usage_semantics IN ('delta', 'legacy_inferred_delta', 'unresolved_snapshot'));

UPDATE model_calls
SET raw_input_tokens = input_tokens,
    raw_cached_input_tokens = cached_input_tokens,
    raw_output_tokens = output_tokens,
    usage_semantics = 'legacy_inferred_delta';

WITH ordered AS (
    SELECT
        call_id,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        LAG(input_tokens) OVER identity_order AS previous_input,
        LAG(cached_input_tokens) OVER identity_order AS previous_cached,
        LAG(output_tokens) OVER identity_order AS previous_output
    FROM model_calls
    WHERE status IN ('completed', 'failed')
    WINDOW identity_order AS (
        PARTITION BY provider, call_kind,
            COALESCE(worker_id, task_id, project_id, call_id),
            COALESCE(session_generation, 1)
        ORDER BY created_at, rowid
    )
)
UPDATE model_calls
SET input_tokens = (
        SELECT CASE
            WHEN previous_input IS NULL OR input_tokens < previous_input
            THEN input_tokens ELSE input_tokens - previous_input END
        FROM ordered WHERE ordered.call_id = model_calls.call_id
    ),
    cached_input_tokens = (
        SELECT CASE
            WHEN previous_cached IS NULL OR cached_input_tokens < previous_cached
            THEN cached_input_tokens ELSE cached_input_tokens - previous_cached END
        FROM ordered WHERE ordered.call_id = model_calls.call_id
    ),
    output_tokens = (
        SELECT CASE
            WHEN previous_output IS NULL OR output_tokens < previous_output
            THEN output_tokens ELSE output_tokens - previous_output END
        FROM ordered WHERE ordered.call_id = model_calls.call_id
    )
WHERE call_id IN (SELECT call_id FROM ordered);

UPDATE model_calls
SET cached_input_tokens = MIN(input_tokens, cached_input_tokens),
    normalized_usage_json = json_set(
        normalized_usage_json,
        '$.source', 'legacy_inferred_delta',
        '$.raw_input_tokens', raw_input_tokens,
        '$.raw_cached_input_tokens', raw_cached_input_tokens,
        '$.raw_output_tokens', raw_output_tokens
    )
WHERE usage_semantics = 'legacy_inferred_delta';
