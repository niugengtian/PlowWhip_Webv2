import { FormEvent, useEffect, useMemo, useState } from 'react'
import { ChatCircleDots, CheckCircle, PaperPlaneTilt, Plus, Robot, X } from '@phosphor-icons/react'
import { api, ButlerConversation, Project, Provider } from '../api'

type ProjectButlerDialogProps = {
  initialProjectId: string
  globalScope?: boolean
  projects: Project[]
  providers: Provider[]
  onClose: () => void
  onDispatched: (goalId: string) => Promise<void>
}

type ProposalField = 'objective' | 'boundaries' | 'acceptance'
type SizePreference = 'small' | 'medium' | 'large' | 'extra_large'

const fieldNames: Record<ProposalField, string> = {
  objective: '目标',
  boundaries: '边界',
  acceptance: '验收标准',
}

const sizingPresets: Record<SizePreference, Record<string, unknown>> = {
  small: sizing(1, 1, 2, 1, 60, 'low'),
  medium: sizing(1, 3, 4, 3, 180, 'medium'),
  large: sizing(3, 6, 10, 4, 300, 'medium'),
  extra_large: sizing(5, 12, 20, 6, 600, 'high'),
}

export function ProjectButlerDialog({
  initialProjectId,
  globalScope = false,
  projects,
  providers,
  onClose,
  onDispatched,
}: ProjectButlerDialogProps) {
  const activeProjects = useMemo(
    () => projects.filter((project) => project.status === 'active'),
    [projects],
  )
  const [projectId, setProjectId] = useState(
    initialProjectId || activeProjects[0]?.id || '',
  )
  const [provider, setProvider] = useState(
    providers.find((item) => item.name === 'codex' && item.enabled)?.name
      ?? providers.find((item) => item.enabled)?.name
      ?? 'codex',
  )
  const [sizePreference, setSizePreference] = useState<SizePreference>('medium')
  const [conversations, setConversations] = useState<ButlerConversation[]>([])
  const [conversation, setConversation] = useState<ButlerConversation | null>(null)
  const [content, setContent] = useState('')
  const [revisionField, setRevisionField] = useState<ProposalField | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    const load = globalScope
      ? api.globalButlerConversations()
      : projectId ? api.projectButlerConversations(projectId) : Promise.resolve([])
    load
      .then((items) => {
        if (!current) return
        setConversations(items)
        setConversation(
          items.find((item) => ['clarifying', 'awaiting_confirmation', 'provider_suspended'].includes(item.status))
          ?? items[0]
          ?? null,
        )
      })
      .catch((reason: unknown) => {
        if (current) setError(messageOf(reason))
      })
    return () => { current = false }
  }, [activeProjects, globalScope, projectId])

  async function startConversation(event: FormEvent) {
    event.preventDefault()
    const instruction = content.trim()
    if ((!globalScope && !projectId) || !instruction) return
    await run(async () => {
      const created = globalScope
        ? await api.startGlobalButler({ instruction, provider })
        : await api.startProjectButler(projectId, {
          instruction,
          source_type: 'human',
          source_id: 'owner',
          provider,
          sizing_inputs: sizingPresets[sizePreference],
        })
      setConversation(created)
      setConversations((items) => [created, ...items])
      setContent('')
    })
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    const message = content.trim()
    if (!conversation || !message) return
    await run(async () => {
      const updated = globalScope
        ? await api.sendGlobalButlerMessage(conversation, message)
        : await api.sendProjectButlerMessage(
          projectId,
          conversation,
          message,
          revisionField ?? undefined,
        )
      replaceConversation(updated)
      setContent('')
      setRevisionField(null)
    })
  }

  async function confirmProposal() {
    if (!conversation?.proposal_hash) return
    await run(async () => {
      const dispatched = await api.confirmProjectButler(projectId, conversation)
      replaceConversation(dispatched)
      if (!dispatched.goal_id) throw new Error('项目管家确认后未返回 Goal')
      await onDispatched(dispatched.goal_id)
    })
  }

  async function resumePlanner() {
    if (!conversation || globalScope) return
    await run(async () => {
      replaceConversation(await api.resumeProjectButler(projectId, conversation))
    })
  }

  async function run(operation: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await operation()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  function replaceConversation(updated: ButlerConversation) {
    setConversation(updated)
    setConversations((items) => [
      updated,
      ...items.filter((item) => item.id !== updated.id),
    ])
  }

  const canWrite = globalScope || conversation?.status === 'clarifying'
    || conversation?.status === 'awaiting_confirmation'

  return <div className="drawer-backdrop" onMouseDown={onClose}>
    <aside className="drawer butler-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <div className="drawer-head">
        <div><span className="kicker">{globalScope ? '跨项目路由 · 持久化会话' : '项目隔离 · 持久化会话'}</span><h1>{globalScope ? '与全局管家对话' : '直接与项目管家对话'}</h1></div>
        <button aria-label="关闭" onClick={onClose}><X size={18} /></button>
      </div>
      <div className="butler-project-bar">
        {!globalScope && <label>当前项目
          <select disabled={busy || !globalScope} value={projectId} onChange={(event) => {
            setProjectId(event.target.value)
            setConversation(null)
            setConversations([])
            setRevisionField(null)
          }}>
            <option value="">选择项目</option>
            {activeProjects.map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}
          </select>
        </label>}
        {globalScope && <div className="butler-scope-copy"><strong>全局工作区</strong><small>只读查询、跨项目汇总与路由；项目执行请切换项目范围后直接对话。</small></div>}
        <button type="button" disabled={busy || (!globalScope && !projectId)} onClick={() => {
          setConversation(null)
          setContent('')
          setRevisionField(null)
        }}><Plus size={15} />新对话</button>
      </div>
      {error && <div className="butler-error">{error}</div>}
      <div className="butler-layout">
        <nav className="butler-history" aria-label={`${globalScope ? '全局' : '项目'}管家历史会话`}>
          <span className="kicker">历史会话</span>
          {conversations.length ? conversations.map((item) => <button
            type="button"
            className={conversation?.id === item.id ? 'selected' : ''}
            key={item.id}
            onClick={() => { setConversation(item); if (item.project_id) setProjectId(item.project_id); setRevisionField(null); setContent('') }}
          >
            <strong>{String(item.spec.title || item.spec.objective || '未命名目标')}</strong>
            <small>{statusName(item.status)} · r{item.revision}</small>
          </button>) : <p>{globalScope ? '还没有全局管家对话。' : '这个项目还没有对话。'}</p>}
        </nav>
        <section className="butler-chat">
          {conversation ? <>
            <div
              className="butler-status-banner"
              data-testid="butler-status-banner"
              data-status={conversation.status}
            >
              <strong>{statusName(conversation.status)}</strong>
              <span>置信度 {conversation.confidence}%</span>
              {conversation.expected_field && <span>当前问题：{fieldNames[conversation.expected_field]}</span>}
              {conversation.status === 'clarifying' && <span>一次只问一个最有价值的问题</span>}
              {conversation.status === 'awaiting_confirmation' && <span>等待主人确认后才会派发</span>}
              {conversation.status === 'provider_suspended' && <span>模型调用失败，已停止机械降级</span>}
            </div>
            <div className="butler-messages" aria-live="polite">
              {conversation.messages.map((message) => <article
                className={message.sender_type === 'project_butler' ? 'butler-message' : 'human-message'}
                key={message.id}
              >
                <div>{message.sender_type === 'project_butler' ? <Robot size={16} /> : <ChatCircleDots size={16} />}<strong>{senderName(message.sender_type)}</strong><small>#{message.ordinal}</small></div>
                <p>{message.content}</p>
              </article>)}
            </div>
            {!globalScope && conversation.status === 'awaiting_confirmation' && <ProposalCard
              conversation={conversation}
              selectedField={revisionField}
              onSelectField={setRevisionField}
              onConfirm={confirmProposal}
              busy={busy}
            />}
            {!globalScope && conversation.status === 'dispatched' && <div className="butler-dispatched"><CheckCircle size={18} weight="fill" />方案已由人确认，Goal 已创建并交给调度链。</div>}
            {!globalScope && conversation.status === 'provider_suspended' && <div className="butler-error">
              <p>项目管家没有获得有效模型结果。本会话已暂停，不会把你的回复机械写入目标、边界或验收标准。</p>
              <button type="button" className="primary" disabled={busy} onClick={resumePlanner}>恢复 Provider 并续接本会话</button>
            </div>}
            {(globalScope || conversation.status !== 'dispatched') && <form className="butler-composer" onSubmit={sendMessage}>
              <textarea
                aria-label="回复项目管家"
                disabled={busy || !canWrite}
                value={content}
                onChange={(event) => setContent(event.target.value)}
                placeholder={globalScope
                  ? '继续查询所有项目的规范化状态，或要求路由到指定项目'
                  : conversation.status === 'clarifying'
                  ? '直接回复、质疑或纠正项目管家；模型会理解后更新方案'
                  : conversation.status === 'provider_suspended'
                    ? '先恢复 Provider；暂停期间不会接受机械问卷回复'
                  : revisionField
                    ? `说明新的${fieldNames[revisionField]}`
                    : '可直接质疑或纠正方案；也可选择目标、边界或验收标准进行精准修改'}
              />
              <button className="primary" disabled={busy || !canWrite || !content.trim()}><PaperPlaneTilt size={16} />发送</button>
            </form>}
          </> : <form className="butler-welcome" onSubmit={startConversation}>
            <Robot size={38} />
            <h2>告诉{globalScope ? '全局' : '项目'}管家你要完成什么</h2>
            <p>{globalScope ? '全局管家使用独立 Codex 会话，负责查询全部工作区资源、汇总状态和把工作引导到项目管家；它不会替项目直接修改代码。' : '可以直接发自然语言。目标、边界或验收不清楚时，管家一次只追问一个问题；方案得到你的明确确认前不会创建 Goal 或唤醒 Worker。'}</p>
            <textarea
              aria-label="给项目管家的指令"
              disabled={busy || (!globalScope && !projectId)}
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder="例如：基于最新 main 完成一次全面审查，并提出可执行改进方案；只审查，不修改项目文件……"
            />
            <div className="butler-intake-options">
              <label>默认 Worker Provider
                <select disabled={busy} value={provider} onChange={(event) => setProvider(event.target.value)}>
                  {providers.filter((item) => item.enabled && item.transport === 'host-bridge' && item.capabilities.includes('new_session')).map((item) => <option value={item.name} key={item.name}>{item.display_name}</option>)}
                </select>
              </label>
              {!globalScope && <label>目标体量
                <select disabled={busy} value={sizePreference} onChange={(event) => setSizePreference(event.target.value as SizePreference)}>
                  <option value="small">小型 · 单角色</option>
                  <option value="medium">中型 · 单角色</option>
                  <option value="large">大型 · 语义角色并行</option>
                  <option value="extra_large">超大型 · 语义角色并行</option>
                </select>
              </label>}
            </div>
            <button className="primary" disabled={busy || (!globalScope && !projectId) || !content.trim()}><PaperPlaneTilt size={16} />发送给{globalScope ? '全局' : '项目'}管家</button>
          </form>}
        </section>
      </div>
    </aside>
  </div>
}

function ProposalCard({
  conversation,
  selectedField,
  onSelectField,
  onConfirm,
  busy,
}: {
  conversation: ButlerConversation
  selectedField: ProposalField | null
  onSelectField: (field: ProposalField | null) => void
  onConfirm: () => void
  busy: boolean
}) {
  return <section className="butler-proposal">
    <header><div><span className="kicker">待人确认方案</span><h2>需求完整度 {conversation.confidence}%</h2></div><code>r{conversation.revision}</code></header>
    <dl>
      <ProposalFact label="目标" value={String(conversation.spec.objective ?? '')} />
      <ProposalFact label="边界" value={listValue(conversation.spec.boundaries)} />
      <ProposalFact label="验收标准" value={listValue(conversation.spec.acceptance)} />
    </dl>
    <p>置信度基于仍未解决、会改变方案的语义缺口，不是字段非空或固定三问。请核对内容；确认后才会拆分和唤醒 Worker。</p>
    <div className="butler-proposal-actions">
      {(Object.keys(fieldNames) as ProposalField[]).map((field) => <button
        type="button"
        className={selectedField === field ? 'selected' : ''}
        key={field}
        onClick={() => onSelectField(selectedField === field ? null : field)}
      >修改{fieldNames[field]}</button>)}
      <button type="button" className="primary" disabled={busy} onClick={onConfirm}><CheckCircle size={16} />确认方案并执行</button>
    </div>
  </section>
}

function ProposalFact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value || '未提供'}</dd></div>
}

function listValue(value: unknown) {
  return Array.isArray(value) ? value.map(String).join('；') : String(value ?? '')
}

function statusName(status: ButlerConversation['status']) {
  return status === 'clarifying' ? '澄清中'
    : status === 'awaiting_confirmation' ? '待确认'
      : status === 'provider_suspended' ? 'Provider 已挂起'
      : status === 'dispatched' ? '已执行'
        : '已拒绝'
}

function senderName(sender: ButlerConversation['messages'][number]['sender_type']) {
  return sender === 'project_butler' ? '项目管家'
    : sender === 'global_butler' ? '全局管家'
      : sender === 'agent' ? 'Agent'
        : '你'
}

function sizing(
  layers: number,
  components: number,
  files: number,
  verificationCommands: number,
  verificationSeconds: number,
  risk: 'low' | 'medium' | 'high',
) {
  return {
    layers_touched: layers,
    components_touched: components,
    estimated_files_changed: files,
    has_migration: false,
    has_deploy: false,
    verification_commands_count: verificationCommands,
    estimated_verification_seconds: verificationSeconds,
    external_dependencies_count: 0,
    risk_level: risk,
    independent_review_required: false,
    gate_artifact: true,
    gate_boundary: true,
    gate_verification: true,
    gate_dependency: true,
  }
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : '项目管家暂时无法响应'
}
