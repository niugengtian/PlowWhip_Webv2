import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

const emptyUsage = {
  input_tokens: 0,
  cached_input_tokens: 0,
  uncached_input_tokens: 0,
  cached_input_tokens_in_total: true,
  output_tokens: 0,
  total_tokens: 0,
  total_formula: 'input_tokens + output_tokens',
  scope: 'all_history',
  timezone: 'Asia/Shanghai',
  usage_semantics: 'physical_session_delta',
  usage_quality: [],
  ratios: {
    input_per_output: null,
    uncached_input_per_output: null,
    is_budget_gate: false,
    is_quality_gate: false,
  },
  today: {
    date: '2026-07-19',
    timezone: 'Asia/Shanghai',
    scope: 'local_day',
    input_tokens: 0,
    cached_input_tokens: 0,
    uncached_input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    calls: 0,
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

const historyDays = [
  {
    date: '2026-07-17',
    input_tokens: 0,
    cached_input_tokens: 0,
    uncached_input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    calls: 0,
  },
  {
    date: '2026-07-18',
    input_tokens: 40,
    cached_input_tokens: 10,
    uncached_input_tokens: 30,
    output_tokens: 5,
    total_tokens: 45,
    calls: 1,
  },
  {
    date: '2026-07-19',
    input_tokens: 70,
    cached_input_tokens: 20,
    uncached_input_tokens: 50,
    output_tokens: 8,
    total_tokens: 78,
    calls: 1,
  },
]

let dailyFail = false
let dayFail = false
let dayPayloads: Record<string, unknown> = {}

function basePayload(path: string) {
  if (path === '/api/projects') {
    return [{
      id: 'project-1', name: 'Console', path: '/projects/console', host_path: '/projects/console',
      status: 'active', created_at: '2026-07-17T00:00:00Z', roles: [], workers: [],
    }]
  }
  if (path === '/api/providers') {
    return [{
      name: 'cursor', display_name: 'Cursor', status: 'available', model_invoked: false,
      capabilities: [], reason: null, adapter: 'cursor', transport: 'host-bridge', executable: 'cursor-agent',
      enabled: true, credential_env: null, revision: 0, last_probed_at: null,
    }]
  }
  if (path === '/api/tasks' || path === '/api/goals' || path === '/api/outbox' || path === '/api/audit' || path === '/api/permissions') {
    return []
  }
  if (path === '/api/settings') {
    return {
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
        context_max_bytes: 8192,
        checkpoint_max_bytes: 4096,
        handoff_max_bytes: 2048,
        observation_tail_lines: 20,
        observation_max_bytes: 4096,
        rotation_max_bytes: 65536,
      },
    }
  }
  if (path === '/api/scheduler/status') {
    return {
      runtime: {
        fencing_token: 1, last_tick_at: null, last_result: null, last_error: null,
        runner_id: null, runner_started_at: null, runner_heartbeat_at: null,
        runner_stopped_at: null, runner_error: null, runner_active: false, last_cron_slot: null,
      },
      engine: { backend: 'embedded', active: false, managed_by: 'app', data_dir: '/tmp' },
      schedule: { enabled: true, expression: '*/1 * * * *', timezone: 'Asia/Shanghai', misfire_policy: 'catch_up_once', next_run_at: null },
      authorization_required: false,
      model_invoked: false,
    }
  }
  if (path === '/api/system/health') {
    return { connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }
  }
  if (path === '/api/usage') return emptyUsage
  if (path === '/api/conventions/global/global') {
    return { scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }
  }
  if (path === '/api/butler/global') {
    return { conversation: null, projects: [], open_tasks: 0 }
  }
  return {
    status: 'ok',
    version: '0.1.0',
    database: { status: 'ok', journal_mode: 'wal', migration_count: 8 },
  }
}

beforeEach(() => {
  dailyFail = false
  dayFail = false
  dayPayloads = {
    '2026-07-18': {
      date: '2026-07-18',
      timezone: 'Asia/Shanghai',
      input_tokens: 40,
      cached_input_tokens: 10,
      uncached_input_tokens: 30,
      output_tokens: 5,
      total_tokens: 45,
      calls: 1,
      total_formula: 'input_tokens + output_tokens',
      cached_input_tokens_in_total: true,
      projects: [
        {
          key: 'project-1', label: 'Console', project_id: 'project-1',
          input_tokens: 40, cached_input_tokens: 10, uncached_input_tokens: 30,
          output_tokens: 5, tokens: 45, calls: 1,
        },
      ],
      tasks: [
        {
          key: 'task-1', label: 'history-task', task_id: 'task-1',
          input_tokens: 40, cached_input_tokens: 10, uncached_input_tokens: 30,
          output_tokens: 5, tokens: 45, calls: 1,
        },
      ],
    },
    '2026-07-19': {
      date: '2026-07-19',
      timezone: 'Asia/Shanghai',
      input_tokens: 70,
      cached_input_tokens: 20,
      uncached_input_tokens: 50,
      output_tokens: 8,
      total_tokens: 78,
      calls: 1,
      total_formula: 'input_tokens + output_tokens',
      cached_input_tokens_in_total: true,
      projects: [
        {
          key: '__unknown_project__', label: '未知/已删除项目', project_id: null,
          input_tokens: 70, cached_input_tokens: 20, uncached_input_tokens: 50,
          output_tokens: 8, tokens: 78, calls: 1,
        },
      ],
      tasks: [
        {
          key: '__unknown_task__', label: '未知/已删除任务', task_id: null,
          input_tokens: 70, cached_input_tokens: 20, uncached_input_tokens: 50,
          output_tokens: 8, tokens: 78, calls: 1,
        },
      ],
    },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const path = String(input).split('?')[0]
      if (path.startsWith('/api/usage/daily/')) {
        if (dayFail) {
          return { ok: false, json: async () => ({ detail: 'day lookup failed' }) }
        }
        const day = path.split('/').at(-1) ?? ''
        return { ok: true, json: async () => dayPayloads[day] ?? dayPayloads['2026-07-19'] }
      }
      if (path === '/api/usage/daily') {
        if (dailyFail) {
          return { ok: false, json: async () => ({ detail: 'history unavailable' }) }
        }
        return {
          ok: true,
          json: async () => ({
            timezone: 'Asia/Shanghai',
            from: '2026-07-17',
            to: '2026-07-19',
            days: historyDays,
            totals: {
              input_tokens: 110,
              cached_input_tokens: 30,
              uncached_input_tokens: 80,
              output_tokens: 13,
              total_tokens: 123,
              calls: 2,
            },
            total_formula: 'input_tokens + output_tokens',
            cached_input_tokens_in_total: true,
          }),
        }
      }
      return { ok: true, json: async () => basePayload(path) }
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

async function openUsage() {
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Token' }))
  await screen.findByRole('heading', { name: 'Token 趋势' })
}

test('renders historical token trend and drills into day pies', async () => {
  await openUsage()
  expect(await screen.findByTestId('token-trend-chart')).toBeInTheDocument()
  fireEvent.click(await screen.findByTestId('token-day-2026-07-18'))
  await waitFor(() => expect(screen.getByTestId('token-day-pies')).toBeInTheDocument())
  expect(screen.getByTestId('token-pie-项目占比')).toBeInTheDocument()
  expect(screen.getByTestId('token-pie-任务占比')).toBeInTheDocument()
  expect(screen.getByTestId('token-pie-项目占比')).toHaveTextContent('Console')
  expect(screen.getByTestId('token-pie-任务占比')).toHaveTextContent('history-task')
  expect(screen.getAllByText(/分片合计 45 \/ 当日 45/).length).toBe(2)
})

test('shows empty day pies when selected day has no consumption', async () => {
  dayPayloads['2026-07-17'] = {
    date: '2026-07-17',
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
  }
  await openUsage()
  fireEvent.click(await screen.findByTestId('token-day-2026-07-17'))
  await waitFor(() => expect(screen.getByText('该日无项目消费。')).toBeInTheDocument())
  expect(screen.getByText('该日无任务消费。')).toBeInTheDocument()
})

test('shows history request failure state', async () => {
  dailyFail = true
  await openUsage()
  expect(await screen.findByTestId('token-history-error')).toHaveTextContent('history unavailable')
})

test('shows day breakdown request failure state', async () => {
  dayFail = true
  await openUsage()
  await waitFor(() => expect(screen.getByTestId('token-day-error')).toHaveTextContent('day lookup failed'))
})
