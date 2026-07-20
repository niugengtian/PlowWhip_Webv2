import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowsClockwise, ChatCircleDots, CheckCircle, Circle, Clock, Coins, Copy, FileCode, FolderOpen,
  Gear, HardDrives, Kanban, ListChecks, MagicWand, Network, Pause, Play,
  Plus, Pulse, Robot, ShieldCheck, TerminalWindow, WarningCircle, X,
} from '@phosphor-icons/react'
import {
  api, AlertIncident, Alerts, AuditEntry, BehaviorBaseline, Convention, ConventionSuggestion, GlobalButlerOverview, Goal, OutboxEvent, PermissionGrant,
  Project, Provider, RoleInstance, RuntimeHealth, RuntimeSettings, RuntimeSettingsOverride, SchedulerStatus, SessionBinding, Task, TaskArtifact, TaskEvent,
  TaskDeletionEligibility, TaskSizingEstimate, TaskSizingInputs, TaskStatus, Usage, UsageDailyBreakdown,
  UsageDailySeries, Worker, WorkerDetail, WorkerStream,
} from './api'
import { ButlerConsole } from './components/ButlerConsole'
import { ProjectButlerDialog } from './components/ProjectButlerDialog'
import { TokenPieChart, TokenTrendChart } from './components/TokenUsageCharts'
import { startLiveRefresh } from './liveRefresh'

type Health = {
  status: string
  version: string
  database: { status: string; journal_mode: string; migration_count: number }
}

type View = 'butler' | 'projects' | 'tasks' | 'usage' | 'alerts' | 'settings'
type ProjectScope = Readonly<{ projectId: string; generation: number }>

const roleNames: Record<string, string> = {
  butler: '常驻管家',
  coordination: '协调 / PM（遗留）',
  backend: '后端',
  frontend: '前端',
  ui: 'UI',
  devops_sre: 'DevOps / SRE',
  verification: '验证实现（非独立证据）',
  fullstack: '全栈（遗留）',
  web3: 'Web3（遗留）',
}

const statusNames: Record<TaskStatus, string> = {
  ready: '待执行', running: '执行中', stopping: '停止中', verifying: '验证中', completed: '已完成',
  terminal_failed: '已熔断', needs_human: '需要处理', cancelled: '已取消', paused: '已暂停',
}

const navItems: { id: View; label: string; icon: typeof Kanban }[] = [
  { id: 'butler', label: '管家', icon: Network },
  { id: 'projects', label: '项目', icon: FolderOpen },
  { id: 'tasks', label: '任务', icon: ListChecks },
  { id: 'usage', label: 'Token', icon: Coins },
  { id: 'alerts', label: '告警', icon: WarningCircle },
  { id: 'settings', label: '设置', icon: Gear },
]

const initialSizingInputs: TaskSizingInputs = {
  layers_touched: 1, components_touched: 3, estimated_files_changed: 4,
  has_migration: false, has_deploy: false, verification_commands_count: 3,
  estimated_verification_seconds: 180, external_dependencies_count: 0,
  risk_level: 'medium', independent_review_required: false,
  gate_artifact: true, gate_boundary: true, gate_verification: true, gate_dependency: true,
}

const initialTask = {
  title: '', objective: '', projectId: '', role: 'fullstack', provider: 'codex',
  networkRequirement: 'none', executable: 'python3',
  argumentsJson: '["-c", "from pathlib import Path; Path(\'result.txt\').write_text(\'quality-pass\', encoding=\'utf-8\')"]',
  verifyPath: 'result.txt', verifyText: 'quality-pass',
  sizingInputs: initialSizingInputs,
}

export function App() {
  const [view, setView] = useState<View>('tasks')
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [goals, setGoals] = useState<Goal[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [globalButler, setGlobalButler] = useState<GlobalButlerOverview | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [settings, setSettings] = useState<RuntimeSettings | null>(null)
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null)
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)
  const [alerts, setAlerts] = useState<Alerts | null>(null)
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [outbox, setOutbox] = useState<OutboxEvent[]>([])
  const [permissions, setPermissions] = useState<PermissionGrant[]>([])
  const [convention, setConvention] = useState<Convention | null>(null)
  const [suggestion, setSuggestion] = useState<ConventionSuggestion | null>(null)
  const [refineProvider, setRefineProvider] = useState('codex')
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)
  const [selectedGoal, setSelectedGoal] = useState<Goal | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([])
  const [artifactError, setArtifactError] = useState<string | null>(null)
  const [deletionEligibility, setDeletionEligibility] = useState<TaskDeletionEligibility | null>(null)
  const [selectedProject, setSelectedProject] = useState<string>('all')
  const projectScope = useRef<ProjectScope>({ projectId: 'all', generation: 0 })
  const [refreshInterval, setRefreshInterval] = useState(
    () => Number(window.localStorage.getItem('plow-whip.refresh-interval') ?? 30_000),
  )
  const [refreshState, setRefreshState] = useState<{ status: 'idle' | 'refreshing' | 'ok' | 'error'; at: string | null }>({ status: 'idle', at: null })
  const [taskForm, setTaskForm] = useState(initialTask)
  const [projectForm, setProjectForm] = useState({ name: '', path: '', hostPath: '' })
  const [showCreateTask, setShowCreateTask] = useState(false)
  const [showProjectButler, setShowProjectButler] = useState(false)
  const [butlerProjectId, setButlerProjectId] = useState('')
  const [taskEstimate, setTaskEstimate] = useState<TaskSizingEstimate | null>(null)
  const [estimatedSizingInputs, setEstimatedSizingInputs] = useState<TaskSizingInputs | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refreshTasks = useCallback(async () => {
    const generation = projectScope.current.generation
    const next = await api.tasks()
    if (generation !== projectScope.current.generation) return
    setTasks(next)
    setSelectedTask((current) => next.find((task) => task.id === current?.id) ?? null)
  }, [])
  const refreshGoals = useCallback(async () => {
    const generation = projectScope.current.generation
    const next = await api.goals()
    if (generation !== projectScope.current.generation) return
    setGoals(next)
    setSelectedGoal((current) => next.find((goal) => goal.id === current?.id) ?? null)
  }, [])
  const refreshProjects = useCallback(async () => setProjects(await api.projects()), [])
  const refreshProviders = useCallback(async () => setProviders(await api.providers()), [])
  const refreshGlobalButler = useCallback(
    async () => setGlobalButler(await api.globalButlerOverview()),
    [],
  )
  const refreshUsage = useCallback(async () => {
    const { generation, projectId } = projectScope.current
    const next = await api.usage(projectId === 'all' ? undefined : projectId)
    if (generation === projectScope.current.generation) setUsage(next)
  }, [])
  const refreshAlerts = useCallback(async () => setAlerts(await api.alerts()), [])

  useEffect(() => {
    Promise.all([
      fetch('/health').then((response) => response.json() as Promise<Health>).then(setHealth),
      api.tasks().then(setTasks), api.goals().then(setGoals), api.projects().then(setProjects), api.providers().then(setProviders),
      api.globalButlerOverview().then(setGlobalButler),
      api.settings().then(setSettings), api.schedulerStatus().then(setScheduler),
      api.runtimeHealth().then(setRuntimeHealth),
      api.alerts().then(setAlerts),
      api.audit().then(setAudit), api.outbox().then(setOutbox), api.permissions().then(setPermissions),
      api.convention('global', 'global').then(setConvention),
    ]).catch((reason: unknown) => setError(messageOf(reason)))
  }, [])

  useEffect(() => {
    const generation = projectScope.current.generation
    api.usage(selectedProject === 'all' ? undefined : selectedProject)
      .then((next) => { if (generation === projectScope.current.generation) setUsage(next) })
      .catch((reason: unknown) => { if (generation === projectScope.current.generation) setError(messageOf(reason)) })
  }, [selectedProject])

  useEffect(() => {
    const taskId = selectedTask?.id
    if (!taskId) return
    let current = true
    api.taskDeletionEligibility(taskId).then((result) => {
      if (current) setDeletionEligibility(result)
    }).catch(() => { if (current) setDeletionEligibility(null) })
    Promise.all([
      api.taskEvents(taskId),
      api.taskArtifacts(taskId),
    ]).then(([nextEvents, nextArtifacts]) => {
      if (!current) return
      setEvents(nextEvents)
      setArtifacts(nextArtifacts)
    }).catch((reason: unknown) => {
      if (!current) return
      setArtifacts([])
      setArtifactError(messageOf(reason))
    })
    return () => { current = false }
  }, [selectedTask?.id, selectedTask?.revision])

  useEffect(() => {
    window.localStorage.setItem('plow-whip.refresh-interval', String(refreshInterval))
    return startLiveRefresh(async () => {
      setRefreshState((current) => ({ ...current, status: 'refreshing' }))
      const { generation, projectId } = projectScope.current
      try {
        const requests: Promise<unknown>[] = [refreshProjects()]
        if (view === 'tasks') requests.push(refreshTasks(), refreshGoals())
        if (view === 'butler') requests.push(refreshGlobalButler())
        if (view === 'usage' || view === 'tasks') requests.push(
          api.usage(projectId === 'all' ? undefined : projectId).then((nextUsage) => {
            if (generation === projectScope.current.generation) setUsage(nextUsage)
          }),
        )
        if (view === 'alerts') requests.push(refreshAlerts())
        if (view === 'settings') requests.push(refreshProviders())
        await Promise.all(requests)
        setRefreshState({ status: 'ok', at: new Date().toISOString() })
      } catch (reason) {
        setRefreshState({ status: 'error', at: new Date().toISOString() })
        throw reason
      }
    }, { intervalMs: refreshInterval })
  }, [refreshAlerts, refreshGlobalButler, refreshGoals, refreshInterval, refreshProjects, refreshProviders, refreshTasks, view])

  function changeProjectScope(projectId: string) {
    projectScope.current = {
      projectId,
      generation: projectScope.current.generation + 1,
    }
    setSelectedProject(projectId)
    setSelectedTask(null)
    setSelectedGoal(null)
    setEvents([])
    setArtifacts([])
    setUsage(null)
    setArtifactError(null)
    setDeletionEligibility(null)
    setShowProjectButler(false)
    setButlerProjectId('')
  }

  function selectTask(task: Task) {
    if (selectedTask?.id !== task.id) {
      setEvents([])
      setArtifacts([])
      setArtifactError(null)
      setDeletionEligibility(null)
    }
    setSelectedTask(task)
    setSelectedGoal(null)
    setView('tasks')
  }

  function selectGoal(goal: Goal) {
    setEvents([])
    setArtifacts([])
    setArtifactError(null)
    setDeletionEligibility(null)
    setSelectedGoal(goal)
    setSelectedTask(null)
    setView('tasks')
  }

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
    if (taskEstimate?.status !== 'estimated' || estimatedSizingInputs !== taskForm.sizingInputs) return
    await action(async () => {
      const args = JSON.parse(taskForm.argumentsJson) as unknown
      if (!Array.isArray(args) || args.some((value) => typeof value !== 'string')) throw new Error('参数必须是字符串 JSON 数组')
      let created: Task
      try {
        created = await api.createTask({
          title: taskForm.title, objective: taskForm.objective, project_id: taskForm.projectId,
          role: taskForm.role, provider: taskForm.provider, network_requirement: taskForm.networkRequirement,
          sizing_inputs: taskForm.sizingInputs,
          command: { argv: [taskForm.executable, ...args] },
          verification: [
            { kind: 'exit_code', expected: 0 },
            { kind: 'file_exists', path: taskForm.verifyPath },
            { kind: 'file_contains', path: taskForm.verifyPath, contains: taskForm.verifyText },
          ],
        })
      } finally {
        await refreshProviders()
      }
      await refreshTasks(); selectTask(created); setShowCreateTask(false)
    }, '诊断任务已加入队列')
  }

  async function openDispatchedGoal(goalId: string) {
    const created = await api.getGoal(goalId)
    await Promise.all([refreshGoals(), refreshTasks(), refreshProviders()])
    selectGoal(created)
    setShowProjectButler(false)
    setNotice('目标已拆分并进入自动推进')
  }

  async function estimateTask() {
    const sizingInputs = taskForm.sizingInputs
    setBusy(true); setError(null); setNotice(null); setTaskEstimate(null); setEstimatedSizingInputs(null)
    try { setTaskEstimate(await api.estimateTask(sizingInputs)); setEstimatedSizingInputs(sizingInputs) } catch (reason) { setError(messageOf(reason)) } finally { setBusy(false) }
  }

  function setSizingInputs(sizingInputs: TaskSizingInputs) {
    setTaskForm((current) => ({ ...current, sizingInputs }))
    setTaskEstimate(null)
    setEstimatedSizingInputs(null)
  }

  async function createProject(event: FormEvent) {
    event.preventDefault()
    await action(async () => {
      const created = await api.createProject({
        name: projectForm.name, path: projectForm.path,
        host_path: projectForm.hostPath.trim() || null,
      })
      await refreshProjects(); changeProjectScope(created.id)
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

  async function deleteTask(task: Task) {
    if (!deletionEligibility?.deletable) return
    const confirmed = window.confirm(
      `是否真的永久删除任务“${task.title}”？\n\n永久删除、不可恢复。`,
    )
    if (!confirmed) return
    await action(async () => {
      await api.deleteTask(task, 'operator_permanent_delete')
      setSelectedTask(null)
      setDeletionEligibility(null)
      await Promise.all([refreshTasks(), refreshGoals()])
    }, '任务已永久删除')
  }

  async function probeProvider(provider: Provider) {
    await action(async () => { await api.probeProvider(provider.name); await refreshProviders() }, `${provider.display_name} 探测完成`)
  }

  async function openArtifact(
    task: Task, artifact: TaskArtifact, target: 'finder' | 'cursor',
  ) {
    await action(
      async () => { await api.openTaskArtifact(task.id, artifact.relative_path, target) },
      target === 'cursor' ? '已交给 Cursor 打开' : '已在 Finder 中定位',
    )
  }

  async function copyArtifactPath(artifact: TaskArtifact) {
    await action(async () => { await navigator.clipboard.writeText(artifact.host_path) }, '主机路径已复制')
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
        <div className="principle">保障质量并消除无价值循环，同时保留高价值上下文。</div>
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
          <label className="scope-control"><span>项目范围</span><select value={selectedProject} onChange={(event) => changeProjectScope(event.target.value)}><option value="all">全部项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
          <div className="context-actions">
            <button className="ghost" onClick={() => action(async () => { await Promise.all([refreshTasks(), refreshGoals(), refreshProjects(), refreshProviders(), refreshGlobalButler(), refreshUsage()]) }, '状态已刷新')}><ArrowsClockwise size={16} />刷新</button>
            <label className="refresh-control">自动刷新<select aria-label="自动刷新间隔" value={refreshInterval} onChange={(event) => setRefreshInterval(Number(event.target.value))}><option value={0}>关闭</option><option value={5_000}>5s</option><option value={10_000}>10s</option><option value={30_000}>30s</option><option value={60_000}>1min</option><option value={300_000}>5min</option><option value={600_000}>10min</option><option value={3_600_000}>1h</option><option value={7_200_000}>2h</option><option value={14_400_000}>4h</option></select></label>
            <span className={`refresh-state refresh-${refreshState.status}`}>{refreshInterval === 0 ? '自动刷新已关闭' : refreshState.status === 'refreshing' ? '刷新中' : refreshState.at ? `上次成功 ${new Date(refreshState.at).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' })}` : '等待首次刷新'}</span>
            {view === 'tasks' && <button className="ghost" onClick={() => { setTaskEstimate(null); setEstimatedSizingInputs(null); setShowCreateTask(true) }}>诊断任务</button>}
          </div>
        </div>

        {error && <div className="banner error"><WarningCircle size={18} weight="fill" /><span>{error}</span><button aria-label="关闭错误" onClick={() => setError(null)}><X size={16} /></button></div>}
        {notice && <div className="banner success"><CheckCircle size={18} weight="fill" /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        {outbox.filter((event) => event.event_type === 'task.needs_human' && !event.delivered_at).length > 0 && <div className="banner warning"><WarningCircle size={18} weight="fill" />存在需要人工判断的任务，自动调度已对这些任务停手。</div>}

        {view === 'butler' && (selectedProject === 'all'
          ? <GlobalButlerView overview={globalButler} onOpenConversation={() => { setButlerProjectId(''); setShowProjectButler(true) }} />
          : <ProjectButlerLanding project={projects.find((project) => project.id === selectedProject)} onOpenConversation={() => { setButlerProjectId(selectedProject); setShowProjectButler(true) }} />)}
        {view === 'tasks' && <UnifiedTasksView usage={usage} tasks={visibleTasks} goals={goals.filter((goal) => selectedProject === 'all' || goal.project_id === selectedProject)} workers={workers.filter((item) => selectedProject === 'all' || item.project.id === selectedProject).map((item) => item.worker)} selected={selectedTask} selectedGoal={selectedGoal} events={events} artifacts={artifacts} artifactError={artifactError} deletionEligibility={deletionEligibility} busy={busy} onSelect={selectTask} onSelectGoal={selectGoal} onDrive={driveTask} onDelete={deleteTask} onControl={controlTask} onOpenArtifact={openArtifact} onCopyArtifact={copyArtifactPath} />}
        {view === 'projects' && <ProjectsView projects={projects} form={projectForm} setForm={setProjectForm} onCreate={createProject} onRelease={(project) => action(async () => { await api.releaseProject(project.id); await refreshProjects() }, '项目已完成并释放 Worker')} busy={busy} />}
        {view === 'usage' && <UsageView key={selectedProject} usage={usage} projectId={selectedProject === 'all' ? undefined : selectedProject} projects={projects} tasks={visibleTasks} />}
        {view === 'alerts' && <AlertsView alerts={alerts} onRefresh={refreshAlerts} />}
        {view === 'settings' && <SettingsView settings={settings} setSettings={setSettings} scheduler={scheduler} convention={convention} setConvention={setConvention} suggestion={suggestion} setSuggestion={setSuggestion} projects={projects} tasks={visibleTasks} workers={workers.filter(({ project }) => selectedProject === 'all' || project.id === selectedProject)} selectedProject={selectedProject === 'all' ? undefined : selectedProject} providers={providers} audit={audit} permissions={permissions} refineProvider={effectiveRefineProvider} setRefineProvider={setRefineProvider} busy={busy} onSaveSettings={saveSettings} onTick={() => action(async () => { await api.schedulerTick(); setScheduler(await api.schedulerStatus()); await Promise.all([refreshTasks(), refreshGoals()]) }, 'Tick 已完成')} onLoadConvention={loadConvention} onSaveConvention={saveConvention} onRefine={refineConvention} onProbeProvider={probeProvider} onToggleProvider={toggleProvider} onRebindWorker={(worker, provider) => action(async () => { await api.rebindWorker(worker.id, provider); await refreshProjects() }, 'Worker 已轮转并重新绑定')} onRefreshAudit={() => api.audit().then(setAudit)} />}
      </main>

      {showProjectButler && <ProjectButlerDialog key={`${selectedProject}:${butlerProjectId || 'new'}`} initialProjectId={butlerProjectId} globalScope={selectedProject === 'all'} projects={projects} providers={providers} onClose={() => setShowProjectButler(false)} onDispatched={openDispatchedGoal} />}
      {showCreateTask && <TaskDrawer form={taskForm} setForm={setTaskForm} setSizingInputs={setSizingInputs} estimate={estimatedSizingInputs === taskForm.sizingInputs ? taskEstimate : null} projects={projects} providers={providers} busy={busy} onClose={() => setShowCreateTask(false)} onEstimate={estimateTask} onSubmit={createTask} />}
    </div>
  )
}

const GOAL_TERMINAL = new Set(['completed', 'terminal_failed', 'cancelled'])
const COMPLETED_GOAL_PAGE_SIZE = 4

function goalStatusPill(status: string): TaskStatus {
  if (status === 'running') return 'running'
  if (status === 'completed') return 'completed'
  if (status === 'needs_human') return 'needs_human'
  return 'terminal_failed'
}

export function Board({ tasks, goals, projects, workers, providers, usage, onNavigate, onSelect, onSelectGoal }: { tasks: Task[]; goals: Goal[]; projects: Project[]; workers: Worker[]; providers: Provider[]; usage: Usage | null; onNavigate: (view: View) => void; onSelect: (task: Task) => void; onSelectGoal: (goal: Goal) => void }) {
  const [goalLane, setGoalLane] = useState<'active' | 'completed'>('active')
  const [completedExpanded, setCompletedExpanded] = useState(false)
  const [completedPage, setCompletedPage] = useState(0)
  const columns: { title: string; statuses: TaskStatus[]; tone: string }[] = [
    { title: '待执行', statuses: ['ready', 'paused'], tone: 'blue' },
    { title: '执行中', statuses: ['running', 'stopping'], tone: 'violet' },
    { title: '质量验证', statuses: ['verifying'], tone: 'yellow' },
    { title: '已终态', statuses: ['completed', 'terminal_failed', 'cancelled', 'needs_human'], tone: 'green' },
  ]
  const activeGoals = goals.filter((goal) => !GOAL_TERMINAL.has(goal.status))
  const completedGoals = goals.filter((goal) => GOAL_TERMINAL.has(goal.status))
  const completedPages = Math.max(1, Math.ceil(completedGoals.length / COMPLETED_GOAL_PAGE_SIZE))
  const completedSlice = completedGoals.slice(
    completedPage * COMPLETED_GOAL_PAGE_SIZE,
    completedPage * COMPLETED_GOAL_PAGE_SIZE + COMPLETED_GOAL_PAGE_SIZE,
  )
  const visibleGoals = goalLane === 'active'
    ? activeGoals
    : (completedExpanded ? completedSlice : completedSlice.slice(0, Math.min(2, completedSlice.length)))
  const todayLabel = usage?.today?.date ? `${usage.today.date} · Asia/Shanghai` : 'Asia/Shanghai 日界'
  return <>
    <section className="metrics-strip">
      <Metric icon={FolderOpen} label="活跃项目" value={projects.filter((p) => p.status === 'active').length} hint="可并行" onClick={() => onNavigate('projects')} />
      <Metric icon={Robot} label="在线 Worker" value={workers.filter((w) => w.status !== 'released').length} hint={`${workers.filter((w) => w.status === 'busy').length} 忙碌`} onClick={() => onNavigate('projects')} />
      <Metric icon={TerminalWindow} label="可用 Provider" value={providers.filter(providerFullyReady).length} hint={`${providers.filter((p) => p.enabled).length} 已启用`} onClick={() => onNavigate('settings')} />
      {usage
        ? <Metric icon={Coins} label="今日 Token" value={usage.today?.total_tokens ?? 0} hint={todayLabel} onClick={() => onNavigate('usage')} />
        : <button type="button" className="metric-card" aria-label="查看今日 Token详情" onClick={() => onNavigate('usage')}><div className="metric-icon"><Coins size={18} /></div><div><span>今日 Token</span><strong>加载中</strong></div></button>}
    </section>
    <section className="panel board-panel goal-panel" data-testid="goal-panel">
      <div className="panel-heading">
        <div>
          <span className="kicker">项目指令 / Goal</span>
          <h1>独立于任务泳道的目标区</h1>
        </div>
        <span className="muted">进行中默认可操作 · 已完成默认有界 · 项目筛选同步约束</span>
      </div>
      <div className="goal-lane-tabs" role="tablist" aria-label="指令状态">
        <button type="button" role="tab" aria-selected={goalLane === 'active'} className={goalLane === 'active' ? 'active' : ''} onClick={() => setGoalLane('active')}>进行中 <b>{activeGoals.length}</b></button>
        <button type="button" role="tab" aria-selected={goalLane === 'completed'} className={goalLane === 'completed' ? 'active' : ''} onClick={() => { setGoalLane('completed'); setCompletedExpanded(false); setCompletedPage(0) }}>已完成 <b>{completedGoals.length}</b></button>
      </div>
      <div className={`goal-strip ${goalLane === 'completed' && !completedExpanded ? 'goal-strip-bounded' : ''}`} data-testid="goal-strip">
        {visibleGoals.length ? visibleGoals.map((goal) => (
          <button
            className={`task-card goal-card ${GOAL_TERMINAL.has(goal.status) ? 'goal-card-completed' : 'goal-card-active'}`}
            key={goal.id}
            onClick={() => onSelectGoal(goal)}
          >
            <div>
              <StatusPill status={goalStatusPill(goal.status)} />
              <small title={goal.provider}>{goal.provider}</small>
            </div>
            <strong title={goal.title}>{goal.title}</strong>
            <p title={goal.objective}>{goal.objective}</p>
            <footer>
              <span>{goal.work_items.length} 工作项</span>
              <span>{goal.status}</span>
            </footer>
          </button>
        )) : <div className="empty-column">{goalLane === 'active' ? '暂无进行中指令。从管家入口提交目标后按 ProjectExecutionPolicy 路由。' : '暂无已完成指令。'}</div>}
      </div>
      {goalLane === 'completed' && completedGoals.length > 0 ? (
        <div className="goal-history-controls">
          {!completedExpanded ? (
            <button type="button" className="ghost" onClick={() => setCompletedExpanded(true)}>展开已完成历史（有界分页）</button>
          ) : (
            <>
              <span className="muted">第 {completedPage + 1}/{completedPages} 页 · 每页 {COMPLETED_GOAL_PAGE_SIZE} 条</span>
              <button type="button" className="ghost" disabled={completedPage <= 0} onClick={() => setCompletedPage((page) => Math.max(0, page - 1))}>上一页</button>
              <button type="button" className="ghost" disabled={completedPage >= completedPages - 1} onClick={() => setCompletedPage((page) => Math.min(completedPages - 1, page + 1))}>下一页</button>
              <button type="button" className="ghost" onClick={() => setCompletedExpanded(false)}>折叠长 objective</button>
            </>
          )}
        </div>
      ) : null}
    </section>
    <section className="panel board-panel"><div className="panel-heading"><div><span className="kicker">全局任务流</span><h1>任务看板</h1></div><span className="muted">项目 + 角色 + Task 会话 · 租约隔离 · 证据完成</span></div>
      <div className="kanban-grid" data-testid="kanban-grid">{columns.map((column) => { const items = tasks.filter((task) => column.statuses.includes(task.status)); return <div className="kanban-column" key={column.title}><div className={`column-title ${column.tone}`}><span>{column.title}</span><b>{items.length}</b></div><div className="column-body" data-testid="column-body">{items.length ? items.map((task) => <button className="task-card" key={task.id} onClick={() => onSelect(task)} title={task.title}><div><StatusPill status={task.status} /><small title={`${task.provider}${task.work_item_kind ? ` · ${task.work_item_kind}` : ''}`}>{task.provider}{task.work_item_kind ? ` · ${task.work_item_kind}` : ''}</small></div><strong title={task.title}>{task.title}</strong><p title={task.objective}>{task.objective}</p><footer><span>{task.attempts_used}/{task.max_attempts} 次</span><span title={`${task.tokens_used} Token`}>{task.tokens_used.toLocaleString()} Token</span></footer></button>) : <div className="empty-column">当前没有任务</div>}</div></div> })}</div>
    </section>
  </>
}

function GlobalButlerView({ overview, onOpenConversation }: { overview: GlobalButlerOverview | null; onOpenConversation: () => void }) {
  if (!overview) return <section className="panel"><div className="empty-state"><Network size={38} /><h2>正在读取全局资源索引</h2></div></section>
  return <><section className="metrics-strip"><Metric icon={FolderOpen} label="项目" value={overview.totals.projects} hint="注册资源" /><Metric icon={Kanban} label="运行中 Goal" value={overview.totals.running_goals} hint="规范状态" /><Metric icon={ListChecks} label="活动 Task" value={overview.totals.active_tasks} hint="非终态" /><Metric icon={Robot} label="活动 Worker" value={overview.totals.active_workers} hint="未释放" /></section><section className="panel butler-home"><div className="panel-heading"><div><span className="kicker">Codex · 全局只读工作区</span><h1>全局管家</h1></div><button className="primary" onClick={onOpenConversation}><ChatCircleDots size={16} />与全局管家对话</button></div><p className="objective">查询全部项目的规范化状态、资源与告警，并把需要执行的工作引导到指定项目管家。全局会话与项目会话严格隔离。</p><section className="work-items"><div className="section-heading"><div><span className="kicker">Registered resources</span><h2>项目状态索引</h2></div><span>真源：{overview.canonical_sources.join(' / ')}</span></div>{overview.projects.map((project) => <article key={project.id}><div><strong>{project.name}</strong><small>{project.resource_path}</small></div><div><span>{project.running_goals} Goal · {project.active_tasks} Task · {project.active_workers} Worker</span><small>切换上方项目范围后直接进入该项目管家</small></div></article>)}</section></section></>
}

function ProjectButlerLanding({ project, onOpenConversation }: { project?: Project; onOpenConversation: () => void }) {
  if (!project) return <section className="panel"><div className="empty-state"><Robot size={38} /><h2>项目不存在或已移除</h2></div></section>
  return <section className="panel butler-home">
    <div className="panel-heading">
      <div><span className="kicker">Codex · 项目隔离会话</span><h1>{project.name} 项目管家</h1></div>
      <button className="primary" onClick={onOpenConversation}><ChatCircleDots size={16} />与项目管家对话</button>
    </div>
    <p className="objective">项目管家只处理当前项目。它会先确保目标、边界和验收标准完整；大型目标一次只问一个问题，确认方案后才生成角色实例、冻结规则快照并派发任务。</p>
    <div className="facts"><Fact label="项目目录" value={project.host_path ?? project.path} mono /><Fact label="常驻角色" value="项目管家（Codex）" /><Fact label="动态 Worker" value={`${project.workers.filter((worker) => worker.status !== 'released').length} 个活动实例`} /><Fact label="会话边界" value="每条新指令一个独立物理会话" /></div>
  </section>
}

function UnifiedTasksView(props: {
  usage: Usage | null
  tasks: Task[]
  goals: Goal[]
  workers: Worker[]
  selected: Task | null
  selectedGoal: Goal | null
  events: TaskEvent[]
  artifacts: TaskArtifact[]
  artifactError: string | null
  deletionEligibility: TaskDeletionEligibility | null
  busy: boolean
  onSelect: (task: Task) => void
  onSelectGoal: (goal: Goal) => void
  onDrive: (task: Task) => void
  onDelete: (task: Task) => void
  onControl: (task: Task, action: 'pause' | 'resume' | 'cancel' | 'needs_human') => void
  onOpenArtifact: (task: Task, artifact: TaskArtifact, target: 'finder' | 'cursor') => void
  onCopyArtifact: (artifact: TaskArtifact) => void
}) {
  const {
    usage, tasks, goals, workers, selected, selectedGoal, events, artifacts, artifactError,
    deletionEligibility, busy, onSelect, onSelectGoal, onDrive, onDelete, onControl,
    onOpenArtifact, onCopyArtifact,
  } = props
  const [completedOpen, setCompletedOpen] = useState(false)
  const activeGoals = goals.filter((goal) => !GOAL_TERMINAL.has(goal.status))
  const completedGoals = goals.filter((goal) => GOAL_TERMINAL.has(goal.status))
  const lanes: { title: string; statuses: TaskStatus[] }[] = [
    { title: '待执行', statuses: ['ready', 'paused'] },
    { title: '执行中', statuses: ['running', 'stopping'] },
    { title: '验证中', statuses: ['verifying'] },
    { title: '已终态', statuses: ['completed', 'terminal_failed', 'cancelled', 'needs_human'] },
  ]
  const activeTasks = tasks.filter((task) => !['completed', 'terminal_failed', 'cancelled'].includes(task.status))
  return <><section className="metrics-strip task-metrics">
    <Metric icon={Coins} label="今日 Token" value={usage?.today?.total_tokens ?? 0} hint={`${usage?.today?.date ?? '—'} · 当前项目范围`} />
    <Metric icon={Kanban} label="进行中 Goal" value={activeGoals.length} hint="待拆分或执行" />
    <Metric icon={ListChecks} label="活动 Task" value={activeTasks.length} hint="非终态" />
    <Metric icon={CheckCircle} label="已终态 Task" value={tasks.length - activeTasks.length} hint="完成、失败、取消" />
  </section><div className="task-workspace" data-testid="unified-task-workspace">
    <aside className="panel goal-rail">
      <div className="panel-heading"><div><span className="kicker">项目指令</span><h1>Goal</h1></div><b>{activeGoals.length}</b></div>
      <div className="goal-rail-list">
        {activeGoals.map((goal) => <button key={goal.id} className={selectedGoal?.id === goal.id ? 'selected' : ''} onClick={() => onSelectGoal(goal)}><strong>{goal.title}</strong><small>{goal.provider} · {goal.status} · {goal.work_items.length} 项</small></button>)}
        {!activeGoals.length && <p className="muted">暂无进行中指令。</p>}
      </div>
      <button className="goal-history-toggle" onClick={() => setCompletedOpen((value) => !value)}>已完成 {completedGoals.length} {completedOpen ? '收起' : '展开'}</button>
      {completedOpen && <div className="goal-rail-list completed">{completedGoals.map((goal) => <button key={goal.id} className={selectedGoal?.id === goal.id ? 'selected' : ''} onClick={() => onSelectGoal(goal)}><strong>{goal.title}</strong><small>{goal.status}</small></button>)}</div>}
    </aside>
    <section className="panel task-lanes">
      <div className="panel-heading"><div><span className="kicker">依赖与状态机</span><h1>任务泳道</h1></div><span className="muted">{tasks.length} 个 Task</span></div>
      <div className="compact-kanban">{lanes.map((lane) => {
        const items = tasks.filter((task) => lane.statuses.includes(task.status))
        return <div className="compact-lane" key={lane.title}><header><strong>{lane.title}</strong><b>{items.length}</b></header><div>{items.map((task) => <button key={task.id} className={selected?.id === task.id ? 'selected' : ''} onClick={() => onSelect(task)}><StatusPill status={task.status} /><strong>{task.title}</strong><small>{task.provider} · {task.tokens_used.toLocaleString()} Token</small></button>)}{!items.length && <p>暂无</p>}</div></div>
      })}</div>
    </section>
    <section className="panel detail-panel task-detail">
      {selectedGoal && !selected ? <GoalDetail goal={selectedGoal} tasks={tasks} workers={workers} /> : selected ? <>
        <div className="panel-heading"><div><span className="kicker">TaskSpec r{selected.spec_revision}</span><h1>{selected.title}</h1></div><StatusPill status={selected.status} /></div>
        <p className="objective">{selected.objective}</p>
        <div className="detail-actions">
          {selected.status === 'ready' && selected.work_item_kind !== 'coordination' && <button className="primary" disabled={busy} onClick={() => onDrive(selected)}><Play size={16} weight="fill" />立即驱动</button>}
          {selected.status === 'ready' && <button disabled={busy} onClick={() => onControl(selected, 'pause')}><Pause size={16} />暂停</button>}
          {selected.status === 'paused' && <button disabled={busy} onClick={() => onControl(selected, 'resume')}><Play size={16} />恢复</button>}
          {!['completed', 'terminal_failed', 'cancelled'].includes(selected.status) && <button className="danger" disabled={busy} onClick={() => onControl(selected, 'cancel')}><X size={16} />取消</button>}
          {deletionEligibility?.deletable && <button className="danger" disabled={busy} onClick={() => onDelete(selected)}><X size={16} />永久删除</button>}
        </div>
        <div className="facts"><Fact label="Provider" value={selected.provider} /><Fact label="状态真源" value={statusNames[selected.status]} /><Fact label="尝试 / 消费" value={`${selected.attempts_used}/${selected.max_attempts} · ${selected.tokens_used} Token`} /><Fact label="依赖" value={(selected.depends_on ?? []).join(', ') || '无'} mono /><Fact label="阻塞原因" value={selected.blocked_reason ?? '无'} /><Fact label="Worker" value={selected.worker_id ?? '尚未领取'} mono /><TaskRuntimeFacts item={selected as unknown as Record<string, unknown>} /></div>
        <section className="artifacts"><div className="section-heading"><div><span className="kicker">Evidence / Artifact</span><h2>任务产物</h2></div><span>{artifacts.filter((item) => item.exists).length} 个已定位</span></div>{artifactError ? <p className="artifact-error">{artifactError}</p> : artifacts.length ? artifacts.map((artifact) => <article className={artifact.exists ? '' : 'missing'} key={artifact.relative_path}><div className="artifact-main"><strong>{artifact.relative_path}</strong><code>{artifact.host_path}</code><small>{artifact.exists ? `${formatBytes(artifact.bytes)} · ${artifact.sha256?.slice(0, 12) ?? '未哈希'}` : '尚未生成'}</small></div><div className="artifact-actions"><button disabled={!artifact.exists} onClick={() => onCopyArtifact(artifact)}><Copy size={15} />路径</button><button disabled={!artifact.actions.includes('finder')} onClick={() => onOpenArtifact(selected, artifact, 'finder')}><FolderOpen size={15} />Finder</button></div></article>) : <p className="muted">该任务没有声明文件产物。</p>}</section>
        <div className="timeline"><h2>最新状态事件</h2>{events.slice(-20).map((event) => <div key={event.sequence}><span>{event.sequence}</span><strong>{event.event_type}</strong><small>{event.created_at}</small></div>)}</div>
      </> : <div className="empty-state"><ListChecks size={38} /><h2>选择一个 Goal 或 Task</h2><p>范围切换后不会保留上一个项目的详情。</p></div>}
    </section>
  </div></>
}

export function TasksView({ tasks, goals, workers, selected, selectedGoal, events, artifacts, artifactError, deletionEligibility, busy, onSelect, onSelectGoal, onDrive, onDelete, onControl, onOpenArtifact, onCopyArtifact }: { tasks: Task[]; goals: Goal[]; workers: Worker[]; selected: Task | null; selectedGoal: Goal | null; events: TaskEvent[]; artifacts: TaskArtifact[]; artifactError: string | null; deletionEligibility: TaskDeletionEligibility | null; busy: boolean; onSelect: (task: Task) => void; onSelectGoal: (goal: Goal) => void; onDrive: (task: Task) => void; onDelete: (task: Task) => void; onControl: (task: Task, action: 'pause' | 'resume' | 'cancel' | 'needs_human') => void; onOpenArtifact: (task: Task, artifact: TaskArtifact, target: 'finder' | 'cursor') => void; onCopyArtifact: (artifact: TaskArtifact) => void }) {
  return <div className="split-layout"><section className="panel list-panel"><div className="panel-heading"><div><span className="kicker">目标 / 任务</span><h1>{goals.length} 目标 · {tasks.length} 工作项</h1></div></div>
    <div className="dense-list">
      {goals.map((goal) => <button key={goal.id} className={selectedGoal?.id === goal.id ? 'selected' : ''} onClick={() => onSelectGoal(goal)}><span className="list-icon"><Kanban size={18} /></span><span><strong>{goal.title}</strong><small>{goal.provider} · {goal.status} · {goal.work_items.length} 项</small></span></button>)}
      {tasks.map((task) => <button key={task.id} className={selected?.id === task.id ? 'selected' : ''} onClick={() => onSelect(task)}><span className="list-icon"><ListChecks size={18} /></span><span><strong>{task.title}</strong><small>TaskSpec r{task.spec_revision} · {task.provider} · {statusNames[task.status]}{task.work_item_kind ? ` · ${task.work_item_kind}` : ''}</small></span><StatusPill status={task.status} /></button>)}
    </div></section>
    <section className="panel detail-panel">{selectedGoal && !selected ? <GoalDetail goal={selectedGoal} tasks={tasks} workers={workers} /> : selected ? <><div className="panel-heading"><div><span className="kicker">{selected.id}</span><h1>{selected.title}</h1></div><StatusPill status={selected.status} /></div><p className="objective">{selected.objective}</p><div className="detail-actions">{selected.status === 'ready' && selected.work_item_kind !== 'coordination' && <button className="primary" disabled={busy} onClick={() => onDrive(selected)}><Play size={16} weight="fill" />立即驱动</button>}{selected.status === 'ready' && <button disabled={busy} onClick={() => onControl(selected, 'pause')}><Pause size={16} />暂停</button>}{selected.status === 'paused' && <button disabled={busy} onClick={() => onControl(selected, 'resume')}><Play size={16} />恢复</button>}{!['completed', 'terminal_failed', 'cancelled'].includes(selected.status) && <button className="danger" disabled={busy} onClick={() => onControl(selected, 'cancel')}><X size={16} />取消</button>}{deletionEligibility?.deletable && <button className="danger" disabled={busy} onClick={() => onDelete(selected)}><X size={16} />永久删除</button>}</div><div className="facts"><Fact label="Provider" value={selected.provider} /><Fact label="验证机制" value="确定性验证" /><Fact label="验证状态" value={verificationState(selected as unknown as Record<string, unknown>)} /><Fact label="工作项" value={selected.work_item_kind ?? 'manual'} /><Fact label="依赖" value={(selected.depends_on ?? []).join(', ') || '无'} mono /><Fact label="阻塞原因" value={selected.blocked_reason ?? '无'} /><Fact label="Worker" value={selected.worker_id ?? '尚未领取'} mono /><Fact label="资源锁" value={selected.resource_key ?? '项目级默认锁'} mono /><Fact label="尝试 / 消费" value={`${selected.attempts_used}/${selected.max_attempts} · ${selected.tokens_used} Token`} /><TaskRuntimeFacts item={selected as unknown as Record<string, unknown>} /></div><section className="artifacts"><div className="section-heading"><div><span className="kicker">主机项目目录</span><h2>任务产物</h2></div><span>{artifacts.filter((item) => item.exists).length} 个已定位</span></div>{artifactError ? <p className="artifact-error">Host Bridge 暂时无法定位产物：{artifactError}</p> : artifacts.length ? artifacts.map((artifact) => <article className={artifact.exists ? '' : 'missing'} key={artifact.relative_path}><div className="artifact-icon"><FileCode size={19} /></div><div className="artifact-main"><strong>{artifact.relative_path}</strong><code title={artifact.host_path}>{artifact.host_path}</code><small>{artifact.exists ? `${formatBytes(artifact.bytes)} · SHA-256 ${artifact.sha256?.slice(0, 12) ?? '文件过大未哈希'}…` : '尚未在主机项目目录生成'}</small></div><div className="artifact-actions"><button disabled={busy || !artifact.exists} onClick={() => onCopyArtifact(artifact)}><Copy size={15} />复制路径</button><button disabled={busy || !artifact.actions.includes('finder')} onClick={() => onOpenArtifact(selected, artifact, 'finder')}><FolderOpen size={15} />Finder</button>{artifact.actions.includes('cursor') && <button className="primary" disabled={busy} onClick={() => onOpenArtifact(selected, artifact, 'cursor')}><TerminalWindow size={15} />Cursor 打开</button>}</div></article>) : <p className="artifact-empty">该任务没有声明文件产物。容器不会保存项目报告或代码。</p>}</section><div className="timeline"><h2>状态事件</h2>{events.map((event) => <div key={event.sequence}><span>{event.sequence}</span><strong>{event.event_type}</strong><small>{event.created_at}</small></div>)}</div></> : <div className="empty-state"><ListChecks size={38} /><h2>选择一个目标或任务</h2><p>查看结构化计划、角色依赖、会话 generation、输出元数据与验证状态。</p></div>}</section></div>
}

function GoalDetail({ goal, tasks, workers }: { goal: Goal; tasks: Task[]; workers: Worker[] }) {
  const [instances, setInstances] = useState<RoleInstance[]>([])
  const [bindings, setBindings] = useState<SessionBinding[]>([])
  useEffect(() => {
    let alive = true
    Promise.all([
      api.roleInstances({ goalId: goal.id }),
      api.sessionBindings({ projectId: goal.project_id }),
    ]).then(([rolePayload, bindingPayload]) => {
      if (!alive) return
      setInstances(rolePayload.items)
      const taskIds = new Set(
        (goal.work_items ?? []).map((item) => String(item.id)),
      )
      setBindings(bindingPayload.items.filter((item) => taskIds.has(item.task_id)))
    }).catch(() => {
      if (!alive) return
      setInstances([])
      setBindings([])
    })
    return () => { alive = false }
    // goal.work_items identity is covered by goal.id; avoid effect churn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goal.id, goal.project_id])
  return <><div className="panel-heading"><div><span className="kicker">{goal.id}</span><h1>{goal.title}</h1></div><span className="status-pill">{goal.status}</span></div><p className="objective">{goal.spec.objective}</p><div className="facts"><Fact label="协调真源" value="GoalSpec / Butler aggregate" /><Fact label="GoalSpec revision" value={String(goal.spec_revision)} /><Fact label="Scope" value={listValue(goal.spec.scope) || '无'} /><Fact label="Acceptance" value={listValue(goal.spec.acceptance) || '无'} /><Fact label="Artifacts" value={listValue(goal.spec.artifacts) || '无'} mono /><Fact label="策略路由" value={String(goal.plan.route ?? 'unknown')} /><Fact label="Provider" value={goal.provider} /><Fact label="工作项" value={String(goal.work_items.length)} /><Fact label="Goal sizing" value={summary(goal.sizing_inputs, ['size_class', 'status', 'risk_level'])} /><Fact label="Goal 状态" value={goal.status} /></div>
  <section className="work-items" data-testid="role-lineage-panel"><div className="section-heading"><div><span className="kicker">Rule → Template → Instance → Session</span><h2>角色来源与绑定</h2></div><span>稳定管家 / 通用模板 / 项目覆盖 / 动态实例</span></div>
    {instances.length ? instances.map((instance) => {
      const binding = bindings.find((item) => item.role_instance_id === instance.id)
      const match = typeof instance.match_reason === 'string'
        ? instance.match_reason
        : summary(instance.match_reason ?? {}, ['reused', 'reason', 'generation_reason'])
      return <article className="work-item-card" key={instance.id}><header><span>RI</span><div><strong>{instance.role_kind}</strong><small>{instance.status}</small></div></header><dl className="facts"><Fact label="模板" value={`${instance.template_id}@${instance.template_revision}`} mono /><Fact label="模板 hash" value={instance.template_hash.slice(0, 12)} mono /><Fact label="实例 hash" value={instance.instance_hash.slice(0, 12)} mono /><Fact label="规则集 hash" value={instance.ruleset_hash.slice(0, 12)} mono /><Fact label="复用/新建理由" value={match || instance.generation_reason || '无'} /><Fact label="Session generation" value={binding ? String(binding.session_generation) : '尚未绑定'} /><Fact label="Binding hash" value={binding ? binding.binding_hash.slice(0, 12) : '—'} mono /><Fact label="TaskSpec revision" value={String(instance.task_spec_revision)} /></dl></article>
    }) : <p className="artifact-empty">尚无 RoleInstance（本地确定性命令可不创建）</p>}
  </section>
  <section className="work-items"><div className="section-heading"><div><span className="kicker">策略路由 → 任务 Gate</span><h2>工作项运行态</h2></div><span>只显示元数据，不读取 stdout/stderr</span></div>{goal.work_items.map((item) => {
    const task = tasks.find((candidate) => candidate.id === String(item.id))
    const detail = { ...item, ...(task ?? {}) } as Record<string, unknown>
    const worker = workers.find((candidate) => candidate.id === value(detail, ['worker_id']))
    return <article className="work-item-card" key={String(item.id)}><header><span>{String(detail.ordinal ?? 'P')}</span><div><strong>{String(detail.title ?? detail.id)}</strong><small>{String(detail.status ?? 'unknown')}</small></div></header><dl className="facts"><Fact label="角色 / Provider" value={`${roleNames[value(detail, ['role'])] ?? value(detail, ['role'])} · ${value(detail, ['provider'])}`} /><Fact label="依赖" value={listValue(detail.depends_on) || '无'} mono /><Fact label="阻塞原因" value={value(detail, ['blocked_reason'], '无')} /><Fact label="Task session" value={value(detail, ['external_session_id'], worker?.external_session_id ?? '尚未建立')} mono /><Fact label="Generation" value={value(detail, ['session_generation'], worker ? String(worker.session_generation) : '尚未建立')} /><Fact label="Session scope" value={value(detail, ['session_scope'], 'worker_legacy')} /><Fact label="Replacement reason" value={value(detail, ['rotation_reason'], worker?.rotation_reason ?? '无')} /><Fact label="Last context pressure" value={value(detail, ['last_context_pressure'], worker ? String(worker.last_context_pressure_tokens) : '暂无（API 未提供）')} /><Fact label="Pressure trigger" value={value(detail, ['last_context_pressure_reason'], worker?.last_context_pressure_reason ?? '暂无（API 未提供）')} /><Fact label="Sizing" value={summary(detail.sizing, ['size_class', 'status'])} /><Fact label="Execution" value={executionSummary(detail)} /><Fact label="Attempt / progress" value={`${value(detail, ['attempts_used'], '0')}/${value(detail, ['max_attempts'], '—')} · ${value(detail, ['status'], 'unknown')}`} /><Fact label="Verification" value={verificationState(detail)} /><TaskRuntimeFacts item={detail} /></dl></article>
  })}</section></>
}

function TaskRuntimeFacts({ item }: { item: Record<string, unknown> }) {
  const handoff = recordValue(item.handoff)
  const spec = recordValue(item.spec)
  const input = value(item, ['input_tokens'], value(handoff, ['input_tokens'], '不可见'))
  const cachedInput = value(item, ['cached_input_tokens'], value(handoff, ['cached_input_tokens'], '不可见'))
  const uncachedInput = value(item, ['uncached_input_tokens'], value(handoff, ['uncached_input_tokens'], '不可见'))
  const output = value(item, ['output_tokens'], value(handoff, ['output_tokens'], '不可见'))
  const total = value(item, ['total_tokens', 'tokens_used'], value(handoff, ['total_tokens'], '不可见'))
  const outputRef = value(item, ['output_ref'], value(handoff, ['output_ref'], '暂无（API 未提供）'))
  const segments = value(item, ['output_segments', 'segments'], value(handoff, ['output_segments', 'segments'], '暂无（API 未提供）'))
  const bytes = value(item, ['output_bytes', 'bytes'], value(handoff, ['output_bytes', 'bytes'], '暂无（API 未提供）'))
  const offset = value(item, ['output_offset', 'offset'], value(handoff, ['output_offset', 'offset'], '暂无（API 未提供）'))
  const deadline = recordValue(spec.deadline)
  const manifest = recordValue(item.evidence_manifest)
  const testReport = recordValue(manifest.test_report)
  return <>
    <Fact label="TaskSpec revision" value={value(item, ['spec_revision'], '不可见')} />
    <Fact label="Scope" value={listValue(spec.scope) || '无'} />
    <Fact label="Acceptance" value={listValue(spec.acceptance) || '无'} />
    <Fact label="Artifacts" value={listValue(spec.artifacts) || '无'} mono />
    <Fact label="Constraints" value={listValue(spec.constraints) || '无'} />
    <Fact label="Deadline" value={`${value(deadline, ['hard_seconds'], '—')}s`} />
    <Fact label="EvidenceManifest" value={value(manifest, ['manifest_hash'], '尚未生成')} mono />
    <Fact label="Call / Run" value={`${value(manifest, ['call_id'], '—')} / ${value(manifest, ['run_id'], '—')}`} mono />
    <Fact label="Evidence environment" value={value(manifest, ['environment_hash'], '—')} mono />
    <Fact label="Test report" value={`${value(testReport, ['checks_passed'], '—')}/${value(testReport, ['checks_total'], '—')} · exit ${value(testReport, ['execution_exit_code'], '—')}`} />
    <Fact label="Input / Cached carry-in / Uncached input / Output / Total" value={`${input} / ${cachedInput} / ${uncachedInput} / ${output} / ${total}`} />
    <Fact label="Cached 计入 Total" value="是，已包含在 Input 中，不重复相加" />
    <Fact label="Attribution" value={`${value(item, ['attribution_granularity'], 'turn')} / ${value(item, ['value_classification'], 'unknown')}（Uncached 不等于新工作或有价值）`} />
    <Fact label="Token control" value="仅计量；不参与准入、调度、熔断或终态" />
    <Fact label="Output ref" value={outputRef} mono />
    <Fact label="Segments / bytes / offset" value={`${segments} / ${bytes} / ${offset}`} />
  </>
}

function ProjectsView({ projects, form, setForm, onCreate, onRelease, busy }: { projects: Project[]; form: { name: string; path: string; hostPath: string }; setForm: (value: { name: string; path: string; hostPath: string }) => void; onCreate: (event: FormEvent) => void; onRelease: (project: Project) => void; busy: boolean }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">多项目并行</span><h1>项目注册表</h1></div></div><div className="project-grid">{projects.map((project) => <article className="project-card" key={project.id}><div className="project-top"><FolderOpen size={20} /><StatusDot ok={project.status === 'active'} label={project.status === 'active' ? '进行中' : '已完成'} /></div><h2>{project.name}</h2><code>{project.path}</code>{project.host_path && <code className="muted-code">产物源目录 · {project.host_path}</code>}<div className="mini-stats"><span>{project.roles.length} 角色</span><span>{project.workers.length} Worker</span></div>{project.status === 'active' && <button disabled={busy} onClick={() => onRelease(project)}>完成并释放 Worker</button>}</article>)}</div></section>
    <form className="panel form-panel" onSubmit={onCreate}><div className="panel-heading"><div><span className="kicker">项目 → 角色 → CLI 会话</span><h1>注册项目</h1></div></div><Field label="项目名"><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：支付网关" /></Field><Field label="控制面挂载路径"><input required value={form.path} onChange={(event) => setForm({ ...form, path: event.target.value })} placeholder="/projects/payment" /></Field><Field label="本机项目目录（产物源目录）"><input value={form.hostPath} onChange={(event) => setForm({ ...form, hostPath: event.target.value })} placeholder="/Users/name/work/payment" /></Field><p className="form-help">Worker 只通过 Host Bridge 在本机项目目录工作；报告、代码和其他产物不会复制进容器。控制面只保存任务状态与路径索引。</p><button className="primary" disabled={busy}><Plus size={16} />注册项目</button></form></div>
}

export function WorkersView({ items, providers, busy, onRebind }: { items: { project: Project; worker: Worker }[]; providers: Provider[]; busy: boolean; onRebind: (worker: Worker, provider: string) => void }) {
  const [selected, setSelected] = useState<Worker | null>(null)
  const [detail, setDetail] = useState<WorkerDetail | null>(null)
  const [stream, setStream] = useState<WorkerStream | null>(null)
  const selectedWorkerId = selected?.id
  const inspect = async (worker: Worker, cursor = '0:0:0') => {
    setSelected(worker)
    const [nextDetail, nextStream] = await Promise.all([
      api.workerDetail(worker.id), api.workerStream(worker.id, cursor),
    ])
    setDetail(nextDetail)
    setStream((current) => cursor === '0:0:0' ? nextStream : {
      ...nextStream, items: mergeWorkerStreamItems(current?.items ?? [], nextStream.items),
    })
  }
  useEffect(() => {
    if (!selectedWorkerId || !stream) return
    const cursor = stream.next_cursor
    const timer = window.setInterval(() => {
      void Promise.all([
        api.workerDetail(selectedWorkerId),
        api.workerStream(selectedWorkerId, cursor),
      ]).then(([nextDetail, nextStream]) => {
        setDetail(nextDetail)
        setStream((current) => current?.next_cursor !== cursor ? current : {
          ...nextStream,
          items: mergeWorkerStreamItems(current.items, nextStream.items),
        })
      }).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [selectedWorkerId, stream])
  return <section className="panel"><div className="panel-heading"><div><span className="kicker">逻辑 Worker：项目 + 角色</span><h1>Worker 状态</h1></div><span className="muted">物理 CLI Session 以当前 Task 详情为准</span></div><div className="worker-table"><div className="table-head"><span>项目 / 角色</span><span>Provider</span><span>最近观测会话</span><span>状态</span><span>操作</span></div>{items.length ? items.map(({ project, worker }) => <div className="table-row" key={worker.id}><div><strong>{project.name}</strong><small>{roleNames[worker.role] ?? worker.role}</small></div><div><code>{worker.provider}</code><small>逻辑 Worker 第 {worker.session_generation} 代</small></div><div><code>{worker.external_session_id ?? '暂无历史观测'}</code><small>仅用于诊断，不会跨 Task 复用</small><small>Input {worker.last_input_tokens} · Cached {worker.last_cached_input_tokens} · Uncached {worker.last_uncached_input_tokens} · Output {worker.last_output_tokens}</small></div><div><StatusDot ok={worker.status === 'idle'} label={worker.status === 'idle' ? '空闲' : worker.status === 'busy' ? '工作中' : '已释放'} />{worker.last_error && <small className="danger-text">{worker.last_error}</small>}</div><div><button type="button" onClick={() => void inspect(worker)}>查看 Task Session</button><select disabled={busy || worker.status !== 'idle'} value={worker.provider} onChange={(event) => onRebind(worker, event.target.value)}>{providers.filter((provider) => provider.enabled && provider.transport === 'host-bridge').map((provider) => <option key={provider.name} value={provider.name}>{provider.display_name}</option>)}</select></div></div>) : <div className="empty-state compact"><Robot size={34} /><h2>暂无 Worker</h2><p>任务领取时按需创建，终态后释放。</p></div>}</div>{selected && detail && <ButlerConsole worker={selected} detail={detail} stream={stream} onClose={() => { setSelected(null); setDetail(null); setStream(null) }} onContinue={(worker, cursor) => { void inspect(worker, cursor) }} />}</section>
}

function mergeWorkerStreamItems(current: WorkerStream['items'], incoming: WorkerStream['items']) {
  const seen = new Set(current.map((item, index) => `${item.ref ?? item.refs?.join(',') ?? item.kind}:${item.offset ?? index}`))
  return [...current, ...incoming.filter((item, index) => {
    const key = `${item.ref ?? item.refs?.join(',') ?? item.kind}:${item.offset ?? current.length + index}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })]
}

function ProvidersView({ providers, busy, onProbe, onToggle }: { providers: Provider[]; busy: boolean; onProbe: (provider: Provider) => void; onToggle: (provider: Provider) => void }) {
  return <section className="panel"><div className="panel-heading"><div><span className="kicker">分层运行健康</span><h1>Worker Provider</h1></div><span className="muted">版本探针、会话续接、真实执行分别判断</span></div><div className="provider-grid">{providers.map((provider) => {
    const readiness = provider.readiness
    const cliProbe = typeof readiness?.cli_probe === 'string' ? readiness.cli_probe : readiness?.cli_probe.status ?? '未知'
    const cliReason = typeof readiness?.cli_probe === 'object' ? readiness.cli_probe.reason : readiness?.cli_probe_reason
    const cliTime = typeof readiness?.cli_probe === 'object' ? readiness.cli_probe.checked_at : readiness?.cli_probe_at
    return <article className={`provider-card ${provider.enabled ? '' : 'disabled'}`} key={provider.name}><div className="provider-head"><div className="provider-icon"><TerminalWindow size={20} /></div><StatusDot ok={providerFullyReady(provider)} label={!provider.enabled ? '已停用' : providerFullyReady(provider) ? '完全就绪' : '未完全就绪'} /></div><h2>{provider.display_name}</h2><code>{provider.name}</code><dl><div><dt>installed / cli_probe</dt><dd>{readiness ? `${readiness.installed ? 'installed' : 'not installed'} · ${cliProbe}` : '未知（API 未提供）'}<small>{readiness?.installed_at ?? cliTime ?? provider.last_probed_at ?? '时间暂无'} · {readiness?.installed_reason ?? cliReason ?? '原因暂无'}</small></dd></div><div><dt>session_resume_ready</dt><dd>{readiness ? readiness.session_resume_ready ? 'ready' : 'not ready' : '未知（API 未提供）'}<small>{readiness?.session_resume_checked_at ?? '时间暂无'} · {readiness?.session_resume_reason ?? '原因暂无'}</small></dd></div><div><dt>recent_execution_health</dt><dd>{readiness?.recent_execution_health ?? '未知（API 未提供）'}<small>{readiness?.recent_execution_checked_at ?? '时间暂无'} · {readiness?.recent_execution_reason ?? '原因暂无'}</small></dd></div><div><dt>适配器 / 位置</dt><dd>{provider.adapter} · {provider.transport === 'host-bridge' ? '本机 Host Bridge' : '容器'}</dd></div><div><dt>可执行文件</dt><dd>{provider.executable ?? '内置'}</dd></div>{provider.credential_env && <div><dt>凭据引用</dt><dd>env {provider.credential_env} · slots {provider.credential_slot_count ?? '暂无'}</dd></div>}<div><dt>能力</dt><dd>{provider.capabilities.join(' · ')}</dd></div></dl><p className="provider-reason">总状态：{provider.status} · {provider.reason ?? '原因暂无'} · {provider.last_probed_at ?? '时间暂无'}</p><div className="card-actions"><button disabled={busy || !provider.enabled} onClick={() => onProbe(provider)}><Pulse size={16} />0 Token 探测</button><button disabled={busy} onClick={() => onToggle(provider)}>{provider.enabled ? '停用' : '启用'}</button></div></article>
  })}</div><details className="provider-setup"><summary>Host Bridge 启动命令（macOS / Linux / Windows）</summary><h3>macOS</h3><pre>{`.venv/bin/python -m plow_whip_web.host_bridge \\
  --env-file .env.local --project-root /Users/you/work \\
  --state-dir "$HOME/.plow-whip-web/host-bridge"`}</pre><h3>Linux</h3><pre>{`.venv/bin/python -m plow_whip_web.host_bridge \\
  --env-file .env.local --project-root /home/you/work \\
  --state-dir "$HOME/.plow-whip-web/host-bridge"`}</pre><h3>Windows PowerShell</h3><pre>{`.\\.venv\\Scripts\\python.exe -m plow_whip_web.host_bridge \`
  --env-file .env.local --project-root C:\\Users\\you\\work \`
  --state-dir "$HOME\\.plow-whip-web\\host-bridge"`}</pre><p>启动后先执行 0 Token 探测。创建任务和每次派发都会由后端重新探测；未就绪时不会入队或领取任务。</p></details><div className="boundary-note"><ShieldCheck size={20} /><div><strong>Host Bridge 安全边界</strong><p>只接受 Codex、Cursor、JSON Worker 三类结构化请求；固定 argv、固定项目根、无 shell 拼接。容器只保存 SQLite、日志和产物路径索引，不保存项目报告或代码。</p></div></div></section>
}

function UsageView({ usage, projectId, projects, tasks }: { usage: Usage | null; projectId?: string; projects: Project[]; tasks: Task[] }) {
  const [series, setSeries] = useState<UsageDailySeries | null>(null)
  const [projectSeries, setProjectSeries] = useState<UsageDailySeries | null>(null)
  const [breakdown, setBreakdown] = useState<UsageDailyBreakdown | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [rangeDays, setRangeDays] = useState(14)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [dayError, setDayError] = useState<string | null>(null)
  const [historyLoading, setHistoryLoading] = useState(true)

  useEffect(() => {
    let active = true
    Promise.all([
      api.usageDaily({ days: rangeDays }),
      projectId ? api.usageDaily({ days: rangeDays, projectId }) : Promise.resolve(null),
    ])
      .then(([globalNext, projectNext]) => {
        if (!active) return
        setSeries(globalNext)
        setProjectSeries(projectNext)
        setHistoryError(null)
        const scoped = projectNext ?? globalNext
        const preferred = scoped.days.find((day) => day.total_tokens > 0)?.date
          ?? scoped.days[scoped.days.length - 1]?.date
          ?? null
        setSelectedDate((current) => {
          if (current && scoped.days.some((day) => day.date === current)) return current
          return preferred
        })
      })
      .catch((reason: unknown) => {
        if (!active) return
        setSeries(null)
        setHistoryError(messageOf(reason))
      })
      .finally(() => {
        if (active) setHistoryLoading(false)
      })
    return () => { active = false }
  }, [projectId, rangeDays])

  useEffect(() => {
    if (!selectedDate) return
    let active = true
    api.usageDailyDay(selectedDate, projectId)
      .then((next) => {
        if (!active) return
        setBreakdown(next)
        setDayError(null)
      })
      .catch((reason: unknown) => {
        if (!active) return
        setBreakdown(null)
        setDayError(messageOf(reason))
      })
    return () => { active = false }
  }, [projectId, selectedDate])

  const scopedSeries = projectSeries ?? series
  const selectedPoint = scopedSeries?.days.find((day) => day.date === selectedDate) ?? null
  const visibleBreakdown = selectedDate && breakdown?.date === selectedDate ? breakdown : null
  const dimensions = [
    ['Provider', usage?.providers?.map((item) => [item.provider, item.tokens, item.calls])],
    ['Model', usage?.models?.map((item) => [item.model, item.tokens, item.calls])],
    ['Call kind', usage?.call_kinds?.map((item) => [item.call_kind, item.tokens, item.calls])],
    ['Session', usage?.sessions?.map((item) => [item.session_id ?? '未绑定', item.tokens, item.calls])],
  ] as const
  const uncached = usage?.uncached_input_tokens ?? Math.max(0, (usage?.input_tokens ?? 0) - (usage?.cached_input_tokens ?? 0))
  const inputOutputRatio = usage?.ratios?.input_per_output
    ?? ((usage?.output_tokens ?? 0) > 0 ? (usage?.input_tokens ?? 0) / (usage?.output_tokens ?? 1) : null)
  const uncachedOutputRatio = usage?.ratios?.uncached_input_per_output
    ?? ((usage?.output_tokens ?? 0) > 0 ? uncached / (usage?.output_tokens ?? 1) : null)
  const today = usage?.today
  const projectName = projects.find((project) => project.id === projectId)?.name
  const scopeLabel = projectName ? `项目 ${projectName}` : '全部项目'

  return (
    <div className="settings-layout">
      <section className="metrics-strip metrics-strip-wide" data-testid="usage-history-metrics">
        <Metric icon={Coins} label={projectName ? `${scopeLabel}全历史` : '全历史 Total'} value={usage?.total_tokens ?? 0} hint={`scope=all_history · ${usage?.timezone ?? 'Asia/Shanghai'}`} />
        <Metric icon={Network} label="Input" value={usage?.input_tokens ?? 0} hint="含 Cached；累计快照不重复相加" />
        <Metric icon={Clock} label="Cached-input" value={usage?.cached_input_tokens ?? 0} hint="Input 子集，不重复相加" />
        <Metric icon={HardDrives} label="Uncached-input" value={uncached} hint="cache miss = Input − Cached" />
        <Metric icon={CheckCircle} label="Output" value={usage?.output_tokens ?? 0} hint="后端已差分计量" />
      </section>
      <section className="metrics-strip" data-testid="usage-today-metrics">
        <Metric icon={Coins} label={projectName ? `${scopeLabel}今日` : '今日 Total'} value={today?.total_tokens ?? 0} hint={`${today?.date ?? '—'} · Asia/Shanghai 日界`} />
        <Metric icon={Network} label="今日 Input" value={today?.input_tokens ?? 0} hint="local_day · 非全历史" />
        <Metric icon={HardDrives} label="今日 Uncached" value={today?.uncached_input_tokens ?? 0} hint="今日 cache miss" />
        <Metric icon={CheckCircle} label="今日 Output" value={today?.output_tokens ?? 0} hint="今日增量" />
      </section>

      <section className="panel" data-testid="usage-ratios">
        <div className="panel-heading">
          <div>
            <span className="kicker">可见比值 · 非 Gate</span>
            <h1>Input/Output 与 Uncached/Output</h1>
          </div>
          <span className="muted">比值仅解释账本；不参与预算准入、调度或质量终态</span>
        </div>
        <div className="usage-columns">
          <div className="usage-row"><span>全历史 Input / Output</span><strong>{formatRatio(inputOutputRatio)}</strong></div>
          <div className="usage-row"><span>全历史 Uncached-input / Output</span><strong>{formatRatio(uncachedOutputRatio)}</strong></div>
        </div>
        <p className="form-help">is_budget_gate={String(usage?.ratios?.is_budget_gate ?? false)} · is_quality_gate={String(usage?.ratios?.is_quality_gate ?? false)}。Cached 已计入 Input，Total = Input + Output。</p>
      </section>

      <section className="panel" data-testid="usage-quality">
        <div className="panel-heading">
          <div>
            <span className="kicker">exact delta · legacy_inferred_delta</span>
            <h1>账本质量占比</h1>
          </div>
          <span className="muted">历史存量可掩盖新链路效果；不作重分类或删除</span>
        </div>
        {usage?.usage_quality?.length ? usage.usage_quality.map((item) => (
          <div className="usage-row" key={item.usage_semantics}>
            <span>
              {item.label ?? (item.usage_semantics === 'delta' ? 'exact_delta' : item.usage_semantics)}
              <small>{item.calls} calls · {item.tokens.toLocaleString()} tokens</small>
            </span>
            <strong>
              Token {(100 * (item.token_share ?? 0)).toFixed(1)}%
              {' · '}
              Calls {(100 * (item.call_share ?? 0)).toFixed(1)}%
            </strong>
          </div>
        )) : <p className="muted">尚无 usage_quality 分桶。</p>}
        {usage?.raw_snapshot_totals ? (
          <p className="form-help">
            Provider 原始累计快照合计 {usage.raw_snapshot_totals.total_tokens}；页面总数按相邻 Session 快照增量计量。迁移前缺少物理 Session id 的记录标记为 legacy_inferred_delta，不伪装成 exact delta。
          </p>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="kicker">日级分析 · Asia/Shanghai</span>
            <h1>Token 趋势</h1>
          </div>
          <label className="token-range">
            范围
            <select
              aria-label="历史日期范围"
              value={rangeDays}
              onChange={(event) => {
                setRangeDays(Number(event.target.value))
                setHistoryLoading(true)
                setHistoryError(null)
              }}
            >
              <option value={7}>近 7 天</option>
              <option value={14}>近 14 天</option>
              <option value={30}>近 30 天</option>
              <option value={90}>近 90 天</option>
            </select>
          </label>
        </div>
        {historyLoading ? <p className="muted">加载日级趋势…</p> : null}
        {historyError ? <div className="error-banner" data-testid="token-history-error"><p>{historyError}</p></div> : null}
        {!historyLoading && !historyError && series ? (
          <>
            <TokenTrendChart
              days={series.days}
              comparisonDays={projectSeries?.days}
              comparisonLabel={projectName ? `项目 ${projectName}` : undefined}
              selectedDate={selectedDate}
              onSelect={(date) => {
                setSelectedDate(date)
                setDayError(null)
              }}
            />
            {selectedPoint ? (
              <p className="form-help">
                已选范围日 {selectedPoint.date}（Asia/Shanghai）：total {selectedPoint.total_tokens}
                （input {selectedPoint.input_tokens}，其中 cached {selectedPoint.cached_input_tokens} /
                uncached {selectedPoint.uncached_input_tokens}；output {selectedPoint.output_tokens}）
                · 非全历史总量
              </p>
            ) : null}
          </>
        ) : null}
        {dayError ? <div className="error-banner" data-testid="token-day-error"><p>{dayError}</p></div> : null}
        {visibleBreakdown ? (
          <div className="token-pie-grid" data-testid="token-day-pies">
            {!projectId ? <TokenPieChart
              title="项目占比"
              slices={visibleBreakdown.projects}
              totalTokens={visibleBreakdown.total_tokens}
              emptyLabel="该日无项目消费。"
            /> : null}
            {projectId ? <TokenPieChart
              title="任务占比"
              slices={visibleBreakdown.tasks}
              totalTokens={visibleBreakdown.total_tokens}
              emptyLabel="该日无任务消费。"
            /> : null}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="kicker">ModelCallLedger · {usage?.usage_semantics ?? 'physical_session_delta'} · 全历史明细</span>
            <h1>消费明细</h1>
          </div>
          <span className="muted">Token 只计量，不参与任务准入、调度、熔断或终态</span>
        </div>
        <div className="usage-columns">
          <div>
            <h2>按项目</h2>
            {usage?.projects?.length ? usage.projects.map((item) => (
              <div className="usage-row" key={item.project_id ?? 'none'}>
                <span>
                  {projects.find((project) => project.id === item.project_id)?.name ?? '未绑定项目'}
                  <small>Input {item.input_tokens} · Cached {item.cached_input_tokens} · Uncached {item.uncached_input_tokens} · Output {item.output_tokens}</small>
                </span>
                <strong>{item.tokens}</strong>
              </div>
            )) : <p className="muted">尚无模型消费。</p>}
          </div>
          <div>
            <h2>按任务</h2>
            {usage?.tasks?.length ? usage.tasks.map((item) => (
              <div className="usage-row" key={item.task_id ?? 'none'}>
                <span>
                  {tasks.find((task) => task.id === item.task_id)?.title ?? item.task_id ?? '未绑定任务'}
                  <small>Input {item.input_tokens} · Cached {item.cached_input_tokens} · Uncached {item.uncached_input_tokens} · Output {item.output_tokens}</small>
                </span>
                <strong>{item.tokens}</strong>
              </div>
            )) : <p className="muted">调度与探测不会制造账单。</p>}
          </div>
        </div>
        <div className="usage-columns">
          {dimensions.map(([label, rows]) => (
            <div key={label}>
              <h2>按 {label}</h2>
              {rows?.map(([key, tokens, calls]) => (
                <div className="usage-row" key={String(key)}>
                  <span>{key}<small>{calls} calls</small></span>
                  <strong>{tokens}</strong>
                </div>
              ))}
            </div>
          ))}
        </div>
        {usage?.calls?.length ? (
          <div>
            <h2>调用清单</h2>
            {usage.calls.map((call) => (
              <div className="usage-row" key={call.call_id}>
                <span>
                  {call.task_id ?? call.call_id}
                  <small>{call.call_kind} · {call.status} · {call.provider}/{call.model} · Worker {call.worker_id ?? '—'} · session {call.session_id ?? '—'}</small>
                </span>
                <strong>{call.input_tokens} / {call.cached_input_tokens} / {call.uncached_input_tokens} / {call.output_tokens}</strong>
              </div>
            ))}
          </div>
        ) : null}
        <p className="muted">Uncached input 仅表示缓存未命中，不等于新工作或有价值内容。</p>
      </section>
    </div>
  )
}

function AuditView({ audit, permissions, onRefresh }: { audit: AuditEntry[]; permissions: PermissionGrant[]; onRefresh: () => void }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">本地不可变审计</span><h1>变更记录</h1></div><button onClick={onRefresh}><ArrowsClockwise size={16} />刷新</button></div><div className="audit-table"><div className="table-head"><span>#</span><span>方法</span><span>路径</span><span>状态</span><span>时间</span></div>{audit.map((entry) => <div className="table-row" key={entry.sequence}><span>{entry.sequence}</span><strong>{entry.method}</strong><code>{entry.path}</code><span>{entry.status_code}</span><small>{entry.created_at}</small></div>)}</div></section><section className="panel"><div className="panel-heading"><div><span className="kicker">权限边界</span><h1>权限决策</h1></div></div>{permissions.map((permission) => <div className="usage-row" key={permission.id}><span>{permission.capability} · {permission.resource}</span><strong>{permission.decision}</strong></div>)}</section></div>
}

function AlertsView({ alerts, onRefresh }: { alerts: Alerts | null; onRefresh: () => Promise<void> }) {
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const visible = (alerts?.items ?? []).filter((incident) => filter === 'all' || incident.status !== 'resolved')
  const grouped = visible.reduce<Record<string, AlertIncident[]>>((result, incident) => {
    const key = incident.root_kind === 'global_network' ? 'network'
      : incident.root_kind.includes('network') ? 'network'
        : incident.root_kind.includes('provider') ? 'provider'
          : incident.root_kind
    ;(result[key] ??= []).push(incident)
    return result
  }, {})
  return <section className="panel alerts-view">
    <div className="panel-heading">
      <div><span className="kicker">根因关联 · 抑制 · 收敛</span><h1>告警中心</h1></div>
      <div className="detail-actions"><select value={filter} onChange={(event) => setFilter(event.target.value as 'active' | 'all')}><option value="active">仅活动</option><option value="all">全部历史</option></select><button onClick={() => void onRefresh()}><ArrowsClockwise size={16} />刷新</button></div>
    </div>
    <div className="boundary-note"><ShieldCheck size={20} /><div><strong>同一根因只显示一条主告警</strong><p>全局断网会抑制 Provider 和任务级派生告警；原始事件仍保留在事件历史中。恢复需通过连续探测，不以单次成功抖动关闭。</p></div></div>
    <div className="alert-summary"><Fact label="活动事件" value={String((alerts?.items ?? []).filter((item) => item.status !== 'resolved').length)} /><Fact label="网络状态" value={networkStatusSummary(alerts?.network)} /><Fact label="分组数" value={String(Object.keys(grouped).length)} /></div>
    {Object.entries(grouped).map(([kind, incidents]) => <section className="alert-group" key={kind}><h2>{kind === 'network' ? '网络根因' : kind === 'provider' ? 'Provider 根因' : kind}</h2>{incidents.map((incident) => <article className={`alert-card severity-${incident.severity}`} key={incident.id}><header><div><strong>{incident.title}</strong><small>{incident.scope_key} · {incident.root_kind}</small></div><span>{incident.status}</span></header><p>{summary(incident.detail, ['reason', 'state', 'provider', 'zone'])}</p><footer><span>首次 {incident.first_seen_at}</span><span>最近 {incident.last_seen_at}</span><span>合并 {incident.occurrence_count} 次</span></footer></article>)}</section>)}
    {!visible.length && <div className="empty-state"><CheckCircle size={38} /><h2>当前没有活动告警</h2><p>网络、Provider 与运行时状态已收敛。</p></div>}
  </section>
}

function SettingsView(props: { settings: RuntimeSettings | null; setSettings: (value: RuntimeSettings) => void; scheduler: SchedulerStatus | null; convention: Convention | null; setConvention: (value: Convention) => void; suggestion: ConventionSuggestion | null; setSuggestion: (value: ConventionSuggestion | null) => void; projects: Project[]; tasks: Task[]; workers: { project: Project; worker: Worker }[]; selectedProject?: string; providers: Provider[]; audit: AuditEntry[]; permissions: PermissionGrant[]; refineProvider: string; setRefineProvider: (value: string) => void; busy: boolean; onSaveSettings: (event: FormEvent) => void; onTick: () => void; onLoadConvention: (scope: Convention['scope'], id: string) => void; onSaveConvention: (event: FormEvent) => void; onRefine: () => void; onProbeProvider: (provider: Provider) => void; onToggleProvider: (provider: Provider) => void; onRebindWorker: (worker: Worker, provider: string) => void; onRefreshAudit: () => void }) {
  const { settings, setSettings, scheduler, convention, setConvention, suggestion, setSuggestion, projects, tasks, workers, selectedProject, providers, audit, permissions, refineProvider, setRefineProvider, busy, onSaveSettings, onTick, onLoadConvention, onSaveConvention, onRefine, onProbeProvider, onToggleProvider, onRebindWorker, onRefreshAudit } = props
  const [baselineRole, setBaselineRole] = useState('backend')
  const [baseline, setBaseline] = useState<BehaviorBaseline | null>(null)
  const [conventionBaseline, setConventionBaseline] = useState<BehaviorBaseline | null>(null)
  const [effectiveBaseline, setEffectiveBaseline] = useState<BehaviorBaseline | null>(null)
  const [effectiveSettings, setEffectiveSettings] = useState<RuntimeSettings | null>(null)
  const [projectOverride, setProjectOverride] = useState<RuntimeSettingsOverride | null>(null)
  const [frontendInstances, setFrontendInstances] = useState<RoleInstance[]>([])
  const [settingsError, setSettingsError] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    api.behaviorBaseline(baselineRole).then((item) => { if (alive) setBaseline(item) }).catch(() => { if (alive) setBaseline(null) })
    return () => { alive = false }
  }, [baselineRole])
  const projectId = selectedProject
  const taskId = tasks[0]?.id
  const taskRoleId = tasks[0]?.role_id ?? undefined
  useEffect(() => {
    let alive = true
    api.conventionInventory({
      projectId,
      taskId,
      roleId: taskRoleId,
      role: baselineRole,
    }).then((item) => {
      if (alive) setConventionBaseline(item.behavior_baseline)
    }).catch(() => { if (alive) setConventionBaseline(null) })
    return () => { alive = false }
  }, [baselineRole, projectId, taskId, taskRoleId])
  useEffect(() => {
    let alive = true
    if (!taskId) {
      api.behaviorBaseline('butler').then((item) => {
        if (alive) setEffectiveBaseline(item)
      }).catch(() => { if (alive) setEffectiveBaseline(null) })
      return () => { alive = false }
    }
    api.conventionEffective(taskId, taskRoleId).then((item) => {
      if (alive) setEffectiveBaseline(item.behavior_baseline)
    }).catch(() => {
      api.behaviorBaseline('butler').then((item) => {
        if (alive) setEffectiveBaseline(item)
      }).catch(() => { if (alive) setEffectiveBaseline(null) })
    })
    return () => { alive = false }
  }, [taskId, taskRoleId])
  useEffect(() => {
    let alive = true
    if (!projectId) {
      return () => { alive = false }
    }
    Promise.all([
      api.effectiveSettings(projectId, taskId, taskRoleId),
      api.settingsOverride('project', projectId),
      api.roleInstances({ projectId }),
    ]).then(([effective, override, instances]) => {
      if (!alive) return
      setEffectiveSettings(effective?.values ? effective : null)
      setProjectOverride(override?.values ? override : null)
      setFrontendInstances(instances.items.filter((item) => item.role_kind === 'frontend'))
      setSettingsError(null)
    }).catch((reason: unknown) => {
      if (alive) setSettingsError(messageOf(reason))
    })
    return () => { alive = false }
  }, [projectId, taskId, taskRoleId])
  async function saveProjectOverride(event: FormEvent) {
    event.preventDefault()
    if (!projectOverride) return
    try {
      setProjectOverride(await api.updateSettingsOverride(projectOverride))
      setEffectiveSettings(await api.effectiveSettings(projectId, taskId, taskRoleId))
      setSettingsError(null)
    } catch (reason) {
      setSettingsError(messageOf(reason))
    }
  }
  const conventionFacts = conventionBaseline ?? baseline
  return <div className="settings-layout">
    <details className="setting-group" open><summary>常规</summary><section className="panel cron-panel"><div className="panel-heading"><div><span className="kicker">单一全局 Crontab</span><h1>无人值守调度</h1></div><StatusDot ok={Boolean(scheduler?.engine.active)} label={scheduler?.engine.active ? '运行中' : '未运行'} /></div><div className="facts"><Fact label="引擎" value={scheduler ? `${scheduler.engine.managed_by} · ${scheduler.engine.backend}` : '检测中'} /><Fact label="下次执行" value={scheduler?.schedule.next_run_at ?? '尚未计算'} /><Fact label="Fencing" value={String(scheduler?.runtime.fencing_token ?? 0)} /><Fact label="最近 Tick" value={scheduler?.runtime.last_tick_at ?? '尚未执行'} /></div><button className="primary" disabled={busy} onClick={onTick}><Play size={16} />立即运行 Tick</button></section></details>
    <details className="setting-group"><summary>管家与 Provider</summary><section className="panel"><div className="panel-heading"><div><span className="kicker">智能入口与故障转移</span><h1>默认路由</h1></div></div>{settings ? <div className="facts"><Fact label="全局/项目管家" value={`${settings.values.default_butler_provider} · ${settings.sources?.default_butler_provider ?? 'global'}`} /><Fact label="默认策略" value={`${settings.values.default_provider_policy} · ${settings.sources?.default_provider_policy ?? 'global'}`} /><Fact label="顺序" value={settings.values.default_provider_order.join(' → ')} /><Fact label="大陆网络" value="DeepSeek / Kimi" /><Fact label="海外网络" value="Codex / Cursor" /></div> : null}<div className="provider-compact">{providers.map((provider) => <div key={provider.name}><strong>{provider.display_name}</strong><StatusDot ok={providerFullyReady(provider)} label={`${provider.status} · ${String((provider as unknown as Record<string, unknown>).network_zone ?? 'zone 未知')}`} /></div>)}</div><p className="form-help">Task 创建时冻结 auto/preferred/pinned、fallback 和顺序。Pinned Provider 不可用时进入 provider_suspended，不会静默换模型。</p></section><ProvidersView providers={providers} busy={busy} onProbe={onProbeProvider} onToggle={onToggleProvider} /></details>
    <details className="setting-group"><summary>持续执行与保护</summary><section className="panel form-panel"><div className="panel-heading"><div><span className="kicker">Task + 角色 ＞ 项目 ＞ 全局</span><h1>{projectId ? '当前项目有效设置' : '全局继承预览'}</h1></div></div>{settingsError ? <div className="error-banner"><p>{settingsError}</p></div> : null}{projectId && effectiveSettings ? <form onSubmit={saveProjectOverride}><div className="facts"><Fact label="Context" value={`${effectiveSettings.values.context_max_bytes} · ${effectiveSettings.sources?.context_max_bytes ?? 'global'}`} /><Fact label="Checkpoint" value={`${effectiveSettings.values.checkpoint_max_bytes} · ${effectiveSettings.sources?.checkpoint_max_bytes ?? 'global'}`} /><Fact label="Handoff" value={`${effectiveSettings.values.handoff_max_bytes} · ${effectiveSettings.sources?.handoff_max_bytes ?? 'global'}`} /></div>{projectOverride ? <div className="form-grid"><NumberField label="项目同类失败熔断" value={projectOverride.values.max_same_failure ?? effectiveSettings.values.max_same_failure} onChange={(value) => setProjectOverride({ ...projectOverride, values: { ...projectOverride.values, max_same_failure: value } })} /><NumberField label="项目无进展熔断" value={projectOverride.values.max_no_progress ?? effectiveSettings.values.max_no_progress} onChange={(value) => setProjectOverride({ ...projectOverride, values: { ...projectOverride.values, max_no_progress: value } })} /><NumberField label="项目 Context 字节" value={projectOverride.values.context_max_bytes ?? effectiveSettings.values.context_max_bytes} onChange={(value) => setProjectOverride({ ...projectOverride, values: { ...projectOverride.values, context_max_bytes: value } })} /></div> : null}<p className="form-help">这里只写项目覆盖，不修改全局值或角色模板。有效来源由后端逐项返回。</p><button className="primary" disabled={busy || !projectOverride}>保存项目覆盖</button></form> : <p className="form-help">选择具体项目后显示逐项有效值与来源；全局值在下方高级表单修改。</p>}</section></details>
    <details className="setting-group"><summary>角色、规则与模板</summary><section className="panel" data-testid="behavior-baseline-panel"><div className="panel-heading"><div><span className="kicker">开发角色行为基线</span><h1>四原则来源与适用性</h1></div></div><div className="form-grid two"><Field label="预览角色"><select value={baselineRole} onChange={(event) => setBaselineRole(event.target.value)}><option value="backend">backend</option><option value="frontend">frontend</option><option value="ui">ui</option><option value="fullstack">fullstack</option><option value="devops_sre">devops_sre</option><option value="verification">verification</option><option value="butler">butler（非开发）</option><option value="coordination">coordination（非开发）</option></select></Field></div>{baseline && <div className="facts" data-testid="behavior-baseline-facts"><Fact label="适用性" value={baseline.not_applicable ? '不适用' : '适用（开发角色）'} /><Fact label="Mandatory" value={baseline.mandatory ? '是' : '否'} /><Fact label="保留量" value={String(baseline.effective_reserve_bytes)} /><Fact label="Revision" value={String(baseline.revision)} /><Fact label="Source" value={baseline.source} mono /><Fact label="配置来源" value={baseline.config_source ?? 'rule_versions:development'} mono />{baseline.reason ? <Fact label="说明" value={baseline.reason} /> : null}</div>}{projectId ? <div className="role-snapshot"><h2>tmpl.frontend@1 · RoleInstance 快照</h2>{frontendInstances.length ? frontendInstances.map((instance) => <div className="facts" key={instance.id}><Fact label="Template" value={`${instance.template_id}@${instance.template_revision}`} mono /><Fact label="Template hash" value={instance.template_hash} mono /><Fact label="Ruleset hash" value={instance.ruleset_hash} mono /><Fact label="Instance revision" value={String(instance.revision)} /><Fact label="Source" value="role_template" /><Fact label="TaskSpec" value={`r${instance.task_spec_revision}`} /></div>) : <p className="form-help">当前项目尚无 frontend RoleInstance。</p>}<ul className="rule-list"><li>dev.think_before_coding@1 · role_template</li><li>dev.simplicity_first@1 · role_template</li><li>dev.surgical_changes@1 · role_template</li><li>dev.goal_driven_execution@1 · role_template</li></ul></div> : null}</section>
    {settings && <form className="panel form-panel" onSubmit={onSaveSettings}><div className="panel-heading"><div><span className="kicker">设置修订 {settings.revision}</span><h1>无人值守、Token 节省与快速续接</h1></div><span className="muted">优先级：Task + 角色 ＞ 项目 ＞ 全局</span></div><h2>调度</h2><div className="form-grid"><Field label="Cron 表达式"><input value={settings.values.cron_expression} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_expression: event.target.value } })} /></Field><Field label="时区"><input value={settings.values.cron_timezone} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_timezone: event.target.value } })} /></Field><Field label="错过执行"><select value={settings.values.cron_misfire_policy} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_misfire_policy: event.target.value as 'catch_up_once' | 'skip' } })}><option value="catch_up_once">恢复后只补跑一次</option><option value="skip">跳过</option></select></Field><NumberField label="最大并行 Worker" value={settings.values.max_parallel_workers} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_parallel_workers: value } })} /></div><h2>统一连续性阈值</h2><div className="form-grid"><NumberField label="同类失败熔断" value={settings.values.max_same_failure} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_same_failure: value } })} /><NumberField label="无进展熔断" value={settings.values.max_no_progress} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_no_progress: value } })} /><NumberField label="Context 最大字节" value={settings.values.context_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, context_max_bytes: value } })} /><NumberField label="Checkpoint 最大字节" value={settings.values.checkpoint_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, checkpoint_max_bytes: value } })} /><NumberField label="Handoff 最大字节" value={settings.values.handoff_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, handoff_max_bytes: value } })} /><NumberField label="观察日志行数" value={settings.values.observation_tail_lines} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, observation_tail_lines: value } })} /><NumberField label="观察最大字节" value={settings.values.observation_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, observation_max_bytes: value } })} /><NumberField label="文件轮转字节" value={settings.values.rotation_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, rotation_max_bytes: value } })} /></div><p className="form-help">当前来源：Context {settings.sources?.context_max_bytes ?? 'global'} · Checkpoint {settings.sources?.checkpoint_max_bytes ?? 'global'} · Handoff {settings.sources?.handoff_max_bytes ?? 'global'} · 观察 {settings.sources?.observation_max_bytes ?? 'global'} · 轮转 {settings.sources?.rotation_max_bytes ?? 'global'}</p>{settings.warnings?.length ? <div className="error-banner">{settings.warnings.map((warning) => <p key={warning}>阈值冲突：{warning}</p>)}</div> : null}<p className="form-help">提交时后端会校验 Checkpoint、Handoff、Context 与轮转/观察阈值；不会用固定 8 KiB 覆盖项目或 Task 特例。</p><label className="toggle-row"><input type="checkbox" checked={settings.values.cron_enabled} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_enabled: event.target.checked } })} /><span>启用容器内 Crontab</span></label><label className="toggle-row"><input type="checkbox" checked={settings.values.auto_dispatch} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, auto_dispatch: event.target.checked } })} /><span>自动派发待执行任务</span></label><button className="primary" disabled={busy}>保存并检查冲突</button></form>}
    {convention && <form className="panel convention-panel" onSubmit={onSaveConvention}><div className="panel-heading"><div><span className="kicker">全局 · 项目 · Task</span><h1>Convention 编辑器</h1></div><span className="revision">修订 {convention.revision}</span></div><div className="form-grid two"><Field label="作用域"><select value={convention.scope} onChange={(event) => { const scope = event.target.value as Convention['scope']; const id = scope === 'global' ? 'global' : scope === 'project' ? projects[0]?.id ?? '' : tasks[0]?.id ?? ''; onLoadConvention(scope, id) }}><option value="global">全局</option><option value="project">项目</option><option value="task">Task</option></select></Field><Field label="目标"><select disabled={convention.scope === 'global'} value={convention.scope_id} onChange={(event) => onLoadConvention(convention.scope, event.target.value)}>{convention.scope === 'global' ? <option value="global">全局</option> : convention.scope === 'project' ? projects.map((project) => <option value={project.id} key={project.id}>{project.name}</option>) : tasks.map((task) => <option value={task.id} key={task.id}>{task.title}</option>)}</select></Field></div>{conventionFacts && <div className="facts" data-testid="convention-baseline-facts"><Fact label="内置基线适用性" value={conventionFacts.not_applicable ? '不适用' : '适用（开发角色）'} /><Fact label="Mandatory" value={conventionFacts.mandatory ? '是' : '否'} /><Fact label="保留量" value={String(conventionFacts.effective_reserve_bytes)} /><Fact label="Source" value={conventionFacts.source} mono /><Fact label="配置来源" value={conventionFacts.config_source ?? 'rule_versions:development'} mono /></div>}<div className="editor-grid"><div><label>当前 Convention</label><textarea value={convention.content} onChange={(event) => setConvention({ ...convention, content: event.target.value })} placeholder="写下质量门、权限边界和必须验证的完成条件。" /></div><div><label>{suggestion ? `${suggestion.provider} 精炼建议` : 'Worker 精炼建议'}</label><textarea value={suggestion?.suggestion ?? ''} readOnly placeholder="点击“模型精炼”后在这里审阅建议；不会自动覆盖原文。" /></div></div><div className="detail-actions"><select className="inline-select" value={refineProvider} onChange={(event) => setRefineProvider(event.target.value)}>{providers.filter((provider) => provider.enabled && provider.capabilities.includes('refine_convention')).map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}{provider.status === 'available' ? '' : '（当前不可用）'}</option>)}</select><button type="button" disabled={busy || !convention.content.trim()} onClick={onRefine}><MagicWand size={16} />模型精炼（计 Token）</button>{suggestion && <button type="button" onClick={() => { setConvention({ ...convention, content: suggestion.suggestion }); setSuggestion(null) }}><CheckCircle size={16} />采用建议</button>}<button className="primary" disabled={busy}>保存 Convention</button></div><p className="form-help">精炼是明确的模型动作，会记录 Token；保存仍需人工确认。内置开发角色四原则与可变 Convention 分层展示，只通过同一套 ContextCompiler 汇总。</p></form>}
    {effectiveBaseline && <section className="panel" data-testid="effective-context-panel"><div className="panel-heading"><div><span className="kicker">Effective Context</span><h1>编译预览中的四原则</h1></div><span className="muted">来自 /api/conventions/effective</span></div><div className="facts" data-testid="effective-context-baseline-facts"><Fact label="适用性" value={effectiveBaseline.not_applicable ? '不适用' : '适用（开发角色）'} /><Fact label="Mandatory" value={effectiveBaseline.mandatory ? '是' : '否'} /><Fact label="保留量" value={String(effectiveBaseline.effective_reserve_bytes)} /><Fact label="Revision" value={String(effectiveBaseline.revision)} /><Fact label="Source" value={effectiveBaseline.source} mono /><Fact label="配置来源" value={effectiveBaseline.config_source ?? 'rule_versions:development'} mono />{effectiveBaseline.reason ? <Fact label="说明" value={effectiveBaseline.reason} /> : null}</div></section>}
    </details>
    <details className="setting-group"><summary>安全与运行时</summary><section className="panel progressive-note"><div className="facts">{settings ? <><Fact label="Episode wall" value={`${settings.values.episode_wall_limit_seconds}s · ${settings.sources?.episode_wall_limit_seconds ?? 'global'}`} /><Fact label="无进展窗口" value={`${settings.values.no_progress_seconds}s · ${settings.sources?.no_progress_seconds ?? 'global'}`} /><Fact label="最大 Host 进程" value={`${settings.values.max_host_processes} · ${settings.sources?.max_host_processes ?? 'global'}`} /><Fact label="网络失败/恢复" value={`${settings.values.network_failure_threshold} / ${settings.values.network_recovery_successes}`} /><Fact label="Provider 失败/恢复" value={`${settings.values.provider_failure_threshold} / ${settings.values.provider_recovery_successes}`} /><Fact label="告警抑制窗口" value={`${settings.values.alert_debounce_seconds}s`} /></> : null}</div><p>Episode 上限会与 Task sizing 的 hard deadline 取最小值；有可核验证据进展才允许有界续期，且永不越过 hard deadline。网络和 Provider 故障进入 suspended 状态，不伪装成业务验证失败。</p></section><WorkersView items={workers} providers={providers} busy={busy} onRebind={onRebindWorker} /></details>
    <details className="setting-group"><summary>高级与审计</summary><section className="panel progressive-note"><p>Convention、Effective Context、迁移与审计记录均显示 revision、来源和最终有效值；项目覆盖不会改写全局模板。危险运行参数保持显式，不提供“一键放宽全部限制”。</p></section><AuditView audit={audit} permissions={permissions} onRefresh={onRefreshAudit} /></details>
  </div>
}

function TaskDrawer(props: { form: typeof initialTask; setForm: (value: typeof initialTask) => void; setSizingInputs: (value: TaskSizingInputs) => void; estimate: TaskSizingEstimate | null; projects: Project[]; providers: Provider[]; busy: boolean; onClose: () => void; onEstimate: () => void; onSubmit: (event: FormEvent) => void }) {
  const { form, setForm, setSizingInputs, estimate, projects, providers, busy, onClose, onEstimate, onSubmit } = props
  const sizing = form.sizingInputs
  const setSizing = <K extends keyof TaskSizingInputs>(key: K, value: TaskSizingInputs[K]) => setSizingInputs({ ...sizing, [key]: value })
  const ready = estimate?.status === 'estimated'
  const provider = providers.find((item) => item.name === form.provider)
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-head"><div><span className="kicker">诊断入口 · 非主流程</span><h1>新建任务</h1></div><button aria-label="关闭" onClick={onClose}><X size={18} /></button></div><form onSubmit={onSubmit}><Field label="标题"><input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="明确、可验证的结果" /></Field><Field label="目标"><textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="Worker 必须完成什么？" /></Field><div className="form-grid two"><Field label="项目"><select required value={form.projectId} onChange={(event) => setForm({ ...form, projectId: event.target.value })}><option value="">选择项目</option>{projects.filter((p) => p.status === 'active').map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></Field><Field label="角色"><select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}>{Object.entries(roleNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></Field><Field label="Provider"><select value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value })}>{providers.filter((provider) => provider.enabled && provider.transport === 'host-bridge').map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}</option>)}</select></Field></div><p className="form-help">Provider 当前状态：{provider?.status === 'available' ? '可用' : provider?.reason ?? '待探测'}。提交和派发前均会重新执行 0 Token 就绪探测。</p><div className="form-grid two"><Field label="验证文件 / 产物路径"><input value={form.verifyPath} onChange={(event) => setForm({ ...form, verifyPath: event.target.value })} /></Field><Field label="必须包含"><input value={form.verifyText} onChange={(event) => setForm({ ...form, verifyText: event.target.value })} /></Field></div><p className="form-help">产物路径相对于本机项目目录；完成后可在任务页复制路径、Finder 定位或用 Cursor 打开。</p><section className="preflight"><div className="preflight-heading"><div><span className="kicker">入队前必做</span><h2>0 Token 规则评估</h2></div><span>不调用模型</span></div><div className="form-grid two"><NumberField label="涉及层数" value={sizing.layers_touched} onChange={(value) => setSizing('layers_touched', value)} /><NumberField label="组件数" value={sizing.components_touched} onChange={(value) => setSizing('components_touched', value)} /><NumberField label="预计文件数" value={sizing.estimated_files_changed} onChange={(value) => setSizing('estimated_files_changed', value)} /><Field label="风险"><select value={sizing.risk_level} onChange={(event) => setSizing('risk_level', event.target.value as TaskSizingInputs['risk_level'])}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></Field></div><details className="preflight-advanced"><summary>高级预判</summary><div className="form-grid two"><NumberField label="验证命令数" value={sizing.verification_commands_count} onChange={(value) => setSizing('verification_commands_count', value)} /><NumberField label="预计验证秒数" value={sizing.estimated_verification_seconds} onChange={(value) => setSizing('estimated_verification_seconds', value)} /><NumberField label="外部依赖数" value={sizing.external_dependencies_count} onChange={(value) => setSizing('external_dependencies_count', value)} /></div><div className="check-grid"><Check label="包含迁移" checked={sizing.has_migration} onChange={(value) => setSizing('has_migration', value)} /><Check label="包含部署" checked={sizing.has_deploy} onChange={(value) => setSizing('has_deploy', value)} /><Check label="附加独立复审偏好（不阻塞）" checked={sizing.independent_review_required} onChange={(value) => setSizing('independent_review_required', value)} /></div><p className="form-help">独立复审仅作为附加偏好；任务自己的 verification Gate 才是终态依据，不会创建 reviewer 依赖。</p></details><div className="gate-grid"><Check label="产物明确" checked={sizing.gate_artifact} onChange={(value) => setSizing('gate_artifact', value)} /><Check label="边界明确" checked={sizing.gate_boundary} onChange={(value) => setSizing('gate_boundary', value)} /><Check label="验证明确" checked={sizing.gate_verification} onChange={(value) => setSizing('gate_verification', value)} /><Check label="依赖明确" checked={sizing.gate_dependency} onChange={(value) => setSizing('gate_dependency', value)} /></div><button type="button" className="full" disabled={busy} onClick={onEstimate}><Pulse size={16} />执行 0 Token 预判</button>{estimate && <EstimateCard estimate={estimate} />}</section><button className="primary full" disabled={busy || !projects.length || !ready}><Plus size={16} />检查 Provider 并加入任务队列</button></form></aside></div>
}

const gateNames = {
  artifact: '可验证产物',
  boundary: '文件或组件边界',
  verification: '验证命令',
  dependency: '外部依赖',
  independent_review_orchestration: '独立复审偏好（不作为调度 Gate）',
}

function EstimateCard({ estimate }: { estimate: TaskSizingEstimate }) {
  if (estimate.status === 'needs_planning') return <div className="estimate-card blocked" role="status"><strong>暂不可入队：先补齐计划</strong><p>缺少：{estimate.missing_gates.map((gate) => gateNames[gate]).join('、')}。请先拆分任务或补齐对应 gate。</p><small>0 Token 规则评估 · 未调用模型</small></div>
  return <div className="estimate-card" role="status"><div><strong>服务端 Tier {estimate.size_class}</strong><span>0 Token 规则评估</span></div><dl><Fact label="Soft Timeout" value={`${estimate.soft_deadline_seconds}s`} /><Fact label="Hard Timeout" value={`${estimate.hard_deadline_seconds}s`} /><Fact label="最大尝试" value={formatNumber(estimate.max_attempts)} /><Fact label="验证超时" value={`${estimate.verification_timeout_seconds}s`} /></dl><p title={estimate.rationale.join('; ')}>{estimate.rationale.slice(-3).join(' · ')}</p><small>{estimate.model_invoked ? '调用了模型' : '未调用模型'} · 服务端规则 {estimate.bootstrap_version}</small></div>
}

function Metric({ icon: Icon, label, value, hint, onClick }: { icon: typeof Kanban; label: string; value: number; hint: string; onClick?: () => void }) {
  const content = <><div className="metric-icon"><Icon size={18} /></div><div><span>{label}</span><strong className="metric-number" title={value.toLocaleString()}>{value.toLocaleString()}</strong></div><small>{hint}</small></>
  return onClick
    ? <button type="button" className="metric-card" aria-label={`查看${label}详情`} onClick={onClick}>{content}</button>
    : <article className="metric-card">{content}</article>
}
function formatRatio(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(2)
}
function StatusDot({ ok, label }: { ok: boolean; label: string }) { return <span className={`status-dot ${ok ? 'ok' : 'off'}`}><Circle size={8} weight="fill" />{label}</span> }
function StatusPill({ status }: { status: TaskStatus }) { return <span className={`status-pill status-${status}`}>{statusNames[status]}</span> }
function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div><dt>{label}</dt><dd className={mono ? 'mono' : ''}>{value}</dd></div> }
function Field({ label, children }: { label: string; children: React.ReactNode }) { return <label className="field"><span>{label}</span>{children}</label> }
function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) { return <Field label={label}><input type="number" min="0" required value={value} onChange={(event) => onChange(Number(event.target.value))} /></Field> }
function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <label><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />{label}</label> }
function recordValue(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {} }
function value(item: Record<string, unknown>, keys: string[], fallback = '暂无（API 未提供）') {
  for (const key of keys) {
    const candidate = item[key]
    if (candidate !== null && candidate !== undefined && candidate !== '') return String(candidate)
  }
  return fallback
}
function listValue(value: unknown) { return Array.isArray(value) ? value.map(String).join(', ') : value ? String(value) : '' }
function summary(value: unknown, keys: string[]) {
  const item = recordValue(value)
  const parts = keys.flatMap((key) => item[key] === null || item[key] === undefined ? [] : [`${key}=${String(item[key])}`])
  return parts.join(' · ') || '暂无（API 未提供）'
}
function networkStatusSummary(network?: Record<string, unknown>) {
  if (!network) return '暂无（API 未提供）'
  if (typeof network.connectivity === 'string') return network.connectivity
  const zones = network.zones && typeof network.zones === 'object'
    ? network.zones as Record<string, Record<string, unknown>>
    : {}
  const domestic = zones.domestic?.state
  const overseas = zones.overseas?.state
  if (network.global_offline === true) return '全局断网'
  if (domestic === 'available' && overseas === 'available') return '在线 · 国内/海外可用'
  const available = [domestic === 'available' ? '国内' : '', overseas === 'available' ? '海外' : ''].filter(Boolean)
  const unavailable = [domestic === 'unavailable' ? '国内' : '', overseas === 'unavailable' ? '海外' : ''].filter(Boolean)
  if (unavailable.length) return `${available.length ? `${available.join('/')}可用 · ` : ''}${unavailable.join('/')}不可用`
  return '探测中'
}
function executionSummary(item: Record<string, unknown>) {
  const policy = recordValue(item.execution_policy)
  const deadline = value(policy, ['hard_deadline_seconds'], '—')
  return `${value(item, ['tokens_used'], '0')} Token consumed · hard ${deadline}s`
}
function verificationState(item: Record<string, unknown>) {
  if (item.last_evidence_hash) return `已验证 · ${String(item.last_evidence_hash).slice(0, 12)}`
  if (item.status === 'verifying') return '验证中'
  if (item.status === 'completed') return '已完成；验证证据字段暂无'
  if (item.status === 'terminal_failed' || item.status === 'needs_human') return `未通过 · ${value(item, ['last_error', 'blocked_reason'], '原因暂无')}`
  return '待验证'
}
function providerFullyReady(provider: Provider) {
  const readiness = provider.readiness
  if (!provider.enabled || !readiness) return false
  const cli = typeof readiness.cli_probe === 'string' ? readiness.cli_probe : readiness.cli_probe.status ?? ''
  const healthy = (state: string) => /^(available|healthy|ok|ready|success)$/i.test(state)
  return readiness.installed && healthy(cli) && readiness.session_resume_ready && healthy(readiness.recent_execution_health)
}
function messageOf(reason: unknown) { return reason instanceof Error ? reason.message : '发生未知错误' }
function formatNumber(value: number | null | undefined) { return value?.toLocaleString() ?? '—' }
function formatBytes(value: number | null) {
  if (value === null) return '大小未知'
  if (value < 1024) return `${value} B`
  if (value < 1_048_576) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1_048_576).toFixed(1)} MiB`
}
