ALTER TABLE token_usage ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE token_usage ADD COLUMN session_generation INTEGER;
ALTER TABLE token_usage ADD COLUMN attribution_granularity TEXT NOT NULL DEFAULT 'turn';
ALTER TABLE token_usage ADD COLUMN value_classification TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE token_usage ADD COLUMN rotation_reason TEXT;
ALTER TABLE task_runs ADD COLUMN cached_input_tokens INTEGER NOT NULL DEFAULT 0;

ALTER TABLE workers ADD COLUMN last_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_cached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_uncached_input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_context_pressure_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_context_pressure_reason TEXT;
ALTER TABLE workers ADD COLUMN last_context_session_generation INTEGER;
ALTER TABLE workers ADD COLUMN last_attribution_granularity TEXT NOT NULL DEFAULT 'turn';
ALTER TABLE workers ADD COLUMN last_value_classification TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE workers ADD COLUMN last_context_guard_decision TEXT;
ALTER TABLE workers ADD COLUMN last_context_guard_reason TEXT;
ALTER TABLE workers ADD COLUMN last_guard_estimated_new_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_guard_carry_in_cached_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_guard_hard_cap INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workers ADD COLUMN last_guard_relation TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE worker_session_archives ADD COLUMN trigger_key TEXT;
CREATE UNIQUE INDEX idx_worker_session_archives_trigger
ON worker_session_archives(trigger_key) WHERE trigger_key IS NOT NULL;
