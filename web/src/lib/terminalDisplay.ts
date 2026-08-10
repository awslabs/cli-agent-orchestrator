import type { Annotation, TerminalMeta } from '../api'
import { STATUS_CONFIG, UNKNOWN_CONFIG, type StatusStyle } from '../status.generated'
import { freshness } from './annotations'

/**
 * A render change is useful evidence of activity, but only briefly. It says
 * nothing about durable task completion and must never be used as a deadman.
 */
export const RECENT_RENDER_SECONDS = 30

const MANAGED_STATUS_CONFIG: Record<string, StatusStyle> = {
  MANAGED_ACTIVE: {
    label: 'Managed Active',
    dotClass: 'bg-cao-success',
    bgClass: 'bg-cao-success/10',
    textClass: 'text-cao-success',
    pulse: true,
    explanation: 'The managed pane rendered something new within the last 30 seconds. This is recent activity evidence, not durable task progress.',
  },
  MANAGED_LIVE: {
    label: 'Managed Live',
    dotClass: 'bg-cao-info',
    bgClass: 'bg-cao-info/10',
    textClass: 'text-cao-info',
    explanation: 'The managed pane is available, but no recent rendering activity is proven. It may be thinking, idle, or awaiting its next instruction.',
  },
  MANAGED_PARKED: {
    label: 'Managed Parked',
    dotClass: 'bg-cao-accent',
    bgClass: 'bg-cao-accent/10',
    textClass: 'text-cao-accent',
    explanation: 'The conductor has a current durable checkpoint saying this managed worker is intentionally parked.',
  },
  MANAGED_STALLED: {
    label: 'Managed Stalled',
    dotClass: 'bg-cao-danger',
    bgClass: 'bg-cao-danger/10',
    textClass: 'text-cao-danger',
    explanation: 'Independent quiet clocks contradict a sustained provider working claim. This needs attention; it is not ordinary silence during a short model turn.',
  },
}

export const DISPLAY_STATUS_CONFIG: Record<string, StatusStyle> = {
  ...STATUS_CONFIG,
  ...MANAGED_STATUS_CONFIG,
  UNKNOWN: UNKNOWN_CONFIG,
}

/** Canonical order for the dashboard's headline worker-state vocabulary. */
export const DISPLAY_STATUS_ORDER = [
  'MANAGED_ACTIVE',
  'MANAGED_PARKED',
  'MANAGED_LIVE',
  'PROCESSING',
  'IDLE',
  'WAITING_USER_ANSWER',
  'MANAGED_STALLED',
  'ERROR',
  'COMPLETED',
  'STOPPED',
  'DEAD',
  'SUPERSEDED',
  'UNKNOWN',
]

const RENDERABLE_STATUSES = new Set(DISPLAY_STATUS_ORDER)
const OVERRIDING_RAW_STATUSES = new Set([
  'ERROR',
  'WAITING_USER_ANSWER',
  'STOPPED',
  'DEAD',
  'SUPERSEDED',
])

export interface TerminalDisplayState {
  key: string
  reason: string
}

function rawStatus(raw: string | null | undefined): string {
  const normalized = (raw || 'UNKNOWN').toUpperCase()
  // NOT_FIFO_MONITORED is the wire-level managed-pane state. It deliberately
  // has no separate headline bucket: the richer managed vocabulary below is
  // its presentation replacement.
  if (normalized === 'NOT_FIFO_MONITORED') return normalized
  return RENDERABLE_STATUSES.has(normalized) ? normalized : 'UNKNOWN'
}

function freshWorkItem(annotations: Annotation[] | undefined): Annotation | undefined {
  return annotations
    ?.filter(annotation => (
      annotation.namespace === 'cao.work-state'
      && annotation.kind === 'work-item'
      && freshness(annotation.valid_until) === 'fresh'
    ))
    .sort((a, b) => b.priority - a.priority)[0]
}

function livenessSeconds(terminal: TerminalMeta | undefined): number | null {
  const signal = terminal?.status_signals?.find(item => (
    item.name === 'liveness'
    && item.state === 'available'
    && typeof item.value === 'number'
  ))
  return signal && typeof signal.value === 'number' ? signal.value : null
}

/**
 * Derive the one headline shown on a terminal row.
 *
 * Provider status remains intact in the evidence card. The headline answers a
 * different, operator-facing question for conductor-managed workers: is the
 * pane visibly active, merely available, intentionally parked, or contradicted
 * strongly enough to call stalled?
 */
export function terminalDisplayState(
  raw: string | null | undefined,
  terminal?: TerminalMeta,
  annotations?: Annotation[],
): TerminalDisplayState {
  const reported = rawStatus(raw)
  const workItem = freshWorkItem(annotations)
  const managed = reported === 'NOT_FIFO_MONITORED' || workItem !== undefined

  if (terminal?.wedged && managed) {
    return {
      key: 'MANAGED_STALLED',
      reason: 'The terminal projection joined a sustained working claim with independent render and input silence.',
    }
  }

  if (OVERRIDING_RAW_STATUSES.has(reported)) {
    return { key: reported, reason: 'An explicit provider or terminal-lifecycle state outranks managed activity presentation.' }
  }

  if (reported === 'UNKNOWN' && terminal?.lifecycle_state !== 'live') {
    return { key: 'UNKNOWN', reason: 'The terminal is not proven live, so managed activity cannot be derived.' }
  }

  const phase = workItem?.details?.phase?.trim().toLowerCase()
  if (phase === 'parked') {
    return {
      key: 'MANAGED_PARKED',
      reason: 'A fresh conductor work-state record says this worker is parked; that durable checkpoint outranks incidental redraws.',
    }
  }

  if (!managed) {
    return { key: reported, reason: 'No current conductor-managed work binding changes the provider status presentation.' }
  }

  const quietFor = livenessSeconds(terminal)
  if (quietFor !== null && quietFor <= RECENT_RENDER_SECONDS) {
    return {
      key: 'MANAGED_ACTIVE',
      reason: `The pane rendered something new within ${quietFor}s (active window: ${RECENT_RENDER_SECONDS}s).`,
    }
  }

  return {
    key: 'MANAGED_LIVE',
    reason: quietFor === null
      ? 'The managed pane is live, but no comparable rendering sample is available yet.'
      : `The managed pane is live, with no rendering change for ${quietFor}s (active window: ${RECENT_RENDER_SECONDS}s).`,
  }
}

/** A work round being open is not proof that the model is active right now. */
export function annotationDisplayLabel(annotation: Annotation): string {
  if (
    annotation.namespace === 'cao.work-state'
    && annotation.kind === 'work-item'
    && annotation.details?.phase?.trim().toLowerCase() === 'in-round'
    && annotation.label.trim().toLowerCase() === 'active'
  ) {
    return 'round open'
  }
  return annotation.label
}
