// The run view: a running step's spinner and elapsed time in the step rail,
// the Output and Terminal tabs, the Result panel's last-step output,
// the timeline's labels and follow, and the guarantee that the run view never
// opens a WebSocket. The screen and transcript routes are mocked in the shapes
// of the contract's Shared API.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api'
import type { ApiError, RunInspection, StepInspection, StepScreen, StepTranscript, WorkflowEvent } from '../api'
import { useStore } from '../store'
import { RunDetail } from '../components/workflow/RunDetail'
import { StepOutput, structuredOutputText } from '../components/workflow/StepOutput'
import { StepConsole } from '../components/workflow/StepConsole'
import type { RailStep } from '../components/workflow/runPage'
import {
  StepTerminal,
  SCREEN_POLL_MS,
  TRANSCRIPT_404_RETRIES,
  TRANSCRIPT_RETRY_MS,
} from '../components/workflow/StepTerminal'
import { RunResultPanel } from '../components/workflow/RunResultPanel'
import { WorkflowTimeline } from '../components/workflow/WorkflowTimeline'
import {
  eventLabels,
  eventOutcome,
  formatDuration,
  formatLocalTime,
  latestStepStarts,
  stepActivity,
  stepTerminalId,
} from '../components/workflow/runView'

const T0 = Date.parse('2026-11-10T10:00:00Z')
/** The server's timestamp format, `s` seconds after T0. */
const at = (s: number) => new Date(T0 + s * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z')

function ev(seq: number, event_type: string, over: Partial<WorkflowEvent> = {}): WorkflowEvent {
  return { run_id: 'run-1', seq, event_type, event_schema_version: 1, ts: at(seq), ...over }
}

function inspection(state: string, steps: StepInspection[]): RunInspection {
  return {
    run_id: 'run-1', workflow_name: 'nightly-check', state, current_step_id: null,
    started_at: at(0), finished_at: null, tier: 'yaml', steps,
  }
}

function apiError(status: number, detail?: string): ApiError {
  return Object.assign(new Error(`${status} Error`), { status, detail })
}

function screenOf(text: string, over: Partial<StepScreen> = {}): StepScreen {
  return { terminal_id: 'ab12cd34', text, truncated: false, redacted: false, captured_at: at(0), ...over }
}

function transcriptOf(text: string, over: Partial<StepTranscript> = {}): StepTranscript {
  return { terminal_id: 'ab12cd34', text, truncated: false, redacted: false, ...over }
}

/** Advance fake time and let the promises it releases settle, inside act. */
async function tick(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** With real timers: let pending mocked responses land inside act before the test ends. */
async function flush() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

/**
 * Route `fetch` by URL. A route returns a status and body; anything unrouted
 * is a 500. Returns the URLs asked for, in order.
 */
function stubFetch(route: (url: string) => { status: number; body: unknown } | undefined = () => undefined) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = String(input)
    calls.push(url)
    const hit = route(url)
    const status = hit?.status ?? 500
    const body = hit ? hit.body : { detail: 'not mocked' }
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: String(status),
      body: null,
      json: async () => body,
      text: async () => JSON.stringify(body),
    }
  }))
  return calls
}

function resetRunStore(over: Partial<ReturnType<typeof useStore.getState>> = {}) {
  useStore.setState({
    workflowRuns: [], selectedRun: null, selectedRunId: null, wfEvents: [], wfGaps: [],
    selectedIndex: 0, followLatest: true, followConnected: false, snackbar: null, ...over,
  })
}

beforeEach(() => resetRunStore())

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  delete (document as { visibilityState?: unknown }).visibilityState
})

// ── Pure helpers ──────────────────────────────────────────────────────────

describe('runView helpers', () => {
  it('formats durations in seconds, then minutes, then hours', () => {
    expect([0, 12, 59, 60, 125, 3600, 3725, -3].map(formatDuration)).toEqual([
      '0 s', '12 s', '59 s', '1 min', '2 min 5 s', '1 h', '1 h 2 min', '0 s',
    ])
  })

  it('reads a step as pending, running or settled, and a run that ended has nothing running', () => {
    expect(stepActivity('pending', false)).toBe('pending')
    expect(stepActivity(undefined, false)).toBe('pending')
    expect(stepActivity('running', false)).toBe('running')
    expect(stepActivity('retried', false)).toBe('running')
    expect(stepActivity('running', true)).toBe('settled')
    for (const s of ['completed', 'completed_unvalidated', 'failed', 'skipped', 'cancelled', 'rerun_authorized', 'mystery']) {
      expect(stepActivity(s, false)).toBe('settled')
    }
  })

  it('takes each step\'s latest start, so a resumed step counts from its new start', () => {
    const events = [
      ev(1, 'step.started', { step_id: 'collect' }),
      ev(2, 'step.attempt.started', { step_id: 'collect' }),
      ev(3, 'step.started', { step_id: 'report' }),
      ev(9, 'step.started', { step_id: 'collect' }),
    ]
    expect(latestStepStarts(events)).toEqual({ collect: T0 + 9000, report: T0 + 3000 })
    expect(latestStepStarts([ev(1, 'step.started', { step_id: 'collect', ts: 'not a time' })])).toEqual({})
  })

  it('reads the terminal of the step\'s latest start, falling back to the inspection', () => {
    const started = ev(1, 'step.started', { step_id: 'collect' })
    const opened = (seq: number, id: string) => ev(seq, 'terminal.created', { step_id: 'collect', terminal_id: id })
    // The live event wins over an inspection that has not caught up.
    expect(stepTerminalId([started, opened(3, 'aaaa1111')], 'collect', 'ffff0000', 'running')).toBe('aaaa1111')
    // A retry's terminal replaces the first attempt's.
    expect(stepTerminalId([started, opened(3, 'aaaa1111'), opened(6, 'bbbb2222')], 'collect', null, 'running')).toBe('bbbb2222')
    // Started again (a resume) with no terminal yet: nothing to poll, whatever the inspection kept.
    const again = [started, opened(3, 'aaaa1111'), ev(8, 'step.started', { step_id: 'collect' })]
    expect(stepTerminalId(again, 'collect', 'aaaa1111', 'running')).toBeNull()
    // Settled, the inspection's id still lets the transcript be read.
    expect(stepTerminalId(again, 'collect', 'aaaa1111', 'settled')).toBe('aaaa1111')
    // No events for the step (a script run records none): the inspection decides.
    expect(stepTerminalId([], 'collect', 'cccc3333', 'running')).toBe('cccc3333')
    expect(stepTerminalId([], 'collect', null, 'settled')).toBeNull()
  })

  it('labels every event plainly, with durations from the matching start', () => {
    const events = [
      ev(1, 'step.started', { step_id: 'collect', state: 'running' }),
      ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 }),
      ev(3, 'terminal.created', { step_id: 'collect', attempt: 1, terminal_id: 'aaaa1111' }),
      ev(4, 'step.attempt.failed', { step_id: 'collect', attempt: 1, error_kind: 'timeout', state: 'running' }),
      ev(5, 'step.attempt.started', { step_id: 'collect', attempt: 2 }),
      ev(30, 'step.completed', { step_id: 'collect', state: 'completed' }),
      ev(31, 'step.started', { step_id: 'report' }),
      ev(32, 'step.failed', { step_id: 'report' }),
      ev(33, 'step.skipped', { step_id: 'publish', reason: 'upstream_halt' }),
      ev(34, 'step.reprompted', { step_id: 'report' }),
      ev(200, 'run.failed'),
    ]
    expect(eventLabels(events, at(0))).toEqual([
      'Step started',
      'Attempt 1 started',
      'Terminal opened',
      'Attempt 1 failed (timeout)',
      'Attempt 2 started',
      'Step completed in 29 s',
      'Step started',
      'Step failed',
      'Step skipped (upstream_halt)',
      'step.reprompted',
      'Run failed in 3 min 20 s',
    ])
  })

  it('times a run from its run.started event when there is one, and leaves out an unknown duration', () => {
    expect(eventLabels([ev(5, 'run.started'), ev(65, 'run.completed')], at(0))).toEqual(['run.started', 'Run completed in 1 min'])
    expect(eventLabels([ev(7, 'run.cancelled')])).toEqual(['Run cancelled'])
    // A resumed run records no new run.started, so its second outcome has no known start.
    expect(eventLabels([ev(10, 'run.failed'), ev(900, 'run.completed')], at(0))).toEqual(['Run failed in 10 s', 'Run completed'])
    expect(eventLabels([ev(7, 'step.completed', { step_id: 'collect' })])).toEqual(['Step completed'])
    expect(eventLabels([ev(1, 'step.attempt.started'), ev(2, 'step.attempt.failed'), ev(3, 'step.skipped'), ev(4, 'custom.thing')]))
      .toEqual(['Attempt started', 'Attempt failed', 'Step skipped', 'custom.thing'])
  })

  it('gives an outcome only to outcome events, from the event type and not its recorded state', () => {
    for (const type of ['step.started', 'step.attempt.started', 'terminal.created', 'step.reprompted', 'run.started', 'step.output.received']) {
      expect(eventOutcome(ev(1, type, { state: 'running' }))).toBeNull()
    }
    expect(eventOutcome(ev(1, 'step.attempt.failed', { state: 'running' }))).toBe('failed')
    expect(eventOutcome(ev(1, 'step.completed', { state: 'completed_unvalidated' }))).toBe('completed_unvalidated')
    expect(eventOutcome(ev(1, 'step.completed'))).toBe('completed')
    expect(eventOutcome(ev(1, 'step.failed'))).toBe('failed')
    expect(eventOutcome(ev(1, 'step.skipped'))).toBe('skipped')
    expect(eventOutcome(ev(1, 'run.completed'))).toBe('completed')
    expect(eventOutcome(ev(1, 'run.failed'))).toBe('failed')
    expect(eventOutcome(ev(1, 'run.cancelled'))).toBe('cancelled')
  })
})

// ── 1. Running state ──────────────────────────────────────────────────────

const BARE_SPIN = /(^|\s)animate-spin(\s|$)/

describe('a running step', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0 + 12_000)
  })

  it('spins only under motion-safe in the step rail, pulses its dot, and ticks a clock', async () => {
    stubFetch()
    resetRunStore({ wfEvents: [ev(0, 'step.started', { step_id: 'collect', state: 'running' })] })
    render(
      <RunDetail
        run={inspection('running', [
          { id: 'collect', state: 'running', attempts: 1 },
          { id: 'report', state: 'pending', attempts: 0 },
        ])}
        allRuns={[]}
      />,
    )
    const dot = screen.getByTestId('rail-dot-1').querySelector('svg') as SVGElement
    expect(dot).toHaveClass('motion-safe:animate-spin')
    expect(dot.getAttribute('class')).not.toMatch(BARE_SPIN)
    expect(screen.getByTestId('rail-pulse-1')).toHaveClass('motion-safe:animate-pulse')
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('0:12')
    await tick(2000)
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('0:14')
    // A step that is not running neither spins nor pulses.
    expect(screen.getByTestId('rail-dot-2').querySelector('svg')).toBeNull()
    expect(screen.queryByTestId('rail-pulse-2')).toBeNull()
  })

  it('spins in the run header, and RunDetail times the step from its step.started event', () => {
    stubFetch()
    resetRunStore({ wfEvents: [ev(0, 'step.started', { step_id: 'collect', state: 'running' })] })
    render(<RunDetail run={inspection('running', [{ id: 'collect', state: 'running', attempts: 1 }])} allRuns={[]} />)
    const header = screen.getByTestId('run-state').querySelector('svg') as SVGElement
    expect(header).toHaveClass('motion-safe:animate-spin')
    expect(header.getAttribute('class')).not.toMatch(BARE_SPIN)
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('0:12')
    // The console follows the running step and says how long it has been running.
    expect(screen.getByTestId('console-state')).toHaveTextContent('Running · 0:12')
    const consoleIcon = screen.getByTestId('console-state').querySelector('svg') as SVGElement
    expect(consoleIcon).toHaveClass('motion-safe:animate-spin')
  })

  it('stops moving once the run has ended, even where a step still reads running', async () => {
    stubFetch()
    resetRunStore({ wfEvents: [ev(0, 'step.started', { step_id: 'collect', state: 'running' })] })
    render(<RunDetail run={inspection('failed', [{ id: 'collect', state: 'running', attempts: 1 }])} allRuns={[]} />)
    expect(screen.getByTestId('run-state').querySelector('svg')).not.toHaveClass('motion-safe:animate-spin')
    expect(screen.getByTestId('rail-dot-1').querySelector('svg')).not.toHaveClass('motion-safe:animate-spin')
    expect(screen.queryByTestId('rail-pulse-1')).toBeNull()
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('Running')
    expect(screen.getByTestId('rail-chip-1')).not.toHaveTextContent('0:12')
    await tick()
  })

  it('stops the header spinning when the list says the run finished, before the snapshot catches up', async () => {
    stubFetch()
    const row = { run_id: 'run-1', workflow_name: 'nightly-check', state: 'completed', tier: 'yaml', started_at: at(0), finished_at: at(30), current_step_id: null }
    render(<RunDetail run={inspection('running', [{ id: 'collect', state: 'running', attempts: 1 }])} allRuns={[row]} />)
    expect(screen.getByTestId('run-state').querySelector('svg')).not.toHaveClass('motion-safe:animate-spin')
    await tick()
  })
})

// ── 2. The step panel: Output tab ─────────────────────────────────────────

describe('the Output tab', () => {
  const step = (over: Partial<StepInspection> = {}): StepInspection => ({ id: 'collect', state: 'completed', attempts: 1, ...over })

  it('pretty-prints the structured output when there is one, in place of the final answer', () => {
    render(
      <StepOutput
        step={step({
          output_json: '{"rows":3,"table":"example-table"}',
          result: { text: 'Done.', status: 'completed', truncated: true, redacted: true },
        })}
        state="completed"
        runEnded
      />,
    )
    expect(screen.getByLabelText('Structured output').textContent).toBe(
      JSON.stringify({ rows: 3, table: 'example-table' }, null, 2),
    )
    expect(screen.queryByText('Done.')).toBeNull()
    // The badges describe the final answer, which is not shown here.
    expect(screen.queryByText('Truncated')).toBeNull()
  })

  it('shows the final answer with the Truncated and Secrets redacted badges', () => {
    render(
      <StepOutput
        step={step({ result: { text: 'Found 3 rows in orders.', status: 'completed', truncated: true, redacted: true } })}
        state="completed"
        runEnded
      />,
    )
    expect(screen.getByLabelText('Final answer')).toHaveTextContent('Found 3 rows in orders.')
    expect(screen.getByText('Truncated')).toBeInTheDocument()
    expect(screen.getByText('Secrets redacted')).toBeInTheDocument()
  })

  it('shows no badge for an answer that was neither truncated nor redacted', () => {
    render(<StepOutput step={step({ result: { text: 'ok', status: 'completed', truncated: false, redacted: false } })} state="completed" runEnded />)
    expect(screen.getByLabelText('Final answer')).toHaveTextContent('ok')
    expect(screen.queryByText('Truncated')).toBeNull()
    expect(screen.queryByText('Secrets redacted')).toBeNull()
  })

  it('says Still running while the step runs, and No answer was recorded once it settled without one', () => {
    const { rerender } = render(<StepOutput step={step({ state: 'running', result: null })} state="running" runEnded={false} />)
    expect(screen.getByTestId('step-output')).toHaveTextContent('Still running')
    rerender(<StepOutput step={step({ result: null })} state="completed" runEnded />)
    expect(screen.getByTestId('step-output')).toHaveTextContent('No answer was recorded')
    // A run from before the answer was saved has no result field at all.
    rerender(<StepOutput step={step()} state="failed" runEnded />)
    expect(screen.getByTestId('step-output')).toHaveTextContent('No answer was recorded')
  })

  it('says a pending step has not started', () => {
    render(<StepOutput step={step({ state: 'pending', attempts: 0 })} state="pending" runEnded={false} />)
    expect(screen.getByTestId('step-output')).toHaveTextContent('This step has not started.')
  })

  it('shows a structured output that is not valid JSON as it was stored', () => {
    expect(structuredOutputText('{not json')).toBe('{not json')
    render(<StepOutput step={step({ output_json: '{not json' })} state="completed" runEnded />)
    expect(screen.getByLabelText('Structured output').textContent).toBe('{not json')
  })
})

// ── 2. The step panel: Terminal tab ───────────────────────────────────────

describe('the Terminal tab', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0)
  })

  const live = { runId: 'run-1', stepId: 'collect', state: 'running', terminalId: 'ab12cd34', runEnded: false }

  it('polls the screen every second and shows Live and when it was last updated', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('line 1'))
    render(<StepTerminal {...live} />)
    await tick()
    expect(read).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledWith('run-1', 'collect')
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('line 1')
    expect(screen.getByTestId('step-terminal-live')).toHaveTextContent('Live')
    expect(screen.getByTestId('step-terminal-updated')).toHaveTextContent('Updated 0 s ago')
    read.mockResolvedValue(screenOf('line 1\nline 2', { redacted: true }))
    await tick(SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('step-terminal-text').textContent).toBe('line 1\nline 2')
    expect(screen.getByText('Secrets redacted')).toBeInTheDocument()
    await tick(SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(3)
  })

  it('counts the seconds since the last screen while a read is slow, without overlapping reads', async () => {
    const read = vi
      .spyOn(api, 'getWorkflowStepScreen')
      .mockResolvedValueOnce(screenOf('x'))
      .mockReturnValue(new Promise(() => {}))
    render(<StepTerminal {...live} />)
    await tick()
    await tick(SCREEN_POLL_MS + 2000)
    expect(screen.getByTestId('step-terminal-updated')).toHaveTextContent('Updated 3 s ago')
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('pauses while the page is hidden and reads at once when it is visible again', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('x'))
    render(<StepTerminal {...live} />)
    await tick()
    expect(read).toHaveBeenCalledTimes(1)
    act(() => setVisibility('hidden'))
    await tick(5 * SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(1)
    act(() => setVisibility('visible'))
    await tick()
    expect(read).toHaveBeenCalledTimes(2)
    await tick(SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(3)
  })

  it('stops polling when it unmounts', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('x'))
    const { unmount } = render(<StepTerminal {...live} />)
    await tick()
    unmount()
    await tick(5 * SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('keeps polling through a 404 (the terminal id not saved yet) and shows other errors', async () => {
    const read = vi
      .spyOn(api, 'getWorkflowStepScreen')
      .mockRejectedValueOnce(apiError(404, 'no terminal recorded'))
      .mockRejectedValueOnce(apiError(500, 'capture failed'))
      .mockResolvedValue(screenOf('back'))
    render(<StepTerminal {...live} />)
    await tick()
    expect(screen.getByText("Waiting for the step's terminal.")).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    await tick(SCREEN_POLL_MS)
    expect(screen.getByRole('alert')).toHaveTextContent('capture failed')
    await tick(SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(3)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('back')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('reads the transcript once the screen route says the terminal has ended (410)', async () => {
    const read = vi
      .spyOn(api, 'getWorkflowStepScreen')
      .mockResolvedValueOnce(screenOf('working'))
      .mockRejectedValue(apiError(410, 'terminal ended'))
    const transcript = vi.spyOn(api, 'getWorkflowStepTranscript').mockResolvedValue(transcriptOf('all done', { truncated: true }))
    render(<StepTerminal {...live} />)
    await tick()
    await tick(SCREEN_POLL_MS)
    await tick()
    expect(transcript).toHaveBeenCalledWith('run-1', 'collect')
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('all done')
    expect(screen.getByText('Saved transcript')).toBeInTheDocument()
    expect(screen.getByText('Truncated')).toBeInTheDocument()
    expect(screen.queryByTestId('step-terminal-live')).toBeNull()
    await tick(5 * SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('reads the transcript when the step settles', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('working'))
    const transcript = vi.spyOn(api, 'getWorkflowStepTranscript').mockResolvedValue(transcriptOf('final screen'))
    const { rerender } = render(<StepTerminal {...live} />)
    await tick()
    rerender(<StepTerminal {...live} state="completed" />)
    await tick()
    expect(transcript).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('final screen')
    await tick(5 * SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('retries a transcript 404 up to three times, one second apart', async () => {
    const transcript = vi
      .spyOn(api, 'getWorkflowStepTranscript')
      .mockRejectedValueOnce(apiError(404))
      .mockRejectedValueOnce(apiError(404))
      .mockRejectedValueOnce(apiError(404))
      .mockResolvedValue(transcriptOf('late file'))
    render(<StepTerminal {...live} state="completed" />)
    await tick()
    expect(transcript).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Loading the transcript...')).toBeInTheDocument()
    await tick(TRANSCRIPT_RETRY_MS - 1)
    expect(transcript).toHaveBeenCalledTimes(1)
    await tick(1)
    expect(transcript).toHaveBeenCalledTimes(2)
    await tick(2 * TRANSCRIPT_RETRY_MS)
    expect(transcript).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('late file')
  })

  it('says no transcript was saved once the retries run out and the terminal is gone', async () => {
    const transcript = vi.spyOn(api, 'getWorkflowStepTranscript').mockRejectedValue(apiError(404))
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockRejectedValue(apiError(410))
    render(<StepTerminal {...live} state="failed" />)
    await tick(10 * TRANSCRIPT_RETRY_MS)
    expect(transcript).toHaveBeenCalledTimes(1 + TRANSCRIPT_404_RETRIES)
    expect(read).toHaveBeenCalledTimes(1)
    expect(screen.getByText('No transcript was saved for this step.')).toBeInTheDocument()
  })

  it('shows the screen of a failed step whose terminal is still open, then its transcript', async () => {
    const transcript = vi
      .spyOn(api, 'getWorkflowStepTranscript')
      .mockRejectedValueOnce(apiError(404))
      .mockRejectedValueOnce(apiError(404))
      .mockRejectedValueOnce(apiError(404))
      .mockRejectedValueOnce(apiError(404))
      .mockResolvedValue(transcriptOf('saved at last'))
    const read = vi
      .spyOn(api, 'getWorkflowStepScreen')
      .mockResolvedValueOnce(screenOf('stuck on a prompt'))
      .mockResolvedValueOnce(screenOf('stuck on a prompt'))
      .mockRejectedValue(apiError(410))
    render(<StepTerminal {...live} state="failed" runEnded />)
    // Four 404s, one second apart, then one look at the screen: still open.
    await tick()
    await tick(TRANSCRIPT_RETRY_MS)
    await tick(TRANSCRIPT_RETRY_MS)
    await tick(TRANSCRIPT_RETRY_MS)
    expect(transcript).toHaveBeenCalledTimes(1 + TRANSCRIPT_404_RETRIES)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('stuck on a prompt')
    expect(screen.getByTestId('step-terminal-live')).toHaveTextContent('Terminal still open')
    // The open terminal is polled like a live one until it ends; then the transcript loads.
    await tick(SCREEN_POLL_MS)
    await tick()
    expect(read).toHaveBeenCalledTimes(3)
    expect(transcript).toHaveBeenCalledTimes(2 + TRANSCRIPT_404_RETRIES)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('saved at last')
    expect(screen.getByText('Saved transcript')).toBeInTheDocument()
    expect(screen.queryByTestId('step-terminal-live')).toBeNull()
  })

  it('shows a transcript error other than 404 at once, without retrying', async () => {
    const transcript = vi.spyOn(api, 'getWorkflowStepTranscript').mockRejectedValue(apiError(400, 'malformed step id'))
    render(<StepTerminal {...live} state="completed" />)
    await tick(5 * TRANSCRIPT_RETRY_MS)
    expect(transcript).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert')).toHaveTextContent('malformed step id')
  })

  it('shows each empty state and reads nothing it cannot have', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen')
    const transcript = vi.spyOn(api, 'getWorkflowStepTranscript')
    const { rerender } = render(<StepTerminal {...live} state="pending" terminalId={null} />)
    expect(screen.getByText('This step has not started.')).toBeInTheDocument()
    rerender(<StepTerminal {...live} terminalId={null} />)
    expect(screen.getByText("Waiting for the step's terminal.")).toBeInTheDocument()
    rerender(<StepTerminal {...live} state="skipped" terminalId={null} />)
    expect(screen.getByText('No transcript was saved for this step.')).toBeInTheDocument()
    await tick(5000)
    expect(read).not.toHaveBeenCalled()
    expect(transcript).not.toHaveBeenCalled()
  })

  it('reads the transcript, not the screen, for a step of a run that has ended', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen')
    vi.spyOn(api, 'getWorkflowStepTranscript').mockResolvedValue(transcriptOf('stopped'))
    render(<StepTerminal {...live} runEnded />)
    await tick()
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('stopped')
    expect(read).not.toHaveBeenCalled()
  })

  it('starts over on a new terminal, such as a retry\'s', async () => {
    const read = vi
      .spyOn(api, 'getWorkflowStepScreen')
      .mockRejectedValueOnce(apiError(410))
      .mockResolvedValue(screenOf('attempt 2'))
    vi.spyOn(api, 'getWorkflowStepTranscript').mockResolvedValue(transcriptOf('attempt 1'))
    const { rerender } = render(<StepTerminal {...live} />)
    await tick()
    await tick()
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('attempt 1')
    rerender(<StepTerminal {...live} terminalId="ef56ab78" />)
    await tick()
    expect(read).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('attempt 2')
    expect(screen.getByTestId('step-terminal-live')).toBeInTheDocument()
  })
})

/** A rail step for the console, as railSteps builds it. */
function railStep(over: Partial<RailStep> = {}): RailStep {
  return {
    id: 'collect', number: 1, state: 'completed', activity: 'settled', inspection: null, spec: null,
    agent: null, provider: null, since: null, seconds: null, attempt: 1, attemptsTotal: null,
    skipReason: null, previousId: null, ...over,
  }
}

describe('StepConsole', () => {
  const settled: StepInspection = {
    id: 'collect', state: 'completed', attempts: 1, terminal_id: 'ab12cd34',
    result: { text: 'Found 3 rows.', status: 'completed', truncated: false, redacted: false },
  }

  it('opens a settled step on Output and a running one on Terminal', () => {
    vi.spyOn(api, 'getWorkflowStepScreen').mockReturnValue(new Promise(() => {}))
    const { unmount } = render(
      <StepConsole runId="run-1" step={railStep({ inspection: settled })} terminalId="ab12cd34" runEnded specStatus="ready" events={[]} />,
    )
    expect(screen.getByRole('tab', { name: 'Output' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Found 3 rows.')
    // A settled step has no live dot on its Terminal tab.
    expect(screen.queryByTestId('console-live-dot')).toBeNull()
    unmount()
    render(
      <StepConsole
        runId="run-1" step={railStep({ state: 'running', activity: 'running', inspection: { ...settled, state: 'running', result: null } })}
        terminalId="ab12cd34" runEnded={false} specStatus="ready" events={[]}
      />,
    )
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('step-terminal')).toHaveAttribute('data-variant', 'console')
    expect(screen.getByTestId('console-live-dot')).toBeInTheDocument()
  })

  it('switches tabs by click and arrow key, and leaving the Terminal tab stops the polling', async () => {
    vi.useFakeTimers()
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('x'))
    render(
      <StepConsole
        runId="run-1" step={railStep({ state: 'running', activity: 'running', inspection: { ...settled, state: 'running', result: null } })}
        terminalId="ab12cd34" runEnded={false} specStatus="ready" events={[]}
      />,
    )
    await tick()
    expect(read).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('tab', { name: 'Output' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Still running')
    await tick(5 * SCREEN_POLL_MS)
    expect(read).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Output' }), { key: 'ArrowLeft' })
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveFocus()
    await tick()
    expect(read).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Terminal' }), { key: 'End' })
    expect(screen.getByRole('tab', { name: 'Events' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Events' }), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Terminal' }), { key: 'Home' })
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('RunDetail opens a step from the rail', () => {
  it('shows the selected step in the console and moves between steps', async () => {
    stubFetch()
    const run = inspection('completed', [
      { id: 'collect', state: 'completed', attempts: 1, result: { text: 'Collected 3 rows.', status: 'completed', truncated: false, redacted: false } },
      { id: 'report', state: 'completed', attempts: 1, output_json: '{"report":"ready"}' },
    ])
    render(<RunDetail run={run} allRuns={[]} />)
    // A finished run opens on its last step with an answer.
    expect(screen.getByTestId('step-console')).toHaveAccessibleName('Step report')
    expect(within(screen.getByTestId('step-console')).getByLabelText('Structured output')).toHaveTextContent('"report": "ready"')
    const rail = screen.getByRole('list', { name: 'Steps of this run' })
    fireEvent.click(within(rail).getByRole('button', { name: /^collect/ }))
    const panel = screen.getByTestId('step-console')
    expect(panel).toHaveAccessibleName('Step collect')
    expect(within(panel).getByLabelText('Final answer')).toHaveTextContent('Collected 3 rows.')
    expect(within(rail).getByRole('button', { name: /^collect/ })).toHaveAttribute('aria-current', 'step')
    expect(within(rail).getByRole('button', { name: /^report/ })).not.toHaveAttribute('aria-current')
    await flush()
  })
})

// ── 3. The Result panel ───────────────────────────────────────────────────

describe('RunResultPanel shows the last step\'s output', () => {
  const result = { run_id: 'run-1', workflow_name: 'nightly-check', state: 'completed', started_at: at(0), steps: [] }

  it('with the Output tab\'s rules', async () => {
    vi.spyOn(api, 'getWorkflowRunResult').mockResolvedValue(result)
    render(
      <RunResultPanel
        runId="run-1" terminal state="completed"
        lastStep={{
          step: { id: 'report', state: 'completed', attempts: 1, result: { text: 'All 3 checks passed.', status: 'completed', truncated: true, redacted: false } },
          state: 'completed',
        }}
      />,
    )
    const last = screen.getByTestId('run-result-last-step')
    expect(last).toHaveTextContent('Last answer, from step report')
    expect(within(last).getByLabelText('Final answer')).toHaveTextContent('All 3 checks passed.')
    expect(within(last).getByText('Truncated')).toBeInTheDocument()
    expect(await screen.findByText('completed')).toBeInTheDocument()
  })

  it('says No answer was recorded for a last step that settled without one', async () => {
    vi.spyOn(api, 'getWorkflowRunResult').mockResolvedValue(result)
    render(<RunResultPanel runId="run-1" terminal state="failed" lastStep={{ step: { id: 'report', state: 'skipped', attempts: 0 }, state: 'skipped' }} />)
    expect(within(screen.getByTestId('run-result-last-step')).getByTestId('step-output')).toHaveTextContent('No answer was recorded')
    await screen.findByText('completed')
  })

  it('stays hidden while the run is going', () => {
    const read = vi.spyOn(api, 'getWorkflowRunResult')
    const { container } = render(
      <RunResultPanel runId="run-1" terminal={false} state="running" lastStep={{ step: { id: 'report', state: 'running', attempts: 1 }, state: 'running' }} />,
    )
    expect(container.textContent).toBe('')
    expect(read).not.toHaveBeenCalled()
  })

  it('RunDetail passes the run\'s last step, under the Result node', async () => {
    stubFetch(url => (url.endsWith('/workflows/runs/run-1/result') ? { status: 200, body: result } : undefined))
    render(
      <RunDetail
        run={inspection('completed', [
          { id: 'collect', state: 'completed', attempts: 1 },
          { id: 'report', state: 'completed', attempts: 1, output_json: '{"checks":3}' },
        ])}
        allRuns={[]}
      />,
    )
    // The run-level panels are not read until Result is selected.
    expect(screen.queryByTestId('run-result-last-step')).toBeNull()
    fireEvent.click(within(screen.getByTestId('rail-result')).getByRole('button'))
    const last = await screen.findByTestId('run-result-last-step')
    expect(last).toHaveTextContent('Last answer, from step report')
    expect(within(last).getByLabelText('Structured output')).toHaveTextContent('"checks": 3')
    expect(await screen.findByText(/Final state:/)).toHaveTextContent('Final state: completed')
  })

  it('RunDetail passes the last step that has an answer when the run halted', async () => {
    const halted = { ...result, state: 'failed' }
    stubFetch(url => (url.endsWith('/workflows/runs/run-1/result') ? { status: 200, body: halted } : undefined))
    render(
      <RunDetail
        run={inspection('failed', [
          {
            id: 'collect', state: 'completed', attempts: 1,
            result: { text: 'Found 3 rows.', status: 'completed', truncated: false, redacted: false },
          },
          { id: 'check', state: 'failed', attempts: 2 },
          { id: 'report', state: 'skipped', attempts: 0 },
        ])}
        allRuns={[]}
      />,
    )
    fireEvent.click(within(screen.getByTestId('rail-result')).getByRole('button'))
    const last = await screen.findByTestId('run-result-last-step')
    expect(last).toHaveTextContent('Last answer, from step collect')
    expect(within(last).getByLabelText('Final answer')).toHaveTextContent('Found 3 rows.')
    expect(await screen.findByText(/Final state:/)).toHaveTextContent('Final state: failed')
  })
})

// ── The run view never opens a WebSocket ──────────────────────────────────

describe('the run view never opens a WebSocket', () => {
  it('watches a running step through the read-only step routes only', async () => {
    const sockets = vi.fn()
    vi.stubGlobal('WebSocket', class {
      constructor(url: string) {
        sockets(url)
      }
    })
    const calls = stubFetch(url =>
      url.endsWith('/workflows/runs/run-1/steps/collect/screen')
        ? { status: 200, body: screenOf('reading example-table') }
        : undefined,
    )
    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect', state: 'running' }),
        ev(2, 'terminal.created', { step_id: 'collect', terminal_id: 'ab12cd34' }),
      ],
      selectedIndex: 1,
    })
    render(<RunDetail run={inspection('running', [{ id: 'collect', state: 'running', attempts: 1, terminal_id: 'ab12cd34' }])} allRuns={[]} />)
    // Follow is on, so the console shows the running step on its Terminal tab.
    const panel = screen.getByTestId('step-console')
    expect(within(panel).getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')
    expect(await within(panel).findByText('reading example-table')).toBeInTheDocument()
    // The pane under the timeline, in the All events slide-over, shows the
    // selected event's step the same way.
    fireEvent.click(screen.getByRole('button', { name: 'All 2 events' }))
    await waitFor(() => expect(screen.getAllByText('reading example-table')).toHaveLength(2))
    expect(sockets).not.toHaveBeenCalled()
    expect(calls.some(u => u.includes('/terminals/'))).toBe(false)
    expect(calls.filter(u => u.endsWith('/steps/collect/screen')).length).toBeGreaterThan(0)
  })
})

// ── 4. The timeline ───────────────────────────────────────────────────────

describe('the timeline', () => {
  // The shape of the run that read wrong: the first row recorded "running".
  const events = [
    ev(1, 'step.started', { step_id: 'collect', state: 'running' }),
    ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 }),
    ev(30, 'terminal.created', { step_id: 'collect', attempt: 1, terminal_id: 'ab12cd34' }),
    ev(31, 'step.completed', { step_id: 'collect', state: 'completed' }),
    ev(32, 'run.completed', { state: 'completed' }),
  ]

  it('shows the local time, the step and a plain label on each row', () => {
    render(<WorkflowTimeline events={events} gaps={[]} selectedIndex={4} onSelectIndex={() => {}} runStartedAt={at(0)} />)
    const rows = events.map(e => screen.getByTestId(`timeline-event-${e.seq}`))
    expect(rows.map(r => within(r).getByTestId('timeline-label').textContent)).toEqual([
      'Step started', 'Attempt 1 started', 'Terminal opened', 'Step completed in 30 s', 'Run completed in 32 s',
    ])
    expect(rows.map(r => within(r).getByTestId('timeline-step').textContent)).toEqual(['collect', 'collect', 'collect', 'collect', ''])
    const time = within(rows[0]).getByTestId('timeline-time').textContent
    expect(time).toBe(formatLocalTime(at(1)))
    expect(time).not.toBe('')
    // The raw type stays reachable as the row's tooltip.
    expect(rows[2]).toHaveAttribute('title', 'terminal.created')
  })

  it('puts an outcome badge only on outcome events, and never shows a recorded state', () => {
    render(<WorkflowTimeline events={events} gaps={[]} selectedIndex={0} onSelectIndex={() => {}} />)
    const badges = screen.getAllByTestId('timeline-outcome')
    expect(badges).toHaveLength(2)
    expect(within(screen.getByTestId('timeline-event-31')).getByTestId('timeline-outcome')).toHaveTextContent('Completed')
    expect(within(screen.getByTestId('timeline-event-32')).getByTestId('timeline-outcome')).toHaveTextContent('Completed')
    // The first row no longer says running after the run completed.
    expect(screen.getByTestId('timeline-event-1')).not.toHaveTextContent(/running/i)
    expect(within(screen.getByTestId('timeline-event-1')).queryByTestId('timeline-outcome')).toBeNull()
  })

  it('badges a failed attempt as failed although it recorded the step as running', () => {
    render(
      <WorkflowTimeline
        events={[ev(1, 'step.attempt.failed', { step_id: 'collect', attempt: 1, error_kind: 'timeout', state: 'running' })]}
        gaps={[]} selectedIndex={0} onSelectIndex={() => {}}
      />,
    )
    const row = screen.getByTestId('timeline-event-1')
    expect(within(row).getByTestId('timeline-label')).toHaveTextContent('Attempt 1 failed (timeout)')
    expect(within(row).getByTestId('timeline-outcome')).toHaveTextContent('Failed')
  })

  it('offers Latest only while not following, and says when it follows a live run', () => {
    const onLatest = vi.fn()
    const { rerender } = render(
      <WorkflowTimeline events={events} gaps={[]} selectedIndex={4} onSelectIndex={() => {}} live following onLatest={onLatest} />,
    )
    expect(screen.queryByRole('button', { name: 'Latest' })).toBeNull()
    expect(screen.getByTestId('timeline-following')).toHaveTextContent('Following the newest event')
    rerender(<WorkflowTimeline events={events} gaps={[]} selectedIndex={1} onSelectIndex={() => {}} live following={false} onLatest={onLatest} />)
    expect(screen.queryByTestId('timeline-following')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))
    expect(onLatest).toHaveBeenCalledTimes(1)
    // A finished run has nothing to follow: no following note and no Latest
    // (the transport's Jump to last event does the same job).
    rerender(<WorkflowTimeline events={events} gaps={[]} selectedIndex={4} onSelectIndex={() => {}} following onLatest={onLatest} />)
    expect(screen.queryByTestId('timeline-following')).toBeNull()
    rerender(<WorkflowTimeline events={events} gaps={[]} selectedIndex={1} onSelectIndex={() => {}} following={false} onLatest={onLatest} />)
    expect(screen.queryByRole('button', { name: 'Latest' })).toBeNull()
  })
})

describe('following the newest event (store)', () => {
  it('selects each new event while following', () => {
    const { appendWorkflowEvent } = useStore.getState()
    appendWorkflowEvent(ev(1, 'step.started'))
    appendWorkflowEvent(ev(2, 'step.attempt.started'))
    expect(useStore.getState().selectedIndex).toBe(1)
    appendWorkflowEvent(ev(3, 'terminal.created'))
    expect(useStore.getState().selectedIndex).toBe(2)
  })

  it('stops following once the user picks an event, and keeps that event selected', () => {
    const s = useStore.getState()
    for (const seq of [1, 2, 3]) s.appendWorkflowEvent(ev(seq, 'step.started'))
    s.setSelectedIndex(0)
    expect(useStore.getState().followLatest).toBe(false)
    s.appendWorkflowEvent(ev(4, 'step.completed'))
    expect(useStore.getState().selectedIndex).toBe(0)
    // A late event that lands before the picked one moves it down, and the
    // selection moves with it.
    s.setSelectedIndex(2)
    s.appendWorkflowEvent({ ...ev(2, 'x'), seq: 0 })
    expect(useStore.getState().wfEvents[useStore.getState().selectedIndex].seq).toBe(3)
  })

  it('picking the event already selected still stops following', () => {
    const s = useStore.getState()
    for (const seq of [1, 2]) s.appendWorkflowEvent(ev(seq, 'step.started'))
    s.setSelectedIndex(1)
    expect(useStore.getState().followLatest).toBe(false)
  })

  it('Latest jumps to the newest event and follows again', () => {
    const s = useStore.getState()
    for (const seq of [1, 2, 3]) s.appendWorkflowEvent(ev(seq, 'step.started'))
    s.setSelectedIndex(0)
    s.followLatestEvent()
    expect(useStore.getState()).toMatchObject({ selectedIndex: 2, followLatest: true })
    s.appendWorkflowEvent(ev(4, 'step.completed'))
    expect(useStore.getState().selectedIndex).toBe(3)
  })

  it('selecting a run or closing it follows again', async () => {
    vi.spyOn(api, 'inspectWorkflowRun').mockResolvedValue(inspection('running', []))
    vi.spyOn(api, 'getWorkflowRunEvents').mockResolvedValue({ events: [ev(1, 'step.started'), ev(2, 'x')], gaps: [], next_after_seq: null })
    resetRunStore({ followLatest: false })
    await useStore.getState().selectWorkflowRun('run-1')
    expect(useStore.getState()).toMatchObject({ followLatest: true, selectedIndex: 1 })
    useStore.setState({ followLatest: false })
    useStore.getState().clearSelectedRun()
    expect(useStore.getState().followLatest).toBe(true)
  })
})

describe('RunDetail follows the newest event until the user picks one', () => {
  it('moves with new events, holds a picked event, and Latest resumes', async () => {
    stubFetch()
    const events = [ev(1, 'step.started', { step_id: 'collect' }), ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 })]
    resetRunStore({ wfEvents: events, selectedIndex: 1 })
    render(<RunDetail run={inspection('running', [{ id: 'collect', state: 'running', attempts: 1 }])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'All 2 events' }))
    const dialog = screen.getByRole('dialog', { name: 'All events' })
    expect(within(dialog).getByTestId('timeline-following')).toBeInTheDocument()
    act(() => useStore.getState().appendWorkflowEvent(ev(3, 'terminal.created', { step_id: 'collect', terminal_id: 'ab12cd34' })))
    expect(screen.getByTestId('timeline-event-3')).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByTestId('timeline-event-1'))
    expect(screen.getByTestId('timeline-event-1')).toHaveAttribute('aria-current', 'true')
    act(() => useStore.getState().appendWorkflowEvent(ev(4, 'step.completed', { step_id: 'collect', state: 'completed' })))
    expect(screen.getByTestId('timeline-event-1')).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))
    expect(screen.getByTestId('timeline-event-4')).toHaveAttribute('aria-current', 'true')
    expect(screen.queryByRole('button', { name: 'Latest' })).toBeNull()
    await flush()
  })

  it('shows a run-level event\'s pane as having no terminal', async () => {
    stubFetch()
    resetRunStore({ wfEvents: [ev(1, 'run.completed', { state: 'completed' })], selectedIndex: 0 })
    render(<RunDetail run={inspection('completed', [])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'All 1 event' }))
    expect(screen.getByTestId('terminal-no-step')).toHaveTextContent('This event belongs to the run, not to a step, so it has no terminal.')
    await flush()
  })
})

// ── API client ────────────────────────────────────────────────────────────

describe('the run view API client', () => {
  it('builds the screen and transcript routes with encoded ids', async () => {
    const calls = stubFetch(url => (url.includes('/steps/') ? { status: 200, body: screenOf('x') } : undefined))
    await api.getWorkflowStepScreen('run 1', 'step/1')
    await api.getWorkflowStepTranscript('run-1', 'collect')
    expect(calls).toEqual([
      '/workflows/runs/run%201/steps/step%2F1/screen',
      '/workflows/runs/run-1/steps/collect/transcript',
    ])
  })

  it('surfaces a 410 from the screen route as its status', async () => {
    stubFetch(() => ({ status: 410, body: { detail: 'terminal ended' } }))
    await expect(api.getWorkflowStepScreen('run-1', 'collect')).rejects.toMatchObject({ status: 410, detail: 'terminal ended' })
  })

  it('adds a limit to the runs list only when one is given', async () => {
    const calls = stubFetch(() => ({ status: 200, body: [] }))
    await api.listWorkflowRuns()
    await api.listWorkflowRuns(500)
    expect(calls).toEqual(['/workflows/runs', '/workflows/runs?limit=500'])
  })
})
