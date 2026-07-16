ALTER TABLE scheduler_state ADD COLUMN runner_id TEXT;
ALTER TABLE scheduler_state ADD COLUMN runner_started_at TEXT;
ALTER TABLE scheduler_state ADD COLUMN runner_heartbeat_at TEXT;
ALTER TABLE scheduler_state ADD COLUMN runner_stopped_at TEXT;
ALTER TABLE scheduler_state ADD COLUMN runner_error TEXT;
ALTER TABLE scheduler_state ADD COLUMN last_cron_slot TEXT;
