import { describe, it, expect } from 'vitest'
import { derivedStepState } from '../components/workflow/derivedStepState'
import type { WorkflowEvent } from '../api'

// Minimal event builder — only the fields the fold reads.
let seqCounter = 0
function ev(
  event_type: string,
  step_id: string | null,
  extra: Partial<WorkflowEvent> = {},
): WorkflowEvent {
  return {
    run_id: 'r1',
    seq: extra.seq ?? ++seqCounter,
    event_type,
    event_schema_version: 1,
    ts: '2026-07-27T00:00:00Z',
    step_id,
    ...extra,
  }
}

describe('derivedStepState fold', () => {
  it('folds a happy-path run to completed states at the final index', () => {
    const events: WorkflowEvent[] = [
      ev('run.started', null, { seq: 1 }),
      ev('step.started', 'a', { seq: 2, state: 'running' }),
      ev('step.completed', 'a', { seq: 3, state: 'completed' }),
      ev('step.started', 'b', { seq: 4, state: 'running' }),
      ev('step.completed', 'b', { seq: 5, state: 'completed' }),
    ]
    const map = derivedStepState(events, events.length - 1)
    expect(map).toEqual({ a: 'completed', b: 'completed' })
  })

  it('is deterministic: same index always yields the same map', () => {
    const events: WorkflowEvent[] = [
      ev('step.started', 'a', { seq: 1, state: 'running' }),
      ev('step.attempt.failed', 'a', { seq: 2, error_kind: 'timeout' }),
      ev('step.completed', 'a', { seq: 3, state: 'completed' }),
    ]
    const first = derivedStepState(events, 1)
    const second = derivedStepState(events, 1)
    expect(first).toEqual(second)
    // A partial index shows the recovered-but-not-settled step as retried.
    expect(first).toEqual({ a: 'retried' })
  })

  it('orders by seq, not array position or ts (out-of-order input)', () => {
    // ts is intentionally NON-monotonic vs seq; the fold must honour seq.
    const events: WorkflowEvent[] = [
      ev('step.completed', 'a', { seq: 3, state: 'completed', ts: '2000-01-01T00:00:00Z' }),
      ev('step.started', 'a', { seq: 1, state: 'running', ts: '2030-01-01T00:00:00Z' }),
    ]
    // At index 0 of the SEQ-ordered stream, only seq=1 (step.started) applies.
    expect(derivedStepState(events, 0)).toEqual({ a: 'running' })
    // At the final index the terminal completed (seq=3) wins.
    expect(derivedStepState(events, 1)).toEqual({ a: 'completed' })
  })

  it('marks a reprompted step as retried', () => {
    const events: WorkflowEvent[] = [
      ev('step.started', 'a', { seq: 1, state: 'running' }),
      ev('step.reprompted', 'a', { seq: 2, reason: 'invalid_or_missing_output' }),
    ]
    expect(derivedStepState(events, 1)).toEqual({ a: 'retried' })
  })

  it('settles still-open steps to cancelled on a run.cancelled event', () => {
    const events: WorkflowEvent[] = [
      ev('step.started', 'a', { seq: 1, state: 'running' }),
      ev('step.completed', 'a', { seq: 2, state: 'completed' }),
      ev('step.started', 'b', { seq: 3, state: 'running' }),
      ev('run.cancelled', null, { seq: 4, state: 'cancelled' }),
    ]
    // Completed step stays completed; the open step becomes cancelled.
    expect(derivedStepState(events, 3)).toEqual({ a: 'completed', b: 'cancelled' })
  })

  it('an earlier index reflects only the prefix (scrub back in time)', () => {
    const events: WorkflowEvent[] = [
      ev('step.started', 'a', { seq: 1, state: 'running' }),
      ev('step.completed', 'a', { seq: 2, state: 'completed' }),
    ]
    expect(derivedStepState(events, 0)).toEqual({ a: 'running' })
    expect(derivedStepState(events, 1)).toEqual({ a: 'completed' })
  })

  it('a step.failed marks the step failed', () => {
    const events: WorkflowEvent[] = [
      ev('step.started', 'a', { seq: 1, state: 'running' }),
      ev('step.attempt.failed', 'a', { seq: 2, error_kind: 'crash' }),
      ev('step.failed', 'a', { seq: 3, state: 'failed' }),
    ]
    expect(derivedStepState(events, 2)).toEqual({ a: 'failed' })
  })

  it('returns an empty map for a negative index', () => {
    const events: WorkflowEvent[] = [ev('step.started', 'a', { seq: 1 })]
    expect(derivedStepState(events, -1)).toEqual({})
  })

  it('does not mutate the caller array when sorting by seq', () => {
    const events: WorkflowEvent[] = [
      ev('step.completed', 'a', { seq: 2, state: 'completed' }),
      ev('step.started', 'a', { seq: 1, state: 'running' }),
    ]
    const snapshot = events.map(e => e.seq)
    derivedStepState(events, 1)
    expect(events.map(e => e.seq)).toEqual(snapshot)
  })
})
