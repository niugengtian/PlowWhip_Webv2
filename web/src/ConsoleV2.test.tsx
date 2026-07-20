import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

const project = {
  id: 'project-a', name: 'Alpha', path: '/projects/alpha', host_path: '/work/alpha',
  execution_policy: {}, status: 'active', created_at: '2026-07-19T00:00:00Z',
  roles: [], workers: [],
}
const task = {
  id: 'task-a', title: 'Alpha task', objective: 'deliver verified result',
  project_path: project.path, project_id: project.id, role_id: null, worker_id: null,
  resource_key: null, network_requirement: 'overseas', same_failure_count: 0,
  no_progress_count: 0, last_failure_fingerprint: null, next_eligible_at: null,
  provider: 'codex', quality_profile: 'deterministic', status: 'running', revision: 2,
  max_attempts: 3, attempts_used: 1, tokens_used: 1234, last_evidence_hash: null,
  last_error: null, created_at: '2026-07-19T00:00:00Z',
  updated_at: '2026-07-19T01:00:00Z', command: {}, verification: [], sizing: {},
  execution_policy: {}, goal_id: 'goal-a', depends_on: [], work_item_kind: 'implementation',
  blocked_reason: null, spec_revision: 1,
  spec: { objective: 'deliver verified result', scope: ['web'], acceptance: ['PASS'], verification: [], artifacts: [], constraints: [], deadline: { hard_seconds: 4800 } },
  evidence_manifest: null,
}
const goal = {
  id: 'goal-a', title: 'Alpha goal', objective: 'ship it', project_id: project.id,
  provider: 'codex', status: 'running', plan: {}, sizing_inputs: {}, parent_task_id: null,
  created_at: '2026-07-19T00:00:00Z', updated_at: '2026-07-19T01:00:00Z',
  work_items: [{ id: task.id }], spec_revision: 1,
  spec: { objective: 'ship it', scope: ['web'], acceptance: ['PASS'], verification: [], artifacts: [], constraints: [], deadline: { hard_seconds: 4800 } },
}

function response(value: unknown) {
  return Promise.resolve({ ok: true, json: async () => value })
}

beforeEach(() => {
  window.localStorage.clear()
  vi.stubGlobal('EventSource', class {
    onmessage = null
    onerror = null
    addEventListener() {}
    close() {}
  })
  vi.stubGlobal('fetch', vi.fn((input: string | URL | Request) => {
    const path = String(input)
    if (path === '/health') return response({ status: 'ok', version: 'test', database: { status: 'ok', journal_mode: 'wal', migration_count: 33 } })
    if (path === '/api/projects') return response([project])
    if (path === '/api/tasks') return response([task])
    if (path === '/api/goals') return response([goal])
    if (path === '/api/providers') return response([{ name: 'codex', display_name: 'Codex', status: 'available', enabled: true, model_invoked: false, capabilities: ['refine_convention'], reason: null, adapter: 'codex', transport: 'host-bridge', executable: 'codex', credential_env: null, revision: 1, last_probed_at: null, network_zone: 'overseas', readiness: { installed: true, cli_probe: 'ok', session_resume_ready: true, recent_execution_health: 'healthy' } }])
    if (path === '/api/system/health') return response({ connectivity: 'online' })
    if (path === '/api/alerts') return response({ items: [], network: { global_offline: false, zones: { domestic: { state: 'available' }, overseas: { state: 'available' } } } })
    if (path === '/api/audit' || path === '/api/outbox' || path === '/api/permissions') return response([])
    if (path === '/api/butlers/global/overview') return response({ scope: 'global', workspace_root: '/work', projects: [{ ...project, resource_path: project.host_path, goal_count: 1, running_goals: 1, active_tasks: 1, active_workers: 0 }], totals: { projects: 1, running_goals: 1, active_tasks: 1, active_workers: 0 }, canonical_sources: ['Goal', 'Task'], model_invoked: false })
    if (path === '/api/butlers/global/conversations') return response([])
    if (path === `/api/projects/${project.id}/butler/conversations`) return response([])
    if (path === '/api/settings') return response({
      revision: 1, updated_at: null, sources: {}, warnings: [], values: {
        scheduler_interval_seconds: 30, scheduler_lease_seconds: 90, cron_enabled: true,
        cron_expression: '*/1 * * * *', cron_timezone: 'Asia/Shanghai', cron_misfire_policy: 'catch_up_once',
        max_parallel_workers: 4, auto_dispatch: true, max_same_failure: 2, max_no_progress: 3,
        context_max_bytes: 32768, rotation_max_bytes: 262144, checkpoint_max_bytes: 4096,
        handoff_max_bytes: 2048, observation_tail_lines: 20, observation_max_bytes: 8192,
        episode_wall_limit_seconds: 4800, checkpoint_interval_seconds: 120, no_progress_seconds: 300,
        max_host_processes: 2, progress_extension_seconds: 120, provider_failure_threshold: 3,
        provider_recovery_successes: 1, provider_open_seconds: 60, network_failure_threshold: 2,
        network_recovery_successes: 3, resume_batch_size: 2, alert_debounce_seconds: 30,
        default_provider_policy: 'auto', default_provider_order: ['codex', 'cursor', 'deepseek', 'kimi'],
        default_butler_provider: 'codex',
      },
    })
    if (path === '/api/scheduler/status') return response({ engine: { active: true, managed_by: 'scheduler', backend: 'sqlite' }, schedule: { next_run_at: null }, runtime: { fencing_token: 1, last_tick_at: null } })
    if (path === '/api/conventions/global/global') return response({ scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null })
    if (path.startsWith('/api/usage/daily/')) return response({ date: '2026-07-19', total_tokens: 300, projects: [], tasks: [] })
    if (path.startsWith('/api/usage/daily')) return response({ timezone: 'Asia/Shanghai', from: '2026-07-17', to: '2026-07-19', days: [
      { date: '2026-07-17', input_tokens: 80, cached_input_tokens: 20, uncached_input_tokens: 60, output_tokens: 20, total_tokens: 100, calls: 1 },
      { date: '2026-07-18', input_tokens: 160, cached_input_tokens: 40, uncached_input_tokens: 120, output_tokens: 40, total_tokens: 200, calls: 1 },
      { date: '2026-07-19', input_tokens: 240, cached_input_tokens: 60, uncached_input_tokens: 180, output_tokens: 60, total_tokens: 300, calls: 1 },
    ], totals: {} })
    if (path.startsWith('/api/usage')) return response({ input_tokens: 240, cached_input_tokens: 60, uncached_input_tokens: 180, output_tokens: 60, total_tokens: 300, timezone: 'Asia/Shanghai', scope: 'all_history', ratios: {}, today: { date: '2026-07-19', input_tokens: 240, cached_input_tokens: 60, uncached_input_tokens: 180, output_tokens: 60, total_tokens: 300 }, projects: [], tasks: [], providers: [], models: [], call_kinds: [], sessions: [], calls: [], usage_quality: [] })
    if (path === `/api/tasks/${task.id}/deletion-eligibility`) return response({ deletable: false, reason: 'active' })
    if (path === `/api/tasks/${task.id}/events` || path === `/api/tasks/${task.id}/artifacts`) return response([])
    if (path.startsWith('/api/role-instances') || path.startsWith('/api/session-bindings')) return response({ items: [] })
    return response({})
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

test('uses the approved six-item information architecture in order', async () => {
  render(<App />)
  const nav = screen.getByRole('navigation', { name: '主导航' })
  expect(within(nav).getAllByRole('button').map((button) => button.textContent)).toEqual([
    '管家', '项目', '任务', 'Token', '告警', '设置',
  ])
})

test('shows one butler for the selected scope and only on the butler page', async () => {
  render(<App />)
  expect(screen.queryByRole('button', { name: '与项目管家对话' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '管家' }))
  expect(await screen.findByRole('button', { name: '与全局管家对话' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('项目范围'), { target: { value: project.id } })
  expect(await screen.findByRole('heading', { name: 'Alpha 项目管家' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '与项目管家对话' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '与全局管家对话' })).not.toBeInTheDocument()
})

test('combines goals, task lanes and details in one workspace and clears stale scope selection', async () => {
  render(<App />)
  const workspace = await screen.findByTestId('unified-task-workspace')
  expect(within(workspace).getByText('Alpha goal')).toBeInTheDocument()
  expect(within(workspace).getByText('任务泳道')).toBeInTheDocument()
  fireEvent.click(within(workspace).getByRole('button', { name: /Alpha task/ }))
  expect(await screen.findByRole('heading', { name: 'Alpha task' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('项目范围'), { target: { value: 'all' } })
  expect(screen.getByText('选择一个 Goal 或 Task')).toBeInTheDocument()
})

test('persists the selected refresh interval and exposes all approved options', async () => {
  render(<App />)
  const select = screen.getByLabelText('自动刷新间隔')
  expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
    '关闭', '5s', '10s', '30s', '1min', '5min', '10min', '1h', '2h', '4h',
  ])
  fireEvent.change(select, { target: { value: '10000' } })
  await waitFor(() => expect(window.localStorage.getItem('plow-whip.refresh-interval')).toBe('10000'))
})

test('exposes the six settings domains without top-level worker/provider pages', async () => {
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '设置' }))
  for (const label of ['常规', '管家与 Provider', '持续执行与保护', '角色、规则与模板', '安全与运行时', '高级与审计']) {
    expect(await screen.findByText(label)).toBeInTheDocument()
  }
})

test('renders the converged alert center', async () => {
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '告警' }))
  expect(await screen.findByRole('heading', { name: '告警中心' })).toBeInTheDocument()
  expect(screen.getByText('同一根因只显示一条主告警')).toBeInTheDocument()
  expect(screen.getByText('在线 · 国内/海外可用')).toBeInTheDocument()
  expect(screen.getByText('当前没有活动告警')).toBeInTheDocument()
})
