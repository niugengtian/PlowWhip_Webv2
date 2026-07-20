import { useMemo, type CSSProperties } from 'react'
import { tokenMagnitude } from '../tokenMagnitude'

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
  project: '#a78bfa',
  input: '#86efac',
  cached: '#fbbf24',
  uncached: '#c4b5fd',
  output: '#fb7185',
}

export function TokenMagnitudeBadge({ tokens }: { tokens: number }) {
  const magnitude = tokenMagnitude(tokens)
  return <span className="token-magnitude" style={{ '--magnitude-color': magnitude.color } as CSSProperties}>{magnitude.label}</span>
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
  comparisonDays,
  comparisonLabel,
  selectedDate,
  onSelect,
}: {
  days: DailyUsagePoint[]
  comparisonDays?: DailyUsagePoint[]
  comparisonLabel?: string
  selectedDate: string | null
  onSelect: (date: string) => void
}) {
  const width = 720
  const height = 220
  const pad = { top: 18, right: 16, bottom: 36, left: 48 }
  const innerW = width - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom
  const maxTotal = Math.max(1, ...days.map((day) => day.total_tokens), ...(comparisonDays ?? []).map((day) => day.total_tokens))
  const localToday = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
  const completed = (items: DailyUsagePoint[]) => items.filter((item) => item.date < localToday)
  const average = (items: DailyUsagePoint[]) => {
    const history = completed(items)
    return {
      days: history.length,
      value: history.length ? history.reduce((sum, item) => sum + item.total_tokens, 0) / history.length : null,
    }
  }
  const globalAverage = average(days)
  const comparisonAverage = comparisonDays ? average(comparisonDays) : null
  const makePoints = (items: DailyUsagePoint[]) => items.map((day, index) => {
    const x = pad.left + (days.length <= 1 ? innerW / 2 : (index / (days.length - 1)) * innerW)
    const y = pad.top + innerH - (day.total_tokens / maxTotal) * innerH
    return { ...day, x, y }
  })
  const points = makePoints(days)
  const comparisonPoints = comparisonDays ? makePoints(comparisonDays) : []
  const segments = (items: typeof points, baseline: { value: number | null; days: number }) =>
    items.slice(1).map((point, index) => ({
      from: items[index],
      to: point,
      magnitude: tokenMagnitude(point.total_tokens, baseline.value, baseline.days),
    }))

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
        {segments(points, globalAverage).map((segment) => <line key={`global-${segment.to.date}`} x1={segment.from.x} y1={segment.from.y} x2={segment.to.x} y2={segment.to.y} stroke={segment.magnitude.color} strokeWidth={comparisonPoints.length ? 2 : 3} strokeDasharray={comparisonPoints.length ? '5 4' : undefined}><title>{`全部项目 ${segment.to.date}: ${segment.to.total_tokens} Token · ${segment.magnitude.label}`}</title></line>)}
        {comparisonAverage ? segments(comparisonPoints, comparisonAverage).map((segment) => <line key={`project-${segment.to.date}`} x1={segment.from.x} y1={segment.from.y} x2={segment.to.x} y2={segment.to.y} stroke={segment.magnitude.color} strokeWidth="3"><title>{`${comparisonLabel} ${segment.to.date}: ${segment.to.total_tokens} Token · ${segment.magnitude.label}`}</title></line>) : null}
        {points.map((point) => {
          const magnitude = tokenMagnitude(point.total_tokens, globalAverage.value, globalAverage.days)
          const active = point.date === selectedDate
          return (
            <g key={`global-point-${point.date}`}>
              <circle
                cx={point.x}
                cy={point.y}
                r={active ? 6 : 4}
                fill={active ? '#f8fafc' : magnitude.color}
                stroke={magnitude.color}
                strokeWidth={2}
              ><title>{`全部项目 ${point.date}: ${point.total_tokens} Token · input ${point.input_tokens} / cached ${point.cached_input_tokens} / uncached ${point.uncached_input_tokens} / output ${point.output_tokens}`}</title></circle>
              <text x={point.x} y={Math.max(11, point.y - 9)} textAnchor="middle" fill={magnitude.color} fontSize="9">{point.total_tokens.toLocaleString()}</text>
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
                aria-label={`查看 ${point.date} 明细，全部项目 ${point.total_tokens} tokens`}
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
        {comparisonPoints.map((point) => {
          const magnitude = tokenMagnitude(point.total_tokens, comparisonAverage?.value, comparisonAverage?.days ?? 0)
          return <g key={`project-point-${point.date}`}><circle cx={point.x} cy={point.y} r={point.date === selectedDate ? 6 : 4} fill={point.date === selectedDate ? '#f8fafc' : magnitude.color} stroke={magnitude.color} strokeWidth="2"><title>{`${comparisonLabel} ${point.date}: ${point.total_tokens} Token · input ${point.input_tokens} / cached ${point.cached_input_tokens} / uncached ${point.uncached_input_tokens} / output ${point.output_tokens}`}</title></circle><text x={point.x} y={Math.max(11, point.y - 9)} textAnchor="middle" fill={magnitude.color} fontSize="9">{point.total_tokens.toLocaleString()}</text></g>
        })}
      </svg>
      <div className="token-legend">
        <span><i style={{ background: LINE_COLORS.total }} />全部项目：历史均值 {globalAverage.value === null ? '不足' : Math.round(globalAverage.value).toLocaleString()}（{globalAverage.days} 完整日）</span>
        {comparisonPoints.length ? <span><i style={{ background: LINE_COLORS.project }} />{comparisonLabel}：历史均值 {comparisonAverage?.value === null ? '不足' : Math.round(comparisonAverage?.value ?? 0).toLocaleString()}（{comparisonAverage?.days ?? 0} 完整日）</span> : null}
        <span><i style={{ background: LINE_COLORS.input }} />input 含 cached</span>
        <span><i style={{ background: LINE_COLORS.cached }} />cached ⊂ input</span>
        <span><i style={{ background: LINE_COLORS.uncached }} />uncached</span>
        <span><i style={{ background: LINE_COLORS.output }} />output</span>
      </div>
      {(globalAverage.days > 0 && globalAverage.days < 3) || (comparisonAverage && comparisonAverage.days > 0 && comparisonAverage.days < 3) ? <p className="token-history-warning">仅有 1–2 个完整历史日，动态颜色仅作趋势提示；每日 00:00（Asia/Shanghai）重新计算基线。</p> : null}
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
