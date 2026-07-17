import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
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
      return {
        ok: true,
        json: async () => ['/api/tasks', '/api/projects', '/api/outbox', '/api/providers', '/api/audit', '/api/permissions'].includes(path) ? [] : path === '/api/settings' ? settings : path === '/api/scheduler/status' ? scheduler : path === '/api/system/health' ? ({ connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }) : path === '/api/usage' ? ({ input_tokens: 0, output_tokens: 0, total_tokens: 0, control_tokens: 0, projects: [], tasks: [] }) : path === '/api/conventions/global/global' ? ({ scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }) : ({
          status: 'ok',
          version: '0.1.0',
          database: { status: 'ok', journal_mode: 'wal', migration_count: 8 },
        }),
      }
    }),
  )
})

afterEach(cleanup)

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
