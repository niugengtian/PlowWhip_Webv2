ALTER TABLE goals ADD COLUMN current_spec_revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE goal_specs (
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE RESTRICT,
    spec_revision INTEGER NOT NULL CHECK (spec_revision > 0),
    spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
    spec_hash TEXT NOT NULL CHECK (length(spec_hash) = 64),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(goal_id, spec_revision)
);

INSERT INTO goal_specs(goal_id, spec_revision, spec_json, spec_hash)
SELECT id, 1, spec_json, sha256(spec_json)
FROM (
    SELECT goals.id AS id, COALESCE((
            SELECT json_set(s.spec_json, '$.objective', goals.objective)
            FROM tasks t
            JOIN task_specs s ON s.task_id = t.id
                AND s.spec_revision = t.current_spec_revision
            WHERE t.goal_id = goals.id
            ORDER BY t.ordinal, t.created_at
            LIMIT 1
        ), json_object(
            'objective', goals.objective,
            'scope', json_array(),
            'acceptance', json_array(),
            'verification', json_array(),
            'artifacts', json_array(),
            'constraints', json_array(),
            'deadline', json_object('hard_seconds', 600)
        )) AS spec_json
    FROM goals
);

CREATE TRIGGER goal_specs_no_update
BEFORE UPDATE ON goal_specs
BEGIN
    SELECT RAISE(ABORT, 'goal_specs are immutable');
END;

CREATE TRIGGER goal_specs_no_delete
BEFORE DELETE ON goal_specs
BEGIN
    SELECT RAISE(ABORT, 'goal_specs are immutable');
END;

CREATE TABLE run_evidence_baselines (
    run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
    spec_revision INTEGER NOT NULL CHECK (spec_revision > 0),
    baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
    baseline_hash TEXT NOT NULL CHECK (length(baseline_hash) = 64),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE evidence_manifests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES task_runs(id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL CHECK (spec_revision > 0),
    task_revision INTEGER NOT NULL CHECK (task_revision >= 0),
    environment_hash TEXT NOT NULL CHECK (length(environment_hash) = 64),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    manifest_hash TEXT NOT NULL UNIQUE CHECK (length(manifest_hash) = 64),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_evidence_manifests_task
ON evidence_manifests(task_id, created_at DESC);

CREATE TRIGGER run_evidence_baselines_no_update
BEFORE UPDATE ON run_evidence_baselines
BEGIN
    SELECT RAISE(ABORT, 'run evidence baselines are immutable');
END;

CREATE TRIGGER evidence_manifests_no_update
BEFORE UPDATE ON evidence_manifests
BEGIN
    SELECT RAISE(ABORT, 'evidence manifests are immutable');
END;

CREATE TRIGGER evidence_manifests_no_delete
BEFORE DELETE ON evidence_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM task_deletion_permits
    WHERE task_deletion_permits.task_id = OLD.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'evidence manifests are immutable');
END;

CREATE TRIGGER run_evidence_baselines_no_delete
BEFORE DELETE ON run_evidence_baselines
WHEN NOT EXISTS (
    SELECT 1 FROM task_deletion_permits
    WHERE task_deletion_permits.task_id = OLD.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'run evidence baselines are immutable');
END;
