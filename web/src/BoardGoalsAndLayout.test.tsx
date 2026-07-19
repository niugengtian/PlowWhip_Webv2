import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'
import './styles.css'

const projectA = {
  id: 'project-a', name: 'Alpha', path: '/projects/alpha', host_path: '/projects/alpha',
  status: 'active', created_at: '2026-07-17T00:00:00Z', roles: [], workers: [],
}
const projectB = {
  id: 'project-b', name: 'Beta', path: '/projects/beta', host_path: '/projects/beta',
  status: 'active', created_at: '2026-07-17T00:00:00Z', roles: [], workers: [],
}

const longProvider = 'cursor-agent-host-bridge-with-very-long-provider-name-that-must-wrap'
const longTitle = '超长任务标题'.repeat(12)
const longObjective = '这是一段很长的目标说明，用于验证卡片不会把泳道撑破。'.repeat(8)

function baseGoal(overrides: Record<string, unknown>) {
  return {
    id: 'goal-base',
    title: 'Goal',
    objective: longObjective,
    project_id: projectA.id,
    provider: longProvider,
    status: 'running',
    spec_revision: 1,
    spec: {
      objective: longObjective,
      scope: ['frontend'],
      acceptance: ['verified'],
      verification: [],
      artifacts: [],
      constraints: [],
      deadline: { hard_seconds: 1200 },
    },
    plan: { status: 'planned' },
    sizing_inputs: { size_class: 'M' },
    parent_task_id: null,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
    work_items: [],
    ...overrides,
  }
}

function baseTask(overrides: Record<string, unknown>) {
  return {
    id: 'task-base',
    title: longTitle,
    objective: longObjective,
    project_path: projectA.path,
    project_id: projectA.id,
    role_id: null,
    worker_id: null,
    resource_key: null,
    network_requirement: 'none',
    same_failure_count: 0,
    no_progress_count: 0,
    last_failure_fingerprint: null,
    next_eligible_at: null,
    provider: longProvider,
    quality_profile: 'deterministic',
    status: 'completed',
    revision: 0,
    max_attempts: 3,
    attempts_used: 1,
    tokens_used: 12_345_678,
    last_evidence_hash: null,
    last_error: null,
    created_at: '2026-07-17T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
    command: {},
    verification: [],
    sizing: {},
    execution_policy: {},
    spec_revision: 1,
    spec: {
      objective: longObjective,
      scope: ['backend'],
      acceptance: ['verified'],
      verification: [],
      artifacts: [],
      constraints: [],
      deadline: { hard_seconds: 1200 },
    },
    ...overrides,
  }
}

let listedTasks: Record<string, unknown>[] = []
let listedGoals: Record<string, unknown>[] = []

beforeEach(() => {
  listedTasks = Array.from({ length: 20 }, (_, index) => baseTask({
    id: `terminal-${index}`,
    title: `${longTitle}-${index}`,
    status: index % 4 === 0 ? 'terminal_failed' : 'completed',
    tokens_used: 9_000_000 + index * 111_111,
  }))
  listedGoals = [
    baseGoal({ id: 'goal-active-1', title: 'Active Goal A', status: 'running', project_id: projectA.id }),
    baseGoal({ id: 'goal-active-2', title: 'Active Goal B', status: 'needs_human', project_id: projectB.id }),
    ...Array.from({ length: 6 }, (_, index) => baseGoal({
      id: `goal-done-${index}`,
      title: `Done Goal ${index}`,
      status: index % 2 === 0 ? 'completed' : 'cancelled',
      project_id: index % 2 === 0 ? projectA.id : projectB.id,
      objective: `completed-objective-${index}-${longObjective}`,
    })),
  ]
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const path = String(input)
      if (path.startsWith('/api/usage/daily')) {
        return {
          ok: true,
          json: async () => ({
            timezone: 'Asia/Shanghai',
            from: '2026-07-06',
            to: '2026-07-19',
            days: [],
            totals: {
              input_tokens: 0, cached_input_tokens: 0, uncached_input_tokens: 0,
              output_tokens: 0, total_tokens: 0, calls: 0,
            },
            total_formula: 'input_tokens + output_tokens',
            cached_input_tokens_in_total: true,
            date: '2026-07-19',
            projects: [],
            tasks: [],
          }),
        }
      }
      return {
        ok: true,
        json: async () => {
          if (path === '/api/projects') return [projectA, projectB]
          if (path === '/api/providers') {
            return [{
              name: 'cursor', display_name: 'Cursor', status: 'available', model_invoked: false,
              capabilities: [], reason: null, adapter: 'cursor', transport: 'host-bridge',
              executable: 'cursor-agent', enabled: true, credential_env: null, revision: 0, last_probed_at: null,
              readiness: {
                installed: true, installed_at: null, installed_reason: 'ok',
                cli_probe: 'ok', cli_probe_at: null, cli_probe_reason: 'ok',
                session_resume_ready: true, session_resume_checked_at: null, session_resume_reason: 'ok',
                recent_execution_health: 'healthy', recent_execution_checked_at: null, recent_execution_reason: 'ok',
              },
            }]
          }
          if (path === '/api/goals') return listedGoals
          if (path === '/api/tasks') return listedTasks
          if (path === '/api/usage') {
            return {
              input_tokens: 200,
              cached_input_tokens: 50,
              uncached_input_tokens: 150,
              cached_input_tokens_in_total: true,
              output_tokens: 40,
              total_tokens: 9999,
              total_formula: 'input_tokens + output_tokens',
              scope: 'all_history',
              timezone: 'Asia/Shanghai',
              usage_semantics: 'mixed_exact_and_legacy_inferred_delta',
              usage_quality: [
                { usage_semantics: 'delta', label: 'exact_delta', calls: 2, tokens: 100, call_share: 0.4, token_share: 0.2 },
                { usage_semantics: 'legacy_inferred_delta', label: 'legacy_inferred_delta', calls: 3, tokens: 400, call_share: 0.6, token_share: 0.8 },
              ],
              ratios: {
                input_per_output: 5,
                uncached_input_per_output: 3.75,
                is_budget_gate: false,
                is_quality_gate: false,
              },
              today: {
                date: '2026-07-19',
                timezone: 'Asia/Shanghai',
                scope: 'local_day',
                input_tokens: 10,
                cached_input_tokens: 2,
                uncached_input_tokens: 8,
                output_tokens: 3,
                total_tokens: 13,
                calls: 1,
              },
              projects: [],
              tasks: [],
              workers: [],
              providers: [],
              models: [],
              call_kinds: [],
              sessions: [],
              calls: [],
            }
          }
          if (['/api/outbox', '/api/audit', '/api/permissions'].includes(path)) return []
          if (path === '/api/settings') {
            return {
              revision: 0, updated_at: null,
              values: {
                scheduler_interval_seconds: 30, scheduler_lease_seconds: 90, cron_enabled: true,
                cron_expression: '*/1 * * * *', cron_timezone: 'Asia/Shanghai', cron_misfire_policy: 'catch_up_once',
                max_parallel_workers: 4, auto_dispatch: true, max_same_failure: 3, max_no_progress: 3,
                context_max_bytes: 8192, checkpoint_max_bytes: 4096, handoff_max_bytes: 4096,
                observation_tail_lines: 20, observation_max_bytes: 4096, rotation_max_bytes: 8192,
              },
            }
          }
          if (path === '/api/scheduler/status') {
            return {
              engine: { active: true, managed_by: 'test', backend: 'memory' },
              schedule: { enabled: true, expression: '*/1 * * * *', timezone: 'Asia/Shanghai', misfire_policy: 'catch_up_once', next_run_at: null },
              runtime: { fencing_token: 1, last_tick_at: null },
            }
          }
          if (path === '/api/system/health') {
            return { connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }
          }
          if (path === '/api/conventions/global/global') {
            return { scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }
          }
          if (path === '/api/butler/global' || path.startsWith('/api/butlers/global')) {
            return {
              scope: 'global', workspace_root: null, projects: [], totals: { projects: 0, running_goals: 0, active_tasks: 0, active_workers: 0 },
              canonical_sources: [], model_invoked: false,
            }
          }
          if (path === '/health') {
            return { status: 'ok', version: '0.1.0', database: { status: 'ok', journal_mode: 'wal', migration_count: 8 } }
          }
          return { status: 'ok', version: '0.1.0', database: { status: 'ok', journal_mode: 'wal', migration_count: 8 } }
        },
      }
    }),
  )
  vi.stubGlobal('EventSource', class {
    close = vi.fn()
    addEventListener() {}
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function assertNoHorizontalOverflow(root: ParentNode) {
  for (const selector of ['.kanban-grid', '.column-body', '.task-card']) {
    for (const node of root.querySelectorAll(selector)) {
      const element = node as HTMLElement
      expect(element.scrollWidth, selector).toBeLessThanOrEqual(element.clientWidth)
    }
  }
}

test('goal panel separates active and completed lanes with bounded history', async () => {
  render(<App />)
  const panel = await screen.findByTestId('goal-panel')
  expect(within(panel).getByRole('tab', { name: /进行中/ })).toHaveAttribute('aria-selected', 'true')
  expect(within(panel).getByRole('tab', { name: /进行中/ })).toHaveTextContent('2')
  expect(within(panel).getByRole('tab', { name: /已完成/ })).toHaveTextContent('6')
  expect(within(panel).getByText('Active Goal A')).toBeInTheDocument()
  expect(within(panel).queryByText('Done Goal 0')).not.toBeInTheDocument()

  fireEvent.click(within(panel).getByRole('tab', { name: /已完成/ }))
  expect(screen.getByTestId('goal-strip')).toHaveClass('goal-strip-bounded')
  expect(within(panel).queryByText('Active Goal A')).not.toBeInTheDocument()
  expect(within(panel).getByText('Done Goal 0')).toBeInTheDocument()
  expect(within(panel).queryByText('Done Goal 5')).not.toBeInTheDocument()

  fireEvent.click(within(panel).getByRole('button', { name: /展开已完成历史/ }))
  expect(screen.getByTestId('goal-strip')).not.toHaveClass('goal-strip-bounded')
  fireEvent.click(within(panel).getByRole('button', { name: '下一页' }))
  expect(within(panel).getByText('Done Goal 4')).toBeInTheDocument()
})

test('project filter keeps constraining goals and tasks across goal lane switches', async () => {
  render(<App />)
  const select = await screen.findByDisplayValue('全部项目')
  fireEvent.change(select, { target: { value: projectA.id } })
  expect(screen.getByText('Active Goal A')).toBeInTheDocument()
  expect(screen.queryByText('Active Goal B')).not.toBeInTheDocument()

  fireEvent.click(screen.getByRole('tab', { name: /已完成/ }))
  expect(screen.getByDisplayValue('Alpha')).toBeInTheDocument()
  expect(screen.getByText('Done Goal 0')).toBeInTheDocument()
  expect(screen.queryByText('Done Goal 1')).not.toBeInTheDocument()

  fireEvent.click(screen.getByText('Done Goal 0'))
  expect(screen.getByRole('heading', { name: 'Done Goal 0' })).toBeInTheDocument()
  expect(screen.getByDisplayValue('Alpha')).toBeInTheDocument()
})

test.each([1920, 1050, 760])('kanban cards stay within lanes at %spx with 19+ terminal cards', async (width) => {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() {
      const element = this as HTMLElement
      if (element.classList.contains('kanban-grid')) return width
      if (element.classList.contains('column-body') || element.classList.contains('kanban-column')) {
        return width <= 760 ? width : width <= 1050 ? Math.floor(width / 2) : Math.floor(width / 4)
      }
      if (element.classList.contains('task-card')) {
        const column = width <= 760 ? width : width <= 1050 ? Math.floor(width / 2) : Math.floor(width / 4)
        return Math.max(80, column - 24)
      }
      return width
    },
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
    configurable: true,
    get() {
      return (this as HTMLElement).clientWidth
    },
  })

  const { container } = render(<App />)
  await screen.findByTestId('kanban-grid')
  expect(screen.getByText('已终态').parentElement).toHaveTextContent('20')
  expect(container.querySelectorAll('.kanban-column .task-card').length).toBeGreaterThanOrEqual(19)
  expect(screen.getByText(/9,000,000 Token/)).toBeInTheDocument()
  assertNoHorizontalOverflow(container)
})

test('token page labels all-history vs today and shows uncached, ratios, exact/legacy shares', async () => {
  render(<App />)
  expect(await screen.findByRole('button', { name: /查看今日 Token详情/ })).toHaveTextContent('13')
  fireEvent.click(screen.getByRole('button', { name: 'Token' }))
  expect(await screen.findByTestId('usage-history-metrics')).toHaveTextContent('全历史 Total')
  expect(screen.getByTestId('usage-history-metrics')).toHaveTextContent('9,999')
  expect(screen.getByTestId('usage-history-metrics')).toHaveTextContent('Uncached-input')
  expect(screen.getByTestId('usage-today-metrics')).toHaveTextContent('今日 Total')
  expect(screen.getByTestId('usage-today-metrics')).toHaveTextContent('13')
  expect(screen.getByTestId('usage-ratios')).toHaveTextContent('Input / Output')
  expect(screen.getByTestId('usage-ratios')).toHaveTextContent('is_budget_gate=false')
  expect(screen.getByTestId('usage-ratios')).toHaveTextContent('is_quality_gate=false')
  expect(screen.getByTestId('usage-quality')).toHaveTextContent('exact_delta')
  expect(screen.getByTestId('usage-quality')).toHaveTextContent('legacy_inferred_delta')
  expect(screen.getByTestId('usage-quality')).toHaveTextContent('Token 80.0%')
})
