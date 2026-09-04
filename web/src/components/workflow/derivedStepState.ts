// derivedStepState — the client-side playback fold (FR-7.1, #504 / U8).
//
// Playback is a PURE PROJECTION over the durable event timeline (U3 replay +
// U4 follow), never a server re-execution. `derivedStepState(events, index)`
// folds the events at seq-ordered positions 0..index (inclusive) into a map
// {step_id -> DerivedStepState}. Deterministic: the same (events, index) always
// yields the same map. Ordering is by `seq` — the sole ordering authority —
// NEVER by `ts` (ts is display/duration only and can be non-monotonic).

import type { WorkflowEvent } from '../../api'

/**
 * The seven derived per-step states. `pending`/`running`/`completed`/`failed`/
 * `skipped` mirror the engine's StepState; `retried` and `cancelled` are
 * DERIVED at fold time (the engine has no such StepState — a retry is inferred
 * from `step.reprompted` / `step.attempt.failed`, a cancellation from the
 * run-level `run.cancelled` event settling still-open steps).
 */
export type DerivedStepState =
  | 'pending'
  | 'running'
  | 'completed'
  | 'retried'
  | 'skipped'
  | 'failed'
  | 'cancelled'

const TERMINAL_STATES: ReadonlySet<DerivedStepState> = new Set([
  'completed',
  'failed',
  'skipped',
  'cancelled',
])

// Engine StepState string values (`state` field on an event) -> derived state.
// `completed_unvalidated` collapses to `completed` (both are settled, non-failure).
const ENGINE_STATE_MAP: Record<string, DerivedStepState> = {
  pending: 'pending',
  running: 'running',
  completed: 'completed',
  completed_unvalidated: 'completed',
  failed: 'failed',
  skipped: 'skipped',
}

/**
 * Fold a run's event timeline into per-step derived states as of position
 * `index` (inclusive, seq-ordered). `index < 0` yields an empty map.
 *
 * The fold is last-relevant-event-wins per step, so it is a deterministic
 * function of the ordered prefix. A step that has retried stays `retried`
 * across subsequent in-progress events (sticky) until it settles into a
 * terminal state — so "retried" reads as "running, but recovered from at least
 * one failed attempt / reprompt" rather than a one-frame flicker. A run-level
 * `run.cancelled` settles every still-open (non-terminal) step to `cancelled`.
 */
export function derivedStepState(
  events: WorkflowEvent[],
  index: number,
): Record<string, DerivedStepState> {
  // Order by seq (never ts). Sort a copy so the caller's array is untouched and
  // the fold is correct even if a producer handed us out-of-order rows.
  const ordered = [...events].sort((a, b) => a.seq - b.seq)
  const upper = Math.min(index, ordered.length - 1)

  const stateByStep: Record<string, DerivedStepState> = {}
  const retried = new Set<string>()

  for (let i = 0; i <= upper; i++) {
    const ev = ordered[i]

    // Run-level cancellation settles every still-open step to `cancelled`.
    if (ev.event_type === 'run.cancelled') {
      for (const stepId of Object.keys(stateByStep)) {
        if (!TERMINAL_STATES.has(stateByStep[stepId])) {
          stateByStep[stepId] = 'cancelled'
        }
      }
      continue
    }

    const stepId = ev.step_id
    if (!stepId) continue // run-level / non-step event: no per-step transition

    // A retry signal marks the step; it stays sticky over later in-progress
    // events until the step reaches a terminal state.
    if (ev.event_type === 'step.reprompted' || ev.event_type === 'step.attempt.failed') {
      retried.add(stepId)
      stateByStep[stepId] = 'retried'
      continue
    }

    // Terminal step transitions — authoritative, clear the retry stickiness.
    if (ev.event_type === 'step.completed') {
      const settled = (ev.state && ENGINE_STATE_MAP[ev.state]) || 'completed'
      stateByStep[stepId] = settled
      continue
    }
    if (ev.event_type === 'step.failed') {
      stateByStep[stepId] = 'failed'
      continue
    }
    if (ev.event_type === 'step.skipped') {
      stateByStep[stepId] = 'skipped'
      continue
    }

    // In-progress / start events: prefer the explicit engine `state` when it is
    // a recognized value, else fall back to the event-type default (running).
    const fromState = ev.state ? ENGINE_STATE_MAP[ev.state] : undefined
    let next: DerivedStepState = fromState ?? 'running'
    // A recovered step that is running again reads as `retried` (sticky) unless
    // the engine state says it is already settled.
    if (next === 'running' && retried.has(stepId)) next = 'retried'
    stateByStep[stepId] = next
  }

  return stateByStep
}
