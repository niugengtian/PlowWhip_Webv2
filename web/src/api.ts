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

type TokenEstimateBand = { min: number; max: number; p90: number }

export type TaskSizingEstimate = {
  status: 'estimated' | 'needs_planning'
  missing_gates: ('artifact' | 'boundary' | 'verification' | 'dependency' | 'independent_review_orchestration')[]
  size_class: 'XS' | 'S' | 'M' | 'L' | 'XL' | null
  rationale: string[]
  estimated_input_tokens: TokenEstimateBand | null
  estimated_output_tokens: TokenEstimateBand | null
  soft_deadline_seconds: number | null
  hard_deadline_seconds: number | null
  max_turns: number | null
  max_attempts: number | null
  verification_timeout_seconds: number | null
  progress_extension_seconds: number | null
  total_token_hard_cap: number | null
  reserved_tokens: number | null
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
  token_budget: number
  tokens_used: number
  last_evidence_hash: string | null
  last_error: string | null
  created_at: string
  updated_at: string
  command: Record<string, unknown>
  verification: { kind: string; path?: string; contains?: string; expected?: number }[]
  sizing: Record<string, unknown>
  execution_budget: Record<string, unknown> | null
  manual_override: boolean
  override_reason: string | null
  goal_id?: string | null
  parent_task_id?: string | null
  depends_on?: string[] | null
  work_item_kind?: string | null
  ordinal?: number | null
  blocked_reason?: string | null
  handoff?: Record<string, unknown> | null
}

export type Goal = {
  id: string
  title: string
  objective: string
  project_id: string
  provider: string
  status: string
  revision: number
  last_evidence_hash: string | null
  plan: Record<string, unknown>
  sizing_inputs: Record<string, unknown> | null
  parent_task_id: string | null
  created_at: string
  updated_at: string
  work_items: Record<string, unknown>[]
}

export type ButlerIntake = {
  id: string
  project_id: string
  source: 'structured' | 'natural_language'
  instruction: string
  input: Record<string, unknown>
  status: 'clarifying' | 'awaiting_confirmation' | 'dispatching' | 'dispatched' | 'interrupted' | 'failed'
  deterministic_size: 'small' | 'medium' | 'large'
  assessed_size: 'small' | 'medium' | 'large'
  confidence: number
  revision: number
  current_question_id: string | null
  proposal: Record<string, unknown> | null
  proposal_hash: string | null
  confirmed_proposal_hash: string | null
  selected_provider: string | null
  goal_id: string | null
  questions: {
    id: string
    question: string
    answer: string | null
    answered_at: string | null
  }[]
}

export type WorkerHelp = {
  id: string
  project_id: string
  goal_id: string | null
  task_id: string
  worker_id: string | null
  category: string
  severity: 'normal' | 'blocking' | 'extreme'
  status: 'open' | 'answered' | 'owner_escalated' | 'interrupted'
  question: string
  checkpoint: Record<string, unknown>
  revision: number
  created_at: string
  updated_at: string
  resolved_at: string | null
  replies: {
    id: string
    revision: number
    sender: 'butler' | 'owner' | 'system'
    content: string
    bounded_context: Record<string, unknown>
    created_at: string
  }[]
}

export type AggregateControlPlane = {
  aggregate_type: 'task' | 'goal'
  aggregate_id: string
  canonical_state: {
    status: string
    revision: number
    evidence_hash: string | null
    updated_at: string
  }
  next_action: { kind: string; label: string; requires_owner: boolean }
  session_identity: 'project_id+role_id+task_id'
  provider_sessions: {
    id: string
    project_id: string
    role_id: string
    task_id: string
    worker_id: string | null
    provider: string
    session_generation: number
    external_session_id: string | null
    state: string
    revision: number
    archive_reason: string | null
    updated_at: string
  }[]
  help_requests: WorkerHelp[]
  lineage: {
    sequence: number
    revision: number
    actor_type: string
    actor_id: string | null
    reason: string
    previous_state: Record<string, unknown>
    new_state: Record<string, unknown>
    previous_evidence_hash: string | null
    new_evidence_hash: string | null
    created_at: string
  }[]
  deletion: {
    status: 'deletable' | 'stop_required' | 'stopping' | 'deleted'
    eligible: boolean
    next_action: string
    expected_revision: number | null
    active_task_ids: string[]
    pending_host_jobs: string[]
    cascade_task_count: number
    artifact_files_deleted: false
    usage_retention: 'anonymous'
  }
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
  last_context_guard_decision: string | null
  last_context_guard_reason: string | null
  last_guard_estimated_new_tokens: number
  last_guard_carry_in_cached_tokens: number
  last_guard_hard_cap: number
  last_guard_relation: string
}
export type Project = {
  id: string
  name: string
  path: string
  host_path: string | null
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
  task_default_token_budget: number
  global_daily_token_budget: number
  max_same_failure: number
  max_no_progress: number
  session_no_progress_rotation_threshold: number
  context_max_bytes: number
  checkpoint_max_bytes: number
  handoff_max_bytes: number
  observation_tail_lines: number
  observation_max_bytes: number
  rotation_max_bytes: number
}
export type RuntimeSettings = { revision: number; values: RuntimeSettingsValues; updated_at: string | null }
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
  control_gate: 'disabled'
  control_tokens: number
  projects: { project_id: string | null; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; tokens: number }[]
  tasks: { task_id: string; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; tokens: number }[]
  attribution: { project_id: string | null; goal_id: string | null; goal_id_hash: string | null; role_id: string | null; task_id: string | null; task_id_hash: string | null; worker_id: string | null; provider: string; physical_session_id: string | null; session_generation: number | null; input_tokens: number; cached_input_tokens: number; output_tokens: number; calls: number }[]
  calls: { call_id: string; call_kind: string; status: 'prepared' | 'completed' | 'failed'; project_id: string | null; goal_id: string | null; goal_id_hash: string | null; role_id: string | null; task_id: string | null; task_id_hash: string | null; attempt_id: string | null; episode_id: string | null; worker_id: string | null; host_job_id: string | null; provider: string; physical_session_id: string | null; session_generation: number | null; snapshot_kind: 'per_call' | 'cumulative'; previous_call_id: string | null; input_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; output_tokens: number; normalized_input_tokens: number; normalized_cached_input_tokens: number; normalized_output_tokens: number; attribution_granularity: string; value_classification: string; rotation_reason: string | null; created_at: string; settled_at: string | null }[]
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
      body: JSON.stringify({ provider, project_id: projectId }),
    }),
  usage: () => request<Usage>('/api/usage'),
  settings: () => request<RuntimeSettings>('/api/settings'),
  updateSettings: (settings: RuntimeSettings) =>
    request<RuntimeSettings>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: settings.revision, values: settings.values }),
    }),
  schedulerStatus: () => request<SchedulerStatus>('/api/scheduler/status'),
  schedulerTick: () => request<Record<string, unknown>>('/api/scheduler/tick', { method: 'POST' }),
  projects: () => request<Project[]>('/api/projects'),
  createProject: (payload: { name: string; path: string; host_path?: string | null }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  releaseProject: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/release`, { method: 'POST' }),
  rebindWorker: (workerId: string, provider: string) =>
    request<Worker>(`/api/workers/${workerId}/rebind`, {
      method: 'POST', body: JSON.stringify({ provider, reason: 'operator_rebind' }),
    }),
  tasks: () => request<Task[]>('/api/tasks'),
  estimateTask: (payload: TaskSizingInputs) =>
    request<TaskSizingEstimate>('/api/tasks/estimate', {
      method: 'POST', body: JSON.stringify(payload),
    }),
  taskEvents: (taskId: string) => request<TaskEvent[]>(`/api/tasks/${taskId}/events`),
  taskArtifacts: (taskId: string) =>
    request<TaskArtifact[]>(`/api/tasks/${taskId}/artifacts`),
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
  createButlerIntake: (payload: Record<string, unknown>) =>
    request<ButlerIntake>('/api/butler/intakes', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(payload),
    }),
  answerButlerIntake: (
    intake: ButlerIntake, answer: string, confidence: number,
  ) => request<ButlerIntake>(`/api/butler/intakes/${intake.id}/answers`, {
    method: 'POST',
    idempotencyKey: crypto.randomUUID(),
    body: JSON.stringify({
      expected_revision: intake.revision,
      answer,
      confidence,
    }),
  }),
  confirmButlerIntake: (intake: ButlerIntake, approved: boolean) =>
    request<ButlerIntake>(`/api/butler/intakes/${intake.id}/confirm`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({
        expected_revision: intake.revision,
        proposal_hash: intake.proposal_hash,
        approved,
        reason: approved ? 'owner_confirmed_exact_proposal' : 'owner_rejected_proposal',
      }),
    }),
  helpRequests: (projectId?: string) => request<WorkerHelp[]>(
    `/api/butler/help${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`,
  ),
  replyHelp: (
    help: WorkerHelp, sender: 'butler' | 'owner', content: string, escalate: boolean,
  ) => request<WorkerHelp>(`/api/butler/help/${help.id}/replies`, {
    method: 'POST',
    idempotencyKey: crypto.randomUUID(),
    body: JSON.stringify({
      expected_revision: help.revision,
      sender,
      content,
      bounded_context: { task_id: help.task_id },
      escalate,
    }),
  }),
  aggregateControlPlane: (aggregateType: 'task' | 'goal', aggregateId: string) =>
    request<AggregateControlPlane>(
      `/api/aggregates/${aggregateType}/${aggregateId}/control-plane`,
    ),
  getGoal: (goalId: string) => request<Goal>(`/api/goals/${goalId}`),
  deleteGoal: (goal: Goal) =>
    request<Record<string, unknown>>(`/api/goals/${goal.id}`, {
      method: 'DELETE',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({
        expected_revision: goal.revision,
        reason: 'owner_delete',
      }),
    }),
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
  deleteTask: (task: Task) =>
    request<Record<string, unknown>>(`/api/tasks/${task.id}`, {
      method: 'DELETE',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({
        expected_revision: task.revision,
        reason: 'owner_delete',
      }),
    }),
}
