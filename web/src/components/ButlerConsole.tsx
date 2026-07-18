import type { Worker, WorkerDetail, WorkerStream } from '../api'

type ButlerConsoleProps = {
  worker: Worker
  detail: WorkerDetail
  stream: WorkerStream | null
  onClose: () => void
  onContinue: (worker: Worker, cursor: string) => void
}

export function ButlerConsole({
  worker,
  detail,
  stream,
  onClose,
  onContinue,
}: ButlerConsoleProps) {
  return (
    <section className="work-items" aria-label="Worker 实时控制台">
      <div className="section-heading">
        <div>
          <span className="kicker">Canonical Worker detail</span>
          <h2>{worker.id}</h2>
        </div>
        <button type="button" onClick={onClose}>关闭</button>
      </div>
      <div className="facts">
        <ConsoleFact
          label="Task / revision"
          value={detail.task ? `${detail.task.title} / ${detail.task.revision}` : '无 active Task'}
        />
        <ConsoleFact
          label="TaskSpec / episode"
          value={`${detail.task?.spec_revision ?? '—'} / ${String(detail.episode?.id ?? '—')}`}
          mono
        />
        <ConsoleFact
          label="Session ownership"
          value={`${String(detail.ownership.external_session_id ?? detail.ownership.session_id ?? '—')} · generation ${String(detail.ownership.session_generation ?? '—')}`}
          mono
        />
        <ConsoleFact label="Provider" value={String(detail.task?.provider ?? detail.worker.provider)} />
        <ConsoleFact label="Host Job identity" value={detail.host_job?.job_id ?? '—'} mono />
        <ConsoleFact
          label="Dispatch outcome"
          value={`${detail.host_job?.dispatch_outcome ?? '—'} · deadline ${detail.host_job?.reconciliation_deadline_at ?? '—'}`}
        />
        <ConsoleFact
          label="Heartbeat"
          value={`${detail.host_job?.heartbeat_at ?? detail.worker.last_seen_at ?? '—'}（仅表示存活，不代表完成）`}
        />
        <ConsoleFact label="真实状态" value={detail.task?.status ?? '无 active Task'} />
      </div>
      <div className="event-list" aria-live="polite">
        {stream?.items.length
          ? stream.items.map((item, index) => (
            <article key={`${item.ref ?? item.refs?.join(',') ?? item.kind}:${item.offset ?? index}`}>
              <div>
                <strong>{item.kind}</strong>
                <small>{item.ref ?? item.refs?.join(', ') ?? 'canonical stream'}</small>
              </div>
              <pre>{item.text}</pre>
            </article>
          ))
          : <p className="muted">暂无 canonical event / Host output。</p>}
      </div>
      {stream && (stream.has_more || stream.items.length > 0) && (
        <button type="button" onClick={() => onContinue(worker, stream.next_cursor)}>
          从 cursor 继续
        </button>
      )}
    </section>
  )
}

function ConsoleFact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><dt>{label}</dt><dd className={mono ? 'mono' : ''}>{value}</dd></div>
}
