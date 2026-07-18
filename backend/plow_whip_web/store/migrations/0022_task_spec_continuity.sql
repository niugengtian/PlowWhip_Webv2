CREATE TABLE task_specs (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    spec_revision INTEGER NOT NULL CHECK (spec_revision > 0),
    spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
    spec_hash TEXT NOT NULL CHECK (length(spec_hash) = 64),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(task_id, spec_revision)
);

ALTER TABLE tasks ADD COLUMN current_spec_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE task_attempts ADD COLUMN spec_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE task_runs ADD COLUMN spec_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE host_jobs ADD COLUMN spec_revision INTEGER NOT NULL DEFAULT 1;

INSERT INTO task_specs(task_id, spec_revision, spec_json, spec_hash)
SELECT id, 1, spec_json, sha256(spec_json)
FROM (
    SELECT tasks.id AS id, json_object(
        'objective', tasks.objective,
        'scope', json_array(),
        'acceptance', json_array(),
        'verification', json(tasks.verification_json),
        'artifacts', json(COALESCE((
            SELECT json_group_array(json_extract(value, '$.path'))
            FROM json_each(tasks.verification_json)
            WHERE json_extract(value, '$.kind') IN ('file_exists', 'file_contains')
              AND json_extract(value, '$.path') IS NOT NULL
        ), '[]')),
        'constraints', json_array(),
        'deadline', json_object(
            'hard_seconds', COALESCE(
                json_extract(tasks.execution_budget_json, '$.hard_deadline_seconds'),
                json_extract(tasks.command_json, '$.timeout_seconds'),
                600
            )
        )
    ) AS spec_json
    FROM tasks
);

CREATE TRIGGER task_specs_no_update
BEFORE UPDATE ON task_specs
BEGIN
    SELECT RAISE(ABORT, 'task_specs are immutable');
END;

CREATE TRIGGER task_specs_no_delete
BEFORE DELETE ON task_specs
BEGIN
    SELECT RAISE(ABORT, 'task_specs are immutable');
END;
