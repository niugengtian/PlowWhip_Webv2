type EventStream = {
  onmessage: (() => void) | null
  onerror: (() => void) | null
  addEventListener: (type: string, listener: () => void) => void
  close: () => void
}

type LiveRefreshOptions = {
  createEventSource?: ((url: string) => EventStream) | null
  intervalMs?: number
  maxBackoffMs?: number
  onStatus?: (status: { failures: number; nextDelayMs: number }) => void
}

export function startLiveRefresh(
  refresh: () => Promise<unknown>,
  options: LiveRefreshOptions = {},
) {
  const intervalMs = options.intervalMs ?? 30_000
  const maxBackoffMs = options.maxBackoffMs ?? Math.max(intervalMs, 300_000)
  const createEventSource = options.createEventSource === undefined
    ? typeof EventSource === 'undefined' ? null : (url: string) => new EventSource(url) as unknown as EventStream
    : options.createEventSource
  let source: EventStream | null = null
  let timer: number | null = null
  let stopped = false
  let refreshing = false
  let pending = false
  let suppressEventsUntilRefreshSettles = false
  let consecutiveFailures = 0

  const clearTimer = () => {
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }
  const schedule = () => {
    clearTimer()
    if (!stopped && intervalMs > 0 && document.visibilityState !== 'hidden') {
      const delay = Math.min(
        maxBackoffMs,
        intervalMs * (2 ** Math.min(consecutiveFailures, 6)),
      )
      options.onStatus?.({ failures: consecutiveFailures, nextDelayMs: delay })
      timer = window.setTimeout(run, delay)
    }
  }
  const run = () => {
    if (stopped || intervalMs <= 0 || document.visibilityState === 'hidden') return
    if (refreshing) {
      pending = true
      return
    }
    refreshing = true
    clearTimer()
    void refresh().then(() => {
      consecutiveFailures = 0
    }).catch(() => {
      consecutiveFailures += 1
    }).finally(() => {
      refreshing = false
      suppressEventsUntilRefreshSettles = false
      if (pending) {
        pending = false
        run()
      } else {
        schedule()
      }
    })
  }
  const closeSource = () => {
    source?.close()
    source = null
  }
  const runFromEvent = () => {
    if (!suppressEventsUntilRefreshSettles) run()
  }
  const connect = () => {
    if (stopped || intervalMs <= 0 || document.visibilityState === 'hidden' || !createEventSource || source) return
    try {
      const next = createEventSource('/api/events/stream')
      source = next
      next.onmessage = runFromEvent
      next.addEventListener('aggregate.updated', runFromEvent)
      next.onerror = () => {
        closeSource()
        schedule()
      }
    } catch {
      schedule()
    }
  }
  const visibilityChanged = () => {
    if (document.visibilityState === 'hidden') {
      clearTimer()
      closeSource()
      return
    }
    suppressEventsUntilRefreshSettles = true
    connect()
    run()
  }

  document.addEventListener('visibilitychange', visibilityChanged)
  connect()
  schedule()

  return () => {
    stopped = true
    pending = false
    document.removeEventListener('visibilitychange', visibilityChanged)
    closeSource()
    clearTimer()
  }
}
