ALTER TABLE host_jobs ADD COLUMN dispatch_outcome TEXT NOT NULL DEFAULT 'unknown'
CHECK (dispatch_outcome IN ('accepted', 'rejected', 'unknown'));
ALTER TABLE host_jobs ADD COLUMN reconciliation_deadline_at TEXT;
ALTER TABLE host_jobs ADD COLUMN dispatch_decided_at TEXT;

UPDATE host_jobs
SET dispatch_outcome = CASE
        WHEN status = 'rejected' THEN 'rejected'
        WHEN status IN ('dispatching', 'recovery_hold') AND host_pid IS NULL THEN 'unknown'
        ELSE 'accepted'
    END,
    reconciliation_deadline_at = CASE
        WHEN status IN ('dispatching', 'recovery_hold') AND host_pid IS NULL
        THEN datetime(COALESCE(created_at, CURRENT_TIMESTAMP), '+120 seconds')
        ELSE NULL
    END,
    dispatch_decided_at = CASE
        WHEN status IN ('dispatching', 'recovery_hold') AND host_pid IS NULL THEN NULL
        ELSE COALESCE(updated_at, CURRENT_TIMESTAMP)
    END;

CREATE INDEX idx_host_jobs_dispatch_reconcile
ON host_jobs(dispatch_outcome, reconciliation_deadline_at, consumed_at);

CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    host_job_id TEXT REFERENCES host_jobs(job_id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'unknown',
    call_kind TEXT NOT NULL CHECK (
        call_kind IN (
            'executor', 'butler_planner', 'router', 'verifier',
            'convention_refinement'
        )
    ),
    session_id TEXT,
    session_generation INTEGER,
    status TEXT NOT NULL DEFAULT 'prepared' CHECK (
        status IN ('prepared', 'dispatched', 'completed', 'failed', 'unknown')
    ),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
        cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
    ),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    normalized_usage_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(normalized_usage_json)
    ),
    error_class TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dispatched_at TEXT,
    settled_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO model_calls(
    call_id, idempotency_key, project_id, task_id, worker_id, provider,
    model, call_kind, session_generation, status, input_tokens,
    cached_input_tokens, output_tokens, normalized_usage_json,
    created_at, dispatched_at, settled_at, updated_at
)
SELECT
    call_id, 'legacy-model-call:' || call_id, project_id, task_id, worker_id,
    provider, provider,
    CASE WHEN call_kind = 'convention_refinement'
         THEN 'convention_refinement' ELSE 'executor' END,
    session_generation, 'completed', input_tokens, cached_input_tokens,
    output_tokens,
    json_object(
        'input_tokens', input_tokens,
        'cached_input_tokens', cached_input_tokens,
        'output_tokens', output_tokens,
        'total_tokens', input_tokens + output_tokens,
        'source', 'legacy_token_usage'
    ),
    created_at, created_at, created_at, created_at
FROM token_usage;

CREATE INDEX idx_model_calls_project_time
ON model_calls(project_id, created_at DESC);
CREATE INDEX idx_model_calls_task_time
ON model_calls(task_id, created_at DESC);
CREATE INDEX idx_model_calls_dimensions
ON model_calls(provider, model, call_kind, status);

CREATE TRIGGER tasks_aggregate_updated
AFTER UPDATE ON tasks
BEGIN
    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
    VALUES (
        'aggregate', NEW.id, 'aggregate.updated',
        json_object('aggregate_type', 'task', 'state_revision', NEW.revision)
    );
END;

CREATE TRIGGER goals_aggregate_updated
AFTER UPDATE ON goals
BEGIN
    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
    VALUES (
        'aggregate', NEW.id, 'aggregate.updated',
        json_object(
            'aggregate_type', 'goal',
            'state_revision', NEW.current_spec_revision
        )
    );
END;

CREATE TRIGGER workers_aggregate_updated
AFTER UPDATE ON workers
BEGIN
    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
    VALUES (
        'aggregate', NEW.id, 'aggregate.updated',
        json_object(
            'aggregate_type', 'worker',
            'state_revision', NEW.session_generation
        )
    );
END;

CREATE TRIGGER host_jobs_aggregate_updated
AFTER UPDATE ON host_jobs
BEGIN
    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
    VALUES (
        'aggregate', NEW.job_id, 'aggregate.updated',
        json_object(
            'aggregate_type', 'host_job',
            'task_id', NEW.task_id,
            'state_revision', COALESCE(
                (SELECT revision FROM tasks WHERE id = NEW.task_id), 0
            )
        )
    );
END;

CREATE TRIGGER model_calls_aggregate_updated
AFTER UPDATE ON model_calls
BEGIN
    INSERT INTO outbox_events(topic, aggregate_id, event_type, payload_json)
    VALUES (
        'aggregate', NEW.call_id, 'aggregate.updated',
        json_object(
            'aggregate_type', 'model_call',
            'task_id', NEW.task_id,
            'state_revision', COALESCE(
                (SELECT revision FROM tasks WHERE id = NEW.task_id), 0
            )
        )
    );
END;
