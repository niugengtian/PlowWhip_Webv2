import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const path = String(input)
      return {
        ok: true,
        json: async () => ['/api/tasks', '/api/projects', '/api/outbox', '/api/providers', '/api/audit', '/api/permissions'].includes(path) ? [] : path === '/api/system/health' ? ({ connectivity: 'unknown', domestic_ok: null, overseas_ok: null, last_tick_at: null, last_resume_at: null, consecutive_failures: 0 }) : path === '/api/usage' ? ({ input_tokens: 0, output_tokens: 0, total_tokens: 0, control_tokens: 0, projects: [], tasks: [] }) : path === '/api/conventions/global/global' ? ({ scope: 'global', scope_id: 'global', content: '', revision: 0, updated_at: null }) : ({
          status: 'ok',
          version: '0.1.0',
          database: { status: 'ok', journal_mode: 'wal', migration_count: 7 },
        }),
      }
    }),
  )
})

test('shows the approved product priority', () => {
  render(<App />)
  expect(
    screen.getByText('保障质量的前提下实现无人值守完成，尽量减少 Token 消费。'),
  ).toBeInTheDocument()
})
