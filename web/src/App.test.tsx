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
        json: async () => ['/api/tasks', '/api/projects'].includes(path) ? [] : ({
          status: 'ok',
          version: '0.1.0',
          database: { status: 'ok', journal_mode: 'wal', migration_count: 4 },
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
