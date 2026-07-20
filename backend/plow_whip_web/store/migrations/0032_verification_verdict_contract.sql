ALTER TABLE tasks ADD COLUMN completed_at TEXT;
ALTER TABLE goals ADD COLUMN completed_at TEXT;
ALTER TABLE workers ADD COLUMN active_fencing_token INTEGER;

ALTER TABLE evidence_manifests ADD COLUMN verdict TEXT
    CHECK (verdict IS NULL OR verdict IN ('PASS', 'CHANGES_REQUIRED'));
ALTER TABLE evidence_manifests ADD COLUMN reason_codes_json TEXT
    CHECK (reason_codes_json IS NULL OR json_valid(reason_codes_json));
ALTER TABLE evidence_manifests ADD COLUMN failed_acceptance_ids_json TEXT
    CHECK (
        failed_acceptance_ids_json IS NULL
        OR json_valid(failed_acceptance_ids_json)
    );

UPDATE tasks
SET completed_at = updated_at
WHERE status = 'completed' AND completed_at IS NULL;

UPDATE goals
SET completed_at = updated_at
WHERE status = 'completed' AND completed_at IS NULL;

CREATE TRIGGER tasks_completed_at_after_status
AFTER UPDATE OF status ON tasks
WHEN (
    (NEW.status = 'completed' AND NEW.completed_at IS NULL)
    OR (NEW.status != 'completed' AND NEW.completed_at IS NOT NULL)
)
BEGIN
    UPDATE tasks
    SET completed_at = CASE
        WHEN NEW.status = 'completed' THEN CURRENT_TIMESTAMP
        ELSE NULL
    END
    WHERE id = NEW.id;
END;

CREATE TRIGGER goals_completed_at_after_status
AFTER UPDATE OF status ON goals
WHEN (
    (NEW.status = 'completed' AND NEW.completed_at IS NULL)
    OR (NEW.status != 'completed' AND NEW.completed_at IS NOT NULL)
)
BEGIN
    UPDATE goals
    SET completed_at = CASE
        WHEN NEW.status = 'completed' THEN CURRENT_TIMESTAMP
        ELSE NULL
    END
    WHERE id = NEW.id;
END;
