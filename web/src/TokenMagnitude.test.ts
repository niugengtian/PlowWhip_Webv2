import { expect, test } from 'vitest'
import { tokenMagnitude } from './tokenMagnitude'

test.each([
  [24, 100, '≤25% 均值', '#a855f7'],
  [40, 100, '25–50% 均值', '#6366f1'],
  [70, 100, '50–75% 均值', '#3b82f6'],
  [100, 100, '75–125% 均值', '#22c55e'],
  [140, 100, '125–150% 均值', '#eab308'],
  [180, 100, '150–200% 均值', '#f97316'],
  [220, 100, '>200% 均值', '#ef4444'],
])('maps %s against daily average %s into %s', (tokens, average, expected, color) => {
  expect(tokenMagnitude(tokens, average, 7)).toMatchObject({ label: expected, color })
})

test('uses neutral styling when there is no completed historical day', () => {
  expect(tokenMagnitude(100, null, 0)).toMatchObject({
    label: '历史不足',
    color: '#64748b',
    ratio: null,
  })
})
