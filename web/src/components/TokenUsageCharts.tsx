import { useMemo } from 'react'

export type DailyUsagePoint = {
  date: string
  input_tokens: number
  cached_input_tokens: number
  uncached_input_tokens: number
  output_tokens: number
  total_tokens: number
  calls: number
}

export type UsageSlice = {
  key: string
  label: string
  tokens: number
  input_tokens: number
  cached_input_tokens: number
  uncached_input_tokens: number
  output_tokens: number
  calls: number
  project_id?: string | null
  task_id?: string | null
}

const LINE_COLORS = {
  total: '#7dd3fc',
  input: '#86efac',
  cached: '#fbbf24',
  uncached: '#c4b5fd',
  output: '#fb7185',
}

const PIE_PALETTE = [
  '#7dd3fc', '#86efac', '#fbbf24', '#c4b5fd', '#fb7185',
  '#67e8f9', '#fdba74', '#a5b4fc', '#f9a8d4', '#bef264',
]

function truncateLabel(label: string, max = 28): string {
  if (label.length <= max) return label
  return `${label.slice(0, max - 1)}…`
}

export function TokenTrendChart({
  days,
  selectedDate,
  onSelect,
}: {
  days: DailyUsagePoint[]
  selectedDate: string | null
  onSelect: (date: string) => void
}) {
  const width = 720
  const height = 220
  const pad = { top: 18, right: 16, bottom: 36, left: 48 }
  const innerW = width - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom
  const maxTotal = Math.max(1, ...days.map((day) => day.total_tokens))
  const points = days.map((day, index) => {
    const x = pad.left + (days.length <= 1 ? innerW / 2 : (index / (days.length - 1)) * innerW)
    const y = pad.top + innerH - (day.total_tokens / maxTotal) * innerH
    return { ...day, x, y }
  })
  const path = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`)
    .join(' ')

  if (!days.length) {
    return <p className="muted" data-testid="token-trend-empty">所选范围内暂无 Token 记录。</p>
  }

  return (
    <div className="token-trend" data-testid="token-trend-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="每日 Token 趋势">
        <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + innerH} stroke="#314263" />
        <line x1={pad.left} y1={pad.top + innerH} x2={pad.left + innerW} y2={pad.top + innerH} stroke="#314263" />
        {[0, 0.5, 1].map((ratio) => {
          const y = pad.top + innerH - ratio * innerH
          return (
            <g key={ratio}>
              <line x1={pad.left} y1={y} x2={pad.left + innerW} y2={y} stroke="#1f2a44" strokeDasharray="3 4" />
              <text x={pad.left - 8} y={y + 4} textAnchor="end" fill="#7b8caf" fontSize="10">
                {Math.round(maxTotal * ratio)}
              </text>
            </g>
          )
        })}
        {points.length > 1 ? <path d={path} fill="none" stroke={LINE_COLORS.total} strokeWidth="2.5" /> : null}
        {points.map((point) => {
          const active = point.date === selectedDate
          return (
            <g key={point.date}>
              <circle
                cx={point.x}
                cy={point.y}
                r={active ? 6 : 4}
                fill={active ? '#f8fafc' : LINE_COLORS.total}
                stroke={active ? LINE_COLORS.total : 'transparent'}
                strokeWidth={2}
              />
              <text
                x={point.x}
                y={height - 12}
                textAnchor="middle"
                fill={active ? '#e2e8f0' : '#7b8caf'}
                fontSize="10"
              >
                {point.date.slice(5)}
              </text>
              <rect
                x={point.x - Math.max(12, innerW / Math.max(days.length, 1) / 2)}
                y={pad.top}
                width={Math.max(24, innerW / Math.max(days.length, 1))}
                height={innerH}
                fill="transparent"
                role="button"
                tabIndex={0}
                aria-label={`查看 ${point.date} 明细，共 ${point.total_tokens} tokens`}
                data-testid={`token-day-${point.date}`}
                onClick={() => onSelect(point.date)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(point.date)
                  }
                }}
                style={{ cursor: 'pointer' }}
              />
            </g>
          )
        })}
      </svg>
      <div className="token-legend">
        <span><i style={{ background: LINE_COLORS.total }} />每日 total（input + output）</span>
        <span><i style={{ background: LINE_COLORS.input }} />input 含 cached</span>
        <span><i style={{ background: LINE_COLORS.cached }} />cached ⊂ input</span>
        <span><i style={{ background: LINE_COLORS.uncached }} />uncached</span>
        <span><i style={{ background: LINE_COLORS.output }} />output</span>
      </div>
    </div>
  )
}

function polar(cx: number, cy: number, radius: number, angle: number) {
  return {
    x: cx + radius * Math.cos(angle),
    y: cy + radius * Math.sin(angle),
  }
}

function slicePath(cx: number, cy: number, radius: number, start: number, end: number) {
  const from = polar(cx, cy, radius, start)
  const to = polar(cx, cy, radius, end)
  const large = end - start > Math.PI ? 1 : 0
  return `M ${cx} ${cy} L ${from.x} ${from.y} A ${radius} ${radius} 0 ${large} 1 ${to.x} ${to.y} Z`
}

export function TokenPieChart({
  title,
  slices,
  totalTokens,
  emptyLabel,
}: {
  title: string
  slices: UsageSlice[]
  totalTokens: number
  emptyLabel: string
}) {
  const cx = 110
  const cy = 110
  const radius = 78
  const arcs = useMemo(() => {
    if (!slices.length || totalTokens <= 0) return []
    let angle = -Math.PI / 2
    return slices.map((slice, index) => {
      const portion = slice.tokens / totalTokens
      const sweep = portion * Math.PI * 2
      const start = angle
      const end = angle + sweep
      angle = end
      const single = slices.length === 1
      return {
        ...slice,
        color: PIE_PALETTE[index % PIE_PALETTE.length],
        path: single
          ? `M ${cx} ${cy - radius} A ${radius} ${radius} 0 1 1 ${cx - 0.01} ${cy - radius} Z`
          : slicePath(cx, cy, radius, start, end),
      }
    })
  }, [slices, totalTokens])

  const sliceSum = slices.reduce((sum, slice) => sum + slice.tokens, 0)

  return (
    <section className="token-pie" data-testid={`token-pie-${title}`}>
      <div className="token-pie-heading">
        <h3>{title}</h3>
        <small>分片合计 {sliceSum} / 当日 {totalTokens}</small>
      </div>
      {!slices.length || totalTokens <= 0 ? (
        <p className="muted">{emptyLabel}</p>
      ) : (
        <div className="token-pie-body">
          <svg viewBox="0 0 220 220" role="img" aria-label={title}>
            {arcs.map((arc) => (
              <path key={arc.key} d={arc.path} fill={arc.color} stroke="#0b1220" strokeWidth="1">
                <title>{`${arc.label}: ${arc.tokens}`}</title>
              </path>
            ))}
          </svg>
          <ul className="token-pie-legend">
            {arcs.map((arc) => (
              <li key={arc.key} title={arc.label}>
                <i style={{ background: arc.color }} />
                <span>{truncateLabel(arc.label)}</span>
                <strong>{arc.tokens}</strong>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
