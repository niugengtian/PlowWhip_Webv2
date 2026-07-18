CREATE TABLE execution_episodes (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    spec_revision INTEGER NOT NULL CHECK (spec_revision > 0),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    recovery_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
    recovery_stage TEXT NOT NULL CHECK (
        recovery_stage IN ('execute', 'resume', 'replan', 'replacement')
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'terminated', 'completed', 'circuit_open')
    ),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline_at TEXT NOT NULL,
    wall_deadline_at TEXT NOT NULL,
    max_host_processes INTEGER NOT NULL CHECK (max_host_processes > 0),
    host_process_count INTEGER NOT NULL DEFAULT 0 CHECK (host_process_count >= 0),
    last_fault_class TEXT,
    same_fault_count INTEGER NOT NULL DEFAULT 0 CHECK (same_fault_count >= 0),
    zero_progress_rounds INTEGER NOT NULL DEFAULT 0 CHECK (zero_progress_rounds >= 0),
    progress_bytes INTEGER NOT NULL DEFAULT 0 CHECK (progress_bytes >= 0),
    observed_tokens INTEGER NOT NULL DEFAULT 0 CHECK (observed_tokens >= 0),
    burn_rate_tokens_per_minute REAL NOT NULL DEFAULT 0,
    burn_rate_alert INTEGER NOT NULL DEFAULT 0 CHECK (burn_rate_alert IN (0, 1)),
    checkpoint_json TEXT CHECK (
        checkpoint_json IS NULL OR json_valid(checkpoint_json)
    ),
    end_reason TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, ordinal)
);

ALTER TABLE host_jobs
ADD COLUMN episode_id TEXT REFERENCES execution_episodes(id) ON DELETE CASCADE;

ALTER TABLE host_jobs
ADD COLUMN episode_process_number INTEGER CHECK (episode_process_number > 0);

INSERT INTO execution_episodes(
    id, task_id, spec_revision, ordinal, recovery_stage, status,
    started_at, deadline_at, wall_deadline_at, max_host_processes,
    host_process_count, ended_at
)
SELECT
    'legacy-' || h.job_id,
    h.task_id,
    h.spec_revision,
    (
        SELECT COUNT(*) FROM host_jobs older
        WHERE older.task_id = h.task_id
          AND (
              older.created_at < h.created_at
              OR (older.created_at = h.created_at AND older.job_id <= h.job_id)
          )
    ),
    'execute',
    CASE WHEN h.consumed_at IS NULL THEN 'active' ELSE 'completed' END,
    h.started_at,
    datetime(
        h.started_at,
        '+' || COALESCE(
            json_extract(s.spec_json, '$.deadline.hard_seconds'), 600
        ) || ' seconds'
    ),
    datetime(h.started_at, '+900 seconds'),
    2,
    1,
    CASE WHEN h.consumed_at IS NULL THEN NULL ELSE h.finished_at END
FROM host_jobs h
JOIN tasks t ON t.id = h.task_id
JOIN task_specs s ON s.task_id = t.id
    AND s.spec_revision = h.spec_revision;

UPDATE host_jobs
SET episode_id = 'legacy-' || job_id,
    episode_process_number = 1;

CREATE INDEX idx_execution_episodes_task
ON execution_episodes(task_id, ordinal DESC);

CREATE INDEX idx_execution_episodes_active
ON execution_episodes(status, deadline_at, wall_deadline_at);

CREATE INDEX idx_host_jobs_episode
ON host_jobs(episode_id, episode_process_number);
