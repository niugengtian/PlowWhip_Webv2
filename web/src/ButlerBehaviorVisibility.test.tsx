import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from './App'

const project = {
  id: 'project-1',
  name: 'demo',
  path: '/projects/demo',
  host_path: '/projects/demo',
  status: 'active',
  role_count: 1,
  worker_count: 0,
  created_at: 'now',
  updated_at: 'now',
  roles: [],
  workers: [],
}

const settings = {
  revision: 1,
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
  sources: {},
  warnings: [],
  updated_at: 'now',
}

const scheduler = {
  engine: { active: true, managed_by: 'embedded', backend: 'apscheduler' },
  schedule: { next_run_at: 'later' },
  runtime: { fencing_token: 1, last_tick_at: 'now' },
}

function json(data: unknown, ok = true) {
  return { ok, json: async () => data, text: async () => JSON.stringify(data) }
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const path = String(input)
      if (path === '/health') return json({ status: 'ok', version: 'test' })
      if (path === '/api/system/health') {
        return json({
          connectivity: 'online', domestic_ok: true, overseas_ok: true,
          last_tick_at: null, last_resume_at: null, consecutive_failures: 0,
        })
      }
      if (path === '/api/projects') return json([project])
      if (path === '/api/providers') {
        return json([{
          name: 'cursor', display_name: 'Cursor', enabled: true, status: 'available',
          transport: 'host-bridge', capabilities: ['execute'], reason: null,
        }])
      }
      if (path === '/api/goals') return json([])
      if (path === '/api/tasks') return json([])
      if (path === '/api/workers') return json([])
      if (path === '/api/outbox' || path === '/api/audit' || path === '/api/permissions') return json([])
      if (path === '/api/settings') return json(settings)
      if (path === '/api/scheduler/status') return json(scheduler)
      if (path.startsWith('/api/usage')) {
        return json({
          input_tokens: 0, output_tokens: 0, total_tokens: 0, control_tokens: 0,
          today: {
            date: '2026-07-19', timezone: 'Asia/Shanghai',
            input_tokens: 0, cached_input_tokens: 0, uncached_input_tokens: 0,
            output_tokens: 0, total_tokens: 0, calls: 0,
          },
          projects: [], tasks: [],
        })
      }
      if (path === '/api/conventions/global/global') {
        return json({
          scope: 'global', scope_id: 'global', content: '', revision: 0,
          updated_at: null, present: false, empty: true,
        })
      }
      if (path.startsWith('/api/butlers/global/overview')) {
        return json({
          scope: 'global',
          workspace_root: null,
          projects: [{
            id: project.id, name: project.name, path: project.path,
            host_path: project.host_path, resource_path: project.path,
            status: 'active', goal_count: 0, running_goals: 0,
            active_tasks: 0, active_workers: 0,
          }],
          totals: { projects: 1, running_goals: 0, active_tasks: 0, active_workers: 0 },
          canonical_sources: ['projects', 'goals', 'tasks', 'workers'],
          model_invoked: false,
        })
      }
      if (path.startsWith('/api/system/behavior-baseline')) {
        const role = new URL(path, 'http://local').searchParams.get('role') || 'backend'
        const applicable = !['butler', 'coordination'].includes(role)
        return json({
          id: 'karpathy-guidelines',
          source: 'https://example.test/karpathy',
          revision: 1,
          version: 1,
          role,
          applicable,
          not_applicable: !applicable,
          applicability: applicable ? 'applicable' : 'not_applicable',
          mandatory: applicable,
          effective_reserve_bytes: applicable ? 1400 : 0,
          config_source: 'rule_versions:development',
          reason: applicable
            ? 'development role receives mandatory four-principle baseline'
            : 'control path',
          model_invoked: false,
        })
      }
      if (path.includes('/butler/conversations')) return json([])
      return json({})
    }),
  )
})

test('global butler stays read-only and routes into project butler', async () => {
  render(<App />)
  fireEvent.click(screen.getAllByRole('button', { name: '全局管家' })[0])
  expect(await screen.findByText('只读跨项目索引')).toBeInTheDocument()
  expect(screen.getByText('不共享项目会话 · 不读取全量文件或聊天')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '直接与项目管家对话' }))
  expect(await screen.findByText('告诉项目管家你要完成什么')).toBeInTheDocument()
})

test('settings show development baseline source and non-dev not-applicable', async () => {
  render(<App />)
  fireEvent.click(screen.getAllByRole('button', { name: '设置' })[0])
  expect(await screen.findByTestId('behavior-baseline-panel')).toBeInTheDocument()
  await waitFor(() => {
    expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('适用（开发角色）')
  })
  expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('1400')
  expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('rule_versions')
  fireEvent.change(screen.getByDisplayValue('backend'), { target: { value: 'butler' } })
  await waitFor(() => {
    expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('不适用')
  })
  expect(screen.getByTestId('behavior-baseline-facts')).not.toHaveTextContent('裁剪')
  expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('否')
  expect(screen.getByTestId('behavior-baseline-facts')).toHaveTextContent('0')
})

test('global butler never shows 95% clarification flow', async () => {
  render(<App />)
  fireEvent.click(screen.getAllByRole('button', { name: '全局管家' })[0])
  expect(await screen.findByText('只读跨项目索引')).toBeInTheDocument()
  expect(screen.queryByText('一次只问一个最有价值的问题')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '需求完整度 95%' })).not.toBeInTheDocument()
  expect(screen.queryByTestId('butler-status-banner')).not.toBeInTheDocument()
})

test('project butler awaiting confirmation shows wait-before-dispatch state', async () => {
  const baseFetch = vi.mocked(fetch)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (
        path === `/api/projects/${project.id}/butler/conversations`
        && init?.method !== 'POST'
      ) {
        return json([{
          id: 'conversation-wait',
          scope: 'project',
          project_id: project.id,
          source_type: 'human',
          source_id: null,
          status: 'awaiting_confirmation',
          revision: 2,
          confidence: 95,
          expected_field: null,
          spec: {
            title: '待确认方案',
            objective: '实现确认门',
            boundaries: ['只改本仓库'],
            acceptance: ['pytest exit 0'],
          },
          proposal_hash: 'b'.repeat(64),
          goal_id: null,
          messages: [
            {
              id: 'm1', ordinal: 1, sender_type: 'human', kind: 'instruction',
              content: '实现确认门', payload: {}, created_at: 'now',
            },
            {
              id: 'm2', ordinal: 2, sender_type: 'project_butler', kind: 'proposal',
              content: '请确认方案', payload: {}, created_at: 'now',
            },
          ],
          direct_project_butler_url: `/api/projects/${project.id}/butler/conversations/conversation-wait`,
        }])
      }
      return baseFetch(input, init)
    }),
  )
  render(<App />)
  fireEvent.click(screen.getAllByRole('button', { name: '与项目管家对话' })[0])
  await waitFor(() => {
    expect(screen.getByTestId('butler-status-banner')).toHaveAttribute(
      'data-status',
      'awaiting_confirmation',
    )
  })
  expect(screen.getByTestId('butler-status-banner')).toHaveTextContent('待确认')
  expect(screen.getByTestId('butler-status-banner')).toHaveTextContent('等待主人确认后才会派发')
  expect(await screen.findByRole('heading', { name: '需求完整度 95%' })).toBeInTheDocument()
})

test('convention and effective context panels show baseline applicability', async () => {
  const baseFetch = vi.mocked(fetch)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const path = String(input)
      if (path.startsWith('/api/conventions/inventory')) {
        return json({
          mutable_conventions: [],
          bundled_behaviors: [],
          behavior_baseline: {
            id: 'karpathy-guidelines',
            source: 'https://example.test/karpathy',
            revision: 1,
            version: 1,
            role: 'backend',
            applicable: true,
            not_applicable: false,
            applicability: 'applicable',
            mandatory: true,
            effective_reserve_bytes: 1400,
            config_source: 'rule_versions:development',
            reason: 'development role receives mandatory four-principle baseline',
            model_invoked: false,
          },
          precedence: ['task_role', 'project', 'global', 'rule_library_baseline'],
          model_invoked: false,
        })
      }
      if (path.startsWith('/api/conventions/effective')) {
        return json({
          project_id: project.id,
          task_id: 'task-1',
          role_id: 'role-1',
          role: 'butler',
          behavior_baseline: {
            id: 'karpathy-guidelines',
            source: 'https://example.test/karpathy',
            revision: 1,
            version: 1,
            role: 'butler',
            applicable: false,
            not_applicable: true,
            applicability: 'not_applicable',
            mandatory: false,
            effective_reserve_bytes: 0,
            config_source: 'rule_versions:development',
            reason: 'control path',
            model_invoked: false,
          },
          layers: [],
          empty_scopes: [],
          model_invoked: false,
        })
      }
      if (path === '/api/tasks') {
        return json([{
          id: 'task-1',
          title: 'demo task',
          objective: 'demo',
          project_path: project.path,
          project_id: project.id,
          role_id: 'role-1',
          worker_id: null,
          resource_key: null,
          network_requirement: 'none',
          same_failure_count: 0,
          no_progress_count: 0,
          last_failure_fingerprint: null,
          next_eligible_at: null,
          provider: 'cursor',
          quality_profile: 'deterministic',
          status: 'ready',
          revision: 1,
          command: {},
          verification: [],
          max_attempts: 1,
          attempts_used: 0,
          tokens_used: 0,
          last_evidence_hash: null,
          last_error: null,
          created_at: 'now',
          updated_at: 'now',
          sizing: {},
          execution_policy: null,
          goal_id: null,
          parent_task_id: null,
          depends_on: [],
          work_item_kind: null,
          ordinal: null,
          blocked_reason: null,
          handoff: null,
          spec_revision: 1,
          spec: {
            objective: 'demo', scope: [], acceptance: [], verification: [],
            artifacts: [], constraints: [], deadline: { hard_seconds: 60 },
          },
          execution_episode: null,
        }])
      }
      return baseFetch(input)
    }),
  )
  render(<App />)
  fireEvent.click(screen.getAllByRole('button', { name: '设置' })[0])
  await waitFor(() => {
    expect(screen.getByTestId('convention-baseline-facts')).toHaveTextContent('适用（开发角色）')
  })
  expect(screen.getByTestId('convention-baseline-facts')).toHaveTextContent('1400')
  await waitFor(() => {
    expect(screen.getByTestId('effective-context-baseline-facts')).toHaveTextContent('不适用')
  })
})

test('project butler clarifying banner is visible while waiting for answers', async () => {
  const baseFetch = vi.mocked(fetch)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === `/api/projects/${project.id}/butler/conversations` && init?.method !== 'POST') {
        return json([])
      }
      if (
        path === `/api/projects/${project.id}/butler/conversations`
        && init?.method === 'POST'
      ) {
        return json({
          id: 'conversation-1',
          scope: 'project',
          project_id: project.id,
          source_type: 'human',
          source_id: null,
          status: 'clarifying',
          revision: 0,
          confidence: 35,
          expected_field: 'boundaries',
          spec: { title: '实现澄清', objective: '实现澄清', boundaries: [], acceptance: [] },
          proposal_hash: null,
          goal_id: null,
          messages: [
            {
              id: 'm1', ordinal: 1, sender_type: 'human', kind: 'instruction',
              content: '实现澄清', payload: {}, created_at: 'now',
            },
            {
              id: 'm2', ordinal: 2, sender_type: 'project_butler', kind: 'question',
              content: '边界是什么？', payload: { field: 'boundaries' }, created_at: 'now',
            },
          ],
          direct_project_butler_url: `/api/projects/${project.id}/butler/conversations/conversation-1`,
        })
      }
      return baseFetch(input, init)
    }),
  )
  render(<App />)
  const openButler = await screen.findAllByRole('button', { name: '与项目管家对话' })
  fireEvent.click(openButler[0])
  expect(await screen.findByRole('heading', { name: '直接与项目管家对话' })).toBeInTheDocument()
  // Force a fresh intake form even if history hydration races.
  fireEvent.click(await screen.findByRole('button', { name: '新对话' }))
  const instruction = await screen.findByLabelText('给项目管家的指令')
  fireEvent.change(instruction, { target: { value: '实现澄清流程' } })
  fireEvent.click(screen.getByRole('button', { name: '发送给项目管家' }))
  await waitFor(() => {
    expect(screen.getByTestId('butler-status-banner')).toHaveAttribute('data-status', 'clarifying')
  })
  expect(screen.getByTestId('butler-status-banner')).toHaveTextContent('一次只问一个最有价值的问题')
  expect(screen.getByTestId('butler-status-banner')).toHaveTextContent('澄清中')
})
