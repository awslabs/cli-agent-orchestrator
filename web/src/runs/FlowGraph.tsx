import { useEffect, useState } from 'react'
import { Run, RunMember } from '../orchestration'
import { FlowPulse } from '../store'
import { AgentAvatar } from './AgentAvatar'
import { statusStyle } from './StatusBadge'

/**
 * Flow graph, Style A (Build Spec §3): planner on the left, workers on the
 * right grouped by role, cubic-Bézier edges, animated message pulses.
 * Amber pulse planner→worker = delegation; emerald worker→planner = report.
 * `mini` renders the 392×190 card variant (no labels, no group boxes).
 */

const AMBER = '#fbbf24'
const EMERALD = '#34d399'

interface Pos { x: number; y: number }

function edgePath(from: Pos, to: Pos): string {
  const span = to.x - from.x
  return `M ${from.x} ${from.y} C ${from.x + span * 0.4} ${from.y}, ${from.x + span * 0.6} ${to.y}, ${to.x} ${to.y}`
}

function groupWorkers(workers: RunMember[]) {
  const devs = workers.filter(w => !(w.profile || '').toLowerCase().includes('review'))
  const reviewers = workers.filter(w => (w.profile || '').toLowerCase().includes('review'))
  return { devs, reviewers }
}

export function FlowGraph({ run, pulses, onShow, mini = true }: {
  run: Run
  pulses: FlowPulse[]
  onShow: (m: RunMember) => void
  mini?: boolean
}) {
  const [, forceRender] = useState(0)
  const planner = run.planner
  if (!planner) return null
  const workers = run.workers
  const { devs, reviewers } = groupWorkers(workers)
  const ordered = [...devs, ...reviewers]

  // ── geometry ──
  const W = mini ? 392 : 640
  const ROW = mini ? 44 : 56
  const groupGap = mini ? 0 : 18
  const rowsH = Math.max(ordered.length, 1) * ROW + (mini ? 0 : (reviewers.length && devs.length ? groupGap : 0))
  const H = Math.max(rowsH + (mini ? 36 : 64), mini ? 190 : 220)
  const plannerSize = mini ? 34 : 50
  const workerSize = mini ? 28 : 38
  const plannerPos: Pos = { x: mini ? 46 : 86, y: H / 2 }
  const workerX = W - (mini ? 64 : 150)

  const yOf = (i: number): number => {
    const inReviewers = i >= devs.length
    const extra = !mini && inReviewers && devs.length ? groupGap : 0
    const top = (H - rowsH) / 2
    return top + i * ROW + ROW / 2 + extra
  }
  const posOf = (tid: string): Pos | null => {
    if (tid === planner.terminalId) return plannerPos
    const i = ordered.findIndex(w => w.terminalId === tid)
    return i >= 0 ? { x: workerX, y: yOf(i) } : null
  }

  // ── pulses (fresh window covers node pop-in latency) ──
  const FRESH_MS = 5000
  const fresh = pulses.filter(p => Date.now() - p.ts < FRESH_MS && posOf(p.sender) && posOf(p.receiver))
  useEffect(() => {
    if (!fresh.length) return
    const youngest = Math.max(...fresh.map(p => p.ts))
    const timer = setTimeout(() => forceRender(x => x + 1), youngest + FRESH_MS + 100 - Date.now())
    return () => clearTimeout(timer)
  }, [fresh.map(p => p.id).join(',')])

  // last traffic direction per worker (for the 30%-alpha edge overlay)
  const lastTraffic = (tid: string): 'out' | 'back' | null => {
    const relevant = pulses.filter(p => (p.sender === tid || p.receiver === tid) && Date.now() - p.ts < 30_000)
    if (!relevant.length) return null
    const last = relevant[relevant.length - 1]
    return last.receiver === tid ? 'out' : 'back'
  }

  const member = (m: RunMember, pos: Pos, size: number, label: boolean) => (
    <div
      key={m.terminalId}
      onClick={() => onShow(m)}
      className="absolute cursor-pointer text-center"
      style={{ left: pos.x, top: pos.y, transform: 'translate(-50%, -50%)' }}
      data-testid={`flow-node-${m.terminalId}`}
      title={`${m.profile} — click to see what it said`}
    >
      <AgentAvatar role={m.isPlanner ? 'planner' : m.profile} status={m.status} size={size} />
      {label && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--t1)' }}>
            {m.isPlanner ? 'Planner' : m.profile.slice(0, 16)}
          </div>
          <div style={{ fontSize: 11, color: 'var(--t3)' }}>{statusStyle(m.status).plain.toLowerCase()}</div>
        </div>
      )}
      {!label && m.isPlanner && <div className="microlabel" style={{ marginTop: 2 }}>Plan</div>}
    </div>
  )

  return (
    <div
      className="relative overflow-hidden"
      style={{
        width: mini ? W : '100%',
        maxWidth: W,
        height: H,
        background: 'var(--page)',
        border: '1px solid var(--border)',
        borderRadius: 8,
      }}
      data-testid={`flow-graph-${run.runId}`}
    >
      <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" className="absolute inset-0">
        {/* role group containers (full variant only) */}
        {!mini && devs.length > 0 && (
          <g>
            <rect x={workerX - 70} y={yOf(0) - ROW / 2 - 6} width={190} height={devs.length * ROW + 10}
              rx={14} fill="rgba(31,41,55,.18)" stroke="#374151" strokeDasharray="5 5" />
            <text x={workerX - 60} y={yOf(0) - ROW / 2 + 8} className="microlabel" fill="var(--t3)"
              style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase', fontWeight: 600 }}>
              Developers
            </text>
          </g>
        )}
        {!mini && reviewers.length > 0 && (
          <g>
            <rect x={workerX - 70} y={yOf(devs.length) - ROW / 2 - 6} width={190} height={reviewers.length * ROW + 10}
              rx={14} fill="rgba(31,41,55,.18)" stroke="#374151" strokeDasharray="5 5" />
            <text x={workerX - 60} y={yOf(devs.length) - ROW / 2 + 8} fill="var(--t3)"
              style={{ fontSize: 10, letterSpacing: '.08em', textTransform: 'uppercase', fontWeight: 600 }}>
              {reviewers.length > 1 ? 'Reviewers' : 'Reviewer'}
            </text>
          </g>
        )}

        {/* edges */}
        {ordered.map((w, i) => {
          const to = { x: workerX - workerSize / 2 - 4, y: yOf(i) }
          const from = { x: plannerPos.x + plannerSize / 2 + 4, y: plannerPos.y }
          const d = edgePath(from, to)
          const traffic = lastTraffic(w.terminalId)
          return (
            <g key={w.terminalId}>
              <path d={d} fill="none" stroke="var(--border)" strokeWidth={1.5}
                strokeDasharray={w.status === 'IDLE' ? '4 5' : undefined} />
              {traffic && (
                <path d={d} fill="none" strokeWidth={1.5} strokeOpacity={0.3}
                  stroke={traffic === 'out' ? AMBER : EMERALD} />
              )}
            </g>
          )
        })}

        {/* message pulses along the same curves */}
        {fresh.map(p => {
          const out = p.sender === planner.terminalId
          const workerId = out ? p.receiver : p.sender
          const i = ordered.findIndex(w => w.terminalId === workerId)
          if (i < 0) return null
          const wPos = { x: workerX - workerSize / 2 - 4, y: yOf(i) }
          const pPos = { x: plannerPos.x + plannerSize / 2 + 4, y: plannerPos.y }
          const d = out ? edgePath(pPos, wPos) : edgePath({ x: wPos.x, y: wPos.y }, pPos)
          const color = out ? AMBER : EMERALD
          return (
            <g key={p.id}>
              <circle r={4} fill={color}>
                <animateMotion dur="1.9s" fill="freeze" path={d} />
                <animate attributeName="opacity" from="1" to="0" begin="1.7s" dur="0.3s" fill="freeze" />
              </circle>
              <circle r={8} fill="none" stroke={color} strokeOpacity={0.35}>
                <animateMotion dur="1.9s" fill="freeze" path={d} />
                <animate attributeName="opacity" from="0.5" to="0" begin="1.6s" dur="0.3s" fill="freeze" />
              </circle>
            </g>
          )
        })}
      </svg>

      {/* nodes (HTML overlay so avatars are real components) */}
      {member(planner, plannerPos, plannerSize, !mini)}
      {ordered.map((w, i) => member(w, { x: workerX, y: yOf(i) }, workerSize, !mini))}

      {workers.length === 0 && (
        <div className="absolute" style={{ left: plannerPos.x + 40, top: H / 2 - 8, fontSize: 11, color: 'var(--t4)' }}>
          waiting to delegate…
        </div>
      )}
    </div>
  )
}

export function FlowLegend() {
  return (
    <div className="flex items-center gap-4" style={{ fontSize: 11, color: 'var(--t3)' }}>
      <span className="inline-flex items-center gap-1.5">
        <span className="rounded-full" style={{ width: 7, height: 7, background: AMBER }} /> planner delegating
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="rounded-full" style={{ width: 7, height: 7, background: EMERALD }} /> worker reporting back
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="rounded-full" style={{ width: 7, height: 7, background: 'var(--border)' }} /> no traffic yet
      </span>
    </div>
  )
}
