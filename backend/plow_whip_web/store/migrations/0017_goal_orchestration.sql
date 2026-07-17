CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'running', 'completed', 'terminal_failed',
            'needs_human', 'cancelled'
        )
    ),
    plan_json TEXT NOT NULL,
    sizing_inputs_json TEXT,
    parent_task_id TEXT REFERENCES tasks(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_goals_project_status
    ON goals(project_id, status, created_at DESC);

ALTER TABLE tasks ADD COLUMN goal_id TEXT REFERENCES goals(id);
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(id);
ALTER TABLE tasks ADD COLUMN depends_on_json TEXT;
ALTER TABLE tasks ADD COLUMN work_item_kind TEXT
    CHECK (
        work_item_kind IS NULL
        OR work_item_kind IN ('coordination', 'implementation', 'verification')
    );
ALTER TABLE tasks ADD COLUMN ordinal INTEGER;
ALTER TABLE tasks ADD COLUMN blocked_reason TEXT;
ALTER TABLE tasks ADD COLUMN handoff_json TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_goal_ordinal
    ON tasks(goal_id, ordinal, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent
    ON tasks(parent_task_id);
