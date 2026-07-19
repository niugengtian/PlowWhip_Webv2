-- Convention scope expansion + worker help / extreme escalation.
-- Non-destructive: rebuilds conventions CHECK to allow task_role while
-- preserving every existing row. Does NOT insert or update global/project/task
-- Convention content, so an existing global revision=1 Session/Context
-- Convention remains untouched.

CREATE TABLE conventions_v2 (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'project', 'task', 'task_role')),
    scope_id TEXT NOT NULL,
    content TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope, scope_id)
);

INSERT INTO conventions_v2(id, scope, scope_id, content, revision, created_at, updated_at)
SELECT id, scope, scope_id, content, revision, created_at, updated_at
FROM conventions;

DROP TABLE conventions;
ALTER TABLE conventions_v2 RENAME TO conventions;
CREATE INDEX IF NOT EXISTS idx_conventions_scope ON conventions(scope, scope_id);

CREATE TABLE worker_help_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    blocker TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    attempted_actions_json TEXT NOT NULL CHECK (json_valid(attempted_actions_json)),
    minimal_question TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('open', 'answered', 'replanned', 'replaced', 'escalated', 'closed')
    ),
    resolution_json TEXT CHECK (resolution_json IS NULL OR json_valid(resolution_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX worker_help_requests_task_idx
ON worker_help_requests(task_id, created_at DESC);

CREATE TABLE task_escalations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    help_request_id TEXT REFERENCES worker_help_requests(id) ON DELETE SET NULL,
    reason_class TEXT NOT NULL CHECK (
        reason_class IN (
            'credential_or_permission',
            'safety_or_irreversible',
            'conflicting_owner_directives',
            'unresolvable_requirement_ambiguity'
        )
    ),
    detail TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'resolved')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX task_escalations_project_idx
ON task_escalations(project_id, status, created_at DESC);
