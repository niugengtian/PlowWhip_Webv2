export type TaskStatus =
  | 'ready'
  | 'running'
  | 'stopping'
  | 'verifying'
  | 'completed'
  | 'terminal_failed'
  | 'needs_human'
  | 'cancelled'
  | 'paused'

export type TaskSizingInputs = {
  layers_touched: number
  components_touched: number
  estimated_files_changed: number
  has_migration: boolean
  has_deploy: boolean
  verification_commands_count: number
  estimated_verification_seconds: number
  external_dependencies_count: number
  risk_level: 'low' | 'medium' | 'high'
  independent_review_required: boolean
  gate_artifact: boolean
  gate_boundary: boolean
  gate_verification: boolean
  gate_dependency: boolean
}

export type TaskSizingEstimate = {
  status: 'estimated' | 'needs_planning'
  missing_gates: ('artifact' | 'boundary' | 'verification' | 'dependency' | 'independent_review_orchestration')[]
  size_class: 'XS' | 'S' | 'M' | 'L' | 'XL' | null
  rationale: string[]
  soft_deadline_seconds: number | null
  hard_deadline_seconds: number | null
  max_turns: number | null
  max_attempts: number | null
  verification_timeout_seconds: number | null
  progress_extension_seconds: number | null
  model_invoked: false
  bootstrap_version: string
}

export type Task = {
  id: string
  title: string
  objective: string
  project_path: string
  project_id: string | null
  role_id: string | null
  worker_id: string | null
  resource_key: string | null
  network_requirement: 'none' | 'any' | 'domestic' | 'overseas'
  same_failure_count: number
  no_progress_count: number
  last_failure_fingerprint: string | null
  next_eligible_at: string | null
  provider: string
  quality_profile: string
  status: TaskStatus
  revision: number
  max_attempts: number
  attempts_used: number
  tokens_used: number
  last_evidence_hash: string | null
  last_error: string | null
  created_at: string
  updated_at: string
  command: Record<string, unknown>
  verification: { kind: string; path?: string; contains?: string; expected?: number }[]
  sizing: Record<string, unknown>
  execution_policy: Record<string, unknown> | null
  goal_id?: string | null
  parent_task_id?: string | null
  depends_on?: string[] | null
  work_item_kind?: string | null
  ordinal?: number | null
  blocked_reason?: string | null
  handoff?: Record<string, unknown> | null
  spec_revision: number
  spec: {
    objective: string
    scope: string[]
    acceptance: string[]
    verification: { kind: string; path?: string; contains?: string; expected?: number }[]
    artifacts: string[]
    constraints: string[]
    deadline: Record<string, number>
  }
  evidence_manifest: Record<string, unknown> | null
}

export type Goal = {
  id: string
  title: string
  objective: string
  project_id: string
  provider: string
  status: string
  plan: Record<string, unknown>
  sizing_inputs: Record<string, unknown> | null
  parent_task_id: string | null
  created_at: string
  updated_at: string
  work_items: Record<string, unknown>[]
  spec_revision: number
  spec: Task['spec']
}

export type ButlerMessage = {
  id: string
  ordinal: number
  sender_type: 'project_butler' | 'global_butler' | 'human' | 'agent'
  kind: 'instruction' | 'question' | 'answer' | 'proposal' | 'confirmation'
  content: string
  payload: Record<string, unknown>
  created_at: string
}

export type ButlerConversation = {
  id: string
  scope: 'global' | 'project'
  project_id: string | null
  source_type: 'human' | 'global_butler' | 'agent'
  source_id: string | null
  status: 'clarifying' | 'awaiting_confirmation' | 'dispatched' | 'rejected'
  revision: number
  confidence: number
  expected_field: 'objective' | 'boundaries' | 'acceptance' | null
  spec: Record<string, unknown>
  proposal_hash: string | null
  goal_id: string | null
  messages: ButlerMessage[]
  direct_project_butler_url: string | null
}

export type GlobalButlerOverview = {
  scope: 'global'
  workspace_root: string | null
  projects: {
    id: string
    name: string
    path: string
    host_path: string | null
    resource_path: string
    status: string
    goal_count: number
    running_goals: number
    active_tasks: number
    active_workers: number
  }[]
  totals: {
    projects: number
    running_goals: number
    active_tasks: number
    active_workers: number
  }
  canonical_sources: string[]
  model_invoked: false
}

export type TaskArtifact = {
  relative_path: string
  host_path: string
  exists: boolean
  bytes: number | null
  sha256: string | null
  modified_at: string | null
  actions: ('finder' | 'cursor')[]
}

export type TaskDeletionEligibility = { deletable: boolean; reason: string | null }
export type TaskDeletion = {
  task_id: string
  title: string
  reason: string
  deleted_revision: number
  idempotency_key: string
  deleted_at: string
}

export type Role = { id: string; kind: string; status: string }
export type Worker = {
  id: string
  role_id: string
  role: string
  provider: string
  session_id: string
  external_session_id: string | null
  session_generation: number
  status: string
  active_task_id: string | null
  last_seen_at: string | null
  last_error: string | null
  released_at: string | null
  rotation_reason?: string | null
  last_input_tokens: number
  last_cached_input_tokens: number
  last_output_tokens: number
  last_uncached_input_tokens: number
  last_context_pressure_tokens: number
  last_context_pressure_reason: string | null
  last_context_session_generation: number | null
  last_attribution_granularity: string
  last_value_classification: string
}
export type WorkerDetail = {
  worker: Worker & Record<string, unknown>
  task: (Pick<Task, 'id' | 'title' | 'objective' | 'status' | 'revision' | 'spec_revision' | 'provider' | 'spec'> & Record<string, unknown>) | null
  host_job: ({
    job_id: string
    task_id: string
    status: string
    dispatch_outcome: 'accepted' | 'rejected' | 'unknown'
    reconciliation_deadline_at: string | null
    external_session_id: string | null
    heartbeat_at: string | null
    result?: Record<string, unknown>
  } & Record<string, unknown>) | null
  episode: Record<string, unknown> | null
  ownership: Record<string, unknown>
}
export type WorkerStream = {
  worker_id: string
  job_id: string | null
  items: { kind: 'stdout' | 'stderr' | 'tool' | 'status'; ref?: string; refs?: string[]; text: string; created_at?: string; state_revision?: number; offset?: number; next_offset?: number }[]
  next_cursor: string
  has_more: boolean
}
export type Project = {
  id: string
  name: string
  path: string
  host_path: string | null
  execution_policy: Record<string, unknown>
  status: string
  created_at: string
  roles: Role[]
  workers: Worker[]
}

export type RuntimeSettingsValues = {
  scheduler_interval_seconds: number
  scheduler_lease_seconds: number
  cron_enabled: boolean
  cron_expression: string
  cron_timezone: string
  cron_misfire_policy: 'catch_up_once' | 'skip'
  max_parallel_workers: number
  auto_dispatch: boolean
  max_same_failure: number
  max_no_progress: number
  context_max_bytes: number
  rotation_max_bytes: number
  checkpoint_max_bytes: number
  handoff_max_bytes: number
  observation_tail_lines: number
  observation_max_bytes: number
}
export type RuntimeSettings = {
  revision: number
  values: RuntimeSettingsValues
  sources: Record<string, string>
  warnings: string[]
  override_revisions?: Record<string, number>
  updated_at: string | null
}
export type RuntimeSettingsOverride = {
  scope: 'project' | 'task_role'
  scope_id: string
  revision: number
  values: Partial<Pick<RuntimeSettingsValues,
    'max_same_failure' | 'max_no_progress' | 'context_max_bytes' |
    'rotation_max_bytes' | 'checkpoint_max_bytes' | 'handoff_max_bytes' |
    'observation_tail_lines' | 'observation_max_bytes'>>
  updated_at: string | null
  effective?: RuntimeSettings
}
export type SchedulerStatus = {
  runtime: {
    fencing_token: number
    last_tick_at: string | null
    last_result: Record<string, unknown> | null
    last_error: string | null
    runner_id: string | null
    runner_started_at: string | null
    runner_heartbeat_at: string | null
    runner_stopped_at: string | null
    runner_error: string | null
    runner_active: boolean
    last_cron_slot: string | null
  }
  engine: { backend: string; active: boolean; managed_by: string; data_dir: string }
  schedule: { enabled: boolean; expression: string; timezone: string; misfire_policy: 'catch_up_once' | 'skip'; next_run_at: string | null }
  authorization_required: boolean
  model_invoked: boolean
}
export type Convention = {
  scope: 'global' | 'project' | 'task'
  scope_id: string
  content: string
  revision: number
  updated_at: string | null
}
export type Usage = {
  input_tokens: number
  cached_input_tokens: number
  cached_input_tokens_in_total: boolean
  output_tokens: number
  total_tokens: number
  total_formula: string
  usage_semantics?: string
  usage_quality?: {
    usage_semantics: 'delta' | 'legacy_inferred_delta' | 'unresolved_snapshot'
    calls: number
    tokens: number
  }[]
  raw_snapshot_totals?: {
    input_tokens: number
    cached_input_tokens: number
    output_tokens: number
    total_tokens: number
  }
  projects: { project_id: string | null; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; tokens: number; calls: number }[]
  tasks: { task_id: string | null; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; tokens: number; calls: number }[]
  workers: { worker_id: string | null; tokens: number; calls: number }[]
  providers: { provider: string; tokens: number; calls: number }[]
  models: { model: string; tokens: number; calls: number }[]
  call_kinds: { call_kind: string; tokens: number; calls: number }[]
  sessions: { session_id: string | null; tokens: number; calls: number }[]
  calls: { call_id: string; call_kind: 'executor' | 'butler_planner' | 'router' | 'verifier' | 'convention_refinement'; status: string; task_id: string | null; worker_id: string | null; provider: string; model: string; session_id: string | null; session_generation: number | null; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; total_tokens: number; error_class: string | null; created_at: string }[]
}
export type RuntimeHealth = {
  connectivity: string
  domestic_ok: number | null
  overseas_ok: number | null
  last_tick_at: string | null
  last_resume_at: string | null
  consecutive_failures: number
}
export type OutboxEvent = {
  sequence: number
  event_type: string
  aggregate_id: string
  payload: Record<string, unknown>
  created_at: string
  delivered_at: string | null
}
export type Provider = {
  name: string; display_name: string; status: string; model_invoked: boolean;
  capabilities: string[]; reason: string | null; adapter: 'codex' | 'cursor' | 'json-worker' | 'generic-command';
  transport: 'host-bridge' | 'container'; executable: string | null; enabled: boolean;
  credential_env: string | null; revision: number; last_probed_at: string | null
  model?: string
  credential_slot_count?: number | null
  readiness?: {
    installed: boolean
    installed_at?: string | null
    installed_reason?: string | null
    cli_probe: string | { status?: string; checked_at?: string | null; reason?: string | null }
    cli_probe_at?: string | null
    cli_probe_reason?: string | null
    session_resume_ready: boolean
    session_resume_checked_at?: string | null
    session_resume_reason?: string | null
    recent_execution_health: string
    recent_execution_checked_at?: string | null
    recent_execution_reason?: string | null
  }
}
export type ConventionSuggestion = {
  id: string; scope: Convention['scope']; scope_id: string; source_revision: number;
  provider: string; suggestion: string; input_tokens: number; output_tokens: number; applied: boolean
}
export type AuditEntry = { sequence: number; actor: string; method: string; path: string; status_code: number; created_at: string }
export type PermissionGrant = {
  id: string; project_id: string | null; capability: string; resource: string;
  decision: string; reason: string; created_at: string; revoked_at: string | null
}

export type TaskEvent = {
  sequence: number
  event_type: string
  payload: Record<string, unknown>
  state_revision: number
  created_at: string
}

type FetchOptions = RequestInit & { idempotencyKey?: string }

async function request<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey)
  const response = await fetch(path, { ...options, headers })
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(payload?.detail ?? `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  providers: () => request<Provider[]>('/api/providers'),
  updateProvider: (provider: Provider) => request<Provider>(`/api/providers/${provider.name}`, {
    method: 'PUT',
    body: JSON.stringify({
      name: provider.name, display_name: provider.display_name, adapter: provider.adapter,
      transport: provider.transport, executable: provider.executable, enabled: provider.enabled,
      credential_env: provider.credential_env, capabilities: provider.capabilities,
      expected_revision: provider.revision,
    }),
  }),
  probeProvider: (name: string) => request<Provider>(`/api/providers/${name}/probe`, { method: 'POST' }),
  audit: () => request<AuditEntry[]>('/api/audit'),
  permissions: () => request<PermissionGrant[]>('/api/permissions'),
  createPermission: (payload: Record<string, unknown>) => request<PermissionGrant>('/api/permissions', { method: 'POST', body: JSON.stringify(payload) }),
  backup: () => request<Record<string, unknown>>('/api/maintenance/backup', { method: 'POST' }),
  diagnostics: () => request<Record<string, unknown>>('/api/maintenance/diagnostics', { method: 'POST' }),
  runtimeHealth: () => request<RuntimeHealth>('/api/system/health'),
  recover: () => request<Record<string, unknown>>('/api/system/recover', { method: 'POST' }),
  outbox: () => request<OutboxEvent[]>('/api/outbox'),
  convention: (scope: string, scopeId: string) => request<Convention>(`/api/conventions/${scope}/${scopeId}`),
  updateConvention: (convention: Convention) =>
    request<Convention>('/api/conventions', {
      method: 'PUT',
      body: JSON.stringify({ ...convention, expected_revision: convention.revision }),
    }),
  refineConvention: (convention: Convention, provider: string, projectId: string | null) =>
    request<ConventionSuggestion>(`/api/conventions/${convention.scope}/${convention.scope_id}/refine`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ provider, project_id: projectId }),
    }),
  usage: () => request<Usage>('/api/usage'),
  settings: () => request<RuntimeSettings>('/api/settings'),
  updateSettings: (settings: RuntimeSettings) =>
    request<RuntimeSettings>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: settings.revision, values: settings.values }),
    }),
  effectiveSettings: (projectId?: string, taskId?: string, roleId?: string) => {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    if (taskId) params.set('task_id', taskId)
    if (roleId) params.set('role_id', roleId)
    return request<RuntimeSettings>(`/api/settings/effective?${params.toString()}`)
  },
  settingsOverride: (scope: 'project' | 'task_role', scopeId: string) =>
    request<RuntimeSettingsOverride>(`/api/settings/overrides/${scope}/${scopeId}`),
  updateSettingsOverride: (override: RuntimeSettingsOverride) =>
    request<RuntimeSettingsOverride>(`/api/settings/overrides/${override.scope}/${override.scope_id}`, {
      method: 'PUT',
      body: JSON.stringify({
        expected_revision: override.revision,
        values: override.values,
      }),
    }),
  schedulerStatus: () => request<SchedulerStatus>('/api/scheduler/status'),
  schedulerTick: () => request<Record<string, unknown>>('/api/scheduler/tick', { method: 'POST' }),
  projects: () => request<Project[]>('/api/projects'),
  globalButlerOverview: (workspaceRoot?: string) =>
    request<GlobalButlerOverview>(
      `/api/butlers/global/overview${workspaceRoot ? `?workspace_root=${encodeURIComponent(workspaceRoot)}` : ''}`,
    ),
  startProjectButler: (projectId: string, payload: Record<string, unknown>) =>
    request<ButlerConversation>(`/api/projects/${projectId}/butler/conversations`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(payload),
    }),
  answerProjectButler: (
    projectId: string,
    conversation: ButlerConversation,
    field: NonNullable<ButlerConversation['expected_field']>,
    values: string[],
  ) => request<ButlerConversation>(
    `/api/projects/${projectId}/butler/conversations/${conversation.id}/answers`,
    {
      method: 'POST',
      body: JSON.stringify({
        expected_revision: conversation.revision,
        field,
        values,
        sender_type: 'human',
      }),
    },
  ),
  confirmProjectButler: (projectId: string, conversation: ButlerConversation) =>
    request<ButlerConversation>(
      `/api/projects/${projectId}/butler/conversations/${conversation.id}/confirm`,
      {
        method: 'POST',
        idempotencyKey: crypto.randomUUID(),
        body: JSON.stringify({
          expected_revision: conversation.revision,
          proposal_hash: conversation.proposal_hash,
          actor_type: 'human',
        }),
      },
    ),
  createProject: (payload: { name: string; path: string; host_path?: string | null }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  releaseProject: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/release`, { method: 'POST' }),
  rebindWorker: (workerId: string, provider: string) =>
    request<Worker>(`/api/workers/${workerId}/rebind`, {
      method: 'POST', body: JSON.stringify({ provider, reason: 'operator_rebind' }),
    }),
  workerDetail: (workerId: string) => request<WorkerDetail>(`/api/workers/${workerId}`),
  workerStream: (workerId: string, cursor = '0:0:0') =>
    request<WorkerStream>(`/api/workers/${workerId}/stream?cursor=${encodeURIComponent(cursor)}`),
  tasks: () => request<Task[]>('/api/tasks'),
  estimateTask: (payload: TaskSizingInputs) =>
    request<TaskSizingEstimate>('/api/tasks/estimate', {
      method: 'POST', body: JSON.stringify(payload),
    }),
  taskEvents: (taskId: string) => request<TaskEvent[]>(`/api/tasks/${taskId}/events`),
  taskArtifacts: (taskId: string) =>
    request<TaskArtifact[]>(`/api/tasks/${taskId}/artifacts`),
  taskDeletionEligibility: (taskId: string) =>
    request<TaskDeletionEligibility>(`/api/tasks/${taskId}/deletion-eligibility`),
  deleteTask: (task: Task, reason: string) =>
    request<TaskDeletion>(`/api/tasks/${task.id}`, {
      method: 'DELETE',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ expected_revision: task.revision, reason }),
    }),
  openTaskArtifact: (
    taskId: string, relativePath: string, action: 'finder' | 'cursor',
  ) => request<Record<string, unknown>>(`/api/tasks/${taskId}/artifacts/open`, {
    method: 'POST',
    body: JSON.stringify({ relative_path: relativePath, action }),
  }),
  createTask: (payload: Record<string, unknown>) =>
    request<Task>('/api/tasks', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ ...payload, quality_profile: 'deterministic' }),
    }),
  goals: () => request<Goal[]>('/api/goals'),
  createGoal: (payload: Record<string, unknown>) =>
    request<Goal>('/api/goals', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(payload),
    }),
  getGoal: (goalId: string) => request<Goal>(`/api/goals/${goalId}`),
  driveTask: (task: Task) =>
    request<Task>(`/api/tasks/${task.id}/drive`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ expected_revision: task.revision }),
    }),
  controlTask: (task: Task, action: 'pause' | 'resume' | 'cancel' | 'needs_human', reason: string) =>
    request<Task>(`/api/tasks/${task.id}/control`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ action, reason, expected_revision: task.revision }),
    }),
}
