export const TOKEN_MAGNITUDES = [
  { maxRatio: .25, label: '≤25% 均值', color: '#a855f7' },
  { maxRatio: .5, label: '25–50% 均值', color: '#6366f1' },
  { maxRatio: .75, label: '50–75% 均值', color: '#3b82f6' },
  { maxRatio: 1.25, label: '75–125% 均值', color: '#22c55e' },
  { maxRatio: 1.5, label: '125–150% 均值', color: '#eab308' },
  { maxRatio: 2, label: '150–200% 均值', color: '#f97316' },
  { maxRatio: Number.POSITIVE_INFINITY, label: '>200% 均值', color: '#ef4444' },
] as const

export function tokenMagnitude(tokens: number, average?: number | null, historyDays = 0) {
  if (!average || historyDays === 0) {
    return { label: '历史不足', color: '#64748b', ratio: null, historyDays }
  }
  const ratio = tokens / average
  const band = TOKEN_MAGNITUDES.find((item) => ratio <= item.maxRatio) ?? TOKEN_MAGNITUDES.at(-1)!
  return { ...band, ratio, historyDays }
}
