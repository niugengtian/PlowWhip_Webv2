export type TaskStatus =
  | 'ready'
  | 'running'
  | 'verifying'
  | 'completed'
  | 'terminal_failed'
  | 'needs_human'
  | 'cancelled'

export type Task = {
  id: string
  title: string
  objective: string
  project_path: string
  project_id: string | null
  role_id: string | null
  worker_id: string | null
  resource_key: string | null
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
}

export type Role = { id: string; kind: string; status: string }
export type Worker = {
  id: string
  role_id: string
  role: string
  provider: string
  session_id: string
  session_generation: number
  status: string
  active_task_id: string | null
  released_at: string | null
}
export type Project = {
  id: string
  name: string
  path: string
  status: string
  created_at: string
  roles: Role[]
  workers: Worker[]
}

export type RuntimeSettingsValues = {
  scheduler_interval_seconds: number
  scheduler_lease_seconds: number
  max_parallel_workers: number
  auto_dispatch: boolean
  system_scheduler_authorized: boolean
  task_default_token_budget: number
  global_daily_token_budget: number
  max_same_failure: number
  max_no_progress: number
  context_max_bytes: number
  rotation_max_bytes: number
}
export type RuntimeSettings = { revision: number; values: RuntimeSettingsValues; updated_at: string | null }
export type SchedulerStatus = {
  runtime: { fencing_token: number; last_tick_at: string | null; last_result: Record<string, unknown> | null; last_error: string | null }
  system: { os: string; backend: string; supported: boolean; target: string | null; reason: string | null }
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
  output_tokens: number
  total_tokens: number
  control_tokens: number
  projects: { project_id: string | null; tokens: number }[]
  tasks: { task_id: string; tokens: number }[]
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
  convention: (scope: string, scopeId: string) => request<Convention>(`/api/conventions/${scope}/${scopeId}`),
  updateConvention: (convention: Convention) =>
    request<Convention>('/api/conventions', {
      method: 'PUT',
      body: JSON.stringify({ ...convention, expected_revision: convention.revision }),
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
  schedulerInstall: () => request<Record<string, unknown>>('/api/scheduler/install', { method: 'POST' }),
  projects: () => request<Project[]>('/api/projects'),
  createProject: (payload: { name: string; path: string }) =>
    request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  releaseProject: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/release`, { method: 'POST' }),
  tasks: () => request<Task[]>('/api/tasks'),
  taskEvents: (taskId: string) => request<TaskEvent[]>(`/api/tasks/${taskId}/events`),
  createTask: (payload: Record<string, unknown>) =>
    request<Task>('/api/tasks', {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(payload),
    }),
  driveTask: (task: Task) =>
    request<Task>(`/api/tasks/${task.id}/drive`, {
      method: 'POST',
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify({ expected_revision: task.revision }),
    }),
}
