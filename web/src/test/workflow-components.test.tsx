import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { RunList } from '../components/workflow/RunList'
import { WorkflowTimeline } from '../components/workflow/WorkflowTimeline'
import { SyncedTerminalPane, hasTerminalOffsets } from '../components/workflow/SyncedTerminalPane'
import { DeleteRunButton } from '../components/workflow/DeleteRunButton'
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

describe('SyncedTerminalPane — the #769 graceful-degrade seam (mutation guard)', () => {
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

  it('BRANCH 2 — NULL offsets: shows the documented #769 degrade state, never crashes/blanks, never fetches', () => {
    const spy = vi.spyOn(api, 'getTerminalOutputRange')
    render(<SyncedTerminalPane event={nullOffsets} />)
    expect(screen.getByText(/sync pending/i)).toBeInTheDocument()
    expect(screen.getByText('#769')).toBeInTheDocument()
    // The range API is NOT called when offsets are null.
    expect(spy).not.toHaveBeenCalled()
  })

  it('BRANCH 2 — no selected event: still shows the degrade state, no crash', () => {
    render(<SyncedTerminalPane event={null} />)
    expect(screen.getByText(/sync pending/i)).toBeInTheDocument()
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
