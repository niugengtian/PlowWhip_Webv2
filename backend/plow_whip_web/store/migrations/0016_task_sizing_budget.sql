ALTER TABLE tasks ADD COLUMN sizing_json TEXT;
ALTER TABLE tasks ADD COLUMN execution_budget_json TEXT;
ALTER TABLE tasks ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1));
ALTER TABLE tasks ADD COLUMN override_reason TEXT;
ALTER TABLE tasks ADD COLUMN budget_overrun_evidence_json TEXT;

-- Pre-existing tasks were never estimated; mark them explicitly instead of
-- letting them masquerade as estimated later.
UPDATE tasks SET sizing_json = '{"status":"legacy_fallback"}' WHERE sizing_json IS NULL;
