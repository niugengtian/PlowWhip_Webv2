import { FormEvent, useCallback, useEffect, useState } from 'react'
import { api, Convention, Project, RuntimeSettings, SchedulerStatus, Task, TaskEvent, Usage } from './api'

type Health = {
  status: string
  version: string
  database: { status: string; journal_mode: string; migration_count: number }
}

type View = 'today' | 'tasks' | 'projects' | 'workforce' | 'usage' | 'settings'

const initialForm = {
  title: 'Create verified result',
  objective: 'Create result.txt and verify its content',
  projectPath: '',
  projectId: '',
  role: 'fullstack',
  executable: 'python3',
  argumentsJson: '["-c", "from pathlib import Path; Path(\'result.txt\').write_text(\'quality-pass\', encoding=\'utf-8\')"]',
  verifyPath: 'result.txt',
  verifyText: 'quality-pass',
}

export function App() {
  const [view, setView] = useState<View>('today')
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSettings | null>(null)
  const [schedulerStatus, setSchedulerStatus] = useState<SchedulerStatus | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)
  const [convention, setConvention] = useState<Convention | null>(null)
  const [selected, setSelected] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [form, setForm] = useState(initialForm)
  const [projectForm, setProjectForm] = useState({ name: '', path: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refreshTasks = useCallback(async () => {
    const next = await api.tasks()
    setTasks(next)
    setSelected((current) => next.find((item) => item.id === current?.id) ?? current)
  }, [])

  const refreshProjects = useCallback(async () => setProjects(await api.projects()), [])

  useEffect(() => {
    fetch('/health')
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json() as Promise<Health>
      })
      .then(setHealth)
      .catch((reason: unknown) => setError(messageOf(reason)))
    api.tasks()
      .then(setTasks)
      .catch((reason: unknown) => setError(messageOf(reason)))
    api.projects()
      .then(setProjects)
      .catch((reason: unknown) => setError(messageOf(reason)))
    api.settings().then(setRuntimeSettings).catch((reason: unknown) => setError(messageOf(reason)))
    api.schedulerStatus().then(setSchedulerStatus).catch((reason: unknown) => setError(messageOf(reason)))
    api.usage().then(setUsage).catch((reason: unknown) => setError(messageOf(reason)))
    api.convention('global', 'global').then(setConvention).catch((reason: unknown) => setError(messageOf(reason)))
  }, [])

  useEffect(() => {
    if (!selected) {
      return
    }
    api.taskEvents(selected.id)
      .then(setEvents)
      .catch((reason: unknown) => setError(messageOf(reason)))
  }, [selected])

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const args = JSON.parse(form.argumentsJson) as unknown
      if (!Array.isArray(args) || args.some((item) => typeof item !== 'string')) {
        throw new Error('Arguments 必须是字符串 JSON 数组')
      }
      const created = await api.createTask({
        title: form.title,
        objective: form.objective,
        ...(form.projectId ? { project_id: form.projectId, role: form.role } : { project_path: form.projectPath }),
        command: { argv: [form.executable, ...args] },
        verification: [
          { kind: 'exit_code', expected: 0 },
          { kind: 'file_exists', path: form.verifyPath },
          { kind: 'file_contains', path: form.verifyPath, contains: form.verifyText },
        ],
      })
      await refreshTasks()
      setSelected(created)
      setView('tasks')
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.createProject(projectForm)
      await refreshProjects()
      setForm((current) => ({ ...current, projectId: created.id, projectPath: created.path }))
      setProjectForm({ name: '', path: '' })
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function releaseProject(project: Project) {
    setBusy(true)
    setError(null)
    try {
      await api.releaseProject(project.id)
      await refreshProjects()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!runtimeSettings) return
    setBusy(true)
    setError(null)
    try {
      setRuntimeSettings(await api.updateSettings(runtimeSettings))
      setSchedulerStatus(await api.schedulerStatus())
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function schedulerAction(action: 'tick' | 'install') {
    setBusy(true)
    setError(null)
    try {
      if (action === 'tick') await api.schedulerTick()
      else await api.schedulerInstall()
      setSchedulerStatus(await api.schedulerStatus())
      await refreshTasks()
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function loadConvention(scope: Convention['scope'], scopeId: string) {
    if (!scopeId) return
    setError(null)
    try {
      setConvention(await api.convention(scope, scopeId))
    } catch (reason) {
      setError(messageOf(reason))
    }
  }

  async function saveConvention(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!convention) return
    setBusy(true)
    setError(null)
    try {
      setConvention(await api.updateConvention(convention))
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  async function drive(task: Task) {
    setBusy(true)
    setError(null)
    try {
      const updated = await api.driveTask(task)
      setSelected(updated)
      await refreshTasks()
      setEvents(await api.taskEvents(task.id))
    } catch (reason) {
      setError(messageOf(reason))
    } finally {
      setBusy(false)
    }
  }

  const active = tasks.filter((task) => !['completed', 'terminal_failed', 'cancelled'].includes(task.status))
  const modelRuns = tasks.reduce((total, task) => total + task.attempts_used, 0)

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <span className="eyebrow">PLOW-WHIP WEB V2</span>
          <h1>无人值守工作控制台</h1>
        </div>
        <span className={`status ${health ? 'healthy' : ''}`}>
          {health ? 'Runtime healthy' : error ? 'Runtime offline' : 'Connecting'}
        </span>
      </header>

      <nav aria-label="主要导航">
        <button className={view === 'today' ? 'active' : ''} onClick={() => setView('today')}>Today</button>
        <button className={view === 'tasks' ? 'active' : ''} onClick={() => setView('tasks')}>Tasks</button>
        <button className={view === 'projects' ? 'active' : ''} onClick={() => setView('projects')}>Projects</button>
        <button className={view === 'workforce' ? 'active' : ''} onClick={() => setView('workforce')}>Workforce</button>
        <button className={view === 'usage' ? 'active' : ''} onClick={() => { setView('usage'); api.usage().then(setUsage) }}>Usage</button><button className={view === 'settings' ? 'active' : ''} onClick={() => setView('settings')}>Settings</button>
      </nav>

      <main>
        {error && <div className="error" role="alert">{error}</div>}
        {view === 'today' ? (
          <>
            <section className="hero">
              <p>产品目标</p>
              <h2>保障质量的前提下实现无人值守完成，尽量减少 Token 消费。</h2>
            </section>
            <section className="metrics" aria-label="运行状态">
              <article><span>Active tasks</span><strong>{active.length}</strong></article>
              <article><span>Needs you</span><strong>{tasks.filter((task) => task.status === 'needs_human').length}</strong></article>
              <article><span>Execute runs</span><strong>{modelRuns}</strong></article>
              <article><span>Control Token</span><strong>0</strong></article>
            </section>
            <section className="workspace-grid">
              <form className="task-form" onSubmit={createTask}>
                <span className="step">SPRINT 2 · BOUND TASK</span>
                <h2>创建可验证任务</h2>
                <label>标题<input required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
                <label>目标<textarea required value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} /></label>
                {projects.length ? <div className="field-row">
                  <label>项目<select required value={form.projectId} onChange={(event) => setForm({ ...form, projectId: event.target.value })}><option value="">选择项目</option>{projects.filter((project) => project.status === 'active').map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
                  <label>角色<select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}><option value="coordination">Coordination</option><option value="fullstack">Fullstack</option><option value="web3">Web3</option><option value="devops_sre">DevOps/SRE</option><option value="verification">Verification</option></select></label>
                </div> : <label>项目绝对路径<input required placeholder="/Users/me/project" value={form.projectPath} onChange={(event) => setForm({ ...form, projectPath: event.target.value })} /></label>}
                <div className="field-row">
                  <label>Executable<input required value={form.executable} onChange={(event) => setForm({ ...form, executable: event.target.value })} /></label>
                  <label>验证文件<input required value={form.verifyPath} onChange={(event) => setForm({ ...form, verifyPath: event.target.value })} /></label>
                </div>
                <label>Arguments JSON<textarea required value={form.argumentsJson} onChange={(event) => setForm({ ...form, argumentsJson: event.target.value })} /></label>
                <label>文件必须包含<input required value={form.verifyText} onChange={(event) => setForm({ ...form, verifyText: event.target.value })} /></label>
                <button className="primary" disabled={busy}>{busy ? '处理中…' : '创建 Task'}</button>
              </form>
              <section className="task-list">
                <div className="section-heading"><div><span className="step">TASK QUEUE</span><h2>真实任务状态</h2></div><button onClick={() => refreshTasks()}>刷新</button></div>
                {tasks.length === 0 ? <p className="muted">还没有 Task。创建后只在明确 Drive 时执行。</p> : tasks.map((task) => (
                  <button className={`task-row ${selected?.id === task.id ? 'selected' : ''}`} key={task.id} onClick={() => { setSelected(task); setView('tasks') }}>
                    <span><strong>{task.title}</strong><small>{task.objective}</small></span>
                    <span className={`task-status status-${task.status}`}>{task.status}</span>
                  </button>
                ))}
              </section>
            </section>
          </>
        ) : view === 'tasks' ? (
          <section className="task-detail">
            <div className="section-heading"><div><span className="step">TASK DETAIL</span><h2>{selected?.title ?? '选择一个 Task'}</h2></div>{selected?.status === 'ready' && <button className="primary" disabled={busy} onClick={() => drive(selected)}>Drive + Verify</button>}</div>
            {selected ? (
              <div className="detail-grid">
                <dl>
                  <div><dt>Status</dt><dd>{selected.status}</dd></div>
                  <div><dt>Revision</dt><dd>{selected.revision}</dd></div>
                  <div><dt>Attempts</dt><dd>{selected.attempts_used} / {selected.max_attempts}</dd></div>
                  <div><dt>Task Token</dt><dd>{selected.tokens_used}</dd></div>
                  <div><dt>Project</dt><dd>{selected.project_path}</dd></div>
                  <div><dt>Worker</dt><dd className="hash">{selected.worker_id ?? 'unbound'}</dd></div>
                  <div><dt>Evidence</dt><dd className="hash">{selected.last_evidence_hash ?? '—'}</dd></div>
                </dl>
                <div className="timeline">
                  <h3>Event timeline</h3>
                  {events.map((item) => <div key={item.sequence}><span>{item.sequence}</span><strong>{item.event_type}</strong><small>rev {item.state_revision}</small></div>)}
                </div>
              </div>
            ) : <p className="muted">从 Today 选择或创建 Task。</p>}
          </section>
        ) : view === 'projects' ? (
          <section className="workspace-grid">
            <form className="task-form" onSubmit={createProject}>
              <span className="step">PROJECT REGISTRY</span><h2>登记项目</h2>
              <label>项目名<input required value={projectForm.name} onChange={(event) => setProjectForm({ ...projectForm, name: event.target.value })} /></label>
              <label>本机绝对路径<input required placeholder="/Users/me/project" value={projectForm.path} onChange={(event) => setProjectForm({ ...projectForm, path: event.target.value })} /></label>
              <button className="primary" disabled={busy}>创建并预置 5 个角色</button>
            </form>
            <section className="task-list">
              <div className="section-heading"><div><span className="step">MULTI PROJECT</span><h2>{projects.length} 个项目</h2></div><button onClick={refreshProjects}>刷新</button></div>
              {projects.map((project) => <article className="project-card" key={project.id}>
                <div><strong>{project.name}</strong><span className={`task-status status-${project.status}`}>{project.status}</span></div>
                <small>{project.path}</small><p>{project.roles.length} roles · {project.workers.length} hired workers</p>
                {project.status === 'active' && <button disabled={busy} onClick={() => releaseProject(project)}>完成并释放会话</button>}
              </article>)}
            </section>
          </section>
        ) : view === 'workforce' ? (
          <section className="task-detail">
            <div className="section-heading"><div><span className="step">PROJECT · ROLE · CLI SESSION</span><h2>Worker 状态</h2></div><button onClick={refreshProjects}>刷新</button></div>
            <div className="worker-grid">
              {projects.flatMap((project) => project.roles.map((role) => {
                const worker = project.workers.find((item) => item.role_id === role.id)
                return <article key={role.id}><small>{project.name}</small><h3>{role.kind}</h3><span className={`task-status status-${worker?.status ?? 'not_hired'}`}>{worker?.status ?? 'not hired'}</span><dl><div><dt>Provider</dt><dd>{worker?.provider ?? '—'}</dd></div><div><dt>Session</dt><dd className="hash">{worker?.session_id ?? '按需创建'}</dd></div><div><dt>Task</dt><dd className="hash">{worker?.active_task_id ?? '—'}</dd></div></dl></article>
              }))}
            </div>
          </section>
        ) : view === 'usage' ? (
          <section className="task-detail">
            <div className="section-heading"><div><span className="step">TOKEN LEDGER</span><h2>消费与节省证据</h2></div><button onClick={() => api.usage().then(setUsage)}>刷新</button></div>
            <section className="metrics usage-metrics"><article><span>Control Token</span><strong>{usage?.control_tokens ?? 0}</strong></article><article><span>Work Token</span><strong>{usage?.total_tokens ?? 0}</strong></article><article><span>Input</span><strong>{usage?.input_tokens ?? 0}</strong></article><article><span>Output</span><strong>{usage?.output_tokens ?? 0}</strong></article></section>
            <div className="detail-grid"><section><h3>按项目</h3>{usage?.projects.length ? usage.projects.map((item) => <div className="usage-row" key={item.project_id ?? 'unbound'}><span>{projects.find((project) => project.id === item.project_id)?.name ?? item.project_id ?? 'unbound'}</span><strong>{item.tokens}</strong></div>) : <p className="muted">尚无模型 Token 消费。</p>}</section><section><h3>按 Task</h3>{usage?.tasks.length ? usage.tasks.map((item) => <div className="usage-row" key={item.task_id}><span>{tasks.find((task) => task.id === item.task_id)?.title ?? item.task_id}</span><strong>{item.tokens}</strong></div>) : <p className="muted">0 Token 控制链不会制造账单。</p>}</section></div>
          </section>
        ) : (
          <section className="settings-layout">
            <section className="task-detail scheduler-card">
              <div className="section-heading"><div><span className="step">ZERO TOKEN CONTROL PATH</span><h2>系统调度器</h2></div><span className={`task-status ${schedulerStatus?.authorization_required ? 'status-terminal_failed' : 'status-completed'}`}>{schedulerStatus?.authorization_required ? '等待授权安装' : '已授权'}</span></div>
              <dl>
                <div><dt>OS / Backend</dt><dd>{schedulerStatus ? `${schedulerStatus.system.os} · ${schedulerStatus.system.backend}` : '检测中'}</dd></div>
                <div><dt>Target</dt><dd className="hash">{schedulerStatus?.system.target ?? '—'}</dd></div>
                <div><dt>Fencing</dt><dd>{schedulerStatus?.runtime.fencing_token ?? 0}</dd></div>
                <div><dt>Last tick</dt><dd>{schedulerStatus?.runtime.last_tick_at ?? '尚未运行'}</dd></div>
                <div><dt>Control model</dt><dd>{schedulerStatus?.model_invoked ? 'invoked' : '0 Token / never invoked'}</dd></div>
              </dl>
              <div className="action-row"><button disabled={busy} onClick={() => schedulerAction('tick')}>立即运行 Tick</button><button className="primary" disabled={busy || schedulerStatus?.authorization_required} onClick={() => schedulerAction('install')}>安装单一系统定时任务</button></div>
            </section>
            {runtimeSettings && <form className="task-form settings-form" onSubmit={saveSettings}>
              <span className="step">CONTROL PLANE SETTINGS · REV {runtimeSettings.revision}</span><h2>运行参数</h2>
              <div className="settings-grid">
                {([
                  ['scheduler_interval_seconds', 'Tick 间隔（秒）'], ['scheduler_lease_seconds', '调度租约（秒）'],
                  ['max_parallel_workers', '最大并行 Worker'], ['task_default_token_budget', 'Task 默认 Token 预算'],
                  ['global_daily_token_budget', '全局每日 Token 预算'], ['max_same_failure', '同类失败熔断次数'],
                  ['max_no_progress', '无进展熔断次数'], ['context_max_bytes', 'Context 最大字节'],
                  ['rotation_max_bytes', '文件轮转字节'],
                ] as const).map(([key, label]) => <label key={key}>{label}<input type="number" value={runtimeSettings.values[key]} onChange={(event) => setRuntimeSettings({ ...runtimeSettings, values: { ...runtimeSettings.values, [key]: Number(event.target.value) } })} /></label>)}
              </div>
              <label className="check"><input type="checkbox" checked={runtimeSettings.values.auto_dispatch} onChange={(event) => setRuntimeSettings({ ...runtimeSettings, values: { ...runtimeSettings.values, auto_dispatch: event.target.checked } })} />无人值守自动派发 Ready Task</label>
              <label className="check"><input type="checkbox" checked={runtimeSettings.values.system_scheduler_authorized} onChange={(event) => setRuntimeSettings({ ...runtimeSettings, values: { ...runtimeSettings.values, system_scheduler_authorized: event.target.checked } })} />授权写入当前用户的系统定时任务</label>
              <button className="primary" disabled={busy}>保存设置</button>
            </form>}
            {convention && <form className="task-form" onSubmit={saveConvention}>
              <span className="step">THREE-SCOPE CONVENTION · REV {convention.revision}</span><h2>约束编辑器</h2>
              <div className="field-row"><label>作用域<select value={convention.scope} onChange={(event) => {
                const scope = event.target.value as Convention['scope']
                const scopeId = scope === 'global' ? 'global' : scope === 'project' ? projects[0]?.id ?? '' : tasks[0]?.id ?? ''
                loadConvention(scope, scopeId)
              }}><option value="global">Global</option><option value="project">Project</option><option value="task">Task</option></select></label>
              <label>目标<select value={convention.scope_id} disabled={convention.scope === 'global'} onChange={(event) => loadConvention(convention.scope, event.target.value)}>{convention.scope === 'global' ? <option value="global">global</option> : convention.scope === 'project' ? projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>) : tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</select></label></div>
              <label>Convention<textarea className="convention-editor" placeholder="质量门、权限边界、项目规范或 Task 特殊约束" value={convention.content} onChange={(event) => setConvention({ ...convention, content: event.target.value })} /></label>
              <button className="primary" disabled={busy}>保存 Convention</button>
            </form>}
          </section>
        )}
      </main>
    </div>
  )
}

function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Unexpected error'
}
