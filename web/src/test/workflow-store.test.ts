import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useStore } from '../store'
import { api } from '../api'
import type { WorkflowEvent, GapMarker } from '../api'

function ev(seq: number, event_type = 'step.started'): WorkflowEvent {
  return { run_id: 'r1', seq, event_type, event_schema_version: 1, ts: '' }
}

describe('workflowRuns store slice (#504 / U8)', () => {
  beforeEach(() => {
    useStore.setState({
      workflowRuns: [],
      selectedRun: null,
      wfEvents: [],
      wfGaps: [],
      selectedIndex: 0,
      followConnected: false,
      snackbar: null,
    })
  })

  it('has additive workflow initial state without disturbing existing slices', () => {
    const s = useStore.getState()
    expect(s.workflowRuns).toEqual([])
    expect(s.wfEvents).toEqual([])
    expect(s.wfGaps).toEqual([])
    expect(s.selectedIndex).toBe(0)
    expect(s.followConnected).toBe(false)
    // Existing slice still present.
    expect(s.sessions).toBeDefined()
  })

  it('appends events dedup-by-seq and keeps them seq-ordered', () => {
    const { appendWorkflowEvent } = useStore.getState()
    appendWorkflowEvent(ev(2))
    appendWorkflowEvent(ev(1))
    appendWorkflowEvent(ev(2)) // duplicate seq — ignored
    const seqs = useStore.getState().wfEvents.map(e => e.seq)
    expect(seqs).toEqual([1, 2])
  })

  it('adds a declared gap and dedupes on the (after,before) span', () => {
    const { addWorkflowGap } = useStore.getState()
    const g: GapMarker = { after_seq: 3, before_seq: 7, missing_count: 3, reason: 'x' }
    addWorkflowGap(g)
    addWorkflowGap({ ...g }) // same span — ignored
    expect(useStore.getState().wfGaps.length).toBe(1)
  })

  it('clamps setSelectedIndex to the events range', () => {
    useStore.setState({ wfEvents: [ev(1), ev(2), ev(3)] })
    const { setSelectedIndex } = useStore.getState()
    setSelectedIndex(99)
    expect(useStore.getState().selectedIndex).toBe(2)
    setSelectedIndex(-5)
    expect(useStore.getState().selectedIndex).toBe(0)
  })

  it('clearSelectedRun resets the playback view', () => {
    useStore.setState({
      selectedRun: { run_id: 'r1', workflow_name: 'w', state: 'running', started_at: '', tier: 'yaml', steps: [] },
      wfEvents: [ev(1)],
      wfGaps: [{ after_seq: 1, before_seq: 3, missing_count: 1, reason: 'x' }],
      selectedIndex: 0,
      followConnected: true,
    })
    useStore.getState().clearSelectedRun()
    const s = useStore.getState()
    expect(s.selectedRun).toBeNull()
    expect(s.wfEvents).toEqual([])
    expect(s.wfGaps).toEqual([])
    expect(s.followConnected).toBe(false)
  })

  it('setFollowConnected toggles the live-follow flag', () => {
    useStore.getState().setFollowConnected(true)
    expect(useStore.getState().followConnected).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// PR #526 review round 3 — IMPORTANT: selectWorkflowRun had no request-generation
// guard. It reset state, awaited Promise.all([...]), then set(...) unconditionally,
// so selecting run A then quickly run B let whichever fetch RESOLVED LAST win. A's
// stale response could overwrite B's, leaving the detail pane and the live-follow
// keyed to different runs.
//
// `selectedRunId` is the token: set synchronously before the awaits, compared after.
// ---------------------------------------------------------------------------
describe('selectWorkflowRun stale-response guard (#526 review round 3)', () => {
  let deferred: Record<string, { resolve: (v: any) => void; reject: (e: any) => void }>

  function inspection(run_id: string) {
    return { run_id, workflow_name: 'wf', state: 'completed', steps: [] } as any
  }

  beforeEach(() => {
    useStore.setState({
      workflowRuns: [],
      selectedRun: null,
      selectedRunId: null,
      wfEvents: [],
      wfGaps: [],
      selectedIndex: 0,
      followConnected: false,
      snackbar: null,
    })
    deferred = {}
    // Hand-controlled promises per run id so resolution ORDER is the variable under
    // test — a timer-based fake would make the race non-deterministic.
    vi.spyOn(api, 'inspectWorkflowRun').mockImplementation(
      (runId: string) =>
        new Promise((resolve, reject) => {
          deferred[runId] = { resolve: v => resolve(v ?? inspection(runId)), reject }
        }) as any,
    )
    vi.spyOn(api, 'getWorkflowRunEvents').mockImplementation(
      async () => ({ events: [], gaps: [], next_after_seq: null }) as any,
    )
  })

  afterEach(() => vi.restoreAllMocks())

  it('discards run A’s response when run B was selected after it', async () => {
    const { selectWorkflowRun } = useStore.getState()
    const pA = selectWorkflowRun('A')
    const pB = selectWorkflowRun('B')

    // B resolves first, then the STALE A arrives.
    deferred['B'].resolve(inspection('B'))
    await pB
    deferred['A'].resolve(inspection('A'))
    await pA

    expect(useStore.getState().selectedRunId).toBe('B')
    expect(useStore.getState().selectedRun?.run_id).toBe('B')
  })

  it('discards a stale FAILURE so it cannot blank the newer run (error path)', async () => {
    const { selectWorkflowRun } = useStore.getState()
    const pA = selectWorkflowRun('A')
    const pB = selectWorkflowRun('B')

    deferred['B'].resolve(inspection('B'))
    await pB
    // A fails AFTER B loaded. Unguarded, the catch clears selectedRun and fires a
    // snackbar — blanking B's pane and blaming it for A's error.
    deferred['A'].reject(new Error('A exploded'))
    await pA

    expect(useStore.getState().selectedRun?.run_id).toBe('B')
    expect(useStore.getState().snackbar).toBeNull()
  })

  it('still applies the response when the selection did NOT change', async () => {
    // The guard must not be so eager that nothing ever loads — the failure mode of
    // comparing against `selectedRun?.run_id` (null mid-flight) instead of the token.
    const { selectWorkflowRun } = useStore.getState()
    const p = selectWorkflowRun('solo')
    deferred['solo'].resolve(inspection('solo'))
    await p

    expect(useStore.getState().selectedRun?.run_id).toBe('solo')
  })

  it('clearSelectedRun drops the token so an in-flight fetch cannot repopulate', async () => {
    const { selectWorkflowRun, clearSelectedRun } = useStore.getState()
    const p = selectWorkflowRun('A')
    clearSelectedRun()
    deferred['A'].resolve(inspection('A'))
    await p

    expect(useStore.getState().selectedRunId).toBeNull()
    expect(useStore.getState().selectedRun).toBeNull()
  })
})

describe('appendWorkflowEvent ordered insert (#526 review round 3)', () => {
  beforeEach(() => {
    useStore.setState({ wfEvents: [], wfGaps: [], selectedIndex: 0 })
  })

  it('keeps events seq-ordered when frames arrive out of order', () => {
    const { appendWorkflowEvent } = useStore.getState()
    // Deliberately adversarial order for an insert-scanning-backwards impl.
    for (const seq of [3, 1, 5, 2, 4]) appendWorkflowEvent(ev(seq))
    expect(useStore.getState().wfEvents.map(e => e.seq)).toEqual([1, 2, 3, 4, 5])
  })

  it('appends an in-order tail frame (the common SSE case) correctly', () => {
    const { appendWorkflowEvent } = useStore.getState()
    for (const seq of [1, 2, 3]) appendWorkflowEvent(ev(seq))
    expect(useStore.getState().wfEvents.map(e => e.seq)).toEqual([1, 2, 3])
  })

  it('inserts a lower seq at the FRONT, not the end', () => {
    const { appendWorkflowEvent } = useStore.getState()
    appendWorkflowEvent(ev(9))
    appendWorkflowEvent(ev(2))
    expect(useStore.getState().wfEvents.map(e => e.seq)).toEqual([2, 9])
  })
})
