type EventStream = {
  onmessage: (() => void) | null
  onerror: (() => void) | null
  addEventListener: (type: string, listener: () => void) => void
  close: () => void
}

type LiveRefreshOptions = {
  createEventSource?: ((url: string) => EventStream) | null
  intervalMs?: number
}

export function startLiveRefresh(
  refresh: () => Promise<unknown>,
  options: LiveRefreshOptions = {},
) {
  const createEventSource = options.createEventSource === undefined
    ? typeof EventSource === 'undefined' ? null : (url: string) => new EventSource(url) as unknown as EventStream
    : options.createEventSource
  let source: EventStream | null = null
  let timer: number | null = null
  let stopped = false
  let refreshing = false

  const run = () => {
    if (stopped || refreshing) return
    refreshing = true
    void refresh().catch(() => undefined).finally(() => { refreshing = false })
  }
  const poll = () => {
    if (timer === null && !stopped) timer = window.setInterval(run, options.intervalMs ?? 30_000)
  }

  if (createEventSource) {
    try {
      const next = createEventSource('/api/events/stream')
      source = next
      next.onmessage = run
      next.addEventListener('task.updated', run)
      next.onerror = () => {
        source?.close()
        source = null
        poll()
      }
    } catch {
      poll()
    }
  } else {
    poll()
  }

  return () => {
    stopped = true
    source?.close()
    if (timer !== null) window.clearInterval(timer)
  }
}
