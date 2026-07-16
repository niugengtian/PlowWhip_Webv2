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
