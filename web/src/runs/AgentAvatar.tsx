import { Crown, Code, ClipboardCheck, Bot } from 'lucide-react'
import { statusStyle } from './StatusBadge'

/**
 * Agent avatar (Build Spec §1.5): #1c1c26 disc, 2px border in the agent's
 * STATUS color, role icon in --t2. Reviewers are the squared shape — that,
 * not a new color, is how they read differently from developers.
 * Pulsing statuses (PROCESSING / WAITING_USER_ANSWER) get the expanding
 * ring in the status color at 45% alpha.
 */
export function AgentAvatar({ role, status, size = 38 }: {
  role: string | null | undefined
  status: string | null | undefined
  size?: number
}) {
  const style = statusStyle(status || 'UNKNOWN')
  const r = (role || '').toLowerCase()
  const isReviewer = r.includes('review')
  const isPlanner = r.includes('supervisor') || r.includes('planner')
  const Icon = isPlanner ? Crown : isReviewer ? ClipboardCheck : r.includes('dev') ? Code : Bot
  const iconSize = Math.round(size * 0.44)

  return (
    <span
      className={`inline-flex items-center justify-center shrink-0 ${style.pulse ? 'ring-pulse' : ''}`}
      style={{
        width: size,
        height: size,
        background: '#1c1c26',
        border: `2px solid ${style.hex}`,
        borderRadius: isReviewer ? `${Math.round(size * 0.26)}px` : '9999px',
        ['--ring-color' as string]: `${style.hex}73`, // 45% alpha
      }}
      title={role || 'agent'}
    >
      <Icon size={iconSize} style={{ color: 'var(--t2)' }} />
    </span>
  )
}
