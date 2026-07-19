-- Canonical RuleLibrary / RoleTemplate / ProjectRoleRule / RoleInstance /
-- SessionBinding. Does NOT rewrite Convention content, Token, audit, or
-- historical task_sessions rows. Existing non-butler roles are marked legacy.

-- Singleton logical GlobalButler identity (exactly one row).
CREATE TABLE global_butler_identity (
    id TEXT PRIMARY KEY CHECK (id = 'global'),
    role_kind TEXT NOT NULL CHECK (role_kind = 'global_butler'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO global_butler_identity(id, role_kind) VALUES ('global', 'global_butler');

-- Append-only rule versions. Never UPDATE content in place.
CREATE TABLE rule_versions (
    rule_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    license TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    applies_to_json TEXT NOT NULL CHECK (json_valid(applies_to_json)),
    mandatory INTEGER NOT NULL CHECK (mandatory IN (0, 1)),
    enforcement TEXT NOT NULL CHECK (
        enforcement IN ('code', 'context', 'verification')
    ),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deprecated_at TEXT,
    PRIMARY KEY (rule_id, revision)
);

CREATE INDEX idx_rule_versions_scope_status
ON rule_versions(scope, status, rule_id);

-- Append-only role template versions.
CREATE TABLE role_template_versions (
    template_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    capability TEXT NOT NULL,
    capability_key TEXT NOT NULL,
    tools_json TEXT NOT NULL CHECK (json_valid(tools_json)),
    provider_requirements_json TEXT NOT NULL
        CHECK (json_valid(provider_requirements_json)),
    boundaries_json TEXT NOT NULL CHECK (json_valid(boundaries_json)),
    workflow_json TEXT NOT NULL CHECK (json_valid(workflow_json)),
    deliverables_json TEXT NOT NULL CHECK (json_valid(deliverables_json)),
    verification_json TEXT NOT NULL CHECK (json_valid(verification_json)),
    context_retention_json TEXT NOT NULL CHECK (json_valid(context_retention_json)),
    source_refs_json TEXT NOT NULL CHECK (json_valid(source_refs_json)),
    template_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated')),
    generated_by_project_butler INTEGER NOT NULL DEFAULT 0
        CHECK (generated_by_project_butler IN (0, 1)),
    source_project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    source_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    generation_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deprecated_at TEXT,
    PRIMARY KEY (template_id, revision)
);

-- Dedup active templates by stable capability key + structural hash.
CREATE UNIQUE INDEX idx_role_template_dedup_active
ON role_template_versions(capability_key, template_hash)
WHERE status = 'active';

CREATE INDEX idx_role_template_capability
ON role_template_versions(capability, status);

-- Template → rule revision references (FK-protected).
CREATE TABLE role_template_rule_refs (
    template_id TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    rule_revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (template_id, template_revision, rule_id),
    FOREIGN KEY (template_id, template_revision)
        REFERENCES role_template_versions(template_id, revision),
    FOREIGN KEY (rule_id, rule_revision)
        REFERENCES rule_versions(rule_id, revision)
);

-- Project-scoped overlays. Never forks/copies a global template body.
CREATE TABLE project_role_rules (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    capability TEXT,
    template_id TEXT,
    rule_id TEXT NOT NULL,
    rule_revision INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deprecated')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deprecated_at TEXT,
    FOREIGN KEY (rule_id, rule_revision)
        REFERENCES rule_versions(rule_id, revision)
);

CREATE UNIQUE INDEX idx_project_role_rules_dedup
ON project_role_rules(
    project_id, revision, rule_id,
    IFNULL(capability, ''), IFNULL(template_id, '')
);

CREATE INDEX idx_project_role_rules_project
ON project_role_rules(project_id, status, capability);

-- Immutable RoleInstance snapshots created only after GoalSpec confirmation.
CREATE TABLE role_instances (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_id TEXT REFERENCES goals(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role_id TEXT REFERENCES roles(id) ON DELETE SET NULL,
    role_kind TEXT NOT NULL,
    template_id TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    template_hash TEXT NOT NULL,
    ruleset_hash TEXT NOT NULL,
    instance_hash TEXT NOT NULL,
    task_spec_revision INTEGER NOT NULL,
    provider TEXT NOT NULL,
    match_reason_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(match_reason_json)),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'replaced', 'terminated')),
    replaced_by TEXT REFERENCES role_instances(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id, template_revision)
        REFERENCES role_template_versions(template_id, revision)
);

CREATE UNIQUE INDEX idx_role_instances_task_active
ON role_instances(task_id)
WHERE status = 'active';

CREATE INDEX idx_role_instances_goal
ON role_instances(goal_id, role_kind);

-- Atomic SessionBinding: project + role_instance + task (+ generation).
CREATE TABLE session_bindings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role_instance_id TEXT NOT NULL REFERENCES role_instances(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    session_generation INTEGER NOT NULL DEFAULT 1,
    external_session_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'bound'
        CHECK (status IN ('bound', 'terminated', 'archived')),
    binding_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, role_instance_id, task_id, session_generation)
);

CREATE INDEX idx_session_bindings_task
ON session_bindings(task_id, status);

-- Soft-extend existing task_sessions without dropping history.
ALTER TABLE task_sessions ADD COLUMN role_instance_id TEXT
    REFERENCES role_instances(id) ON DELETE SET NULL;
ALTER TABLE task_sessions ADD COLUMN session_binding_id TEXT
    REFERENCES session_bindings(id) ON DELETE SET NULL;

-- Historical fixed development roles remain readable but marked legacy.
ALTER TABLE roles ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0;
UPDATE roles
SET legacy = 1
WHERE kind IN (
    'frontend', 'backend', 'ui', 'fullstack', 'devops_sre',
    'verification', 'web3', 'coordination', 'simple-worker'
);

-- Exactly one active ProjectButler per project (kind=butler).
CREATE UNIQUE INDEX idx_roles_one_project_butler
ON roles(project_id)
WHERE kind = 'butler' AND legacy = 0;
