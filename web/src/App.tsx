import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  ArrowsClockwise, CheckCircle, Circle, Clock, Coins, Copy, FileCode, FolderOpen,
  Gear, HardDrives, Kanban, ListChecks, MagicWand, Network, Pause, Play,
  Plus, Pulse, Robot, ShieldCheck, TerminalWindow, WarningCircle, X,
} from '@phosphor-icons/react'
import {
  api, AuditEntry, Convention, ConventionSuggestion, Goal, OutboxEvent, PermissionGrant,
  Project, Provider, RuntimeHealth, RuntimeSettings, SchedulerStatus, Task, TaskArtifact, TaskEvent,
  TaskSizingEstimate, TaskSizingInputs, TaskStatus, Usage, Worker,
} from './api'
import { startLiveRefresh } from './liveRefresh'

type Health = {
  status: string
  version: string
  database: { status: string; journal_mode: string; migration_count: number }
}

type View = 'board' | 'tasks' | 'projects' | 'workers' | 'providers' | 'usage' | 'audit' | 'settings'

const roleNames: Record<string, string> = {
  coordination: '协调 / PM',
  backend: '后端',
  frontend: '前端',
  ui: 'UI',
  devops_sre: 'DevOps / SRE',
  verification: '独立验证',
  fullstack: '全栈（遗留）',
  web3: 'Web3（遗留）',
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

const initialSizingInputs: TaskSizingInputs = {
  layers_touched: 1, components_touched: 3, estimated_files_changed: 4,
  has_migration: false, has_deploy: false, verification_commands_count: 3,
  estimated_verification_seconds: 180, external_dependencies_count: 0,
  risk_level: 'medium', independent_review_required: false,
  gate_artifact: true, gate_boundary: true, gate_verification: true, gate_dependency: true,
}

const initialTask = {
  title: '', objective: '', projectId: '', role: 'fullstack', provider: 'cursor',
  networkRequirement: 'none', executable: 'python3',
  argumentsJson: '["-c", "from pathlib import Path; Path(\'result.txt\').write_text(\'quality-pass\', encoding=\'utf-8\')"]',
  verifyPath: 'result.txt', verifyText: 'quality-pass',
  sizingInputs: initialSizingInputs,
}

export function App() {
  const [view, setView] = useState<View>('board')
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [goals, setGoals] = useState<Goal[]>([])
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
  const [selectedGoal, setSelectedGoal] = useState<Goal | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([])
  const [artifactError, setArtifactError] = useState<string | null>(null)
  const [selectedProject, setSelectedProject] = useState<string>('all')
  const [taskForm, setTaskForm] = useState(initialTask)
  const [goalForm, setGoalForm] = useState({
    title: '', objective: '', projectId: '', provider: 'cursor',
    networkRequirement: 'none',
    executable: 'python3',
    argumentsJson: '["-c", "from pathlib import Path; Path(\'result.txt\').write_text(\'quality-pass\', encoding=\'utf-8\')"]',
    verifyPath: 'result.txt', verifyText: 'quality-pass',
    sizingInputs: initialSizingInputs,
  })
  const [projectForm, setProjectForm] = useState({ name: '', path: '', hostPath: '' })
  const [showCreateTask, setShowCreateTask] = useState(false)
  const [showCreateGoal, setShowCreateGoal] = useState(false)
  const [taskEstimate, setTaskEstimate] = useState<TaskSizingEstimate | null>(null)
  const [goalEstimate, setGoalEstimate] = useState<TaskSizingEstimate | null>(null)
  const [estimatedSizingInputs, setEstimatedSizingInputs] = useState<TaskSizingInputs | null>(null)
  const [estimatedGoalSizingInputs, setEstimatedGoalSizingInputs] = useState<TaskSizingInputs | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refreshTasks = useCallback(async () => {
    const next = await api.tasks()
    setTasks(next)
    setSelectedTask((current) => next.find((task) => task.id === current?.id) ?? current)
  }, [])
  const refreshGoals = useCallback(async () => {
    const next = await api.goals()
    setGoals(next)
    setSelectedGoal((current) => next.find((goal) => goal.id === current?.id) ?? current)
  }, [])
  const refreshProjects = useCallback(async () => setProjects(await api.projects()), [])
  const refreshProviders = useCallback(async () => setProviders(await api.providers()), [])

  useEffect(() => {
    Promise.all([
      fetch('/health').then((response) => response.json() as Promise<Health>).then(setHealth),
      api.tasks().then(setTasks), api.goals().then(setGoals), api.projects().then(setProjects), api.providers().then(setProviders),
      api.settings().then(setSettings), api.schedulerStatus().then(setScheduler),
      api.runtimeHealth().then(setRuntimeHealth), api.usage().then(setUsage),
      api.audit().then(setAudit), api.outbox().then(setOutbox), api.permissions().then(setPermissions),
      api.convention('global', 'global').then(setConvention),
    ]).catch((reason: unknown) => setError(messageOf(reason)))
  }, [])

  useEffect(() => {
    const taskId = selectedTask?.id
    if (!taskId) return
    let current = true
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
    return startLiveRefresh(() => Promise.all([refreshTasks(), refreshGoals()]))
  }, [refreshTasks, refreshGoals])

  function selectTask(task: Task) {
    if (selectedTask?.id !== task.id) {
      setEvents([])
      setArtifacts([])
      setArtifactError(null)
    }
    setSelectedTask(task)
    setSelectedGoal(null)
    setView('tasks')
  }

  function selectGoal(goal: Goal) {
    setEvents([])
    setArtifacts([])
    setArtifactError(null)
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

  async function createGoal(event: FormEvent) {
    event.preventDefault()
    if (goalEstimate?.status !== 'estimated' || estimatedGoalSizingInputs !== goalForm.sizingInputs) return
    await action(async () => {
      const provider = providers.find((item) => item.name === goalForm.provider)
      const payload: Record<string, unknown> = {
        title: goalForm.title,
        objective: goalForm.objective,
        project_id: goalForm.projectId,
        provider: goalForm.provider,
        network_requirement: goalForm.networkRequirement,
        sizing_inputs: goalForm.sizingInputs,
        verification: [
          { kind: 'exit_code', expected: 0 },
          { kind: 'file_exists', path: goalForm.verifyPath },
          { kind: 'file_contains', path: goalForm.verifyPath, contains: goalForm.verifyText },
        ],
      }
      if (provider?.adapter === 'generic-command' || goalForm.provider === 'generic-command') {
        const args = JSON.parse(goalForm.argumentsJson) as unknown
        if (!Array.isArray(args) || args.some((value) => typeof value !== 'string')) throw new Error('参数必须是字符串 JSON 数组')
        payload.command = { argv: [goalForm.executable, ...args] }
      }
      let created: Goal
      try {
        created = await api.createGoal(payload)
      } finally {
        await refreshProviders()
      }
      await Promise.all([refreshGoals(), refreshTasks()])
      selectGoal(created)
      setShowCreateGoal(false)
    }, '目标已拆分并进入自动推进')
  }

  async function estimateTask() {
    const sizingInputs = taskForm.sizingInputs
    setBusy(true); setError(null); setNotice(null); setTaskEstimate(null); setEstimatedSizingInputs(null)
    try { setTaskEstimate(await api.estimateTask(sizingInputs)); setEstimatedSizingInputs(sizingInputs) } catch (reason) { setError(messageOf(reason)) } finally { setBusy(false) }
  }

  async function estimateGoal() {
    const sizingInputs = goalForm.sizingInputs
    setBusy(true); setError(null); setNotice(null); setGoalEstimate(null); setEstimatedGoalSizingInputs(null)
    try { setGoalEstimate(await api.estimateTask(sizingInputs)); setEstimatedGoalSizingInputs(sizingInputs) } catch (reason) { setError(messageOf(reason)) } finally { setBusy(false) }
  }

  function setSizingInputs(sizingInputs: TaskSizingInputs) {
    setTaskForm((current) => ({ ...current, sizingInputs }))
    setTaskEstimate(null)
    setEstimatedSizingInputs(null)
  }

  function setGoalSizingInputs(sizingInputs: TaskSizingInputs) {
    setGoalForm((current) => ({ ...current, sizingInputs }))
    setGoalEstimate(null)
    setEstimatedGoalSizingInputs(null)
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
          <div><span>项目范围</span><select value={selectedProject} onChange={(event) => setSelectedProject(event.target.value)}><option value="all">全部项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></div>
          <div className="context-actions">
            <button className="ghost" onClick={() => action(async () => { await Promise.all([refreshTasks(), refreshGoals(), refreshProjects(), refreshProviders()]) }, '状态已刷新')}><ArrowsClockwise size={16} />刷新</button>
            <button className="ghost" onClick={() => { setTaskEstimate(null); setEstimatedSizingInputs(null); setShowCreateTask(true) }}>诊断任务</button>
            <button className="primary" onClick={() => { setGoalEstimate(null); setEstimatedGoalSizingInputs(null); setShowCreateGoal(true) }}><Plus size={16} weight="bold" />提交目标</button>
          </div>
        </div>

        {error && <div className="banner error"><WarningCircle size={18} weight="fill" /><span>{error}</span><button aria-label="关闭错误" onClick={() => setError(null)}><X size={16} /></button></div>}
        {notice && <div className="banner success"><CheckCircle size={18} weight="fill" /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
        {outbox.filter((event) => event.event_type === 'task.needs_human' && !event.delivered_at).length > 0 && <div className="banner warning"><WarningCircle size={18} weight="fill" />存在需要人工判断的任务，自动调度已对这些任务停手。</div>}

        {view === 'board' && <Board tasks={visibleTasks} goals={goals.filter((goal) => selectedProject === 'all' || goal.project_id === selectedProject)} projects={projects} workers={workers.map((item) => item.worker)} providers={providers} usage={usage} onNavigate={setView} onSelect={selectTask} onSelectGoal={selectGoal} />}
        {view === 'tasks' && <TasksView tasks={visibleTasks} goals={goals.filter((goal) => selectedProject === 'all' || goal.project_id === selectedProject)} workers={workers.map((item) => item.worker)} selected={selectedTask} selectedGoal={selectedGoal} events={events} artifacts={artifacts} artifactError={artifactError} busy={busy} onSelect={selectTask} onSelectGoal={selectGoal} onDrive={driveTask} onControl={controlTask} onOpenArtifact={openArtifact} onCopyArtifact={copyArtifactPath} />}
        {view === 'projects' && <ProjectsView projects={projects} form={projectForm} setForm={setProjectForm} onCreate={createProject} onRelease={(project) => action(async () => { await api.releaseProject(project.id); await refreshProjects() }, '项目已完成并释放 Worker')} busy={busy} />}
        {view === 'workers' && <WorkersView items={workers.filter(({ project }) => selectedProject === 'all' || project.id === selectedProject)} providers={providers} busy={busy} onRebind={(worker, provider) => action(async () => { await api.rebindWorker(worker.id, provider); await refreshProjects() }, 'Worker 已轮转并重新绑定')} />}
        {view === 'providers' && <ProvidersView providers={providers} busy={busy} onProbe={probeProvider} onToggle={toggleProvider} />}
        {view === 'usage' && <UsageView usage={usage} projects={projects} tasks={tasks} />}
        {view === 'audit' && <AuditView audit={audit} permissions={permissions} onRefresh={() => api.audit().then(setAudit)} />}
        {view === 'settings' && <SettingsView settings={settings} setSettings={setSettings} scheduler={scheduler} convention={convention} setConvention={setConvention} suggestion={suggestion} setSuggestion={setSuggestion} projects={projects} tasks={tasks} providers={providers} refineProvider={effectiveRefineProvider} setRefineProvider={setRefineProvider} busy={busy} onSaveSettings={saveSettings} onTick={() => action(async () => { await api.schedulerTick(); setScheduler(await api.schedulerStatus()); await Promise.all([refreshTasks(), refreshGoals()]) }, 'Tick 已完成')} onLoadConvention={loadConvention} onSaveConvention={saveConvention} onRefine={refineConvention} />}
      </main>

      {showCreateGoal && <GoalDrawer form={goalForm} setForm={setGoalForm} setSizingInputs={setGoalSizingInputs} estimate={estimatedGoalSizingInputs === goalForm.sizingInputs ? goalEstimate : null} projects={projects} providers={providers} busy={busy} onClose={() => setShowCreateGoal(false)} onEstimate={estimateGoal} onSubmit={createGoal} />}
      {showCreateTask && <TaskDrawer form={taskForm} setForm={setTaskForm} setSizingInputs={setSizingInputs} estimate={estimatedSizingInputs === taskForm.sizingInputs ? taskEstimate : null} projects={projects} providers={providers} busy={busy} onClose={() => setShowCreateTask(false)} onEstimate={estimateTask} onSubmit={createTask} />}
    </div>
  )
}

function Board({ tasks, goals, projects, workers, providers, usage, onNavigate, onSelect, onSelectGoal }: { tasks: Task[]; goals: Goal[]; projects: Project[]; workers: Worker[]; providers: Provider[]; usage: Usage | null; onNavigate: (view: View) => void; onSelect: (task: Task) => void; onSelectGoal: (goal: Goal) => void }) {
  const columns: { title: string; statuses: TaskStatus[]; tone: string }[] = [
    { title: '待执行', statuses: ['ready', 'paused'], tone: 'blue' },
    { title: '执行中', statuses: ['running', 'stopping'], tone: 'violet' },
    { title: '质量验证', statuses: ['verifying'], tone: 'yellow' },
    { title: '已终态', statuses: ['completed', 'terminal_failed', 'cancelled', 'needs_human'], tone: 'green' },
  ]
  return <>
    <section className="metrics-strip">
      <Metric icon={FolderOpen} label="活跃项目" value={projects.filter((p) => p.status === 'active').length} hint="可并行" onClick={() => onNavigate('projects')} />
      <Metric icon={Robot} label="在线 Worker" value={workers.filter((w) => w.status !== 'released').length} hint={`${workers.filter((w) => w.status === 'busy').length} 忙碌`} onClick={() => onNavigate('workers')} />
      <Metric icon={TerminalWindow} label="可用 Provider" value={providers.filter(providerFullyReady).length} hint={`${providers.filter((p) => p.enabled).length} 已启用`} onClick={() => onNavigate('providers')} />
      <Metric icon={Coins} label="今日 Token" value={usage?.total_tokens ?? 0} hint="控制链 0" onClick={() => onNavigate('usage')} />
    </section>
    <section className="panel board-panel"><div className="panel-heading"><div><span className="kicker">目标主流程</span><h1>提交目标 → 计划 → 子项 → 验证</h1></div><span className="muted">结构化计划 · 同角色会话续接 · 独立验证完成</span></div>
      <div className="goal-strip">{goals.length ? goals.map((goal) => <button className="task-card" key={goal.id} onClick={() => onSelectGoal(goal)}><div><StatusPill status={(goal.status === 'running' ? 'ready' : goal.status === 'completed' ? 'completed' : goal.status === 'needs_human' ? 'needs_human' : 'terminal_failed') as TaskStatus} /><small>{goal.provider}</small></div><strong>{goal.title}</strong><p>{goal.objective}</p><footer><span>{goal.work_items.length} 工作项</span><span>{goal.status}</span></footer></button>) : <div className="empty-column">提交目标后生成结构化/确定性计划；模型 PM 尚未实现。</div>}</div>
    </section>
    <section className="panel board-panel"><div className="panel-heading"><div><span className="kicker">全局任务流</span><h1>任务看板</h1></div><span className="muted">同一角色单会话 · 租约隔离 · 证据完成</span></div>
      <div className="kanban-grid">{columns.map((column) => { const items = tasks.filter((task) => column.statuses.includes(task.status)); return <div className="kanban-column" key={column.title}><div className={`column-title ${column.tone}`}><span>{column.title}</span><b>{items.length}</b></div><div className="column-body">{items.length ? items.map((task) => <button className="task-card" key={task.id} onClick={() => onSelect(task)}><div><StatusPill status={task.status} /><small>{task.provider}{task.work_item_kind ? ` · ${task.work_item_kind}` : ''}</small></div><strong>{task.title}</strong><p>{task.objective}</p><footer><span>{task.attempts_used}/{task.max_attempts} 次</span><span>{task.tokens_used} Token</span></footer></button>) : <div className="empty-column">当前没有任务</div>}</div></div> })}</div>
    </section>
  </>
}

function TasksView({ tasks, goals, workers, selected, selectedGoal, events, artifacts, artifactError, busy, onSelect, onSelectGoal, onDrive, onControl, onOpenArtifact, onCopyArtifact }: { tasks: Task[]; goals: Goal[]; workers: Worker[]; selected: Task | null; selectedGoal: Goal | null; events: TaskEvent[]; artifacts: TaskArtifact[]; artifactError: string | null; busy: boolean; onSelect: (task: Task) => void; onSelectGoal: (goal: Goal) => void; onDrive: (task: Task) => void; onControl: (task: Task, action: 'pause' | 'resume' | 'cancel' | 'needs_human') => void; onOpenArtifact: (task: Task, artifact: TaskArtifact, target: 'finder' | 'cursor') => void; onCopyArtifact: (artifact: TaskArtifact) => void }) {
  return <div className="split-layout"><section className="panel list-panel"><div className="panel-heading"><div><span className="kicker">目标 / 任务</span><h1>{goals.length} 目标 · {tasks.length} 工作项</h1></div></div>
    <div className="dense-list">
      {goals.map((goal) => <button key={goal.id} className={selectedGoal?.id === goal.id ? 'selected' : ''} onClick={() => onSelectGoal(goal)}><span className="list-icon"><Kanban size={18} /></span><span><strong>{goal.title}</strong><small>{goal.provider} · {goal.status} · {goal.work_items.length} 项</small></span></button>)}
      {tasks.map((task) => <button key={task.id} className={selected?.id === task.id ? 'selected' : ''} onClick={() => onSelect(task)}><span className="list-icon"><ListChecks size={18} /></span><span><strong>{task.title}</strong><small>{task.provider} · {statusNames[task.status]}{task.work_item_kind ? ` · ${task.work_item_kind}` : ''}</small></span><StatusPill status={task.status} /></button>)}
    </div></section>
    <section className="panel detail-panel">{selectedGoal && !selected ? <GoalDetail goal={selectedGoal} tasks={tasks} workers={workers} /> : selected ? <><div className="panel-heading"><div><span className="kicker">{selected.id}</span><h1>{selected.title}</h1></div><StatusPill status={selected.status} /></div><p className="objective">{selected.objective}</p><div className="detail-actions">{selected.status === 'ready' && selected.work_item_kind !== 'coordination' && <button className="primary" disabled={busy} onClick={() => onDrive(selected)}><Play size={16} weight="fill" />立即驱动</button>}{selected.status === 'ready' && <button disabled={busy} onClick={() => onControl(selected, 'pause')}><Pause size={16} />暂停</button>}{selected.status === 'paused' && <button disabled={busy} onClick={() => onControl(selected, 'resume')}><Play size={16} />恢复</button>}{!['completed', 'terminal_failed', 'cancelled'].includes(selected.status) && <button className="danger" disabled={busy} onClick={() => onControl(selected, 'cancel')}><X size={16} />取消</button>}</div><div className="facts"><Fact label="Provider" value={selected.provider} /><Fact label="验证机制" value="确定性验证" /><Fact label="验证状态" value={verificationState(selected as unknown as Record<string, unknown>)} /><Fact label="工作项" value={selected.work_item_kind ?? 'manual'} /><Fact label="依赖" value={(selected.depends_on ?? []).join(', ') || '无'} mono /><Fact label="阻塞原因" value={selected.blocked_reason ?? '无'} /><Fact label="Worker" value={selected.worker_id ?? '尚未领取'} mono /><Fact label="资源锁" value={selected.resource_key ?? '项目级默认锁'} mono /><Fact label="尝试 / 消费" value={`${selected.attempts_used}/${selected.max_attempts} · ${selected.tokens_used} Token`} /><TaskRuntimeFacts item={selected as unknown as Record<string, unknown>} /></div><section className="artifacts"><div className="section-heading"><div><span className="kicker">主机项目目录</span><h2>任务产物</h2></div><span>{artifacts.filter((item) => item.exists).length} 个已定位</span></div>{artifactError ? <p className="artifact-error">Host Bridge 暂时无法定位产物：{artifactError}</p> : artifacts.length ? artifacts.map((artifact) => <article className={artifact.exists ? '' : 'missing'} key={artifact.relative_path}><div className="artifact-icon"><FileCode size={19} /></div><div className="artifact-main"><strong>{artifact.relative_path}</strong><code title={artifact.host_path}>{artifact.host_path}</code><small>{artifact.exists ? `${formatBytes(artifact.bytes)} · SHA-256 ${artifact.sha256?.slice(0, 12) ?? '文件过大未哈希'}…` : '尚未在主机项目目录生成'}</small></div><div className="artifact-actions"><button disabled={busy || !artifact.exists} onClick={() => onCopyArtifact(artifact)}><Copy size={15} />复制路径</button><button disabled={busy || !artifact.actions.includes('finder')} onClick={() => onOpenArtifact(selected, artifact, 'finder')}><FolderOpen size={15} />Finder</button>{artifact.actions.includes('cursor') && <button className="primary" disabled={busy} onClick={() => onOpenArtifact(selected, artifact, 'cursor')}><TerminalWindow size={15} />Cursor 打开</button>}</div></article>) : <p className="artifact-empty">该任务没有声明文件产物。容器不会保存项目报告或代码。</p>}</section><div className="timeline"><h2>状态事件</h2>{events.map((event) => <div key={event.sequence}><span>{event.sequence}</span><strong>{event.event_type}</strong><small>{event.created_at}</small></div>)}</div></> : <div className="empty-state"><ListChecks size={38} /><h2>选择一个目标或任务</h2><p>查看结构化计划、角色依赖、会话 generation、输出元数据与验证状态。</p></div>}</section></div>
}

function GoalDetail({ goal, tasks, workers }: { goal: Goal; tasks: Task[]; workers: Worker[] }) {
  const modelPmImplemented = goal.plan.model_pm_implemented === true
  return <><div className="panel-heading"><div><span className="kicker">{goal.id}</span><h1>{goal.title}</h1></div><span className="status-pill">{goal.status}</span></div><p className="objective">{goal.objective}</p><div className="facts"><Fact label="计划机制" value={modelPmImplemented ? '模型 PM 计划' : '结构化/确定性计划，模型 PM 尚未实现'} /><Fact label="Provider" value={goal.provider} /><Fact label="父任务" value={goal.parent_task_id ?? '—'} mono /><Fact label="工作项" value={String(goal.work_items.length)} /><Fact label="Goal sizing" value={summary(goal.sizing_inputs, ['size_class', 'status', 'risk_level'])} /><Fact label="Goal 状态" value={goal.status} /></div><section className="work-items"><div className="section-heading"><div><span className="kicker">计划 → 子项 → 验证</span><h2>工作项运行态</h2></div><span>只显示元数据，不读取 stdout/stderr</span></div>{goal.work_items.map((item) => {
    const task = tasks.find((candidate) => candidate.id === String(item.id))
    const detail = { ...item, ...(task ?? {}) } as Record<string, unknown>
    const worker = workers.find((candidate) => candidate.id === value(detail, ['worker_id']))
    return <article className="work-item-card" key={String(item.id)}><header><span>{String(detail.ordinal ?? 'P')}</span><div><strong>{String(detail.title ?? detail.id)}</strong><small>{String(detail.status ?? 'unknown')}</small></div></header><dl className="facts"><Fact label="角色 / Provider" value={`${roleNames[value(detail, ['work_item_kind', 'role'])] ?? value(detail, ['work_item_kind', 'role'])} · ${value(detail, ['provider'])}`} /><Fact label="依赖" value={listValue(detail.depends_on) || '无'} mono /><Fact label="阻塞原因" value={value(detail, ['blocked_reason'], '无')} /><Fact label="Worker session" value={worker?.external_session_id ?? worker?.session_id ?? '暂无（API 未提供）'} mono /><Fact label="Generation" value={worker ? String(worker.session_generation) : '暂无（API 未提供）'} /><Fact label="Rotation reason" value={worker?.rotation_reason ?? '暂无（API 未提供）'} /><Fact label="Last context pressure" value={value(detail, ['last_context_pressure'], worker ? String(worker.last_context_pressure_tokens) : '暂无（API 未提供）')} /><Fact label="Pressure trigger" value={value(detail, ['last_context_pressure_reason'], worker?.last_context_pressure_reason ?? '暂无（API 未提供）')} /><Fact label="Sizing" value={summary(detail.sizing, ['size_class', 'status'])} /><Fact label="Execution" value={executionSummary(detail)} /><Fact label="Attempt / progress" value={`${value(detail, ['attempts_used'], '0')}/${value(detail, ['max_attempts'], '—')} · ${value(detail, ['status'], 'unknown')}`} /><Fact label="Verification" value={verificationState(detail)} /><TaskRuntimeFacts item={detail} /></dl></article>
  })}</section></>
}

function TaskRuntimeFacts({ item }: { item: Record<string, unknown> }) {
  const handoff = recordValue(item.handoff)
  const input = value(item, ['input_tokens'], value(handoff, ['input_tokens'], '不可见'))
  const cachedInput = value(item, ['cached_input_tokens'], value(handoff, ['cached_input_tokens'], '不可见'))
  const uncachedInput = value(item, ['uncached_input_tokens'], value(handoff, ['uncached_input_tokens'], '不可见'))
  const output = value(item, ['output_tokens'], value(handoff, ['output_tokens'], '不可见'))
  const total = value(item, ['total_tokens', 'tokens_used'], value(handoff, ['total_tokens'], '不可见'))
  const outputRef = value(item, ['output_ref'], value(handoff, ['output_ref'], '暂无（API 未提供）'))
  const segments = value(item, ['output_segments', 'segments'], value(handoff, ['output_segments', 'segments'], '暂无（API 未提供）'))
  const bytes = value(item, ['output_bytes', 'bytes'], value(handoff, ['output_bytes', 'bytes'], '暂无（API 未提供）'))
  const offset = value(item, ['output_offset', 'offset'], value(handoff, ['output_offset', 'offset'], '暂无（API 未提供）'))
  return <><Fact label="Input / Cached carry-in / Uncached input / Output / Total" value={`${input} / ${cachedInput} / ${uncachedInput} / ${output} / ${total}`} /><Fact label="Cached 计入 Total" value="是，已包含在 Input 中，不重复相加" /><Fact label="Attribution" value={`${value(item, ['attribution_granularity'], 'turn')} / ${value(item, ['value_classification'], 'unknown')}（Uncached 不等于新工作或有价值）`} /><Fact label="Token control" value="仅计量；不参与准入、调度、熔断或终态" /><Fact label="Output ref" value={outputRef} mono /><Fact label="Segments / bytes / offset" value={`${segments} / ${bytes} / ${offset}`} /></>
}

function ProjectsView({ projects, form, setForm, onCreate, onRelease, busy }: { projects: Project[]; form: { name: string; path: string; hostPath: string }; setForm: (value: { name: string; path: string; hostPath: string }) => void; onCreate: (event: FormEvent) => void; onRelease: (project: Project) => void; busy: boolean }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">多项目并行</span><h1>项目注册表</h1></div></div><div className="project-grid">{projects.map((project) => <article className="project-card" key={project.id}><div className="project-top"><FolderOpen size={20} /><StatusDot ok={project.status === 'active'} label={project.status === 'active' ? '进行中' : '已完成'} /></div><h2>{project.name}</h2><code>{project.path}</code>{project.host_path && <code className="muted-code">产物源目录 · {project.host_path}</code>}<div className="mini-stats"><span>{project.roles.length} 角色</span><span>{project.workers.length} Worker</span></div>{project.status === 'active' && <button disabled={busy} onClick={() => onRelease(project)}>完成并释放 Worker</button>}</article>)}</div></section>
    <form className="panel form-panel" onSubmit={onCreate}><div className="panel-heading"><div><span className="kicker">项目 → 角色 → CLI 会话</span><h1>注册项目</h1></div></div><Field label="项目名"><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：支付网关" /></Field><Field label="控制面挂载路径"><input required value={form.path} onChange={(event) => setForm({ ...form, path: event.target.value })} placeholder="/projects/payment" /></Field><Field label="本机项目目录（产物源目录）"><input value={form.hostPath} onChange={(event) => setForm({ ...form, hostPath: event.target.value })} placeholder="/Users/name/work/payment" /></Field><p className="form-help">Worker 只通过 Host Bridge 在本机项目目录工作；报告、代码和其他产物不会复制进容器。控制面只保存任务状态与路径索引。</p><button className="primary" disabled={busy}><Plus size={16} />注册项目</button></form></div>
}

function WorkersView({ items, providers, busy, onRebind }: { items: { project: Project; worker: Worker }[]; providers: Provider[]; busy: boolean; onRebind: (worker: Worker, provider: string) => void }) {
  return <section className="panel"><div className="panel-heading"><div><span className="kicker">项目 · 角色 · CLI 会话</span><h1>Worker 状态</h1></div><span className="muted">项目完成后自动归档并释放</span></div><div className="worker-table"><div className="table-head"><span>项目 / 角色</span><span>Provider</span><span>会话</span><span>状态</span><span>操作</span></div>{items.length ? items.map(({ project, worker }) => <div className="table-row" key={worker.id}><div><strong>{project.name}</strong><small>{roleNames[worker.role] ?? worker.role}</small></div><div><code>{worker.provider}</code><small>第 {worker.session_generation} 代 · rotation {worker.rotation_reason ?? '无'}</small></div><div><code>{worker.external_session_id ?? '等待 CLI 建立'}</code><small>Input {worker.last_input_tokens} · Cached {worker.last_cached_input_tokens} · Uncached {worker.last_uncached_input_tokens} · Output {worker.last_output_tokens}</small><small>Attribution {worker.last_attribution_granularity} / {worker.last_value_classification} · Token 仅计量，不参与调度或终态</small></div><div><StatusDot ok={worker.status === 'idle'} label={worker.status === 'idle' ? '空闲' : worker.status === 'busy' ? '工作中' : '已释放'} />{worker.last_error && <small className="danger-text">{worker.last_error}</small>}</div><div><select disabled={busy || worker.status !== 'idle'} value={worker.provider} onChange={(event) => onRebind(worker, event.target.value)}>{providers.filter((provider) => provider.enabled && provider.transport === 'host-bridge').map((provider) => <option key={provider.name} value={provider.name}>{provider.display_name}</option>)}</select></div></div>) : <div className="empty-state compact"><Robot size={34} /><h2>暂无 Worker</h2><p>首个项目任务被领取时创建，并持续工作到项目完成。</p></div>}</div></section>
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

function UsageView({ usage, projects, tasks }: { usage: Usage | null; projects: Project[]; tasks: Task[] }) {
  return <div className="settings-layout"><section className="metrics-strip"><Metric icon={Coins} label="Total Token" value={usage?.total_tokens ?? 0} hint="Input + Output" /><Metric icon={Network} label="Input" value={usage?.input_tokens ?? 0} hint="包含 Cached" /><Metric icon={Clock} label="Cached-input" value={usage?.cached_input_tokens ?? 0} hint="Input 子集，不重复相加" /><Metric icon={CheckCircle} label="Output" value={usage?.output_tokens ?? 0} hint="后端已计量" /></section><section className="panel"><div className="panel-heading"><div><span className="kicker">Token 账本</span><h1>消费明细</h1></div><span className="muted">Token 只计量，不参与任务准入、调度、熔断或终态</span></div><div className="usage-columns"><div><h2>按项目</h2>{usage?.projects.length ? usage.projects.map((item) => <div className="usage-row" key={item.project_id ?? 'none'}><span>{projects.find((project) => project.id === item.project_id)?.name ?? '未绑定项目'}<small>Input {item.input_tokens} · Cached {item.cached_input_tokens} · Uncached {item.uncached_input_tokens} · Output {item.output_tokens}</small></span><strong>{item.tokens}</strong></div>) : <p className="muted">尚无模型消费。</p>}</div><div><h2>按任务</h2>{usage?.tasks.length ? usage.tasks.map((item) => <div className="usage-row" key={item.task_id}><span>{tasks.find((task) => task.id === item.task_id)?.title ?? item.task_id}<small>Input {item.input_tokens} · Cached {item.cached_input_tokens} · Uncached {item.uncached_input_tokens} · Output {item.output_tokens}</small></span><strong>{item.tokens}</strong></div>) : <p className="muted">调度与探测不会制造账单。</p>}</div></div>{usage?.calls.length ? <div><h2>调用归因</h2>{usage.calls.map((call) => <div className="usage-row" key={call.call_id}><span>{call.task_id ?? call.call_id}<small>{call.call_kind} · Worker {call.worker_id ?? '—'} · generation {call.session_generation ?? '—'} · {call.attribution_granularity} / {call.value_classification}</small></span><strong>{call.input_tokens} / {call.cached_input_tokens} / {call.uncached_input_tokens} / {call.output_tokens}</strong></div>)}</div> : null}<p className="muted">Uncached input 仅表示缓存未命中，不等于新工作或有价值内容。</p></section></div>
}

function AuditView({ audit, permissions, onRefresh }: { audit: AuditEntry[]; permissions: PermissionGrant[]; onRefresh: () => void }) {
  return <div className="settings-layout"><section className="panel"><div className="panel-heading"><div><span className="kicker">本地不可变审计</span><h1>变更记录</h1></div><button onClick={onRefresh}><ArrowsClockwise size={16} />刷新</button></div><div className="audit-table"><div className="table-head"><span>#</span><span>方法</span><span>路径</span><span>状态</span><span>时间</span></div>{audit.map((entry) => <div className="table-row" key={entry.sequence}><span>{entry.sequence}</span><strong>{entry.method}</strong><code>{entry.path}</code><span>{entry.status_code}</span><small>{entry.created_at}</small></div>)}</div></section><section className="panel"><div className="panel-heading"><div><span className="kicker">权限边界</span><h1>权限决策</h1></div></div>{permissions.map((permission) => <div className="usage-row" key={permission.id}><span>{permission.capability} · {permission.resource}</span><strong>{permission.decision}</strong></div>)}</section></div>
}

function SettingsView(props: { settings: RuntimeSettings | null; setSettings: (value: RuntimeSettings) => void; scheduler: SchedulerStatus | null; convention: Convention | null; setConvention: (value: Convention) => void; suggestion: ConventionSuggestion | null; setSuggestion: (value: ConventionSuggestion | null) => void; projects: Project[]; tasks: Task[]; providers: Provider[]; refineProvider: string; setRefineProvider: (value: string) => void; busy: boolean; onSaveSettings: (event: FormEvent) => void; onTick: () => void; onLoadConvention: (scope: Convention['scope'], id: string) => void; onSaveConvention: (event: FormEvent) => void; onRefine: () => void }) {
  const { settings, setSettings, scheduler, convention, setConvention, suggestion, setSuggestion, projects, tasks, providers, refineProvider, setRefineProvider, busy, onSaveSettings, onTick, onLoadConvention, onSaveConvention, onRefine } = props
  return <div className="settings-layout"><section className="panel cron-panel"><div className="panel-heading"><div><span className="kicker">单一全局 Crontab</span><h1>无人值守调度</h1></div><StatusDot ok={Boolean(scheduler?.engine.active)} label={scheduler?.engine.active ? '运行中' : '未运行'} /></div><div className="facts"><Fact label="引擎" value={scheduler ? `${scheduler.engine.managed_by} · ${scheduler.engine.backend}` : '检测中'} /><Fact label="下次执行" value={scheduler?.schedule.next_run_at ?? '尚未计算'} /><Fact label="Fencing" value={String(scheduler?.runtime.fencing_token ?? 0)} /><Fact label="最近 Tick" value={scheduler?.runtime.last_tick_at ?? '尚未执行'} /></div><button className="primary" disabled={busy} onClick={onTick}><Play size={16} />立即运行 Tick</button></section>
    {settings && <form className="panel form-panel" onSubmit={onSaveSettings}><div className="panel-heading"><div><span className="kicker">设置修订 {settings.revision}</span><h1>Crontab 与防循环</h1></div></div><div className="form-grid"><Field label="Cron 表达式"><input value={settings.values.cron_expression} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_expression: event.target.value } })} /></Field><Field label="时区"><input value={settings.values.cron_timezone} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_timezone: event.target.value } })} /></Field><Field label="错过执行"><select value={settings.values.cron_misfire_policy} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_misfire_policy: event.target.value as 'catch_up_once' | 'skip' } })}><option value="catch_up_once">恢复后只补跑一次</option><option value="skip">跳过</option></select></Field><NumberField label="最大并行 Worker" value={settings.values.max_parallel_workers} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_parallel_workers: value } })} /><NumberField label="同类失败熔断" value={settings.values.max_same_failure} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_same_failure: value } })} /><NumberField label="无进展熔断" value={settings.values.max_no_progress} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, max_no_progress: value } })} /><NumberField label="Context 最大字节" value={settings.values.context_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, context_max_bytes: value } })} /><NumberField label="文件轮转字节" value={settings.values.rotation_max_bytes} onChange={(value) => setSettings({ ...settings, values: { ...settings.values, rotation_max_bytes: value } })} /></div><label className="toggle-row"><input type="checkbox" checked={settings.values.cron_enabled} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, cron_enabled: event.target.checked } })} /><span>启用容器内 Crontab</span></label><label className="toggle-row"><input type="checkbox" checked={settings.values.auto_dispatch} onChange={(event) => setSettings({ ...settings, values: { ...settings.values, auto_dispatch: event.target.checked } })} /><span>自动派发待执行任务</span></label><button className="primary" disabled={busy}>保存设置</button></form>}
    {convention && <form className="panel convention-panel" onSubmit={onSaveConvention}><div className="panel-heading"><div><span className="kicker">全局 · 项目 · Task</span><h1>Convention 编辑器</h1></div><span className="revision">修订 {convention.revision}</span></div><div className="form-grid two"><Field label="作用域"><select value={convention.scope} onChange={(event) => { const scope = event.target.value as Convention['scope']; const id = scope === 'global' ? 'global' : scope === 'project' ? projects[0]?.id ?? '' : tasks[0]?.id ?? ''; onLoadConvention(scope, id) }}><option value="global">全局</option><option value="project">项目</option><option value="task">Task</option></select></Field><Field label="目标"><select disabled={convention.scope === 'global'} value={convention.scope_id} onChange={(event) => onLoadConvention(convention.scope, event.target.value)}>{convention.scope === 'global' ? <option value="global">全局</option> : convention.scope === 'project' ? projects.map((project) => <option value={project.id} key={project.id}>{project.name}</option>) : tasks.map((task) => <option value={task.id} key={task.id}>{task.title}</option>)}</select></Field></div><div className="editor-grid"><div><label>当前 Convention</label><textarea value={convention.content} onChange={(event) => setConvention({ ...convention, content: event.target.value })} placeholder="写下质量门、权限边界和必须验证的完成条件。" /></div><div><label>{suggestion ? `${suggestion.provider} 精炼建议` : 'Worker 精炼建议'}</label><textarea value={suggestion?.suggestion ?? ''} readOnly placeholder="点击“模型精炼”后在这里审阅建议；不会自动覆盖原文。" /></div></div><div className="detail-actions"><select className="inline-select" value={refineProvider} onChange={(event) => setRefineProvider(event.target.value)}>{providers.filter((provider) => provider.enabled && provider.capabilities.includes('refine_convention')).map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}{provider.status === 'available' ? '' : '（当前不可用）'}</option>)}</select><button type="button" disabled={busy || !convention.content.trim()} onClick={onRefine}><MagicWand size={16} />模型精炼（计 Token）</button>{suggestion && <button type="button" onClick={() => { setConvention({ ...convention, content: suggestion.suggestion }); setSuggestion(null) }}><CheckCircle size={16} />采用建议</button>}<button className="primary" disabled={busy}>保存 Convention</button></div><p className="form-help">精炼是明确的模型动作，会记录 Token；保存仍需人工确认。Crontab、探测和状态扫描不会调用模型。</p></form>}
  </div>
}

function GoalDrawer(props: {
  form: {
    title: string; objective: string; projectId: string; provider: string; networkRequirement: string
    executable: string; argumentsJson: string; verifyPath: string; verifyText: string; sizingInputs: TaskSizingInputs
  }
  setForm: (value: {
    title: string; objective: string; projectId: string; provider: string; networkRequirement: string
    executable: string; argumentsJson: string; verifyPath: string; verifyText: string; sizingInputs: TaskSizingInputs
  }) => void
  setSizingInputs: (value: TaskSizingInputs) => void
  estimate: TaskSizingEstimate | null
  projects: Project[]
  providers: Provider[]
  busy: boolean
  onClose: () => void
  onEstimate: () => void
  onSubmit: (event: FormEvent) => void
}) {
  const { form, setForm, setSizingInputs, estimate, projects, providers, busy, onClose, onEstimate, onSubmit } = props
  const sizing = form.sizingInputs
  const setSizing = <K extends keyof TaskSizingInputs>(key: K, value: TaskSizingInputs[K]) => setSizingInputs({ ...sizing, [key]: value })
  const ready = estimate?.status === 'estimated'
  const provider = providers.find((item) => item.name === form.provider)
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-head"><div><span className="kicker">目标主流程唯一入口</span><h1>提交目标</h1></div><button aria-label="关闭" onClick={onClose}><X size={18} /></button></div><form onSubmit={onSubmit}><Field label="标题"><input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="一个可验收的业务目标" /></Field><Field label="目标"><textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="系统会按角色拆成有序工作项并自动推进" /></Field><div className="form-grid two"><Field label="项目"><select required value={form.projectId} onChange={(event) => setForm({ ...form, projectId: event.target.value })}><option value="">选择项目</option>{projects.filter((p) => p.status === 'active').map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></Field><Field label="Provider"><select value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value })}>{providers.filter((item) => item.enabled).map((item) => <option value={item.name} key={item.name}>{item.display_name}</option>)}</select></Field></div><p className="form-help">当前生成结构化/确定性计划，模型 PM 尚未实现；各角色复用 project+role 稳定会话。Provider 总状态：{provider?.status ?? '待探测'}。</p><div className="form-grid two"><Field label="验证文件 / 产物路径"><input value={form.verifyPath} onChange={(event) => setForm({ ...form, verifyPath: event.target.value })} /></Field><Field label="必须包含"><input value={form.verifyText} onChange={(event) => setForm({ ...form, verifyText: event.target.value })} /></Field></div><section className="preflight"><div className="preflight-heading"><div><span className="kicker">入队前必做</span><h2>0 Token 规则评估</h2></div><span>不调用模型</span></div><div className="form-grid two"><NumberField label="涉及层数" value={sizing.layers_touched} onChange={(value) => setSizing('layers_touched', value)} /><NumberField label="组件数" value={sizing.components_touched} onChange={(value) => setSizing('components_touched', value)} /><NumberField label="预计文件数" value={sizing.estimated_files_changed} onChange={(value) => setSizing('estimated_files_changed', value)} /><Field label="风险"><select value={sizing.risk_level} onChange={(event) => setSizing('risk_level', event.target.value as TaskSizingInputs['risk_level'])}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></Field></div><details className="preflight-advanced"><summary>高级预判</summary><div className="form-grid two"><NumberField label="验证命令数" value={sizing.verification_commands_count} onChange={(value) => setSizing('verification_commands_count', value)} /><NumberField label="预计验证秒数" value={sizing.estimated_verification_seconds} onChange={(value) => setSizing('estimated_verification_seconds', value)} /><NumberField label="外部依赖数" value={sizing.external_dependencies_count} onChange={(value) => setSizing('external_dependencies_count', value)} /></div><div className="check-grid"><Check label="包含迁移" checked={sizing.has_migration} onChange={(value) => setSizing('has_migration', value)} /><Check label="包含部署" checked={sizing.has_deploy} onChange={(value) => setSizing('has_deploy', value)} /></div></details><div className="gate-grid"><Check label="产物明确" checked={sizing.gate_artifact} onChange={(value) => setSizing('gate_artifact', value)} /><Check label="边界明确" checked={sizing.gate_boundary} onChange={(value) => setSizing('gate_boundary', value)} /><Check label="验证明确" checked={sizing.gate_verification} onChange={(value) => setSizing('gate_verification', value)} /><Check label="依赖明确" checked={sizing.gate_dependency} onChange={(value) => setSizing('gate_dependency', value)} /></div><button type="button" className="full" disabled={busy} onClick={onEstimate}><Pulse size={16} />执行 0 Token 预判</button>{estimate && <EstimateCard estimate={estimate} />}</section><button className="primary full" disabled={busy || !projects.length || !ready}><Plus size={16} />检查 Provider 并提交目标</button></form></aside></div>
}

function TaskDrawer(props: { form: typeof initialTask; setForm: (value: typeof initialTask) => void; setSizingInputs: (value: TaskSizingInputs) => void; estimate: TaskSizingEstimate | null; projects: Project[]; providers: Provider[]; busy: boolean; onClose: () => void; onEstimate: () => void; onSubmit: (event: FormEvent) => void }) {
  const { form, setForm, setSizingInputs, estimate, projects, providers, busy, onClose, onEstimate, onSubmit } = props
  const sizing = form.sizingInputs
  const setSizing = <K extends keyof TaskSizingInputs>(key: K, value: TaskSizingInputs[K]) => setSizingInputs({ ...sizing, [key]: value })
  const ready = estimate?.status === 'estimated'
  const provider = providers.find((item) => item.name === form.provider)
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-head"><div><span className="kicker">诊断入口 · 非主流程</span><h1>新建任务</h1></div><button aria-label="关闭" onClick={onClose}><X size={18} /></button></div><form onSubmit={onSubmit}><Field label="标题"><input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="明确、可验证的结果" /></Field><Field label="目标"><textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="Worker 必须完成什么？" /></Field><div className="form-grid two"><Field label="项目"><select required value={form.projectId} onChange={(event) => setForm({ ...form, projectId: event.target.value })}><option value="">选择项目</option>{projects.filter((p) => p.status === 'active').map((project) => <option value={project.id} key={project.id}>{project.name}</option>)}</select></Field><Field label="角色"><select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}>{Object.entries(roleNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></Field><Field label="Provider"><select value={form.provider} onChange={(event) => setForm({ ...form, provider: event.target.value })}>{providers.filter((provider) => provider.enabled && provider.transport === 'host-bridge').map((provider) => <option value={provider.name} key={provider.name}>{provider.display_name}</option>)}</select></Field></div><p className="form-help">Provider 当前状态：{provider?.status === 'available' ? '可用' : provider?.reason ?? '待探测'}。提交和派发前均会重新执行 0 Token 就绪探测。</p><div className="form-grid two"><Field label="验证文件 / 产物路径"><input value={form.verifyPath} onChange={(event) => setForm({ ...form, verifyPath: event.target.value })} /></Field><Field label="必须包含"><input value={form.verifyText} onChange={(event) => setForm({ ...form, verifyText: event.target.value })} /></Field></div><p className="form-help">产物路径相对于本机项目目录；完成后可在任务页复制路径、Finder 定位或用 Cursor 打开。</p><section className="preflight"><div className="preflight-heading"><div><span className="kicker">入队前必做</span><h2>0 Token 规则评估</h2></div><span>不调用模型</span></div><div className="form-grid two"><NumberField label="涉及层数" value={sizing.layers_touched} onChange={(value) => setSizing('layers_touched', value)} /><NumberField label="组件数" value={sizing.components_touched} onChange={(value) => setSizing('components_touched', value)} /><NumberField label="预计文件数" value={sizing.estimated_files_changed} onChange={(value) => setSizing('estimated_files_changed', value)} /><Field label="风险"><select value={sizing.risk_level} onChange={(event) => setSizing('risk_level', event.target.value as TaskSizingInputs['risk_level'])}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></Field></div><details className="preflight-advanced"><summary>高级预判</summary><div className="form-grid two"><NumberField label="验证命令数" value={sizing.verification_commands_count} onChange={(value) => setSizing('verification_commands_count', value)} /><NumberField label="预计验证秒数" value={sizing.estimated_verification_seconds} onChange={(value) => setSizing('estimated_verification_seconds', value)} /><NumberField label="外部依赖数" value={sizing.external_dependencies_count} onChange={(value) => setSizing('external_dependencies_count', value)} /></div><div className="check-grid"><Check label="包含迁移" checked={sizing.has_migration} onChange={(value) => setSizing('has_migration', value)} /><Check label="包含部署" checked={sizing.has_deploy} onChange={(value) => setSizing('has_deploy', value)} /><Check label="要求独立复审（当前不可入队）" checked={sizing.independent_review_required} onChange={(value) => setSizing('independent_review_required', value)} /></div><p className="form-help">勾选独立复审会触发 Planning Gate；当前尚无独立 reviewer 编排，因此任务不能入队。</p></details><div className="gate-grid"><Check label="产物明确" checked={sizing.gate_artifact} onChange={(value) => setSizing('gate_artifact', value)} /><Check label="边界明确" checked={sizing.gate_boundary} onChange={(value) => setSizing('gate_boundary', value)} /><Check label="验证明确" checked={sizing.gate_verification} onChange={(value) => setSizing('gate_verification', value)} /><Check label="依赖明确" checked={sizing.gate_dependency} onChange={(value) => setSizing('gate_dependency', value)} /></div><button type="button" className="full" disabled={busy} onClick={onEstimate}><Pulse size={16} />执行 0 Token 预判</button>{estimate && <EstimateCard estimate={estimate} />}</section><button className="primary full" disabled={busy || !projects.length || !ready}><Plus size={16} />检查 Provider 并加入任务队列</button></form></aside></div>
}

const gateNames = {
  artifact: '可验证产物',
  boundary: '文件或组件边界',
  verification: '验证命令',
  dependency: '外部依赖',
  independent_review_orchestration: '尚无独立 reviewer 编排，要求独立复审的任务当前不能入队',
}

function EstimateCard({ estimate }: { estimate: TaskSizingEstimate }) {
  if (estimate.status === 'needs_planning') return <div className="estimate-card blocked" role="status"><strong>暂不可入队：先补齐计划</strong><p>缺少：{estimate.missing_gates.map((gate) => gateNames[gate]).join('、')}。请先拆分任务或补齐对应 gate。</p><small>0 Token 规则评估 · 未调用模型</small></div>
  return <div className="estimate-card" role="status"><div><strong>服务端 Tier {estimate.size_class}</strong><span>0 Token 规则评估</span></div><dl><Fact label="Soft Timeout" value={`${estimate.soft_deadline_seconds}s`} /><Fact label="Hard Timeout" value={`${estimate.hard_deadline_seconds}s`} /><Fact label="最大尝试" value={formatNumber(estimate.max_attempts)} /><Fact label="验证超时" value={`${estimate.verification_timeout_seconds}s`} /></dl><p title={estimate.rationale.join('; ')}>{estimate.rationale.slice(-3).join(' · ')}</p><small>{estimate.model_invoked ? '调用了模型' : '未调用模型'} · 服务端规则 {estimate.bootstrap_version}</small></div>
}

function Metric({ icon: Icon, label, value, hint, onClick }: { icon: typeof Kanban; label: string; value: number; hint: string; onClick?: () => void }) {
  const content = <><div className="metric-icon"><Icon size={18} /></div><div><span>{label}</span><strong>{value.toLocaleString()}</strong></div><small>{hint}</small></>
  return onClick
    ? <button type="button" className="metric-card" aria-label={`查看${label}详情`} onClick={onClick}>{content}</button>
    : <article className="metric-card">{content}</article>
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
