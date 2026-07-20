import { afterEach, expect, test, vi } from 'vitest'
import { startLiveRefresh } from './liveRefresh'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  })
})

test('pauses while hidden, refreshes once on resume, and cleans up its only timer', async () => {
  vi.useFakeTimers()
  const source = {
    onmessage: null as (() => void) | null,
    onerror: null as (() => void) | null,
    addEventListener: vi.fn(),
    close: vi.fn(),
  }
  const createEventSource = vi.fn(() => source)
  const refresh = vi.fn(async () => undefined)
  const stop = startLiveRefresh(refresh, { createEventSource, intervalMs: 5_000 })

  await vi.advanceTimersByTimeAsync(5_000)
  expect(refresh).toHaveBeenCalledTimes(1)

  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'hidden',
  })
  document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(20_000)
  expect(refresh).toHaveBeenCalledTimes(1)
  expect(source.close).toHaveBeenCalledTimes(1)

  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  })
  document.dispatchEvent(new Event('visibilitychange'))
  await vi.runAllTicks()
  expect(refresh).toHaveBeenCalledTimes(2)
  expect(createEventSource).toHaveBeenCalledTimes(2)

  stop()
  expect(vi.getTimerCount()).toBe(0)
})

test('coalesces event bursts and close mode creates no stream or timer', async () => {
  vi.useFakeTimers()
  let finish: (() => void) | undefined
  const refresh = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
  const source = {
    onmessage: null as (() => void) | null,
    onerror: null as (() => void) | null,
    addEventListener: vi.fn(),
    close: vi.fn(),
  }
  const stop = startLiveRefresh(refresh, {
    createEventSource: () => source,
    intervalMs: 10_000,
  })

  source.onmessage?.()
  source.onmessage?.()
  source.onmessage?.()
  expect(refresh).toHaveBeenCalledTimes(1)
  finish?.()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  expect(refresh).toHaveBeenCalledTimes(2)
  stop()

  const disabledSource = vi.fn(() => source)
  const disabledRefresh = vi.fn(async () => undefined)
  const stopDisabled = startLiveRefresh(disabledRefresh, {
    createEventSource: disabledSource,
    intervalMs: 0,
  })
  await vi.advanceTimersByTimeAsync(60_000)
  expect(disabledSource).not.toHaveBeenCalled()
  expect(disabledRefresh).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
  stopDisabled()
})

test('backs off repeated failures and resets after a successful refresh', async () => {
  vi.useFakeTimers()
  const statuses: { failures: number; nextDelayMs: number }[] = []
  const refresh = vi.fn()
    .mockRejectedValueOnce(new Error('offline'))
    .mockRejectedValueOnce(new Error('still offline'))
    .mockResolvedValue(undefined)
  const stop = startLiveRefresh(refresh, {
    createEventSource: null,
    intervalMs: 1_000,
    maxBackoffMs: 8_000,
    onStatus: (status) => statuses.push(status),
  })

  await vi.advanceTimersByTimeAsync(1_000)
  expect(refresh).toHaveBeenCalledTimes(1)
  expect(statuses.at(-1)).toEqual({ failures: 1, nextDelayMs: 2_000 })
  await vi.advanceTimersByTimeAsync(2_000)
  expect(refresh).toHaveBeenCalledTimes(2)
  expect(statuses.at(-1)).toEqual({ failures: 2, nextDelayMs: 4_000 })
  await vi.advanceTimersByTimeAsync(4_000)
  expect(refresh).toHaveBeenCalledTimes(3)
  expect(statuses.at(-1)).toEqual({ failures: 0, nextDelayMs: 1_000 })
  stop()
})
