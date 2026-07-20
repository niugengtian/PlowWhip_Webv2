import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ButlerConversation, Project, Provider } from './api'
import { ProjectButlerDialog } from './components/ProjectButlerDialog'

const project = {
  id: 'project-1',
  name: 'Planner',
  path: '/projects/planner',
  host_path: '/work/planner',
  status: 'active',
  roles: [],
  workers: [],
} as unknown as Project

const provider = {
  name: 'codex',
  display_name: 'Codex CLI',
  status: 'available',
  enabled: true,
  transport: 'host-bridge',
  capabilities: ['new_session', 'resume_session'],
} as Provider

const suspended = {
  id: 'conversation-1',
  scope: 'project',
  project_id: project.id,
  source_type: 'human',
  source_id: 'owner',
  status: 'provider_suspended',
  revision: 1,
  confidence: 35,
  expected_field: 'boundaries',
  spec: { objective: '实现智能项目管家' },
  proposal_hash: null,
  goal_id: null,
  messages: [
    {
      id: 'message-1',
      ordinal: 1,
      sender_type: 'human',
      kind: 'instruction',
      content: '实现智能项目管家',
      payload: {},
      created_at: '2026-07-20 09:00:00',
    },
  ],
  direct_project_butler_url: null,
  provider: 'codex',
  external_session_id: null,
  session_generation: 1,
  archived_at: null,
  planner: {
    status: 'provider_suspended',
    error_class: 'command_failed',
  },
} as ButlerConversation

describe('ProjectButlerDialog provider suspension', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows fail-closed state and resumes the same conversation', async () => {
    vi.spyOn(api, 'projectButlerConversations').mockResolvedValue([suspended])
    vi.spyOn(api, 'resumeProjectButler').mockResolvedValue({
      ...suspended,
      status: 'awaiting_confirmation',
      revision: 2,
      confidence: 95,
      expected_field: null,
      proposal_hash: 'a'.repeat(64),
      planner: { status: 'planned' },
    })

    render(<ProjectButlerDialog
      initialProjectId={project.id}
      projects={[project]}
      providers={[provider]}
      onClose={() => undefined}
      onDispatched={async () => undefined}
    />)

    expect(await screen.findByText('Provider 已挂起')).toBeInTheDocument()
    expect(screen.getByText('模型调用失败，已停止机械降级')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '回复项目管家' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', {
      name: '恢复 Provider 并续接本会话',
    }))

    await waitFor(() => {
      expect(api.resumeProjectButler).toHaveBeenCalledWith(project.id, suspended)
      expect(screen.getByText('待确认')).toBeInTheDocument()
    })
  })

  it('keeps new-conversation mode across project-list refreshes', async () => {
    vi.spyOn(api, 'projectButlerConversations').mockResolvedValue([suspended])

    const { rerender } = render(<ProjectButlerDialog
      initialProjectId={project.id}
      projects={[project]}
      providers={[provider]}
      onClose={() => undefined}
      onDispatched={async () => undefined}
    />)

    expect(await screen.findByText('Provider 已挂起')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '新对话' }))
    expect(screen.getByRole('textbox', { name: '给项目管家的指令' })).toBeEnabled()

    rerender(<ProjectButlerDialog
      initialProjectId={project.id}
      projects={[{ ...project }]}
      providers={[provider]}
      onClose={() => undefined}
      onDispatched={async () => undefined}
    />)

    expect(screen.getByRole('textbox', { name: '给项目管家的指令' })).toBeEnabled()
    expect(screen.queryByText('Provider 已挂起')).not.toBeInTheDocument()
  })

  it('allows only one resume request at a time', async () => {
    vi.spyOn(api, 'projectButlerConversations').mockResolvedValue([suspended])
    let resolveResume: (value: ButlerConversation) => void = () => undefined
    vi.spyOn(api, 'resumeProjectButler').mockImplementation(() => new Promise(
      (resolve) => { resolveResume = resolve },
    ))

    render(<ProjectButlerDialog
      initialProjectId={project.id}
      projects={[project]}
      providers={[provider]}
      onClose={() => undefined}
      onDispatched={async () => undefined}
    />)

    const resume = await screen.findByRole('button', {
      name: '恢复 Provider 并续接本会话',
    })
    fireEvent.click(resume)
    fireEvent.click(resume)
    expect(api.resumeProjectButler).toHaveBeenCalledTimes(1)
    resolveResume(suspended)
  })

  it('sends proposal objections as conversational input without forcing a field', async () => {
    const proposal = {
      ...suspended,
      status: 'awaiting_confirmation',
      expected_field: null,
      proposal_hash: 'a'.repeat(64),
      planner: { status: 'planned' },
    } as ButlerConversation
    vi.spyOn(api, 'projectButlerConversations').mockResolvedValue([proposal])
    vi.spyOn(api, 'sendProjectButlerMessage').mockResolvedValue(proposal)

    render(<ProjectButlerDialog
      initialProjectId={project.id}
      projects={[project]}
      providers={[provider]}
      onClose={() => undefined}
      onDispatched={async () => undefined}
    />)

    const composer = await screen.findByRole('textbox', { name: '回复项目管家' })
    fireEvent.change(composer, {
      target: { value: '你为什么选择这个角色？先重新分析。' },
    })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => {
      expect(api.sendProjectButlerMessage).toHaveBeenCalledWith(
        project.id,
        proposal,
        '你为什么选择这个角色？先重新分析。',
        undefined,
      )
    })
  })
})
