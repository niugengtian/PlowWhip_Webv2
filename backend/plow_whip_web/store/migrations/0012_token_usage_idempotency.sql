ALTER TABLE token_usage ADD COLUMN run_id TEXT REFERENCES task_runs(id);

CREATE UNIQUE INDEX idx_token_usage_run
ON token_usage(run_id) WHERE run_id IS NOT NULL;
