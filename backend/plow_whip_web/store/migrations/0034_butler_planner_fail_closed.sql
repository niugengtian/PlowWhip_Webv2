ALTER TABLE butler_conversations
ADD COLUMN planner_state TEXT NOT NULL DEFAULT 'idle'
CHECK (planner_state IN ('idle', 'provider_suspended'));

ALTER TABLE butler_conversations ADD COLUMN planner_call_id TEXT;
ALTER TABLE butler_conversations ADD COLUMN planner_error_class TEXT;
ALTER TABLE butler_conversations ADD COLUMN planner_failure_at TEXT;

CREATE INDEX butler_conversations_planner_state_idx
ON butler_conversations(planner_state, updated_at DESC);
