import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  ArrowsClockwise, CheckCircle, Circle, Clock, Coins, Cpu, FolderOpen,
  Gear, HardDrives, Kanban, ListChecks, MagicWand, Network, Pause, Play,
  Plus, Pulse, Robot, ShieldCheck, TerminalWindow, WarningCircle, X,
} from '@phosphor-icons/react'
import {
  api, AuditEntry, Convention, ConventionSuggestion, OutboxEvent, PermissionGrant,
  Project, Provider, RuntimeHealth, RuntimeSettings, SchedulerStatus, Task, TaskEvent,
  TaskStatus, Usage, Worker,
} from './api'

type Health = {
  status: string
  version: string
  database: { status: string; journal_mode: string; migration_count: number }
}

type View = 'board' | 'tasks' | 'projects' | 'workers' | 'providers' | 'usage' | 'audit' | 'settings'

const roleNames: Record<string, string> = {
  coordination: '协调', fullstack: '全栈', web3: 'Web3', devops_sre: '运维', verification: '验证',
}

const statusNames: Record<TaskStatus, string> = {
  ready: '待执行', running: '执行中', stopping: '停止中', verifying: '验证中', completed: '已完成',
  terminal_failed: '已熔断', needs_human: '需要处理', cancelled: '已取消', paused: '已暂停',
}

const navItems: { id: View; label: string; icon: typeof Kanban }[] = [
  { id: 'board', label: '任务看板', icon: Kanban },
  { id: 'tasks', label: '任务', icon: ListChecks },
  { id: 'projects', label: '项目', icon: FolderOpen },
  { id: 'workers', label: 'Worker', icon: Robot },
  { id: 'providers', label: 'Provider', icon: TerminalWindow },
  { id: 'usage', label: 'Token', icon: Coins },
  { id: 'audit', label: '审计', icon: ShieldCheck },
  { id: 'settings', label: '设置', icon: Gear },
]

const initialTask = {
  title: '', objective: '', projectId: '', role: 'fullstack', provider: 'generic-command',
  networkRequirement: 'none', qualityProfile: 'balanced', executable: 'python3',
  argumentsJson: '["-c", "from pathlib import Path; Path(\'result.txt\').write_text(\'quality-pass\', encoding=\'utf-8\')"]',
  verifyPath: 'result.txt', verifyText: 'quality-pass',
}

export function App() {
  const [view, setView] = useState<View>('board')
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [settings, setSettings] = useState<RuntimeSettings | null>(null)
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null)
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [outbox, setOutbox] = useState<OutboxEvent[]>([])
  const [permissions, setPermissions] = useState<PermissionGrant[]>([])
  const [convention, setConvention] = useState<Convention | null>(null)
  const [suggestion, setSuggestion] = useState<ConventionSuggestion | null>(null)
  const [refineProvider, setRefineProvider] = useState('simple-worker')
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [selectedProject, setSelectedProject] = useState<string>('all')
  const [taskForm, setTaskForm] = useState(initialTask)
  const [projectForm, setProjectForm] = useState({ name: '', path: '', hostPath: '' })
  const [showCreateTask, setShowCreateTask] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refreshTasks = useCallback(async () => {
    const next = await api.tasks()
    setTasks(next)
    setSelectedTask((current) => next.find((task) => task.id === current?.id) ?? current)
  }, [])
  const refreshProjects = useCallback(async () => setProjects(await api.projects()), [])
  const refreshProviders = useCallback(async () => setProviders(await api.providers()), [])

  useEffect(() => {
    Promise.all([
      fetch('/health').then((response) => response.json() as Promise<Health>).then(setHealth),
      api.tasks().then(setTasks), api.projects().then(setProjects), api.providers().then(setProviders),
      api.settings().then(setSettings), api.schedulerStatus().then(setScheduler),
      api.runtimeHealth().then(setRuntimeHealth), api.usage().then(setUsage),
      api.audit().then(setAudit), api.outbox().then(setOutbox), api.permissions().then(setPermissions),
      api.convention('global', 'global').then(setConvention),
    ]).catch((reason: unknown) => setError(messageOf(reason)))
  }, [])

  useEffect(() => {
    if (!selectedTask) return
    api.taskEvents(selectedTask.id).then(setEvents).catch((reason: unknown) => setError(messageOf(reason)))
  }, [selectedTask])

  const visibleTasks = useMemo(
    () => selectedProject === 'all' ? tasks : tasks.filter((task) => task.project_id === selectedProject),
    [selectedProject, tasks],
  )
  const workers = useMemo(
    () => projects.flatMap((project) => project.workers.map((worker) => ({ project, worker }))),
    [projects],
  )
  const effectiveRefineProvider = useMemo(() => {
    const selected = providers.find((provider) => provider.name === refineProvider)
    if (selected?.status === 'available') return refineProvider
    return providers.find(
      (provider) => provider.enabled && provider.status === 'available' && provider.capabilities.includes('refine_convention'),
    )?.name ?? refineProvider
  }, [providers, refineProvider])

  async function action(run: () => Promise<void>, success?: string) {
    setBusy(true); setError(null); setNotice(null)
    try { await run(); if (success) setNotice(success) } catch (reason) { setError(messageOf(reason)) } finally { setBusy(false) }
  }

  async function createTask(event: FormEvent) {
    event.preventDefault()
    await action(async () => {
      const args = JSON.parse(taskForm.argumentsJson) as unknown
      if (!Array.isArray(args) || args.some((value) => typeof value !== 'string')) throw new Error('参数必须是字符串 JSON 数组')
      const created = await api.createTask({
        title: taskForm.title, objective: taskForm.objective, project_id: taskForm.projectId,
        role: taskForm.role, provider: taskForm.provider, network_requirement: taskForm.networkRequirement,
        quality_profile: taskForm.qualityProfile,
        command: { argv: [taskForm.executable, ...args], timeout_seconds: taskForm.provider === 'generic-command' ? 60 : 600 },
        verification: [
          { kind: 'exit_code', expected: 0 },
          { kind: 'file_exists', path: taskForm.verifyPath },
          { kind: 'file_contains', path: taskForm.verifyPath, contains: taskForm.verifyText },
        ],
      })
      await refreshTasks(); setSelectedTask(created); setShowCreateTask(false); setView('tasks')
    }, '任务已加入队列')
  }

  async function createProject(event: FormEvent) {
    event.preventDefault()
    await action(async () => {
      const created = await api.createProject({
        name: projectForm.name, path: projectForm.path,
        host_path: projectForm.hostPath.trim() || null,
      })
      await refreshProjects(); setSelectedProject(created.id)
      setTaskForm((current) => ({ ...current, projectId: created.id }))
      setProjectForm({ name: '', path: '', hostPath: '' })
    }, '项目已注册')
  }

  async function controlTask(task: Task, control: 'pause' | 'resume' | 'cancel' | 'needs_human') {
    await action(async () => { await api.controlTask(task, control, `operator_${control}`); await refreshTasks() })
  }

  async function driveTask(task: Task) {
    await action(async () => { const next = await api.driveTask(task); await refreshTasks(); setSelectedTask(next) }, '执行完成，已进入质量门')
  }

  async function probeProvider(provider: Provider) {
    await action(async () => { await api.probeProvider(provider.name); await refreshProviders() }, `${provider.display_name} 探测完成`)
  }

  async function toggleProvider(provider: Provider) {
    await action(async () => {
      await api.updateProvider({ ...provider, enabled: !provider.enabled }); await refreshProviders()
    }, `${provider.display_name} 已${provider.enabled ? '停用' : '启用'}`)
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault(); if (!settings) return
    await action(async () => { setSettings(await api.updateSettings(settings)); setScheduler(await api.schedulerStatus()) }, '设置已保存')
  }

  async function loadConvention(scope: Convention['scope'], scopeId: string) {
    if (!scopeId) return
    await action(async () => { setConvention(await api.convention(scope, scopeId)); setSuggestion(null) })
  }

  async function saveConvention(event: FormEvent) {
    event.preventDefault(); if (!convention) return
    await action(async () => { setConvention(await api.updateConvention(convention)); setSuggestion(null) }, 'Convention 已保存')
  }

  async function refineConvention() {
    if (!convention) return
    const projectId = convention.scope === 'project' ? convention.scope_id : selectedProject === 'all' ? projects[0]?.id ?? null : selectedProject
    await action(async () => {
      setSuggestion(await api.refineConvention(convention, effectiveRefineProvider, projectId))
    }, '精炼建议已生成，尚未写入')
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><HardDrives size={20} weight="fill" /></div><div><strong>Plow Whip</strong><span>无人值守控制台</span></div></div>
        <div className="principle">保障质量的前提下实现无人值守完成，尽量减少 Token 消费。</div>
        <div className="top-status">
          <StatusDot ok={health?.status === 'ok'} label={health?.status === 'ok' ? '控制面在线' : '连接中'} />
          <StatusDot ok={Boolean(scheduler?.engine.active)} label={scheduler?.engine.active ? 'Crontab 运行中' : 'Crontab 停止'} />
          <StatusDot ok={runtimeHealth?.connectivity === 'online'} label={`网络 ${runtimeHealth?.connectivity ?? '检测中'}`} />
        </div>
      </header>

      <nav className="tabs" aria-label="主导航">
        {navItems.map(({ id, label, icon: Icon }) => <button key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}><Icon size={16} />{label}</button>)}
      </nav>

      <main>
        <div className="context-bar">
          <div><span>项目范围</span><select value={selectedProject} onChange={(event) => setSelectedProject(event.target.value)}><option value="all">全部项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></div>
          <div className="context-actions"><button className="ghost" onClick={() => action(async () => { await Promise.all([refreshTasks(), refreshProjects(), refreshProviders()]) }, '状态已刷新')}><ArrowsClockwise size={16} />刷新</button><button className="primary" onClick={() => setShowCreateTask(true)}><Plus size={16} weight="bold" />新建任务</button></div>
        </div>

        {error && <div className="banner error"><WarningCircle size={18} weight="fill" /><span>{error}</span><button aria-label="关闭错误" onClick={() => setError(null)}><X size={16} /></button></div>}
        {notice && <div className="banner success"><CheckCircle size={18} weight="fill" /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        {outbox.filter((event) => event.event_type === 'task.needs_human' && !event.delivered_at).length > 0 && <div className="banner warning"><WarningCircle size={18} weight="fill" />存在需要人工判断的任务，自动调度已对这些任务停手。</div>}

        {view === 'board' && <Board tasks={visibleTasks} projects={projects} workers={workers.map((item) => item.worker)} providers={providers} usage={usage} onSelect={(task) => { setSelectedTask(task); setView('tasks') }} />}
        {view === 'tasks' && <TasksView tasks={visibleTasks} selected={selectedTask} events={events} busy={busy} onSelect={setSelectedTask} onDrive={driveTask} onControl={controlTask} />}
        {view === 'projects' && <ProjectsView projects={projects} form={projectForm} setForm={setProjectForm} onCreate={createProject} onRelease={(project) => action(async () => { await api.releaseProject(project.id); await refreshProjects() }, '项目已完成并释放 Worker')} busy={busy} />}
        {view === 'workers' && <WorkersView items={workers.filter(({ project }) => selectedProject === 'all' || project.id === selectedProject)} providers={providers} busy={busy} onRebind={(worker, provider) => action(async () => { await api.rebindWorker(worker.id, provider); await refreshProjects() }, 'Worker 已轮转并重新绑定')} />}
        {view === 'providers' && <ProvidersView providers={providers} busy={busy} onProbe={probeProvider} onToggle={toggleProvider} />}
        {view === 'usage' && <UsageView usage={usage} projects={projects} tasks={tasks} />}
        {view === 'audit' && <AuditView audit={audit} permissions={permissions} onRefresh={() => api.audit().then(setAudit)} />}
        {view === 'settings' && <SettingsView settings={settings} setSettings={setSettings} scheduler={scheduler} convention={convention} setConvention={setConvention} suggestion={suggestion} setSuggestion={setSuggestion} projects={projects} tasks={tasks} providers={providers} refineProvider={effectiveRefineProvider} setRefineProvider={setRefineProvider} busy={busy} onSaveSettings={saveSettings} onTick={() => action(async () => { await api.schedulerTick(); setScheduler(await api.schedulerStatus()); await refreshTasks() }, 'Tick 已完成')} onLoadConvention={loadConvention} onSaveConvention={saveConvention} onRefine={refineConvention} />}
      </main>

      {showCreateTask && <TaskDrawer form={taskForm} setForm={setTaskForm} projects={projects} providers={providers} busy={busy} onClose={() => setShowCreateTask(false)} onSubmit={createTask} />}
    </div>
  )
}

function Board({ tasks, projects, workers, providers, usage, onSelect }: { tasks: Task[]; projects: Project[]; workers: Worker[]; providers: Provider[]; usage: Usage | null; onSelect: (task: Task) => void }) {
  const columns: { title: string; statuses: TaskStatus[]; tone: string }[] = [
    { title: '待执行', statuses: ['ready', 'paused'], tone: 'blue' },
    { title: '执行中', statuses: ['running', 'stopping'], tone: 'violet' },
    { title: '质量验证', statuses: ['verifying'], tone: 'yellow' },
    { title: '已终态', statuses: ['completed', 'terminal_failed', 'cancelled', 'needs_human'], tone: 'green' },
  ]
  return <>
    <section className="metrics-strip">
      <Metric icon={FolderOpen} label="活跃项目" value={projects.filter((p) => p.status === 'active').length} hint="可并行" />
      <Metric icon={Robot} label="在线 Worker" value={workers.filter((w) => w.status !== 'released').length} hint={`${workers.filter((w) => w.status === 'busy').length} 忙碌`} />
      <Metric icon={TerminalWindow} label="可用 Provider" value={providers.filter((p) => p.status === 'available').length} hint={`${providers.filter((p) => p.enabled).length} 已启用`} />
      <Metric icon={Coins} label="今日 Token" value={usage?.total_tokens ?? 0} hint="控制链 0" />
    </section>
    <section className="panel board-panel"><div className="panel-heading"><div><span className="kicker">全局任务流</span><h1>任务看板</h1></div><span className="muted">同一角色单会话 · 租约隔离 · 证据完成</span></div>
      <div className="kanban-grid">{columns.map((column) => { const items = tasks.filter((task) => column.statuses.includes(task.status)); return <div className="kanban-column" key={column.title}><div className={`column-title ${column.tone}`}><span>{column.title}</span><b>{items.length}</b></div><div className="column-body">{items.length ? items.map((task) => <button className="task-card" key={task.id} onClick={() => onSelect(task)}><div><StatusPill status={task.status} /><small>{task.provider}</small></div><strong>{task.title}</strong><p>{task.objective}</p><footer><span>{task.attempts_used}/{task.max_attempts} 次</span><span>{task.tokens_used} Token</span></footer></button>) : <div className="empty-column">当前没有任务</div>}</div></div> })}</div>
    </section>
  </>
}

function TasksView({ tasks, selected, events, busy, onSelect, onDrive, onControl }: { tasks: Task[]; selected: Task | null; events: TaskEvent[]; busy: boolean; onSelect: (task: Task) => void; onDrive: (task: Task) => void; onControl: (task: Task, action: 'pause' | 'resume' | 'cancel' | 'needs_human') => void }) {
  return <div className="split-layout"><section className="panel list-panel"><div className="panel-heading"><div><span className="kicker">任务队列</span><h1>{tasks.length} 个任务</h1></div></div><div className="dense-list">{tasks.map((task) => <button key={task.id} className={selected?.id === task.id ? 'selected' : ''} onClick={() => onSelect(task)}><span className="list-icon"><ListChecks size={18} /></span><span><strong>{task.title}</strong><small>{task.provider} · {statusNames[task.status]}</small></span><StatusPill status={task.status} /></button>)}</div></section>
    <section className="panel detail-panel">{selected ? <><div className="panel-heading"><div><span className="kicker">{selected.id}</span><h1>{selected.title}</h1></div><StatusPill status={selected.status} /></div><p className="objective">{selected.objective}</p><div className="detail-actions">{selected.status === 'ready' && <button className="primary" disabled={busy} onClick={() => onDrive(selected)}><Play size={16} weight="fill" />立即驱动</button>}{selected.status === 'ready' && <button disabled={busy} onClick={() => onControl(selected, 'pause')}><Pause size={16} />暂停</button>}{selected.status === 'paused' && <button disabled={busy} onClick={() => onControl(selected, 'resume')}><Play size={16} />恢复</button>}{!['completed', 'terminal_failed', 'cancelled'].includes(selected.status) && <button className="danger" disabled={busy} onClick={() => onControl(selected, 'cancel')}><X size={16} />取消</button>}</div><div className="facts"><Fact label="Provider" value={selected.provider} /><Fact label="质量档位" value={selected.quality_profile} /><Fact label="Worker" value={selected.worker_id ?? '尚未领取'} mono /><Fact label="资源锁" value={selected.resource_key ?? '项目级默认锁'} mono /><Fact label="重复失败" value={`${selected.same_failure_count} / 无进展 ${selected.no_progress_count}`} /><Fact label="Token" value={`${selected.tokens_used} / ${selected.token_budget}`} /></div><div className="timeline"><h2>状态事件</h2>{events.map((event) => <div key={event.sequence}><span>{event.sequence}</span><strong>{event.event_type}</strong><small>{event.created_at}</small></div>)}</div></> : <div className="empty-state"><ListChecks size={38} /><h2>选择一个任务</h2><p>查看租约、会话绑定、质量证据与状态历史。</p></div>}</section></div>
}

function ProjectsView({ projects, form, setForm, onCreate, onRelease, busy }: { projects: Project[]; form: { name: string; path: string; hostPath: string }; setForm: (value: { name: string; path: string; hostPath: string }) => void; onCreate: (event: FormEvent) => void; onRelease: (project: Project) => void; busy: boolean }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">多项目并行</span><h1>项目注册表</h1></div></div><div className="project-grid">{projects.map((project) => <article className="project-card" key={project.id}><div className="project-top"><FolderOpen size={20} /><StatusDot ok={project.status === 'active'} label={project.status === 'active' ? '进行中' : '已完成'} /></div><h2>{project.name}</h2><code>{project.path}</code>{project.host_path && <code className="muted-code">Host · {project.host_path}</code>}<div className="mini-stats"><span>{project.roles.length} 角色</span><span>{project.workers.length} Worker</span></div>{project.status === 'active' && <button disabled={busy} onClick={() => onRelease(project)}>完成并释放 Worker</button>}</article>)}</div></section>
    <form className="panel form-panel" onSubmit={onCreate}><div className="panel-heading"><div><span className="kicker">项目 → 角色 → CLI 会话</span><h1>注册项目</h1></div></div><Field label="项目名"><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：支付网关" /></Field><Field label="容器路径"><input required value={form.path} onChange={(event) => setForm({ ...form, path: event.target.value })} placeholder="/projects/payment" /></Field><Field label="本机路径（Host CLI 使用）"><input value={form.hostPath} onChange={(event) => setForm({ ...form, hostPath: event.target.value })} placeholder="/Users/name/work/payment" /></Field><p className="form-help">容器 Worker 使用容器路径；Codex/Cursor/simple-worker 通过 Host Bridge 使用本机路径。两者必须指向同一份项目文件。</p><button className="primary" disabled={busy}><Plus size={16} />注册项目</button></form></div>
}

function WorkersView({ items, providers, busy, onRebind }: { items: { project: Project; worker: Worker }[]; providers: Provider[]; busy: boolean; onRebind: (worker: Worker, provider: string) => void }) {
  return <section className="panel"><div className="panel-heading"><div><span className="kicker">项目 · 角色 · CLI 会话</span><h1>Worker 状态</h1></div><span className="muted">项目完成后自动归档并释放</span></div><div className="worker-table"><div className="table-head"><span>项目 / 角色</span><span>Provider</span><span>会话</span><span>状态</span><span>操作</span></div>{items.length ? items.map(({ project, worker }) => <div className="table-row" key={worker.id}><div><strong>{project.name}</strong><small>{roleNames[worker.role] ?? worker.role}</small></div><div><code>{worker.provider}</code><small>第 {worker.session_generation} 代</small></div><div><code>{worker.external_session_id ?? '等待 CLI 建立'}</code><small>{worker.last_seen_at ?? '尚未执行'}</small></div><div><StatusDot ok={worker.status === 'idle'} label={worker.status === 'idle' ? '空闲' : worker.status === 'busy' ? '工作中' : '已释放'} />{worker.last_error && <small className="danger-text">{worker.last_error}</small>}</div><div><select disabled={busy || worker.status !== 'idle'} value={worker.provider} onChange={(event) => onRebind(worker, event.target.value)}>{providers.filter((provider) => provider.enabled).map((provider) => <option key={provider.name} value={provider.name}>{provider.display_name}</option>)}</select></div></div>) : <div className="empty-state compact"><Robot size={34} /><h2>暂无 Worker</h2><p>首个项目任务被领取时创建，并持续工作到项目完成。</p></div>}</div></section>
}

function ProvidersView({ providers, busy, onProbe, onToggle }: { providers: Provider[]; busy: boolean; onProbe: (provider: Provider) => void; onToggle: (provider: Provider) => void }) {
  return <section className="panel"><div className="panel-heading"><div><span className="kicker">打工仔池</span><h1>Worker Provider</h1></div><span className="muted">CLI 只负责干活；控制、探测、租约不调用模型</span></div><div className="provider-grid">{providers.map((provider) => <article className={`provider-card ${provider.enabled ? '' : 'disabled'}`} key={provider.name}><div className="provider-head"><div className="provider-icon">{provider.transport === 'host-bridge' ? <TerminalWindow size={20} /> : <Cpu size={20} />}</div><StatusDot ok={provider.status === 'available'} label={provider.enabled ? provider.status === 'available' ? '可用' : provider.status === 'unknown' ? '待探测' : '不可用' : '已停用'} /></div><h2>{provider.display_name}</h2><code>{provider.name}</code><dl><div><dt>适配器</dt><dd>{provider.adapter}</dd></div><div><dt>运行位置</dt><dd>{provider.transport === 'host-bridge' ? '本机 Host Bridge' : '容器内'}</dd></div><div><dt>可执行文件</dt><dd>{provider.executable ?? '内置'}</dd></div><div><dt>能力</dt><dd>{provider.capabilities.join(' · ')}</dd></div></dl><p className="provider-reason">{provider.reason ?? '已就绪'}{provider.last_probed_at && ` · ${provider.last_probed_at}`}</p><div className="card-actions"><button disabled={busy || !provider.enabled} onClick={() => onProbe(provider)}><Pulse size={16} />0 Token 探测</button>{provider.name !== 'generic-command' && <button disabled={busy} onClick={() => onToggle(provider)}>{provider.enabled ? '停用' : '启用'}</button>}</div></article>)}</div><div className="boundary-note"><ShieldCheck size={20} /><div><strong>Host Bridge 安全边界</strong><p>只接受 Codex、Cursor、JSON Worker 三类结构化请求；固定 argv、固定项目根、无 shell 拼接。容器无法直接执行 macOS 二进制，因此本机 CLI 必须经过这条窄桥。</p></div></div></section>
}

function UsageView({ usage, projects, tasks }: { usage: Usage | null; projects: Project[]; tasks: Task[] }) {
  return <div className="settings-layout"><section className="metrics-strip"><Metric icon={Coins} label="总消费" value={usage?.total_tokens ?? 0} hint="可计量模型调用" /><Metric icon={Clock} label="控制链" value={usage?.control_tokens ?? 0} hint="永远应为 0" /><Metric icon={Network} label="输入" value={usage?.input_tokens ?? 0} hint="编译后 Context" /><Metric icon={CheckCircle} label="输出" value={usage?.output_tokens ?? 0} hint="Worker 结果" /></section><section className="panel"><div className="panel-heading"><div><span className="kicker">Token 账本</span><h1>消费明细</h1></div></div><div className="usage-columns"><div><h2>按项目</h2>{usage?.projects.length ? usage.projects.map((item) => <div className="usage-row" key={item.project_id ?? 'none'}><span>{projects.find((project) => project.id === item.project_id)?.name ?? '未绑定项目'}</span><strong>{item.tokens}</strong></div>) : <p className="muted">尚无模型消费。</p>}</div><div><h2>按任务</h2>{usage?.tasks.length ? usage.tasks.map((item) => <div className="usage-row" key={item.task_id}><span>{tasks.find((task) => task.id === item.task_id)?.title ?? item.task_id}</span><strong>{item.tokens}</strong></div>) : <p className="muted">调度与探测不会制造账单。</p>}</div></div></section></div>
}

function AuditView({ audit, permissions, onRefresh }: { audit: AuditEntry[]; permissions: PermissionGrant[]; onRefresh: () => void }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">本地不可变审计</span><h1>变更记录</h1></div><button onClick={onRefresh}><ArrowsClockwise size={16} />刷新</button></div><div className="audit-table"><div className="table-head"><span>#</span><span>方法</span><span>路径</span><span>状态</span><span>时间</span></div>{audit.map((entry) => <div className="table-row" key={entry.sequence}><span>{entry.sequence}</span><strong>{entry.method}</strong><code>{entry.path}</code><span>{entry.status_code}</span><small>{entry.created_at}</small></div>)}</div></section><section className="panel"><div className="panel-heading"><div><span className="kicker">权限边界</span><h1>权限决策</h1></div></div>{permissions.map((permission) => <div className="usage-row" key={permission.id}><span>{permission.capability} · {permission.resource}</span><strong>{permission.decision}</strong></div>)}</section></div>
}

function SettingsView(props: { settings: RuntimeSettings | null; setSettings: (value: RuntimeSettings) => void; scheduler: SchedulerStatus | null; convention: Convention | null; setConvention: (value: Convention) => void; suggestion: ConventionSuggestion | null; setSuggestion: (value: ConventionSuggestion | null) => void; projects: Project[]; tasks: Task[]; providers: Provider[]; refineProvider: string; setRefineProvider: (value: string) => void; busy: boolean; onSaveSettings: (event: FormEvent) => void; onTick: () => void; onLoadConvention: (scope: Convention['scope'], id: string) => void; onSaveConvention: (event: FormEvent) => void; onRefine: () => void }) {
  const { settings, setSettings, scheduler, convention, setConvention, suggestion, setSuggestion, projects, tasks, providers, refineProvider, setRefineProvider, busy, onSaveSettings, onTick, onLoadConvention, onSaveConvention, onRefine } = props
  return <div className="settings-layout"><section className="panel cron-panel"><div className="panel-heading"><div><span className="kicker">单一全局 Crontab</span><h1>无人值守调度</h1></div><StatusDot ok={Boolean(scheduler?.engine.active)} label={scheduler?.engine.active ? '运行中' : '未运行'} /></div><div className="facts"><Fact label="引擎" value={scheduler ? `${scheduler.engine.managed_by} · ${scheduler.engine.backend}` : '检测中'} /><Fact label="下次执行" value={scheduler?.schedule.next_run_at ?? '尚未计算'} /><Fact label="Fencing" value={String(scheduler?.runtime.fencing_token ?? 0)} /><Fact label="最近 Tick" value={scheduler?.runtime.last_tick_at ?? '尚未执行'} /></div><button className="primary" disabled={busy} onClick={onTick}><Play size={16} />立即运行 Tick</button></section>
    {settings && <form className="panel form-panel" onSubmit={onSaveSettings}><div className="panel-heading"><div><span className="kicker">设置修订 {settings.revision}</span><h1>Crontab 与防循环</h1></div></div><div className="form-grid"><Field label="Cron 表达式"><input value={settings.values.cron_expression} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_expression: event.target.value } })} /></Field><Field label="时区"><input value={settings.values.cron_timezone} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_timezone: event.target.value } })} /></Field><Field label="错过执行"><select value={settings.values.cron_misfire_policy} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_misfire_policy: event.target.value as 'catch_up_once' | 'skip' } })}><option value="catch_up_once">恢复后只补跑一次</option><option value="skip">跳过</option></select></Field><NumberField label="最大并行 Worker" value={settings.values.max_parallel_workers} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_parallel_workers: value } })} /><NumberField label="同类失败熔断" value={settings.values.max_same_failure} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_same_failure: value } })} /><NumberField label="无进展熔断" value={settings.values.max_no_progress} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_no_progress: value } })} /><NumberField label="Context 最大字节" value={settings.values.context_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, context_max_bytes: value } })} /><NumberField label="文件轮转字节" value={settings.values.rotation_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, rotation_max_bytes: value } })} /><NumberField label="默认 Token 预算" value={settings.values.task_default_token_budget} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, task_default_token_budget: value } })} /></div><label className="toggle-row"><input type="checkbox" checked={settings.values.cron_enabled} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_enabled: event.target.checked } })} /><span>启用容器内 Crontab</span></label><label className="toggle-row"><input type="checkbox" checked={settings.values.auto_dispatch} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, auto_dispatch: event.target.checked } })} /><span>自动派发待执行任务</span></label><button className="primary" disabled={busy}>保存设置</button></form>}
    {convention && <form className="panel convention-panel" onSubmit={onSaveConvention}><div className="panel-heading"><div><span className="kicker">全局 · 项目 · Task</span><h1>Convention 编辑器</h1></div><span className="revision">修订 {convention.revision}</span></div><div className="form-grid two"><Field label="作用域"><select value={convention.scope} onChange={(event) => { const scope = event.target.value as Convention['scope']; const id = scope === 'global' ? 'global' : scope === 'project' ? projects[0]?.id ?? '' : tasks[0]?.id ?? ''; onLoadConvention(scope, id) }}><option value="global">全局</option><option value="project">项目</option><option value="task">Task</option></select></Field><Field label="目标"><select disabled={convention.scope === 'global'} value={convention.scope_id} onChange={(event) => onLoadConvention(convention.scope, event.target.value)}>{convention.scope === 'global' ? <option value="global">全局</option> : convention.scope === 'project' ? projects.map((project) => <option value={project.id} key={project.id}>{project.name}</option>) : tasks.map((task) => <option value={task.id} key={task.id}>{task.title}</option>)}</select></Field></div><div className="editor-grid"><div><label>当前 Convention</label><textarea value={convention.content} onChange={(event) => setConvention({ ...convention, content: event.target.value })} placeholder="写下质量门、权限边界和必须验证的完成条件。" /></div><div><label>{suggestion ? `${suggestion.provider} 精炼建议` : 'Worker 精炼建议'}</label><textarea value={suggestion?.suggestion ?? ''} readOnly placeholder="点击“模型精炼”后在这里审阅建议；不会自动覆盖原文。" /></div></div><div className="detail-actions"><select className="inline-select" value={refineProvider} onChange={(event) => setRefineProvider(event.target.value)}>{providers.filter((provider) => provider.enabled && provider.capabilities.includes('refine_convention')).map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}{provider.status === 'available' ? '' : '（当前不可用）'}</option>)}</select><button type="button" disabled={busy || !convention.content.trim()} onClick={onRefine}><MagicWand size={16} />模型精炼（计 Token）</button>{suggestion && <button type="button" onClick={() => { setConvention({ ...convention, content: suggestion.suggestion }); setSuggestion(null) }}><CheckCircle size={16} />采用建议</button>}<button className="primary" disabled={busy}>保存 Convention</button></div><p className="form-help">精炼是明确的模型动作，会记录 Token；保存仍需人工确认。Crontab、探测和状态扫描不会调用模型。</p></form>}
  </div>
}

function TaskDrawer({ form, setForm, projects, providers, busy, onClose, onSubmit }: { form: typeof initialTask; setForm: (value: typeof initialTask) => void; projects: Project[]; providers: Provider[]; busy: boolean; onClose: () => void; onSubmit: (event: FormEvent) => void }) {
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-head"><div><span className="kicker">质量驱动</span><h1>新建任务</h1></div><button aria-label="关闭" onClick={onClose}><X size={18} /></button></div><form onSubmit={onSubmit}><Field label="标题"><input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="明确、可验证的结果" /></Field><Field label="目标"><textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="Worker 必须完成什么？" /></Field><div className="form-grid two"><Field label="项目"><select required value={form.projectId} onChange={(event) => setForm({ ...form, projectId: event.target.value })}><option value="">选择项目</option>{projects.filter((p) => p.status === 'active').map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></Field><Field label="角色"><select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}>{Object.entries(roleNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></Field><Field label="Provider"><select value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value })}>{providers.filter((provider) => provider.enabled).map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}</option>)}</select></Field><Field label="质量档位"><select value={form.qualityProfile} onChange={(event) => setForm({ ...form, qualityProfile: event.target.value })}><option value="fast">快速</option><option value="balanced">均衡</option><option value="strict">严格</option></select></Field></div>{form.provider === 'generic-command' && <><Field label="可执行文件"><input value={form.executable} onChange={(event) => setForm({ ...form, executable: event.target.value })} /></Field><Field label="参数 JSON"><textarea className="mono" value={form.argumentsJson} onChange={(event) => setForm({ ...form, argumentsJson: event.target.value })} /></Field></>}<div className="form-grid two"><Field label="验证文件"><input value={form.verifyPath} onChange={(event) => setForm({ ...form, verifyPath: event.target.value })} /></Field><Field label="必须包含"><input value={form.verifyText} onChange={(event) => setForm({ ...form, verifyText: event.target.value })} /></Field></div><button className="primary full" disabled={busy || !projects.length}><Plus size={16} />加入任务队列</button></form></aside></div>
}

function Metric({ icon: Icon, label, value, hint }: { icon: typeof Kanban; label: string; value: number; hint: string }) { return <article><div className="metric-icon"><Icon size={18} /></div><div><span>{label}</span><strong>{value.toLocaleString()}</strong></div><small>{hint}</small></article> }
function StatusDot({ ok, label }: { ok: boolean; label: string }) { return <span className={`status-dot ${ok ? 'ok' : 'off'}`}><Circle size={8} weight="fill" />{label}</span> }
function StatusPill({ status }: { status: TaskStatus }) { return <span className={`status-pill status-${status}`}>{statusNames[status]}</span> }
function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div><dt>{label}</dt><dd className={mono ? 'mono' : ''}>{value}</dd></div> }
function Field({ label, children }: { label: string; children: React.ReactNode }) { return <label className="field"><span>{label}</span>{children}</label> }
function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) { return <Field label={label}><input type="number" value={value} onChange={(event) => onChange(Number(event.target.value))} /></Field> }
function messageOf(reason: unknown) { return reason instanceof Error ? reason.message : '发生未知错误' }
