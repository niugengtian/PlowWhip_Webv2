import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
}
const estimate = {
  status: 'estimated', missing_gates: [], size_class: 'M',
  rationale: ['estimated_verification_seconds=180 (+6)', 'complexity_score=68', 'size_class=M'],
  estimated_input_tokens: { min: 45_000, max: 150_000, p90: 112_500 },
  estimated_output_tokens: { min: 15_000, max: 50_000, p90: 37_500 },
  soft_deadline_seconds: 480, hard_deadline_seconds: 1200, max_turns: 40, max_attempts: 3,
  verification_timeout_seconds: 300, progress_extension_seconds: 120,
  total_token_hard_cap: 225_000, reserved_tokens: 150_000,
  model_invoked: false, bootstrap_version: 'sprint10-v1',
}
let estimateFails = false
let estimatePayloads: Record<string, unknown>[] = []
let createPayloads: Record<string, unknown>[] = []
let listedTasks: Record<string, unknown>[] = []

function taskWithQualityProfile(qualityProfile: string, index: number) {
  return {
    id: `task-${index}`, title: `legacy-${qualityProfile}`, objective: '兼容旧任务',
    project_path: project.path, project_id: project.id, role_id: null, worker_id: null,
    resource_key: null, network_requirement: 'none', same_failure_count: 0, no_progress_count: 0,
    last_failure_fingerprint: null, next_eligible_at: null, provider: 'cursor', quality_profile: qualityProfile,
    status: 'completed', revision: 0, max_attempts: 3, attempts_used: 1, token_budget: 225_000, tokens_used: 100,
    last_evidence_hash: null, last_error: null, created_at: '2026-07-17T00:00:00Z', updated_at: '2026-07-17T00:00:00Z',
    command: {}, verification: [], sizing: estimate, execution_budget: {}, manual_override: false, override_reason: null,
  }
}

beforeEach(() => {
  estimateFails = false
  estimatePayloads = []
  createPayloads = []
  listedTasks = []
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
          task_default_token_budget: 50000,
          global_daily_token_budget: 500000,
          max_same_failure: 3,
          max_no_progress: 3,
          context_max_bytes: 32768,
          rotation_max_bytes: 262144,
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
        if (payload.independent_review_required) missing.push('independent_review_orchestration')
        return { ok: true, json: async () => missing.length ? ({
          ...estimate, status: 'needs_planning', missing_gates: missing, size_class: null,
          estimated_input_tokens: null, estimated_output_tokens: null,
          soft_deadline_seconds: null, hard_deadline_seconds: null, max_turns: null,
          max_attempts: null, verification_timeout_seconds: null, progress_extension_seconds: null,
          total_token_hard_cap: null, reserved_tokens: null,
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
          status: 'ready', revision: 0, max_attempts: 3, attempts_used: 0, token_budget: 225_000, tokens_used: 0,
          last_evidence_hash: null, last_error: null, created_at: '2026-07-17T00:00:00Z', updated_at: '2026-07-17T00:00:00Z',
          command: {}, verification: [], sizing: estimate, execution_budget: {}, manual_override: false, override_reason: null,
        }) }
      }
      return {
        ok: true,
        json: async () => path === '/api/projects' ? [project] : path === '/api/providers' ? [provider] : path === '/api/tasks' ? listedTasks : path.endsWith('/events') || path.endsWith('/artifacts') || ['/api/outbox', '/api/audit', '/api/permissions'].includes(path) ? [] : path === '/api/settings' ? settings : path === '/api/scheduler/status' ? scheduler : path === '/api/system/health' ? ({ connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }) : path === '/api/usage' ? ({ input_tokens: 0, output_tokens: 0, total_tokens: 0, control_tokens: 0, projects: [], tasks: [] }) : path === '/api/conventions/global/global' ? ({ scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }) : ({
          status: 'ok',
          version: '0.1.0',
          database: { status: 'ok', journal_mode: 'wal', migration_count: 8 },
        }),
      }
    }),
  )
})

afterEach(cleanup)

async function openTaskDrawer() {
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '新建任务' }))
  await screen.findByRole('heading', { name: '新建任务' })
}

test('shows the approved product priority', () => {
  render(<App />)
  expect(
    screen.getByText('保障质量的前提下实现无人值守完成，尽量减少 Token 消费。'),
  ).toBeInTheDocument()
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

test('opens task creation and completes a 0 Token preflight with dynamic budget facts', async () => {
  await openTaskDrawer()
  expect(screen.queryByLabelText('质量档位')).not.toBeInTheDocument()
  expect(screen.queryByText('快速')).not.toBeInTheDocument()
  expect(screen.queryByText('均衡')).not.toBeInTheDocument()
  expect(screen.queryByText('严格')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))

  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()
  expect(screen.getByText('45,000–150,000 · p90 112,500')).toBeInTheDocument()
  expect(screen.getByText('15,000–50,000 · p90 37,500')).toBeInTheDocument()
  expect(screen.getByText('225,000')).toBeInTheDocument()
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
  expect(screen.getByRole('button', { name: '加入任务队列' })).toBeDisabled()
})

test('invalidates a successful preflight as soon as a sizing input changes', async () => {
  await openTaskDrawer()
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  expect(await screen.findByText('服务端 Tier M')).toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('涉及层数'), { target: { value: '2' } })
  expect(screen.queryByText('服务端 Tier M')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '加入任务队列' })).toBeDisabled()
})

test('sends the exact preflight sizing inputs when creating a task', async () => {
  await openTaskDrawer()
  fireEvent.change(screen.getByLabelText('标题'), { target: { value: '接入预判' } })
  fireEvent.change(screen.getByLabelText('目标'), { target: { value: '服务端重算后入队' } })
  fireEvent.change(screen.getByLabelText('项目'), { target: { value: project.id } })
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))
  await screen.findByText('服务端 Tier M')
  fireEvent.click(screen.getByRole('button', { name: '加入任务队列' }))

  expect(await screen.findByText('任务已加入队列')).toBeInTheDocument()
  expect(createPayloads).toHaveLength(1)
  expect(createPayloads[0].quality_profile).toBe('deterministic')
  expect(createPayloads[0].sizing_inputs).toEqual(estimatePayloads[0])
  expect(createPayloads[0]).not.toHaveProperty('token_budget')
  expect(createPayloads[0].command).not.toHaveProperty('timeout_seconds')
})

test('independent review triggers its planning gate and cannot be created', async () => {
  await openTaskDrawer()
  expect(screen.getByText(/当前尚无独立 reviewer 编排，因此任务不能入队/)).toBeInTheDocument()
  fireEvent.click(screen.getByLabelText('要求独立复审（当前不可入队）'))
  fireEvent.click(screen.getByRole('button', { name: '执行 0 Token 预判' }))

  expect(await screen.findByText('暂不可入队：先补齐计划')).toBeInTheDocument()
  expect(screen.getByText(/尚无独立 reviewer 编排，要求独立复审的任务当前不能入队/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '加入任务队列' })).toBeDisabled()
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
  expect(screen.getByRole('button', { name: '加入任务队列' })).toBeDisabled()
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
