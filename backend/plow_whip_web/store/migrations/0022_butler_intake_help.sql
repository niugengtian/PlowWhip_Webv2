CREATE TABLE butler_intakes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('structured', 'natural_language')),
    instruction TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(input_json)),
    status TEXT NOT NULL CHECK (status IN (
        'received', 'clarifying', 'awaiting_confirmation', 'dispatching',
        'dispatched', 'interrupted', 'failed'
    )),
    revision INTEGER NOT NULL DEFAULT 0,
    deterministic_size TEXT NOT NULL CHECK (
        deterministic_size IN ('small', 'medium', 'large')
    ),
    assessed_size TEXT NOT NULL CHECK (
        assessed_size IN ('small', 'medium', 'large')
    ),
    confidence INTEGER NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 100),
    current_question_id TEXT,
    proposal_json TEXT CHECK (proposal_json IS NULL OR json_valid(proposal_json)),
    proposal_hash TEXT,
    confirmed_proposal_hash TEXT,
    selected_provider TEXT,
    goal_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE butler_questions (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES butler_intakes(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT,
    asked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at TEXT
);

CREATE UNIQUE INDEX idx_butler_one_open_question
ON butler_questions(intake_id) WHERE answered_at IS NULL;

CREATE TABLE butler_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id TEXT NOT NULL REFERENCES butler_intakes(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_butler_events_intake
ON butler_events(intake_id, sequence);

CREATE TABLE worker_help_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_id TEXT REFERENCES goals(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES workers(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('normal', 'blocking', 'extreme')),
    status TEXT NOT NULL CHECK (
        status IN ('open', 'answered', 'owner_escalated', 'interrupted')
    ),
    question TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(checkpoint_json)),
    revision INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE UNIQUE INDEX idx_help_one_owner_question
ON worker_help_requests(task_id)
WHERE status = 'owner_escalated';

CREATE TABLE worker_help_replies (
    id TEXT PRIMARY KEY,
    help_id TEXT NOT NULL REFERENCES worker_help_requests(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    sender TEXT NOT NULL CHECK (sender IN ('butler', 'owner', 'system')),
    content TEXT NOT NULL,
    bounded_context_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(bounded_context_json)
    ),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_help_replies_help
ON worker_help_replies(help_id, revision);
