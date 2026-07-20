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
  status: 'clarifying' | 'awaiting_confirmation' | 'provider_suspended' | 'dispatched' | 'rejected'
  revision: number
  confidence: number
  expected_field: 'objective' | 'boundaries' | 'acceptance' | null
  spec: Record<string, unknown>
  proposal_hash: string | null
  goal_id: string | null
  messages: ButlerMessage[]
  direct_project_butler_url: string | null
  auto_dispatch?: boolean
  structured_goal_spec?: boolean
  semantic?: Record<string, unknown> | null
  planner?: Record<string, unknown> | null
  provider: string
  external_session_id: string | null
  session_generation: number
  archived_at: string | null
}

export type AlertIncident = {
  id: string
  fingerprint: string
  root_kind: string
  scope_key: string
  severity: 'critical' | 'error' | 'warning' | 'info'
  title: string
  status: 'open' | 'recovering' | 'resolved'
  occurrence_count: number
  first_seen_at: string
  last_seen_at: string
  resolved_at: string | null
  detail: Record<string, unknown>
}

export type Alerts = {
  items: AlertIncident[]
  network: Record<string, unknown>
}

export type BehaviorBaseline = {
  id: string
  source: string
  revision: number
  version?: number
  role: string | null
  applicable: boolean
  not_applicable?: boolean
  applicability: string
  mandatory: boolean
  effective_reserve_bytes: number
  config_source?: string
  reason?: string
  model_invoked: boolean
}

export type ConventionInventory = {
  mutable_conventions: Record<string, unknown>[]
  bundled_behaviors: Record<string, unknown>[]
  behavior_baseline: BehaviorBaseline
  precedence: string[]
  model_invoked: false
}

export type EffectiveContextPreview = {
  project_id: string | null
  task_id: string
  role_id: string | null
  role: string | null
  behavior_baseline: BehaviorBaseline
  layers: Record<string, unknown>[]
  empty_scopes: string[]
  model_invoked: false
}

export type RoleInstance = {
  id: string
  revision: number
  project_id: string
  goal_id: string | null
  task_id: string
  role_kind: string
  template_id: string
  template_revision: number
  template_hash: string
  ruleset_hash: string
  instance_hash: string
  task_spec_revision: number
  provider: string
  status: string
  match_reason?: Record<string, unknown> | string | null
  source_chain?: Record<string, unknown> | null
  generation_reason?: string | null
}

export type SessionBinding = {
  id: string
  project_id: string
  role_instance_id: string
  task_id: string
  provider: string
  session_generation: number
  status: string
  binding_hash: string
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
  episode_wall_limit_seconds: number
  checkpoint_interval_seconds: number
  no_progress_seconds: number
  max_host_processes: number
  progress_extension_seconds: number
  provider_failure_threshold: number
  provider_recovery_successes: number
  provider_open_seconds: number
  network_failure_threshold: number
  network_recovery_successes: number
  resume_batch_size: number
  alert_debounce_seconds: number
  default_provider_policy: 'auto' | 'preferred' | 'pinned'
  default_provider_order: string[]
  default_butler_provider: string
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
  uncached_input_tokens?: number
  cached_input_tokens_in_total: boolean
  output_tokens: number
  total_tokens: number
  total_formula: string
  scope?: 'all_history' | string
  usage_semantics?: string
  timezone?: string
  today?: {
    date: string
    timezone: string
    scope?: 'local_day' | string
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    output_tokens: number
    total_tokens: number
    calls: number
  }
  usage_quality?: {
    usage_semantics: 'delta' | 'legacy_inferred_delta' | 'unresolved_snapshot'
    label?: string
    calls: number
    tokens: number
    call_share?: number
    token_share?: number
  }[]
  ratios?: {
    input_per_output: number | null
    uncached_input_per_output: number | null
    is_budget_gate: boolean
    is_quality_gate: boolean
  }
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
export type UsageDailySeries = {
  timezone: string
  from: string
  to: string
  days: {
    date: string
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    output_tokens: number
    total_tokens: number
    calls: number
  }[]
  totals: {
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    output_tokens: number
    total_tokens: number
    calls: number
  }
  total_formula: string
  cached_input_tokens_in_total: boolean
}
export type UsageDailyBreakdown = {
  date: string
  timezone: string
  input_tokens: number
  cached_input_tokens: number
  uncached_input_tokens: number
  output_tokens: number
  total_tokens: number
  calls: number
  total_formula: string
  cached_input_tokens_in_total: boolean
  projects: {
    key: string
    label: string
    project_id: string | null
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    output_tokens: number
    tokens: number
    calls: number
  }[]
  tasks: {
    key: string
    label: string
    task_id: string | null
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    output_tokens: number
    tokens: number
    calls: number
  }[]
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
    const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null
    throw new Error(apiErrorMessage(payload?.detail, response.status))
  }
  return response.json() as Promise<T>
}

function apiErrorMessage(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (!item || typeof item !== 'object') return []
      const record = item as Record<string, unknown>
      const message = typeof record.msg === 'string' ? record.msg : ''
      const location = Array.isArray(record.loc)
        ? record.loc.filter((part) => part !== 'body').map(String).join('.')
        : ''
      return message ? [`${location ? `${location}：` : ''}${message}`] : []
    })
    if (messages.length) return messages.join('；')
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    if (typeof record.message === 'string') return record.message
  }
  return `HTTP ${status}`
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
  behaviorBaseline: (role?: string) =>
    request<BehaviorBaseline>(
      role ? `/api/system/behavior-baseline?role=${encodeURIComponent(role)}` : '/api/system/behavior-baseline',
    ),
  roleInstances: (params?: { projectId?: string; goalId?: string; taskId?: string }) => {
    const query = new URLSearchParams()
    if (params?.projectId) query.set('project_id', params.projectId)
    if (params?.goalId) query.set('goal_id', params.goalId)
    if (params?.taskId) query.set('task_id', params.taskId)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<{ items: RoleInstance[]; model_invoked: false }>(`/api/role-instances${suffix}`)
  },
  sessionBindings: (params?: { projectId?: string; taskId?: string }) => {
    const query = new URLSearchParams()
    if (params?.projectId) query.set('project_id', params.projectId)
    if (params?.taskId) query.set('task_id', params.taskId)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<{ items: SessionBinding[]; model_invoked: false }>(`/api/session-bindings${suffix}`)
  },
  roleTemplates: (capability?: string) => {
    const suffix = capability ? `?capability=${encodeURIComponent(capability)}` : ''
    return request<{ items: Record<string, unknown>[]; model_invoked: false }>(`/api/role-templates${suffix}`)
  },
  conventionInventory: (params?: { projectId?: string; taskId?: string; roleId?: string; role?: string }) => {
    const query = new URLSearchParams()
    if (params?.projectId) query.set('project_id', params.projectId)
    if (params?.taskId) query.set('task_id', params.taskId)
    if (params?.roleId) query.set('role_id', params.roleId)
    if (params?.role) query.set('role', params.role)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<ConventionInventory>(`/api/conventions/inventory${suffix}`)
  },
  conventionEffective: (taskId: string, roleId?: string, role?: string) => {
    const query = new URLSearchParams({ task_id: taskId })
    if (roleId) query.set('role_id', roleId)
    if (role) query.set('role', role)
    return request<EffectiveContextPreview>(`/api/conventions/effective?${query.toString()}`)
  },
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
  usage: (projectId?: string) => request<Usage>(
    `/api/usage${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`,
  ),
  usageDaily: (params?: { start?: string; end?: string; days?: number; projectId?: string }) => {
    const query = new URLSearchParams()
    if (params?.start) query.set('start', params.start)
    if (params?.end) query.set('end', params.end)
    if (params?.days != null) query.set('days', String(params.days))
    if (params?.projectId) query.set('project_id', params.projectId)
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<UsageDailySeries>(`/api/usage/daily${suffix}`)
  },
  usageDailyDay: (day: string, projectId?: string) => request<UsageDailyBreakdown>(
    `/api/usage/daily/${day}${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`,
  ),
  alerts: (status?: AlertIncident['status']) =>
    request<Alerts>(`/api/alerts${status ? `?status=${status}` : ''}`),
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
  globalButlerConversations: () =>
    request<ButlerConversation[]>('/api/butlers/global/conversations'),
  startGlobalButler: (payload: { instruction: string; provider?: string; workspace_root?: string | null }) =>
    request<ButlerConversation>('/api/butlers/global/conversations', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({
        source_type: 'human',
        source_id: 'owner',
        provider: payload.provider ?? 'codex',
        ...payload,
      }),
    }),
  sendGlobalButlerMessage: (conversation: ButlerConversation, content: string) =>
    request<ButlerConversation>(
      `/api/butlers/global/conversations/${conversation.id}/messages`,
      {
        method: 'POST',
        idempotencyKey: crypto.randomUUID(),
        body: JSON.stringify({
          expected_revision: conversation.revision,
          content,
          sender_type: 'human',
        }),
      },
    ),
  routeGlobalButler: (projectId: string, payload: Record<string, unknown>) =>
    request<ButlerConversation>('/api/butlers/global/route', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ ...payload, project_id: projectId }),
    }),
  startProjectButler: (projectId: string, payload: Record<string, unknown>) =>
    request<ButlerConversation>(`/api/projects/${projectId}/butler/conversations`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(payload),
    }),
  projectButlerConversations: (projectId: string) =>
    request<ButlerConversation[]>(`/api/projects/${projectId}/butler/conversations`),
  sendProjectButlerMessage: (
    projectId: string,
    conversation: ButlerConversation,
    content: string,
    field?: NonNullable<ButlerConversation['expected_field']>,
  ) => request<ButlerConversation>(
    `/api/projects/${projectId}/butler/conversations/${conversation.id}/messages`,
    {
      method: 'POST',
      body: JSON.stringify({
        expected_revision: conversation.revision,
        content,
        ...(field ? { field } : {}),
        sender_type: 'human',
      }),
    },
  ),
  resumeProjectButler: (projectId: string, conversation: ButlerConversation) =>
    request<ButlerConversation>(
      `/api/projects/${projectId}/butler/conversations/${conversation.id}/resume`,
      {
        method: 'POST',
        idempotencyKey: crypto.randomUUID(),
        body: JSON.stringify({
          expected_revision: conversation.revision,
          actor_type: 'human',
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
