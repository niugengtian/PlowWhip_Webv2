import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

const project = {
  id: 'project-1', name: 'Console', path: '/projects/console', host_path: '/projects/console',
  status: 'active', created_at: '2026-07-17T00:00:00Z', roles: [], workers: [],
}
const provider = {
  name: 'cursor', display_name: 'Cursor', status: 'available', model_invoked: false,
  capabilities: [], reason: null, adapter: 'cursor', transport: 'host-bridge', executable: 'cursor-agent',
  enabled: true, credential_env: null, revision: 0, last_probed_at: null,
  readiness: {
    installed: true, installed_at: '2026-07-17T00:01:00Z', installed_reason: 'binary found',
    cli_probe: 'ok', cli_probe_at: '2026-07-17T00:02:00Z', cli_probe_reason: 'version ok',
    session_resume_ready: true, session_resume_checked_at: '2026-07-17T00:03:00Z', session_resume_reason: 'resume probe passed',
    recent_execution_health: 'healthy', recent_execution_checked_at: '2026-07-17T00:04:00Z', recent_execution_reason: 'last run passed',
  },
}
const estimate = {
  status: 'estimated', missing_gates: [], size_class: 'M',
  rationale: ['estimated_verification_seconds=180 (+6)', 'complexity_score=68', 'size_class=M'],
  soft_deadline_seconds: 480, hard_deadline_seconds: 1200, max_turns: 40, max_attempts: 3,
  verification_timeout_seconds: 300, progress_extension_seconds: 120,
  model_invoked: false, bootstrap_version: 'sprint10-v1',
}
let estimateFails = false
let estimatePayloads: Record<string, unknown>[] = []
let createPayloads: Record<string, unknown>[] = []
let listedTasks: Record<string, unknown>[] = []
let listedGoals: Record<string, unknown>[] = []
let listedProviders: Record<string, unknown>[] = []
let listedProjects: Record<string, unknown>[] = []
let deletionAllowed = false
let deleteRequests = 0

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onmessage: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, () => void> = {}
  close = vi.fn()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: () => void) {
    this.listeners[type] = listener
  }
}

function taskWithQualityProfile(qualityProfile: string, index: number) {
  return {
    id: `task-${index}`, title: `legacy-${qualityProfile}`, objective: '兼容旧任务',
    project_path: project.path, project_id: project.id, role_id: null, worker_id: null,
    resource_key: null, network_requirement: 'none', same_failure_count: 0, no_progress_count: 0,
    last_failure_fingerprint: null, next_eligible_at: null, provider: 'cursor', quality_profile: qualityProfile,
    status: 'completed', revision: 0, max_attempts: 3, attempts_used: 1, tokens_used: 100,
    last_evidence_hash: null, last_error: null, created_at: '2026-07-17T00:00:00Z', updated_at: '2026-07-17T00:00:00Z',
    command: {}, verification: [], sizing: estimate, execution_policy: {},
    spec_revision: 1,
    spec: {
      objective: '兼容旧任务', scope: ['backend'], acceptance: ['verified'],
      verification: [], artifacts: ['result.txt'], constraints: ['bounded'],
      deadline: { hard_seconds: 1200 },
    },
  }
}

beforeEach(() => {
  estimateFails = false
  estimatePayloads = []
  createPayloads = []
  listedTasks = []
  listedGoals = []
  listedProviders = [provider]
  listedProjects = [project]
  deletionAllowed = false
  deleteRequests = 0
  FakeEventSource.instances = []
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      const settings = {
        revision: 0,
        updated_at: null,
        values: {
          scheduler_interval_seconds: 30,
          scheduler_lease_seconds: 90,
          cron_enabled: true,
          cron_expression: '*/1 * * * *',
          cron_timezone: 'Asia/Shanghai',
          cron_misfire_policy: 'catch_up_once',
          max_parallel_workers: 4,
          auto_dispatch: true,
          max_same_failure: 3,
          max_no_progress: 3,
          context_max_bytes: 32768,
          rotation_max_bytes: 262144,
          checkpoint_max_bytes: 4096,
          handoff_max_bytes: 2048,
          observation_tail_lines: 20,
          observation_max_bytes: 8192,
        },
      }
      const scheduler = {
        runtime: { fencing_token: 0, last_tick_at: null, last_result: null, last_error: null, runner_id: null, runner_started_at: null, runner_heartbeat_at: null, runner_stopped_at: null, runner_error: null },
        engine: { backend: 'embedded-cron', active: true, managed_by: 'docker', data_dir: '/data' },
        schedule: { enabled: true, expression: '*/1 * * * *', timezone: 'Asia/Shanghai', misfire_policy: 'catch_up_once', next_run_at: null },
        authorization_required: false,
        model_invoked: false,
      }
      if (path === '/api/tasks/estimate') {
        const payload = JSON.parse(String(init?.body)) as Record<string, unknown>
        estimatePayloads.push(payload)
        if (estimateFails) return { ok: false, status: 503, json: async () => ({ detail: '预判服务暂不可用' }) }
        const gateKeys = ['gate_artifact', 'gate_boundary', 'gate_verification', 'gate_dependency']
        const missing = gateKeys.filter((key) => !payload[key]).map((key) => key.replace('gate_', ''))
        return { ok: true, json: async () => missing.length ? ({
          ...estimate, status: 'needs_planning', missing_gates: missing, size_class: null,
          soft_deadline_seconds: null, hard_deadline_seconds: null, max_turns: null,
          max_attempts: null, verification_timeout_seconds: null, progress_extension_seconds: null,
        }) : estimate }
      }
      if (path === '/api/tasks' && init?.method === 'POST') {
        const payload = JSON.parse(String(init.body)) as Record<string, unknown>
        createPayloads.push(payload)
        return { ok: true, json: async () => ({
          id: 'task-1', title: payload.title, objective: payload.objective,
          project_path: project.path, project_id: project.id, role_id: null, worker_id: null,
          resource_key: null, network_requirement: 'none', same_failure_count: 0, no_progress_count: 0,
          last_failure_fingerprint: null, next_eligible_at: null, provider: 'cursor', quality_profile: 'deterministic',
          status: 'ready', revision: 0, max_attempts: 3, attempts_used: 0, tokens_used: 0,
          last_evidence_hash: null, last_error: null, created_at: '2026-07-17T00:00:00Z', updated_at: '2026-07-17T00:00:00Z',
          command: {}, verification: [], sizing: estimate, execution_policy: {},
        }) }
      }
      if (path === '/api/workers/worker-1') {
        return { ok: true, json: async () => ({
          worker: {
            id: 'worker-1', role_id: 'role-ui', role: 'ui', provider: 'cursor',
            session_id: 'session-local', external_session_id: 'cursor-session-7',
            session_generation: 18, status: 'busy', active_task_id: 'child-1',
            last_seen_at: '2026-07-17T00:05:00Z', last_error: null, released_at: null,
          },
          task: {
            id: 'child-1', title: 'UI child', objective: 'real work', status: 'running',
            revision: 7, spec_revision: 2, provider: 'cursor',
            spec: { objective: 'real work', scope: ['ui'], acceptance: ['verified'], verification: [], artifacts: [], constraints: [], deadline: { hard_seconds: 1200 } },
          },
          host_job: {
            job_id: 'host-job-1', task_id: 'child-1', status: 'running',
            dispatch_outcome: 'accepted', reconciliation_deadline_at: '2026-07-17T00:06:00Z',
            external_session_id: 'cursor-session-7', heartbeat_at: '2026-07-17T00:05:00Z',
          },
          episode: { id: 'episode-1' },
          ownership: { session_id: 'session-local', external_session_id: 'cursor-session-7', session_generation: 18 },
        }) }
      }
      if (path.startsWith('/api/workers/worker-1/stream')) {
        return { ok: true, json: async () => ({
          worker_id: 'worker-1', job_id: 'host-job-1', next_cursor: '9:128:0', has_more: false,
          items: [
            { kind: 'status', ref: 'task-event:9', text: 'task.started', state_revision: 7 },
            { kind: 'stdout', refs: ['host-job-1/stdout.000000.log'], text: 'real progress', offset: 0, next_offset: 128 },
          ],
        }) }
      }
      if (path.endsWith('/deletion-eligibility')) {
        return { ok: true, json: async () => ({ deletable: deletionAllowed, reason: deletionAllowed ? null : 'task has execution evidence' }) }
      }
      if (path.startsWith('/api/tasks/') && init?.method === 'DELETE') {
        deleteRequests += 1
        const payload = JSON.parse(String(init.body)) as { expected_revision: number; reason: string }
        return { ok: true, json: async () => ({
          task_id: path.split('/').at(-1), title: 'deletable-task', reason: payload.reason,
          deleted_revision: payload.expected_revision, idempotency_key: 'delete-ui',
          deleted_at: '2026-07-18T00:00:00Z',
        }) }
      }
      if (path.startsWith('/api/usage/daily/')) {
        const day = path.split('/').at(-1)
        return {
          ok: true,
          json: async () => ({
            date: day,
            timezone: 'Asia/Shanghai',
            input_tokens: 0,
            cached_input_tokens: 0,
            uncached_input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            calls: 0,
            total_formula: 'input_tokens + output_tokens',
            cached_input_tokens_in_total: true,
            projects: [],
            tasks: [],
          }),
        }
      }
      if (path.startsWith('/api/usage/daily')) {
        return {
          ok: true,
          json: async () => ({
            timezone: 'Asia/Shanghai',
            from: '2026-07-06',
            to: '2026-07-19',
            days: Array.from({ length: 14 }, (_, index) => {
              const day = String(6 + index).padStart(2, '0')
              return {
                date: `2026-07-${day}`,
                input_tokens: 0,
                cached_input_tokens: 0,
                uncached_input_tokens: 0,
                output_tokens: 0,
                total_tokens: 0,
                calls: 0,
              }
            }),
            totals: {
              input_tokens: 0,
              cached_input_tokens: 0,
              uncached_input_tokens: 0,
              output_tokens: 0,
              total_tokens: 0,
              calls: 0,
            },
            total_formula: 'input_tokens + output_tokens',
            cached_input_tokens_in_total: true,
          }),
        }
      }
      return {
        ok: true,
        json: async () => path === '/api/projects' ? listedProjects : path === '/api/providers' ? listedProviders : path === '/api/goals' ? listedGoals : path === '/api/tasks' ? listedTasks : path.endsWith('/events') || path.endsWith('/artifacts') || ['/api/outbox', '/api/audit', '/api/permissions'].includes(path) ? [] : path === '/api/settings' ? settings : path === '/api/scheduler/status' ? scheduler : path === '/api/system/health' ? ({ connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }) : path === '/api/usage' ? ({ input_tokens: 0, output_tokens: 0, total_tokens: 0, control_tokens: 0, today: { date: '2026-07-19', timezone: 'Asia/Shanghai', input_tokens: 0, cached_input_tokens: 0, uncached_input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0 }, projects: [], tasks: [] }) : path === '/api/conventions/global/global' ? ({ scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }) : ({
          status: 'ok',
          version: '0.1.0',
          database: { status: 'ok', journal_mode: 'wal', migration_count: 8 },
        }),
      }
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function openTaskDrawer() {
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '诊断任务' }))
  await screen.findByRole('heading', { name: '新建任务' })
}

test('shows the approved product priority', () => {
  render(<App />)
  expect(
    screen.getByText('保障质量并消除无价值循环，同时保留高价值上下文。'),
  ).toBeInTheDocument()
})

test('shows the immutable TaskSpec revision and fields', async () => {
  listedTasks = [taskWithQualityProfile('deterministic', 7)]
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '任务' }))
  expect(await screen.findByText('TaskSpec r1 · cursor · 已完成')).toBeInTheDocument()
  fireEvent.click(screen.getByText('legacy-deterministic').closest('button')!)
  expect(await screen.findByText('backend')).toBeInTheDocument()
  expect(screen.getByText('1200s')).toBeInTheDocument()
})

test('requires explicit permanent-delete confirmation and honors cancel/success', async () => {
  listedTasks = [{
    ...taskWithQualityProfile('deterministic', 8),
    title: 'deletable-task', status: 'ready', attempts_used: 0, tokens_used: 0,
  }]
  deletionAllowed = true
  const confirm = vi.spyOn(window, 'confirm')
    .mockReturnValueOnce(false)
    .mockReturnValueOnce(true)
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '任务' }))
  fireEvent.click(await screen.findByText('deletable-task'))
  const button = await screen.findByRole('button', { name: '永久删除' })

  fireEvent.click(button)
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('永久删除、不可恢复'))
  expect(deleteRequests).toBe(0)

  fireEvent.click(button)
  expect(await screen.findByText('任务已永久删除')).toBeInTheDocument()
  expect(deleteRequests).toBe(1)
})

test('uses EventSource refresh and closes it on unmount', async () => {
  vi.stubGlobal('EventSource', FakeEventSource)
  const { unmount } = render(<App />)
  await screen.findByText('Console')
  const source = FakeEventSource.instances[0]
  expect(source.url).toBe('/api/events/stream')
  const before = vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === '/api/tasks').length

  source.listeners['aggregate.updated']()
  await vi.waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === '/api/tasks').length).toBeGreaterThan(before)
  })

  unmount()
  expect(source.close).toHaveBeenCalledOnce()
})

test('worker click shows canonical host identity and never treats heartbeat as completion', async () => {
  const interval = vi.spyOn(window, 'setInterval')
  listedProjects = [{
    ...project,
    workers: [{
      id: 'worker-1', role_id: 'role-ui', role: 'ui', provider: 'cursor',
      session_id: 'session-local', external_session_id: 'cursor-session-7',
      session_generation: 18, status: 'busy', active_task_id: 'child-1',
      last_seen_at: '2026-07-17T00:05:00Z', last_error: null, released_at: null,
      last_input_tokens: 10, last_cached_input_tokens: 2, last_uncached_input_tokens: 8,
      last_output_tokens: 3, last_context_pressure_tokens: 10,
      last_context_pressure_reason: 'usage_observed', last_context_session_generation: 18,
      last_attribution_granularity: 'turn', last_value_classification: 'unknown',
    }],
  }]
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Worker' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看 Task Session' }))

  expect(await screen.findByText('host-job-1')).toBeInTheDocument()
  expect(screen.getByText(/accepted · deadline/)).toBeInTheDocument()
  expect(screen.getByText(/仅表示存活，不代表完成/)).toBeInTheDocument()
  expect(screen.getByText('real progress')).toBeInTheDocument()
  expect(screen.getByText('running')).toBeInTheDocument()
  expect(screen.queryByText('已完成')).not.toBeInTheDocument()
  await vi.waitFor(() => {
    expect(interval).toHaveBeenCalledWith(expect.any(Function), 1500)
  })
})

test('falls back to one low-frequency poller without EventSource and clears it', () => {
  vi.useFakeTimers()
  vi.stubGlobal('EventSource', undefined)
  const { unmount } = render(<App />)

  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

test('falls back after an EventSource error and cleans both resources', () => {
  vi.useFakeTimers()
  vi.stubGlobal('EventSource', FakeEventSource)
  const { unmount } = render(<App />)
  const source = FakeEventSource.instances[0]

  source.onerror?.()
  expect(source.close).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

test.each([
  ['活跃项目', '项目注册表'],
  ['在线 Worker', 'Worker 状态'],
  ['可用 Provider', 'Worker Provider'],
  ['今日 Token', '消费明细'],
])('opens the %s detail page from the dashboard metric', (metric, heading) => {
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: `查看${metric}详情` }))
  expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
})

test('shows Host Bridge commands for macOS, Linux and Windows', () => {
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '查看可用 Provider详情' }))
  fireEvent.click(screen.getByText('Host Bridge 启动命令（macOS / Linux / Windows）'))
  expect(screen.getByText('macOS')).toBeInTheDocument()
  expect(screen.getByText('Linux')).toBeInTheDocument()
  expect(screen.getByText('Windows PowerShell')).toBeInTheDocument()
  expect(screen.getByText(/创建任务和每次派发都会由后端重新探测/)).toBeInTheDocument()
})

test('keeps task and goal selection mutually exclusive and shows layered goal runtime metadata', async () => {
  listedTasks = [{
    ...taskWithQualityProfile('deterministic', 1),
    id: 'child-1', title: 'UI child', worker_id: 'worker-1', work_item_kind: 'ui',
    provider: 'cursor', depends_on: ['child-0'], blocked_reason: 'waiting for backend',
    status: 'running', attempts_used: 1, max_attempts: 3, tokens_used: 184_000,
    input_tokens: 120_000, cached_input_tokens: 96_000,
    output_tokens: 64_000, total_tokens: 184_000,
    output_ref: 'sessions/ui/current.jsonl', output_segments: 3, output_bytes: 8192,
    output_offset: 4096, last_evidence_hash: null,
  }]
  listedGoals = [{
    id: 'goal-1', title: 'Release Goal', objective: 'ship verified UI', project_id: project.id,
    provider: 'cursor', status: 'running',
    spec_revision: 1,
    spec: {
      objective: 'ship verified UI',
      scope: ['frontend'],
      acceptance: ['artifact:result.txt:contains:quality-pass'],
      verification: [{ kind: 'file_exists', path: 'result.txt' }],
      artifacts: ['result.txt'],
      constraints: [],
      deadline: { hard_seconds: 1200 },
    },
    plan: { status: 'planned', model_invoked: false, model_pm_implemented: false },
    sizing_inputs: { size_class: 'M', risk_level: 'medium' }, parent_task_id: 'parent-1',
    created_at: '2026-07-17T00:00:00Z', updated_at: '2026-07-17T00:00:00Z',
    work_items: [{
      id: 'child-1', ordinal: 2, title: 'UI child', work_item_kind: 'ui', provider: 'cursor',
      depends_on: ['child-0'], status: 'running', blocked_reason: 'waiting for backend',
      sizing: { size_class: 'M', status: 'estimated' },
      execution_policy: { hard_deadline_seconds: 1200 },
      verification: [{ kind: 'file_exists', path: 'result.txt' }],
    }],
  }]
  listedProjects = [{
    ...project,
    workers: [{
      id: 'worker-1', role_id: 'role-ui', role: 'ui', provider: 'cursor',
      session_id: 'session-local', external_session_id: 'cursor-session-7', session_generation: 18,
      status: 'busy', active_task_id: 'child-1', last_seen_at: '2026-07-17T00:05:00Z',
      last_error: null, released_at: null, rotation_reason: 'context_threshold',
      last_input_tokens: 120_000, last_cached_input_tokens: 96_000,
      last_output_tokens: 64_000, last_context_pressure_tokens: 120_000,
      last_context_pressure_reason: 'usage_observed', last_context_session_generation: 18,
    }],
  }]
  render(<App />)

  fireEvent.click(await screen.findByRole('button', { name: /UI child/ }))
  expect(screen.getByText('确定性验证')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Release Goal/ }))

  expect(screen.getByRole('heading', { name: 'Release Goal' })).toBeInTheDocument()
  expect(screen.getByText('GoalSpec / Butler aggregate')).toBeInTheDocument()
  for (const label of ['角色 / Provider', '依赖', '阻塞原因', 'Task session', 'Generation', 'Session scope', 'Replacement reason', 'Last context pressure', 'Pressure trigger', 'Sizing', 'Execution', 'Attempt / progress', 'Verification', 'Output ref', 'Segments / bytes / offset', 'Cached 计入 Total', 'Token control']) {
    expect(screen.getByText(label)).toBeInTheDocument()
  }
  expect(screen.getByText('cursor-session-7')).toBeInTheDocument()
  expect(screen.getByText('context_threshold')).toBeInTheDocument()
  expect(screen.getByText('usage_observed')).toBeInTheDocument()
  expect(screen.getByText('sessions/ui/current.jsonl')).toBeInTheDocument()
  expect(screen.getByText('3 / 8192 / 4096')).toBeInTheDocument()
  expect(screen.getByText('是，已包含在 Input 中，不重复相加')).toBeInTheDocument()
  expect(screen.getByText('仅计量；不参与准入、调度、熔断或终态')).toBeInTheDocument()
  expect(screen.getByText(/turn \/ unknown/)).toBeInTheDocument()
  expect(screen.getByText(/Uncached 不等于新工作或有价值/)).toBeInTheDocument()
})

test('ignores stale task details after the operator selects another task', async () => {
  listedTasks = [
    taskWithQualityProfile('fast', 1),
    taskWithQualityProfile('balanced', 2),
  ]
  let resolveOldEvents!: (value: Record<string, unknown>[]) => void
  let resolveOldArtifacts!: (value: Record<string, unknown>[]) => void
  const oldEvents = new Promise<Record<string, unknown>[]>((resolve) => {
    resolveOldEvents = resolve
  })
  const oldArtifacts = new Promise<Record<string, unknown>[]>((resolve) => {
    resolveOldArtifacts = resolve
  })
  const defaultFetch = vi.mocked(fetch)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/tasks/task-1/events') {
        return { ok: true, json: async () => oldEvents }
      }
      if (path === '/api/tasks/task-1/artifacts') {
        return { ok: true, json: async () => oldArtifacts }
      }
      if (path === '/api/tasks/task-2/events') {
        return {
          ok: true,
          json: async () => [{
            sequence: 22, event_type: 'task.two.event', payload: {},
            state_revision: 0, created_at: '2026-07-17T00:02:00Z',
          }],
        }
      }
      if (path === '/api/tasks/task-2/artifacts') {
        return {
          ok: true,
          json: async () => [{
            relative_path: 'task-two.txt', host_path: '/projects/console/task-two.txt',
            exists: true, bytes: 8, sha256: 'b'.repeat(64), modified_at_ns: 2,
            actions: ['finder'],
          }],
        }
      }
      return defaultFetch(input, init)
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /legacy-fast/ }))
  await vi.waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(
      ([input]) => String(input) === '/api/tasks/task-1/events',
    )).toBe(true)
  })
  fireEvent.click(screen.getByRole('button', { name: /legacy-balanced/ }))

  expect(await screen.findByText('task.two.event')).toBeInTheDocument()
  expect(await screen.findByText('task-two.txt')).toBeInTheDocument()

  await act(async () => {
    resolveOldEvents([{
      sequence: 11, event_type: 'task.one.stale', payload: {},
      state_revision: 0, created_at: '2026-07-17T00:01:00Z',
    }])
    resolveOldArtifacts([{
      relative_path: 'task-one-stale.txt',
      host_path: '/projects/console/task-one-stale.txt',
      exists: true, bytes: 8, sha256: 'a'.repeat(64), modified_at_ns: 1,
      actions: ['finder'],
    }])
    await Promise.resolve()
  })

  expect(screen.queryByText('task.one.stale')).not.toBeInTheDocument()
  expect(screen.queryByText('task-one-stale.txt')).not.toBeInTheDocument()
  expect(screen.getByText('task.two.event')).toBeInTheDocument()
  expect(screen.getByText('task-two.txt')).toBeInTheDocument()
})

test('shows provider readiness layers and does not hide unhealthy execution behind a successful probe', async () => {
  listedProviders = [{
    ...provider,
    name: 'deepseek', display_name: 'DeepSeek', adapter: 'json-worker',
    credential_env: 'DEEPSEEK_API_KEY_SLOTS', credential_slot_count: 3,
    status: 'available',
    readiness: {
      ...provider.readiness,
      recent_execution_health: 'unhealthy',
      recent_execution_checked_at: '2026-07-17T00:09:00Z',
      recent_execution_reason: 'last execution failed',
    },
  }]
  render(<App />)
  await screen.findByText('Console')
  fireEvent.click(screen.getByRole('button', { name: '查看可用 Provider详情' }))

  expect(screen.getByText('未完全就绪')).toBeInTheDocument()
  expect(screen.getByText('installed / cli_probe')).toBeInTheDocument()
  expect(screen.getByText('session_resume_ready')).toBeInTheDocument()
  expect(screen.getByText('recent_execution_health')).toBeInTheDocument()
  expect(screen.queryByText('healthy')).not.toBeInTheDocument()
  expect(screen.getByText('unhealthy')).toBeInTheDocument()
  expect(screen.getByText(/2026-07-17T00:09:00Z · last execution failed/)).toBeInTheDocument()
  expect(screen.getByText('env DEEPSEEK_API_KEY_SLOTS · slots 3')).toBeInTheDocument()
})

test('opens task creation and completes a 0 Token preflight with execution facts', async () => {
  await openTaskDrawer()
  expect(screen.queryByLabelText('质量档位')).not.toBeInTheDocument()
  expect(screen.queryByText('快速')).not.toBeInTheDocument()
  expect(screen.queryByText('均衡')).not.toBeInTheDocument()
  expect(screen.queryByText('严格')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))

  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()
  expect(screen.getByText('480s')).toBeInTheDocument()
  expect(screen.getByText('1200s')).toBeInTheDocument()
  expect(screen.getByText('3')).toBeInTheDocument()
  expect(screen.getByText('300s')).toBeInTheDocument()
  expect(screen.getByText(/未调用模型 · 服务端规则/)).toBeInTheDocument()
  expect(estimatePayloads[0]).toMatchObject({ layers_touched: 1, components_touched: 3, estimated_files_changed: 4, risk_level: 'medium' })
})

test('blocks enqueue when a gate is missing and the server requires planning', async () => {
  await openTaskDrawer()
  fireEvent.click(screen.getByLabelText('产物明确'))
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))

  expect(await screen.findByText('暂不可入队：先补齐计划')).toBeInTheDocument()
  expect(screen.getByText(/可验证产物/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '检查 Provider 并加入任务队列' })).toBeDisabled()
})

test('invalidates a successful preflight as soon as a sizing input changes', async () => {
  await openTaskDrawer()
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('涉及层数'), { target: { value: '2' } })
  expect(screen.queryByText('服务端 Tier M')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '检查 Provider 并加入任务队列' })).toBeDisabled()
})

test('sends the exact preflight sizing inputs when creating a task', async () => {
  await openTaskDrawer()
  fireEvent.change(screen.getByLabelText('标题'), { target: { value: '接入预判' } })
  fireEvent.change(screen.getByLabelText('目标'), { target: { value: '服务端重算后入队' } })
  fireEvent.change(screen.getByLabelText('项目'), { target: { value: project.id } })
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  await screen.findByText('服务端 Tier M')
  fireEvent.click(screen.getByRole('button', { name: '检查 Provider 并加入任务队列' }))

  expect(await screen.findByText('诊断任务已加入队列')).toBeInTheDocument()
  expect(createPayloads).toHaveLength(1)
  expect(createPayloads[0].quality_profile).toBe('deterministic')
  expect(createPayloads[0].sizing_inputs).toEqual(estimatePayloads[0])
  expect(createPayloads[0]).not.toHaveProperty('token_budget')
  expect(createPayloads[0].command).not.toHaveProperty('timeout_seconds')
})

test('independent review preference does not create a reviewer dispatch gate', async () => {
  await openTaskDrawer()
  expect(screen.getByText(/任务自己的 verification Gate 才是终态依据/)).toBeInTheDocument()
  fireEvent.click(screen.getByLabelText('附加独立复审偏好（不阻塞）'))
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))

  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()
  expect(screen.queryByText('暂不可入队：先补齐计划')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '检查 Provider 并加入任务队列' })).toBeEnabled()
  expect(createPayloads).toHaveLength(0)
})

test('clears the successful preflight when the estimate API later fails', async () => {
  await openTaskDrawer()
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()

  estimateFails = true
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  expect(await screen.findByText('预判服务暂不可用')).toBeInTheDocument()
  expect(screen.queryByText('服务端 Tier M')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '检查 Provider 并加入任务队列' })).toBeDisabled()
})

test('holds a persistent dialogue with the project Butler before human confirmation', async () => {
  let intakePayload: Record<string, unknown> | null = null
  const messagePayloads: Record<string, unknown>[] = []
  let confirmationPayload: Record<string, unknown> | null = null
  const baseFetch = vi.mocked(fetch)
  const messages = [
    {
      id: 'message-1', ordinal: 1, sender_type: 'human',
      kind: 'instruction', content: '自动拆分并推进', payload: {}, created_at: 'now',
    },
    {
      id: 'message-2', ordinal: 2, sender_type: 'project_butler',
      kind: 'question', content: '这个目标允许修改什么、明确不应改变什么？', payload: { field: 'boundaries' }, created_at: 'now',
    },
  ]
  const conversation = (
    status: 'clarifying' | 'awaiting_confirmation' | 'dispatched',
    revision: number,
    expectedField: 'boundaries' | 'acceptance' | null,
  ) => ({
    id: 'conversation-1', scope: 'project', project_id: project.id,
    source_type: 'human', source_id: null, status, revision,
    confidence: expectedField === 'boundaries' ? 35 : expectedField === 'acceptance' ? 65 : 95,
    expected_field: expectedField,
    spec: {
      title: '自动拆分并推进', objective: '自动拆分并推进',
      boundaries: revision >= 1 ? ['只改控制面'] : [],
      acceptance: revision >= 2 ? ['并行角色可见'] : [],
      provider: 'cursor',
    },
    proposal_hash: status === 'clarifying' ? null : 'a'.repeat(64),
    goal_id: status === 'dispatched' ? 'goal-1' : null,
    messages: [...messages],
    direct_project_butler_url: `/api/projects/${project.id}/butler/conversations/conversation-1`,
  })
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (
        path === `/api/projects/${project.id}/butler/conversations`
        && init?.method !== 'POST'
      ) {
        return { ok: true, json: async () => [] }
      }
      if (
        path === `/api/projects/${project.id}/butler/conversations`
        && init?.method === 'POST'
      ) {
        intakePayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return { ok: true, json: async () => conversation('clarifying', 0, 'boundaries') }
      }
      if (path.endsWith('/butler/conversations/conversation-1/messages')) {
        const payload = JSON.parse(String(init?.body)) as Record<string, unknown>
        messagePayloads.push(payload)
        if (messagePayloads.length === 1) {
          messages.push(
            { id: 'message-3', ordinal: 3, sender_type: 'human', kind: 'answer', content: '只改控制面', payload: {}, created_at: 'now' },
            { id: 'message-4', ordinal: 4, sender_type: 'project_butler', kind: 'question', content: '用哪些可验证结果判断目标已经完成？', payload: {}, created_at: 'now' },
          )
          return { ok: true, json: async () => conversation('clarifying', 1, 'acceptance') }
        }
        messages.push(
          { id: 'message-5', ordinal: 5, sender_type: 'human', kind: 'answer', content: '并行角色可见', payload: {}, created_at: 'now' },
          { id: 'message-6', ordinal: 6, sender_type: 'project_butler', kind: 'proposal', content: '请确认', payload: {}, created_at: 'now' },
        )
        return { ok: true, json: async () => conversation('awaiting_confirmation', 2, null) }
      }
      if (path.endsWith('/butler/conversations/conversation-1/confirm')) {
        confirmationPayload = JSON.parse(String(init?.body)) as Record<string, unknown>
        return { ok: true, json: async () => conversation('dispatched', 3, null) }
      }
      if (path === '/api/goals/goal-1') {
        return { ok: true, json: async () => ({
          id: 'goal-1', title: '无人值守目标', objective: '自动拆分并推进',
          project_id: project.id, provider: 'cursor', status: 'running',
          spec_revision: 1,
          spec: {
            objective: '自动拆分并推进', scope: ['只改控制面'],
            acceptance: ['并行角色可见'], verification: [], artifacts: [],
            constraints: [], deadline: { hard_seconds: 1200 },
          },
          plan: { status: 'planned', items: [], model_invoked: false },
          sizing_inputs: estimate, parent_task_id: null,
          created_at: '2026-07-17T00:00:00Z',
          updated_at: '2026-07-17T00:00:00Z', work_items: [],
        }) }
      }
      return baseFetch(input, init)
    }),
  )
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '与项目管家对话' }))
  await screen.findByRole('heading', { name: '直接与项目管家对话' })
  fireEvent.change(screen.getByLabelText('给项目管家的指令'), { target: { value: '自动拆分并推进' } })
  fireEvent.click(screen.getByRole('button', { name: '发送给项目管家' }))
  expect(await screen.findByText('这个目标允许修改什么、明确不应改变什么？')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('回复项目管家'), { target: { value: '只改控制面' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  expect(await screen.findByText('用哪些可验证结果判断目标已经完成？')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('回复项目管家'), { target: { value: '并行角色可见' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  expect(await screen.findByRole('heading', { name: '需求完整度 95%' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '确认方案并执行' }))
  expect(await screen.findByText('目标已拆分并进入自动推进')).toBeInTheDocument()
  expect(intakePayload).toMatchObject({
    instruction: '自动拆分并推进',
    source_type: 'human',
    provider: 'cursor',
    sizing_inputs: {
      layers_touched: 1,
      components_touched: 3,
      estimated_files_changed: 4,
    },
  })
  expect(messagePayloads).toEqual([
    { expected_revision: 0, content: '只改控制面', sender_type: 'human' },
    { expected_revision: 1, content: '并行角色可见', sender_type: 'human' },
  ])
  expect(confirmationPayload).toEqual({
    expected_revision: 2,
    proposal_hash: 'a'.repeat(64),
    actor_type: 'human',
  })
})

test('shows every legacy quality profile as deterministic verification', async () => {
  listedTasks = ['fast', 'balanced', 'strict', 'deterministic'].map(taskWithQualityProfile)
  render(<App />)

  for (const profile of ['fast', 'balanced', 'strict', 'deterministic']) {
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(`legacy-${profile}`) }))
    expect(screen.getByText('验证机制')).toBeInTheDocument()
    expect(screen.getByText('确定性验证')).toBeInTheDocument()
    expect(screen.queryByText('质量档位')).not.toBeInTheDocument()
  }
})
