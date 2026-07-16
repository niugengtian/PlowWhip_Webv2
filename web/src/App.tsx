import { FormEvent, useCallback, useEffect, useState } from 'react'
import { api, Project, Task, TaskEvent } from './api'

type Health = {
  status: string
  version: string
  database: { status: string; journal_mode: string; migration_count: number }
}

type View = 'today' | 'tasks' | 'projects' | 'workforce'

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
        <button disabled>Usage</button><button disabled>Settings</button>
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
        ) : (
          <section className="task-detail">
            <div className="section-heading"><div><span className="step">PROJECT · ROLE · CLI SESSION</span><h2>Worker 状态</h2></div><button onClick={refreshProjects}>刷新</button></div>
            <div className="worker-grid">
              {projects.flatMap((project) => project.roles.map((role) => {
                const worker = project.workers.find((item) => item.role_id === role.id)
                return <article key={role.id}><small>{project.name}</small><h3>{role.kind}</h3><span className={`task-status status-${worker?.status ?? 'not_hired'}`}>{worker?.status ?? 'not hired'}</span><dl><div><dt>Provider</dt><dd>{worker?.provider ?? '—'}</dd></div><div><dt>Session</dt><dd className="hash">{worker?.session_id ?? '按需创建'}</dd></div><div><dt>Task</dt><dd className="hash">{worker?.active_task_id ?? '—'}</dd></div></dl></article>
              }))}
            </div>
          </section>
        )}
      </main>
    </div>
  )
}

function messageOf(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Unexpected error'
}
