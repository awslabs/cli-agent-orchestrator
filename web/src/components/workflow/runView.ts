// runView: pure helpers for the run view. They read a run's events and its
// inspection and never fetch, so the components that use them stay thin and
// the rules are tested in one place.
//
// Events are read in `seq` order, the sole ordering authority. Their `ts` is a
// second-precision UTC time and is used only for clocks and durations.

import type { WorkflowEvent } from '../../api'
import { parseServerTime } from '../../time'

/** Step states that mean the step is in progress. `retried` is the fold's "running again after a failed attempt". */
export const ACTIVE_STEP_STATES: ReadonlySet<string> = new Set(['running', 'retried'])

export type StepActivity = 'pending' | 'running' | 'settled'

/**
 * Whether a step has not started, is in progress, or has settled. Once the run
 * has ended no step is in progress, whatever an older snapshot of it says, so
 * nothing polls a terminal that is gone. An unknown state counts as settled.
 */
export function stepActivity(state: string | null | undefined, runEnded: boolean): StepActivity {
  const key = (state || 'pending').toLowerCase()
  if (key === 'pending') return 'pending'
  if (ACTIVE_STEP_STATES.has(key)) return runEnded ? 'settled' : 'running'
  return 'settled'
}

/** An event time in ms since the epoch, or null when it is missing or unreadable. */
export function eventTimeMs(ts: string | null | undefined): number | null {
  if (!ts) return null
  const ms = parseServerTime(ts).getTime()
  return Number.isFinite(ms) ? ms : null
}

/** The events in seq order. The store keeps them sorted, so this copies only when they are not. */
export function bySeq(events: WorkflowEvent[]): WorkflowEvent[] {
  for (let i = 1; i < events.length; i++) {
    if (events[i - 1].seq > events[i].seq) return [...events].sort((a, b) => a.seq - b.seq)
  }
  return events
}

/**
 * When each step's latest `step.started` was recorded, in ms since the epoch.
 * A resumed run starts a step again, and the latest start is the one that is
 * running now.
 */
export function latestStepStarts(events: WorkflowEvent[]): Record<string, number> {
  const out: Record<string, number> = {}
  for (const ev of bySeq(events)) {
    if (ev.event_type !== 'step.started' || !ev.step_id) continue
    const ms = eventTimeMs(ev.ts)
    if (ms != null) out[ev.step_id] = ms
    else delete out[ev.step_id]
  }
  return out
}

/**
 * The terminal a step's view reads.
 *
 * A `terminal.created` event after the step's latest `step.started` wins: it
 * arrives on the live stream, before the next inspection poll, and it names a
 * retry's new terminal. While the step runs, a start with no terminal after it
 * means there is no terminal for this start yet, so an id left over from an
 * earlier start is not polled. Otherwise, and when the events show no start
 * for the step (a script run records no events), the inspection's id is used.
 * The routes read the terminal from the journal, so this id only decides what
 * the view asks for.
 */
export function stepTerminalId(
  events: WorkflowEvent[],
  stepId: string,
  inspected: string | null | undefined,
  activity: StepActivity,
): string | null {
  let started = false
  let fromEvents: string | null = null
  for (const ev of bySeq(events)) {
    if (ev.step_id !== stepId) continue
    if (ev.event_type === 'step.started') {
      started = true
      fromEvents = null
    } else if (ev.event_type === 'terminal.created' && ev.terminal_id) {
      fromEvents = ev.terminal_id
    }
  }
  if (activity === 'running' && started) return fromEvents
  return fromEvents ?? inspected ?? null
}

/** A duration in whole seconds, as "12 s", "2 min 5 s" or "1 h 4 min". */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  if (s < 60) return `${s} s`
  if (s < 3600) {
    const m = Math.floor(s / 60)
    const rest = s % 60
    return rest === 0 ? `${m} min` : `${m} min ${rest} s`
  }
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}

/** Whole seconds from `startMs` to `endMs`, or null when either is unknown or the end is earlier. */
function secondsBetween(startMs: number | null | undefined, endMs: number | null): number | null {
  if (startMs == null || endMs == null || endMs < startMs) return null
  return Math.round((endMs - startMs) / 1000)
}

/** An event time as a local clock time, or '' when it is missing or unreadable. */
export function formatLocalTime(ts: string | null | undefined): string {
  const ms = eventTimeMs(ts)
  if (ms == null) return ''
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/**
 * The outcome an outcome event records, as a step state key for its badge, or
 * null for an event that records no outcome. The badge comes from the event's
 * type, not from its `state` field: a `step.attempt.failed` row records the
 * step as still running, which is what made the old timeline read wrong.
 */
export function eventOutcome(ev: WorkflowEvent): string | null {
  switch (ev.event_type) {
    case 'step.completed':
      return ev.state === 'completed_unvalidated' ? 'completed_unvalidated' : 'completed'
    case 'step.attempt.failed':
    case 'step.failed':
    case 'run.failed':
      return 'failed'
    case 'step.skipped':
      return 'skipped'
    case 'run.completed':
      return 'completed'
    case 'run.cancelled':
      return 'cancelled'
    default:
      return null
  }
}

const RUN_OUTCOME_WORDS: Record<string, string> = {
  'run.completed': 'completed',
  'run.failed': 'failed',
  'run.cancelled': 'cancelled',
}

/**
 * A plain label for each event, in the order given (seq order).
 *
 * A step's duration runs from its latest `step.started` before the outcome. A
 * run's duration runs from its latest `run.started`, or from `runStartedAt`
 * when there is none: a run started from the web UI records no `run.started`
 * event. A run outcome after an earlier one (a resumed run) has no known
 * start. Without a known start the label has no duration.
 */
export function eventLabels(events: WorkflowEvent[], runStartedAt?: string | null): string[] {
  const stepStarts = new Map<string, number | null>()
  let runStart: number | null = eventTimeMs(runStartedAt)
  return events.map(ev => {
    const at = eventTimeMs(ev.ts)
    const attempt = ev.attempt != null ? `Attempt ${ev.attempt}` : 'Attempt'
    switch (ev.event_type) {
      case 'step.started':
        if (ev.step_id) stepStarts.set(ev.step_id, at)
        return 'Step started'
      case 'step.attempt.started':
        return `${attempt} started`
      case 'terminal.created':
        return 'Terminal opened'
      case 'step.attempt.failed':
        return ev.error_kind ? `${attempt} failed (${ev.error_kind})` : `${attempt} failed`
      case 'step.completed': {
        const s = secondsBetween(ev.step_id ? stepStarts.get(ev.step_id) : null, at)
        return s != null ? `Step completed in ${formatDuration(s)}` : 'Step completed'
      }
      case 'step.failed':
        return 'Step failed'
      case 'step.skipped':
        return ev.reason ? `Step skipped (${ev.reason})` : 'Step skipped'
      case 'run.started':
        if (at != null) runStart = at
        return ev.event_type
      case 'run.completed':
      case 'run.failed':
      case 'run.cancelled': {
        const word = RUN_OUTCOME_WORDS[ev.event_type]
        const s = secondsBetween(runStart, at)
        // A resume records no run.started, so a later outcome has no known
        // start: counting from the first start would include the time the run
        // sat stopped.
        runStart = null
        return s != null ? `Run ${word} in ${formatDuration(s)}` : `Run ${word}`
      }
      default:
        return ev.event_type
    }
  })
}
