import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { RunList } from '../components/workflow/RunList'
import { WorkflowTimeline } from '../components/workflow/WorkflowTimeline'
import {
  SyncedTerminalPane,
  hasTerminalOffsets,
  canTailTerminal,
  TAIL_MAX_CHARS,
} from '../components/workflow/SyncedTerminalPane'
import { DeleteRunButton } from '../components/workflow/DeleteRunButton'
import { WorkflowsPanel } from '../components/WorkflowsPanel'
import { RunDetail } from '../components/workflow/RunDetail'
import { api } from '../api'
import { useStore } from '../store'
import type { RunSummaryRow, WorkflowEvent, GapMarker } from '../api'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const sampleRun: RunSummaryRow = {
  run_id: 'run-abc-123',
  workflow_name: 'my-workflow',
  state: 'completed',
  tier: 'yaml',
  started_at: '2026-07-27T00:00:00Z',
  finished_at: '2026-07-27T00:01:00Z',
  current_step_id: null,
}

describe('RunList', () => {
  it('renders the loading state', () => {
    render(<RunList runs={[]} selectedRunId={null} loading={true} error={null} onSelect={() => {}} />)
    expect(screen.getByText(/loading runs/i)).toBeInTheDocument()
  })

  it('renders the empty state (distinct from loading/error)', () => {
    render(<RunList runs={[]} selectedRunId={null} loading={false} error={null} onSelect={() => {}} />)
    expect(screen.getByTestId('runlist-empty')).toBeInTheDocument()
    expect(screen.getByText(/no workflow runs yet/i)).toBeInTheDocument()
  })

  it('renders the error state', () => {
    render(<RunList runs={[]} selectedRunId={null} loading={false} error="boom" onSelect={() => {}} />)
    expect(screen.getByRole('alert')).toHaveTextContent('boom')
  })

  it('renders a row and fires onSelect with the run id', () => {
    const onSelect = vi.fn()
    render(<RunList runs={[sampleRun]} selectedRunId={null} loading={false} error={null} onSelect={onSelect} />)
    expect(screen.getByText('my-workflow')).toBeInTheDocument()
    fireEvent.click(screen.getByText('my-workflow'))
    expect(onSelect).toHaveBeenCalledWith('run-abc-123')
  })
})

describe('WorkflowTimeline', () => {
  const events: WorkflowEvent[] = [
    { run_id: 'r', seq: 1, event_type: 'step.started', event_schema_version: 1, ts: '', step_id: 'a', state: 'running' },
    { run_id: 'r', seq: 5, event_type: 'step.completed', event_schema_version: 1, ts: '', step_id: 'a', state: 'completed' },
  ]

  it('renders a DECLARED gap as a hatched segment, distinct from empty', () => {
    const gaps: GapMarker[] = [{ after_seq: 1, before_seq: 5, missing_count: 3, reason: 'append_swallowed' }]
    render(<WorkflowTimeline events={events} gaps={gaps} selectedIndex={0} onSelectIndex={() => {}} />)
    const gap = screen.getByTestId('timeline-gap')
    expect(gap).toBeInTheDocument()
    expect(gap).toHaveTextContent(/3 missing/i)
    expect(gap).toHaveTextContent(/append_swallowed/)
    // NOT the empty state.
    expect(screen.queryByTestId('timeline-empty')).not.toBeInTheDocument()
  })

  it('renders the empty state when there are no events (distinct from a gap)', () => {
    render(<WorkflowTimeline events={[]} gaps={[]} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.getByTestId('timeline-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('timeline-gap')).not.toBeInTheDocument()
  })

  it('has an ARIA slider (scrubber) and an aria-live region for the selected event', () => {
    render(<WorkflowTimeline events={events} gaps={[]} selectedIndex={0} onSelectIndex={() => {}} />)
    const slider = screen.getByRole('slider', { name: /playback position/i })
    expect(slider).toHaveAttribute('aria-valuenow', '0')
    expect(screen.getByTestId('timeline-live')).toHaveTextContent(/step\.started/)
  })

  it('scrubber ArrowRight advances the selected index', () => {
    const onSelectIndex = vi.fn()
    render(<WorkflowTimeline events={events} gaps={[]} selectedIndex={0} onSelectIndex={onSelectIndex} />)
    fireEvent.keyDown(screen.getByRole('slider', { name: /playback position/i }), { key: 'ArrowRight' })
    expect(onSelectIndex).toHaveBeenCalledWith(1)
  })
})

describe('SyncedTerminalPane — null-offset graceful-degrade seam (mutation guard)', () => {
  const withOffsets: WorkflowEvent = {
    run_id: 'r', seq: 3, event_type: 'step.output.received', event_schema_version: 1, ts: '',
    step_id: 'a', terminal_id: 't1', terminal_offset_start: 10, terminal_offset_len: 40,
  }
  const nullOffsets: WorkflowEvent = {
    run_id: 'r', seq: 2, event_type: 'step.started', event_schema_version: 1, ts: '',
    step_id: 'a', terminal_id: 't1', terminal_offset_start: null, terminal_offset_len: null,
  }

  it('hasTerminalOffsets: true only when terminal_id AND both offsets are non-null', () => {
    expect(hasTerminalOffsets(withOffsets)).toBe(true)
    expect(hasTerminalOffsets(nullOffsets)).toBe(false)
    expect(hasTerminalOffsets(null)).toBe(false)
  })

  it('BRANCH 1 — offsets present: fetches the U5 range API and shows the output', async () => {
    const spy = vi
      .spyOn(api, 'getTerminalOutputRange')
      .mockResolvedValue({ terminal_id: 't1', offset: 10, length: 40, data: 'captured output here' })
    render(<SyncedTerminalPane event={withOffsets} />)
    await waitFor(() => expect(screen.getByText('captured output here')).toBeInTheDocument())
    expect(spy).toHaveBeenCalledWith('t1', 10, 40)
    // NOT the degrade state.
    expect(screen.queryByText(/sync pending/i)).not.toBeInTheDocument()
  })

  it('BRANCH 2 — NULL offsets: never calls the offset-exact RANGE api', async () => {
    const rangeSpy = vi.spyOn(api, 'getTerminalOutputRange')
    vi.spyOn(api, 'getTerminalOutput').mockResolvedValue({ output: 'tail', mode: 'full' })
    render(<SyncedTerminalPane event={nullOffsets} />)
    await waitFor(() => expect(screen.getByText('tail')).toBeInTheDocument())
    // The range API is NOT called when offsets are null — there is no window.
    expect(rangeSpy).not.toHaveBeenCalled()
  })

  it('BRANCH 3 — no selected event: shows the degrade state, no crash', () => {
    render(<SyncedTerminalPane event={null} />)
    expect(screen.getByText(/sync pending/i)).toBeInTheDocument()
    // No issue link: the pane used to cite a fabricated issue URL (wrong org,
    // non-existent number), so the degrade state is plain prose.
    expect(screen.queryByRole('link')).toBeNull()
  })
})

// PR #526 human review — IMPORTANT: the PR body claimed an 8 KiB terminal tail
// fallback on the NULL-offset branch, but the branch rendered a static "sync
// pending" message and fetched nothing. Rather than editing the body down, the
// fallback is implemented — so these tests pin the behaviour the body describes.
describe('SyncedTerminalPane — NULL-offset TAIL fallback', () => {
  const nullOffsetsWithTerminal: WorkflowEvent = {
    run_id: 'r', seq: 2, event_type: 'step.started', event_schema_version: 1, ts: '',
    step_id: 'a', terminal_id: 't1', terminal_offset_start: null, terminal_offset_len: null,
  }
  const noTerminal: WorkflowEvent = {
    run_id: 'r', seq: 4, event_type: 'run.started', event_schema_version: 1, ts: '',
    step_id: null, terminal_id: null, terminal_offset_start: null, terminal_offset_len: null,
  }
  const withOffsets: WorkflowEvent = {
    run_id: 'r', seq: 3, event_type: 'step.output.received', event_schema_version: 1, ts: '',
    step_id: 'a', terminal_id: 't1', terminal_offset_start: 10, terminal_offset_len: 40,
  }

  it('canTailTerminal: true only when a terminal_id exists WITHOUT offsets', () => {
    expect(canTailTerminal(nullOffsetsWithTerminal)).toBe(true)
    expect(canTailTerminal(noTerminal)).toBe(false)
    expect(canTailTerminal(null)).toBe(false)
    // With offsets present it is branch 1's job, not the fallback's.
    expect(canTailTerminal(withOffsets)).toBe(false)
  })

  it('fetches the terminal tail and RENDERS it when offsets are null', async () => {
    const spy = vi
      .spyOn(api, 'getTerminalOutput')
      .mockResolvedValue({ output: 'recent terminal tail text', mode: 'full' })

    render(<SyncedTerminalPane event={nullOffsetsWithTerminal} />)

    await waitFor(() =>
      expect(screen.getByText('recent terminal tail text')).toBeInTheDocument()
    )
    expect(spy).toHaveBeenCalledWith('t1')
    // It is no longer the do-nothing pending state.
    expect(screen.queryByText(/sync pending/i)).not.toBeInTheDocument()
  })

  it('labels the tail as NOT synced to the event (the degrade must be visible)', async () => {
    vi.spyOn(api, 'getTerminalOutput').mockResolvedValue({ output: 'x', mode: 'full' })
    render(<SyncedTerminalPane event={nullOffsetsWithTerminal} />)
    await waitFor(() => expect(screen.getByText('x')).toBeInTheDocument())

    const notice = screen.getByTestId('terminal-tail-notice')
    expect(notice).toHaveTextContent(/recent tail \(not synced to this event\)/i)
    // And it must NOT claim a byte range it does not have.
    expect(screen.queryByText(/^bytes /)).not.toBeInTheDocument()
  })

  it('caps the rendered tail at TAIL_MAX_CHARS, keeping the END of the output', async () => {
    const long = 'A'.repeat(TAIL_MAX_CHARS) + 'TAIL_END'
    vi.spyOn(api, 'getTerminalOutput').mockResolvedValue({ output: long, mode: 'full' })

    render(<SyncedTerminalPane event={nullOffsetsWithTerminal} />)

    const pre = await waitFor(() => screen.getByText(/TAIL_END$/))
    expect(pre.textContent).toHaveLength(TAIL_MAX_CHARS)
    expect(pre.textContent?.endsWith('TAIL_END')).toBe(true)
  })

  it('degrades VISIBLY when the tail fetch fails — an alert, not a crash or a spinner', async () => {
    vi.spyOn(api, 'getTerminalOutput').mockRejectedValue(
      Object.assign(new Error('boom'), { detail: 'terminal not found' })
    )

    render(<SyncedTerminalPane event={nullOffsetsWithTerminal} />)

    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert).toHaveTextContent('terminal not found')
    // The spinner is gone — a failed fetch must not leave it spinning forever.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders an explicit empty note (not a blank pane) when the tail is empty', async () => {
    vi.spyOn(api, 'getTerminalOutput').mockResolvedValue({ output: '', mode: 'full' })
    render(<SyncedTerminalPane event={nullOffsetsWithTerminal} />)
    await waitFor(() => expect(screen.getByText(/no terminal output yet/i)).toBeInTheDocument())
  })

  it('does NOT fetch anything when the event names no terminal (branch 3)', () => {
    const tailSpy = vi.spyOn(api, 'getTerminalOutput')
    const rangeSpy = vi.spyOn(api, 'getTerminalOutputRange')
    render(<SyncedTerminalPane event={noTerminal} />)
    expect(screen.getByText(/sync pending/i)).toBeInTheDocument()
    expect(tailSpy).not.toHaveBeenCalled()
    expect(rangeSpy).not.toHaveBeenCalled()
  })
})

describe('DeleteRunButton', () => {
  beforeEach(() => {
    useStore.setState({ snackbar: null, workflowRuns: [], selectedRun: null, wfEvents: [], wfGaps: [] })
  })

  it('opens the ConfirmModal and calls the U7 DELETE on confirm', async () => {
    const del = vi.spyOn(api, 'deleteWorkflowRun').mockResolvedValue(undefined as any)
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    render(<DeleteRunButton runId="run-abc-123" workflowName="my-workflow" />)

    // Confirm dialog not shown until the trigger is clicked.
    expect(screen.queryByText(/permanently deletes/i)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /delete this run/i }))
    expect(screen.getByText(/permanently deletes/i)).toBeInTheDocument()

    // Confirm -> DELETE fires with the run id.
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('run-abc-123'))
  })

  it('does not call DELETE when the modal is cancelled', () => {
    const del = vi.spyOn(api, 'deleteWorkflowRun').mockResolvedValue(undefined as any)
    render(<DeleteRunButton runId="run-abc-123" workflowName="my-workflow" />)
    fireEvent.click(screen.getByRole('button', { name: /delete this run/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(del).not.toHaveBeenCalled()
  })
})

// ── PR #526 review remediation ─────────────────────────────────────────────

describe('WorkflowTimeline — declared-gap list semantics (a11y)', () => {
  const events: WorkflowEvent[] = [
    { run_id: 'r', seq: 1, event_type: 'step.started', event_schema_version: 1, ts: '', step_id: 'a', state: 'running' },
    { run_id: 'r', seq: 5, event_type: 'step.completed', event_schema_version: 1, ts: '', step_id: 'a', state: 'completed' },
  ]
  const gaps: GapMarker[] = [{ after_seq: 1, before_seq: 5, missing_count: 3, reason: 'append_swallowed' }]

  // Guards the nested-listitem fix: the gap marker must be a SIBLING list item,
  // never a listitem nested inside the event's <li>. Reverting the fix (putting
  // the marker back inside the <li> with role="listitem") makes the gap's
  // closest <li> BE the event's <li>, so both assertions below go red.
  it('renders the gap as its own <li>, not a listitem nested in an event <li>', () => {
    render(<WorkflowTimeline events={events} gaps={gaps} selectedIndex={0} onSelectIndex={() => {}} />)
    const gap = screen.getByTestId('timeline-gap')

    // The marker itself IS the list item — not a div wrapped in one.
    expect(gap.tagName).toBe('LI')
    // ...and it contains no nested listitem, and sits in no <li> ancestor.
    expect(gap.querySelector('[role="listitem"], li')).toBeNull()
    expect(gap.parentElement?.tagName).toBe('OL')

    // The gap is a sibling of the event's list item, so the list has 3 items
    // (1 gap + 2 events), each exactly one level under the <ol>.
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(3)
    for (const li of items) expect(li.parentElement?.tagName).toBe('OL')
  })

  // A roleless div's aria-label is not exposed; a real <li> keeps it addressable.
  it('keeps the gap announcement reachable by its accessible name', () => {
    render(<WorkflowTimeline events={events} gaps={gaps} selectedIndex={0} onSelectIndex={() => {}} />)
    const gap = screen.getByRole('listitem', { name: /3 event\(s\) missing between seq 1 and 5/i })
    expect(gap).toHaveAttribute('data-testid', 'timeline-gap')
  })
})

// PR #526 human review — BLOCKING 1b: the web playback surface dropped TRAILING
// declared gaps exactly as the SSE arm did.
//
// ScrubBar keyed its hatched ticks off `gapSet.has(ev.seq)` INSIDE events.map(),
// and WorkflowTimeline's list keyed off `gapByBeforeSeq.get(ev.seq)`. A trailing
// gap ("the run ended and the last N events were lost") carries
// before_seq = high_water + 1, a sentinel matching no stored event, so BOTH
// surfaces rendered nothing at all for the most severe loss the API can declare.
describe('ScrubBar / WorkflowTimeline — TRAILING declared gap', () => {
  const events: WorkflowEvent[] = [
    { run_id: 'r', seq: 1, event_type: 'step.started', event_schema_version: 1, ts: '', step_id: 'a', state: 'running' },
    { run_id: 'r', seq: 2, event_type: 'step.completed', event_schema_version: 1, ts: '', step_id: 'a', state: 'completed' },
  ]
  // before_seq 4 = high_water(3) + 1 — past the last event's seq (2), so it can
  // never match an event and is invisible to a per-event lookup.
  const trailing: GapMarker[] = [
    { after_seq: 2, before_seq: 4, missing_count: 1, reason: 'append_failed_trailing' },
  ]

  it('renders a trailing tick at the end of the scrub bar', () => {
    render(<WorkflowTimeline events={events} gaps={trailing} selectedIndex={0} onSelectIndex={() => {}} />)
    const tick = screen.getByTestId('scrub-trailing-gap')
    expect(tick).toBeInTheDocument()
    // Hatched like an interior gap — a striped fill, NOT a colour-only signal.
    expect(tick.className).toMatch(/repeating-linear-gradient/)
    expect(tick).toHaveAttribute('title', expect.stringMatching(/1 event\(s\) lost after event 2/i))
  })

  it('announces the trailing gap in the slider aria-valuetext (not visual-only)', () => {
    render(<WorkflowTimeline events={events} gaps={trailing} selectedIndex={0} onSelectIndex={() => {}} />)
    // Every tick is aria-hidden, so the slider's value text is the ONLY route by
    // which a screen-reader user learns the run lost its final events.
    const slider = screen.getByRole('slider', { name: /playback position/i })
    expect(slider).toHaveAttribute(
      'aria-valuetext',
      expect.stringMatching(/Trailing gap: 1 event\(s\) lost after event 2/i)
    )
  })

  it('renders the trailing gap as a labelled list item after the last event', () => {
    render(<WorkflowTimeline events={events} gaps={trailing} selectedIndex={0} onSelectIndex={() => {}} />)
    const gap = screen.getByTestId('timeline-trailing-gap')
    expect(gap.tagName).toBe('LI')
    expect(gap).toHaveTextContent(/1 event\(s\) lost after seq 2/i)
    expect(gap).toHaveTextContent(/append_failed_trailing/)
    // Reachable by accessible name, and LAST in the list (the hole is at the end).
    expect(
      screen.getByRole('listitem', { name: /1 event\(s\) lost after seq 2/i })
    ).toBe(gap)
    const items = screen.getAllByRole('listitem')
    expect(items[items.length - 1]).toBe(gap)
  })

  it('renders NO trailing marker when every gap is interior (negative control)', () => {
    const interior: GapMarker[] = [
      { after_seq: 1, before_seq: 2, missing_count: 0, reason: 'append_failed' },
    ]
    render(<WorkflowTimeline events={events} gaps={interior} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.queryByTestId('scrub-trailing-gap')).not.toBeInTheDocument()
    expect(screen.queryByTestId('timeline-trailing-gap')).not.toBeInTheDocument()
    const slider = screen.getByRole('slider', { name: /playback position/i })
    expect(slider.getAttribute('aria-valuetext')).not.toMatch(/trailing gap/i)
  })

  it('renders NO trailing marker when there are no gaps at all', () => {
    render(<WorkflowTimeline events={events} gaps={[]} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.queryByTestId('scrub-trailing-gap')).not.toBeInTheDocument()
    expect(screen.queryByTestId('timeline-trailing-gap')).not.toBeInTheDocument()
  })

  it('renders interior AND trailing markers together without relocating the interior one', () => {
    const spread: WorkflowEvent[] = [
      { run_id: 'r', seq: 1, event_type: 'step.started', event_schema_version: 1, ts: '', step_id: 'a', state: 'running' },
      { run_id: 'r', seq: 3, event_type: 'step.completed', event_schema_version: 1, ts: '', step_id: 'a', state: 'completed' },
    ]
    const both: GapMarker[] = [
      { after_seq: 1, before_seq: 3, missing_count: 1, reason: 'append_failed' },
      { after_seq: 3, before_seq: 6, missing_count: 2, reason: 'append_failed_trailing' },
    ]
    render(<WorkflowTimeline events={spread} gaps={both} selectedIndex={0} onSelectIndex={() => {}} />)

    expect(screen.getByTestId('timeline-gap')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-trailing-gap')).toBeInTheDocument()
    // Order: event 1, interior gap, event 3, trailing gap.
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(4)
    expect(items[1]).toBe(screen.getByTestId('timeline-gap'))
    expect(items[3]).toBe(screen.getByTestId('timeline-trailing-gap'))
  })
})

describe('WorkflowsPanel — inline error is genuinely reachable', () => {
  beforeEach(() => {
    useStore.setState({ workflowRuns: [], selectedRun: null, wfEvents: [], wfGaps: [], snackbar: null })
  })

  // The Copilot finding: fetchWorkflowRuns handles its own errors, so the old
  // `.catch(...)` never ran and RunList's error branch was dead code. The store
  // action now RESOLVES to the message, and the panel mirrors it. Reverting
  // either half (store returning void, or the panel using .catch) leaves the
  // inline error unrendered and this test goes red.
  it('renders RunList\'s inline error when the list read fails', async () => {
    vi.spyOn(api, 'listWorkflowRuns').mockRejectedValue(new Error('502 Bad Gateway'))
    render(<WorkflowsPanel />)
    expect(await screen.findByText('502 Bad Gateway')).toBeInTheDocument()
    // Still surfaced via the snackbar too — the two are complementary.
    expect(useStore.getState().snackbar?.type).toBe('error')
  })

  it('clears the inline error once a later poll succeeds', async () => {
    const spy = vi.spyOn(api, 'listWorkflowRuns')
      .mockRejectedValueOnce(new Error('502 Bad Gateway'))
      .mockResolvedValue([sampleRun])
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render(<WorkflowsPanel />)
      await waitFor(() => expect(screen.getByText('502 Bad Gateway')).toBeInTheDocument())
      await vi.advanceTimersByTimeAsync(10_000)
      await waitFor(() => expect(screen.queryByText('502 Bad Gateway')).not.toBeInTheDocument())
      expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2)
      expect(screen.getByText('my-workflow')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  // fetchWorkflowRuns must never REJECT: the 10s setInterval calls it with no
  // catch, so a throwing action would raise an unhandled rejection every poll.
  it('fetchWorkflowRuns resolves (never rejects) and returns the message', async () => {
    vi.spyOn(api, 'listWorkflowRuns').mockRejectedValue(new Error('boom'))
    await expect(useStore.getState().fetchWorkflowRuns()).resolves.toBe('boom')
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    await expect(useStore.getState().fetchWorkflowRuns()).resolves.toBeNull()
  })
})

// ── PR 526 review: RunDetail must observe the run going terminal ───────────
// `run` is the inspection snapshot taken at SELECTION time and is never
// reassigned, and the panel polls the run LIST (not the selected run) — so
// deriving isTerminal from `run.state` alone could never observe completion and
// the SSE follow reconnected every 1.5s forever. The fresh state comes from the
// polled `allRuns` row.
describe('RunDetail — terminal state is observed from the polled run list (#526)', () => {
  const inspection = {
    run_id: 'run-abc-123',
    workflow_name: 'my-workflow',
    state: 'running', // stale snapshot: captured while the run was live
    started_at: '2026-07-27T00:00:00Z',
    finished_at: null,
    tier: 'yaml',
    steps: [],
  } as any

  const listRow = (state: string): RunSummaryRow => ({ ...sampleRun, state })

  beforeEach(() => {
    useStore.setState({ workflowRuns: [], selectedRun: null, wfEvents: [], wfGaps: [], snackbar: null })
  })

  it('does NOT open the follow stream when the list says the run finished', async () => {
    const fetchMock = vi.fn(async (_url: string) => {
      throw new Error('no stream expected')
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      // Snapshot says running; the polled list says completed. The list wins.
      render(<RunDetail run={inspection} allRuns={[listRow('completed')]} />)
      await new Promise(r => setTimeout(r, 50))
      const sseCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/events'))
      expect(sseCalls).toHaveLength(0)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('DOES open the follow stream while the list still says the run is live', async () => {
    const fetchMock = vi.fn(async (_url: string) => ({ ok: false, status: 500, body: null }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      render(<RunDetail run={inspection} allRuns={[listRow('running')]} />)
      await waitFor(() => {
        const sseCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/events'))
        expect(sseCalls.length).toBeGreaterThan(0)
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('falls back to the snapshot state when the run has no list row yet', async () => {
    const fetchMock = vi.fn(async (_url: string) => ({ ok: false, status: 500, body: null }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      // Empty list -> fall back to `run.state` ('running') -> follow opens.
      render(<RunDetail run={inspection} allRuns={[]} />)
      await waitFor(() => {
        const sseCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/events'))
        expect(sseCalls.length).toBeGreaterThan(0)
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

// PR 526 review fix cycle 1 — BLOCKING: a total-loss run (no events survived, but
// the server DECLARED the hole) must not render as the benign empty state. The
// `events.length === 0` early return fired before the trailing-gap render, so the
// most severe loss the journal can report ("every append was swallowed") appeared
// as "No events recorded for this run yet" — the exact confusion FR-3.3/BR-4 exist
// to prevent.
describe('WorkflowTimeline — total loss is not the empty state', () => {
  const totalLoss: GapMarker[] = [
    { after_seq: 0, before_seq: 4, missing_count: 3, reason: 'append_failed_trailing' },
  ]

  it('declares the loss instead of the empty state when no events survived', () => {
    render(<WorkflowTimeline events={[]} gaps={totalLoss} selectedIndex={0} onSelectIndex={() => {}} />)
    const loss = screen.getByTestId('timeline-total-loss')
    expect(loss).toBeInTheDocument()
    expect(loss).toHaveTextContent(/3 events declared lost/i)
    // The benign "nothing happened" state must NOT be what the user sees.
    expect(screen.queryByTestId('timeline-empty')).not.toBeInTheDocument()
  })

  it('announces the loss assertively (it is a data-loss report, not decoration)', () => {
    render(<WorkflowTimeline events={[]} gaps={totalLoss} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/declared lost/i)
  })

  it('still shows the empty state for a genuinely idle run (no events, no gaps)', () => {
    render(<WorkflowTimeline events={[]} gaps={[]} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.getByTestId('timeline-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('timeline-total-loss')).not.toBeInTheDocument()
  })

  it('singularises a one-event loss', () => {
    const one: GapMarker[] = [
      { after_seq: 0, before_seq: 2, missing_count: 1, reason: 'append_failed_trailing' },
    ]
    render(<WorkflowTimeline events={[]} gaps={one} selectedIndex={0} onSelectIndex={() => {}} />)
    expect(screen.getByTestId('timeline-total-loss')).toHaveTextContent(/1 event declared lost/i)
  })
})
