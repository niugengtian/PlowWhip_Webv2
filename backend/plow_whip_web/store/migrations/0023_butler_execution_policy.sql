ALTER TABLE projects ADD COLUMN execution_policy_json TEXT NOT NULL
DEFAULT '{"max_milestones":6,"release_worker_on_terminal":true,"routing":{"L":"capability-milestones","M":"ephemeral-fullstack","S":"ephemeral-fullstack","XL":"capability-milestones","XS":"simple-worker"},"verification_gate_required":true,"version":"butler-v1"}'
CHECK (json_valid(execution_policy_json));

INSERT INTO roles(id, project_id, kind)
SELECT lower(hex(randomblob(16))), projects.id, 'butler'
FROM projects
WHERE NOT EXISTS (
    SELECT 1 FROM roles
    WHERE roles.project_id = projects.id AND roles.kind = 'butler'
);

INSERT INTO worker_session_archives(
    worker_id, project_id, role_id, session_id, session_generation, reason
)
SELECT w.id, w.project_id, w.role_id, w.session_id, w.session_generation,
       'legacy_project_policy_retired'
FROM workers w JOIN roles r ON r.id = w.role_id
WHERE r.kind != 'butler' AND w.released_at IS NULL
  AND w.active_task_id IS NULL;

UPDATE workers
SET status = 'released', released_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE released_at IS NULL AND active_task_id IS NULL
  AND role_id IN (SELECT id FROM roles WHERE kind != 'butler');

UPDATE roles
SET status = CASE
    WHEN EXISTS (
        SELECT 1 FROM workers w
        WHERE w.role_id = roles.id AND w.released_at IS NULL
          AND w.active_task_id IS NOT NULL
    ) THEN 'draining'
    ELSE 'released'
END
WHERE kind != 'butler';

UPDATE tasks
SET status = 'cancelled', blocked_reason = 'retired_to_goal_aggregate',
    last_error = 'legacy coordination parent retired',
    revision = revision + 1, updated_at = CURRENT_TIMESTAMP
WHERE work_item_kind = 'coordination'
  AND status NOT IN ('completed', 'terminal_failed', 'cancelled');

UPDATE goals SET parent_task_id = NULL, updated_at = CURRENT_TIMESTAMP
WHERE parent_task_id IS NOT NULL;

UPDATE tasks SET parent_task_id = NULL, updated_at = CURRENT_TIMESTAMP
WHERE parent_task_id IN (
    SELECT id FROM tasks WHERE work_item_kind = 'coordination'
);

CREATE TABLE task_deletion_permits (
    task_id TEXT PRIMARY KEY
);

CREATE TABLE task_deletion_tombstones (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    deleted_revision INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DROP TRIGGER task_specs_no_delete;
CREATE TRIGGER task_specs_no_delete
BEFORE DELETE ON task_specs
WHEN NOT EXISTS (
    SELECT 1 FROM task_deletion_permits
    WHERE task_deletion_permits.task_id = OLD.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'task_specs are immutable');
END;
