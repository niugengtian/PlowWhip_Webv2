CREATE TABLE butler_conversations (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id TEXT REFERENCES projects(id),
    source_type TEXT NOT NULL CHECK (source_type IN ('human', 'global_butler', 'agent')),
    source_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('clarifying', 'awaiting_confirmation', 'dispatched', 'rejected')
    ),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    expected_field TEXT CHECK (
        expected_field IS NULL OR expected_field IN ('objective', 'boundaries', 'acceptance')
    ),
    spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
    proposal_hash TEXT,
    goal_id TEXT REFERENCES goals(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (scope = 'project' AND project_id IS NOT NULL)
        OR (scope = 'global' AND project_id IS NULL)
    ),
    CHECK (
        (status = 'clarifying' AND expected_field IS NOT NULL AND proposal_hash IS NULL)
        OR (status = 'awaiting_confirmation' AND expected_field IS NULL AND proposal_hash IS NOT NULL)
        OR (status IN ('dispatched', 'rejected') AND expected_field IS NULL)
    )
);

CREATE INDEX butler_conversations_project_status_idx
ON butler_conversations(project_id, status, updated_at DESC);

CREATE TABLE butler_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES butler_conversations(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    sender_type TEXT NOT NULL CHECK (
        sender_type IN ('project_butler', 'global_butler', 'human', 'agent')
    ),
    kind TEXT NOT NULL CHECK (kind IN ('instruction', 'question', 'answer', 'proposal', 'confirmation')),
    content TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conversation_id, ordinal)
);

CREATE TRIGGER butler_dispatched_requires_goal
BEFORE UPDATE OF status ON butler_conversations
WHEN NEW.status = 'dispatched' AND NEW.goal_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'dispatched butler conversation requires goal');
END;
