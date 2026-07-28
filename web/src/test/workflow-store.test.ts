import { describe, it, expect, beforeEach } from 'vitest'
import { useStore } from '../store'
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
