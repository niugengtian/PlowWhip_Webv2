-- Sprint 11 P0: expand role catalog, scrub SQLite bodies, unify max_attempts.

-- Ensure capability roles exist on every project (idempotent inserts).
INSERT INTO roles(id, project_id, kind)
SELECT lower(hex(randomblob(16))), p.id, role.kind
FROM projects p
CROSS JOIN (
    SELECT 'backend' AS kind
    UNION ALL SELECT 'frontend'
    UNION ALL SELECT 'ui'
) AS role
WHERE NOT EXISTS (
    SELECT 1 FROM roles existing
    WHERE existing.project_id = p.id AND existing.kind = role.kind
);

-- Unify max_attempts column from execution_budget when present.
UPDATE tasks
SET max_attempts = CAST(json_extract(execution_budget_json, '$.max_attempts') AS INTEGER)
WHERE execution_budget_json IS NOT NULL
  AND json_extract(execution_budget_json, '$.max_attempts') IS NOT NULL
  AND max_attempts != CAST(json_extract(execution_budget_json, '$.max_attempts') AS INTEGER);
