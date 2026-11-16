// The Workflows home and the run page (the live console): home to run page and
// back, the run switcher, Follow, every rail state, the console tabs, Copy, the
// events strip and its slide-over, the Result node, the More menu, the banners,
// the home list's step progress, the console terminal, the page's height, and
// the helpers behind them. Routes are mocked in the shapes the API returns.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api'
import type {
  ApiError,
  RunInspection,
  RunSummaryRow,
  StepInspection,
  StepScreen,
  StepTranscript,
  WorkflowEvent,
  WorkflowSpec,
  WorkflowSpecStep,
} from '../api'
import { useStore } from '../store'
import { WorkflowsPanel } from '../components/WorkflowsPanel'
import { RunDetail } from '../components/workflow/RunDetail'
import { RunList } from '../components/workflow/RunList'
import { StepTerminal, SCREEN_POLL_MS } from '../components/workflow/StepTerminal'
import { copyText } from '../components/workflow/runPageParts'
import { MAX_IN_FLIGHT, PROGRESS_REFRESH_MS, useRunProgress } from '../components/workflow/useRunProgress'
import { formatLocalTime } from '../components/workflow/runView'
import {
  DEFAULT_STEP_RETRIES,
  agentInitials,
  agentTone,
  answerPreview,
  currentAttempt,
  eventAgent,
  eventsText,
  finishedTarget,
  firstLine,
  followTarget,
  formatClock,
  formatRunStart,
  labelledEvents,
  runSeconds,
  shortRunId,
  skipReason,
  stepDurations,
  stepOutputText,
  stepParamsText,
  stepProgress,
  totalAttempts,
} from '../components/workflow/runPage'

const T0 = Date.parse('2026-11-10T10:00:00Z')
/** The server's timestamp format, `s` seconds after T0. */
const at = (s: number) => new Date(T0 + s * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z')

function ev(seq: number, event_type: string, over: Partial<WorkflowEvent> = {}): WorkflowEvent {
  return { run_id: 'run-1', seq, event_type, event_schema_version: 1, ts: at(seq), ...over }
}

function result(text: string) {
  return { text, status: 'completed', truncated: false, redacted: false }
}

function step(id: string, state: string, over: Partial<StepInspection> = {}): StepInspection {
  const attempts = state === 'pending' ? 0 : 1
  return { id, state, attempts, ...over }
}

function inspection(state: string, steps: StepInspection[], over: Partial<RunInspection> = {}): RunInspection {
  return {
    run_id: 'run-1', workflow_name: 'nightly-check', state, current_step_id: null,
    started_at: at(0), finished_at: null, tier: 'yaml', steps, ...over,
  }
}

function row(run_id: string, state: string, over: Partial<RunSummaryRow> = {}): RunSummaryRow {
  return {
    run_id, workflow_name: 'nightly-check', state, tier: 'yaml', started_at: at(0),
    finished_at: null, current_step_id: null, ...over,
  }
}

const SPEC_NULLS = { engine: null, output_schema: null, when: null, until: null, max_iterations: null, on_stall: null, on_no_progress: null }

function specStep(id: string, over: Partial<WorkflowSpecStep> = {}): WorkflowSpecStep {
  return { id, provider: 'kiro_cli', agent: `${id}-agent`, prompt: `Do ${id}.`, call: null, params: null, retries: null, on_failure: null, ...SPEC_NULLS, ...over }
}

const SPEC: WorkflowSpec = {
  name: 'nightly-check',
  mode: 'sequential',
  steps: [
    specStep('collect', { agent: 'orders-reader' }),
    specStep('check', { agent: 'dlq-checker', params: { queue: 'orders-dlq', hours: '24' }, retries: 1 }),
    specStep('report', { agent: 'report-writer' }),
  ],
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

const specRoute = (spec: WorkflowSpec = SPEC) => (url: string) =>
  url.endsWith(`/workflows/${spec.name}`) ? { status: 200, body: spec } : undefined

function resetRunStore(over: Partial<ReturnType<typeof useStore.getState>> = {}) {
  useStore.setState({
    workflowRuns: [], selectedRun: null, selectedRunId: null, selectedRunError: null, wfEvents: [], wfGaps: [],
    selectedIndex: 0, followLatest: true, followConnected: false, snackbar: null, ...over,
  })
}

const rail = () => screen.getByRole('list', { name: 'Steps of this run' })
const consoleName = () => screen.getByTestId('step-console').getAttribute('aria-label')

const EMPTY_PAGE = { events: [], gaps: [], next_after_seq: null }

beforeEach(() => {
  localStorage.clear()
  resetRunStore()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  delete (navigator as { clipboard?: unknown }).clipboard
  delete (document as { execCommand?: unknown }).execCommand
})

// ── Helpers ───────────────────────────────────────────────────────────────

describe('run page helpers', () => {
  it('shortens run ids and makes two-letter agent initials with a stable colour', () => {
    expect(shortRunId('run-7f3c91a2d4e04b18')).toBe('run-7f3c91a2')
    expect(agentInitials('dynamodb-query-agent')).toBe('DQ')
    expect(agentInitials('report_writer')).toBe('RW')
    expect(agentInitials('developer')).toBe('DE')
    expect(agentInitials('')).toBe('?')
    expect(agentInitials(null)).toBe('?')
    expect(agentTone('orders-reader')).toBe(agentTone('orders-reader'))
    expect(agentTone('orders-reader')).toMatch(/^bg-[a-z]+-500\/15 text-[a-z]+-300$/)
  })

  it('formats a running step as a clock', () => {
    expect([0, 9, 48, 60, 125, 3600, 3725, -4].map(formatClock)).toEqual([
      '0:00', '0:09', '0:48', '1:00', '2:05', '1:00:00', '1:02:05', '0:00',
    ])
  })

  it('counts how far a run got, from the furthest step that started', () => {
    expect(stepProgress([])).toEqual({ reached: 0, total: 0 })
    expect(stepProgress([{ state: 'completed' }, { state: 'running' }, { state: 'pending' }])).toEqual({ reached: 2, total: 3 })
    expect(stepProgress([{ state: 'completed' }, { state: 'failed' }, { state: 'skipped', attempts: 0 }])).toEqual({ reached: 2, total: 3 })
    // A step cancelled while it ran settles skipped with an attempt, and counts.
    expect(stepProgress([{ state: 'completed' }, { state: 'skipped', attempts: 1 }, { state: 'skipped', attempts: 0 }])).toEqual({ reached: 2, total: 3 })
    expect(stepProgress([{ state: 'completed' }, { state: 'completed' }, { state: 'completed' }])).toEqual({ reached: 3, total: 3 })
    expect(stepProgress([{ state: 'pending' }, { state: 'pending' }])).toEqual({ reached: 0, total: 2 })
  })

  it('times each settled step from its latest start to its outcome', () => {
    const events = [
      ev(1, 'step.started', { step_id: 'collect' }),
      ev(29, 'step.completed', { step_id: 'collect' }),
      ev(30, 'step.started', { step_id: 'check' }),
      ev(40, 'step.failed', { step_id: 'check' }),
      // Resumed: the step started again and has not settled.
      ev(50, 'step.started', { step_id: 'check' }),
      ev(61, 'step.skipped', { step_id: 'report' }),
    ]
    expect(stepDurations(events)).toEqual({ collect: 28 })
    expect(stepDurations(events.slice(0, 4))).toEqual({ collect: 28, check: 10 })
  })

  it('reads the attempt a step is on, and the attempts it gets', () => {
    const events = [
      ev(1, 'step.started', { step_id: 'check' }),
      ev(2, 'step.attempt.started', { step_id: 'check', attempt: 1 }),
      ev(9, 'step.attempt.failed', { step_id: 'check', attempt: 1 }),
      ev(10, 'step.attempt.started', { step_id: 'check', attempt: 2 }),
    ]
    expect(currentAttempt(events, 'check', 0)).toBe(2)
    // A new start (a resume) counts from the inspection again.
    expect(currentAttempt([...events, ev(20, 'step.started', { step_id: 'check' })], 'check', 1)).toBe(1)
    expect(currentAttempt([], 'check', 3)).toBe(3)
    expect(currentAttempt([], 'check', 0)).toBe(1)
    expect(totalAttempts(specStep('a', { retries: 1 }))).toBe(2)
    expect(totalAttempts(specStep('a', { retries: 0 }))).toBe(1)
    expect(totalAttempts(specStep('a', { retries: null }))).toBe(DEFAULT_STEP_RETRIES + 1)
    expect(totalAttempts(null)).toBeNull()
  })

  it('previews the first two lines of an answer, or of the structured output', () => {
    expect(answerPreview(step('a', 'completed', { result: result('\nFirst line.\n\nSecond line.\nThird line.') }))).toBe('First line.\nSecond line.')
    expect(answerPreview(step('a', 'completed', { output_json: '{"rows": 3,\n "table": "orders"}' }))).toBe('{"rows":3,"table":"orders"}')
    expect(answerPreview(step('a', 'completed', { output_json: '{not json' }))).toBe('{not json')
    // The final answer is the preview; the Output tab shows the structured output first.
    const both = step('a', 'completed', { output_json: '{"rows":3}', result: result('Found 3 rows.') })
    expect(answerPreview(both)).toBe('Found 3 rows.')
    expect(stepOutputText(both)).toBe(JSON.stringify({ rows: 3 }, null, 2))
    expect(answerPreview(step('a', 'completed', { result: result('   ') }))).toBeNull()
    expect(answerPreview(null)).toBeNull()
    expect(stepOutputText(step('a', 'completed', { result: result('ok') }))).toBe('ok')
    expect(stepOutputText(step('a', 'completed'))).toBeNull()
    expect(firstLine('\n  Step timed out after 600 s \nTraceback')).toBe('Step timed out after 600 s')
    expect(firstLine('  ')).toBeNull()
  })

  it('picks the step to follow, and the step a finished run opens on', () => {
    const ids = ['collect', 'check', 'report']
    expect(followTarget(ids, { check: T0 }, [], {})).toBe('check')
    // Between two steps: the one that started last.
    const events = [
      ev(1, 'step.started', { step_id: 'collect' }),
      ev(5, 'step.completed', { step_id: 'collect' }),
      ev(6, 'step.started', { step_id: 'check' }),
      ev(9, 'step.completed', { step_id: 'check' }),
    ]
    expect(followTarget(ids, {}, events, {})).toBe('check')
    // No events (a script run): the last step that left pending.
    expect(followTarget(ids, {}, [], { collect: 'completed', check: 'completed', report: 'pending' })).toBe('check')
    expect(followTarget(ids, {}, [], {})).toBe('collect')
    expect(followTarget([], {}, [], {})).toBeNull()

    const failed = inspection('failed', [step('collect', 'completed', { result: result('ok') }), step('check', 'failed'), step('report', 'skipped')])
    expect(finishedTarget(failed, ids, { collect: 'completed', check: 'failed', report: 'skipped' })).toBe('check')
    const done = inspection('completed', [step('collect', 'completed', { result: result('ok') }), step('check', 'completed', { output_json: '{"n":1}' }), step('report', 'completed')])
    expect(finishedTarget(done, ids, { collect: 'completed', check: 'completed', report: 'completed' })).toBe('check')
    const cancelled = inspection('cancelled', [step('collect', 'completed'), step('check', 'skipped'), step('report', 'pending')])
    expect(finishedTarget(cancelled, ids, { collect: 'completed', check: 'skipped', report: 'pending' })).toBe('check')
    expect(finishedTarget(inspection('cancelled', []), ['collect'], {})).toBe('collect')
  })

  it('formats a run start, with the date in front when it was not today', () => {
    expect(formatRunStart(at(0), T0 + 60_000)).toBe(
      new Date(T0).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    )
    expect(formatRunStart(at(0), T0 + 60_000, false)).toBe(new Date(T0).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
    const earlier = formatRunStart(at(0), T0 + 3 * 86_400_000)
    expect(earlier.startsWith(`${new Date(T0).toLocaleDateString([], { month: 'short', day: 'numeric' })}, `)).toBe(true)
    expect(formatRunStart('nonsense')).toBe('')
    expect(formatRunStart(null)).toBe('')
    expect(runSeconds(at(0), at(72))).toBe(72)
    expect(runSeconds(at(72), at(0))).toBeNull()
    expect(runSeconds(at(0), null)).toBeNull()
  })

  it('reads why a step was skipped and which agent its events name', () => {
    const events = [
      ev(1, 'step.started', { step_id: 'notify', agent_profile: 'notifier', provider: 'kiro_cli' }),
      ev(2, 'step.skipped', { step_id: 'notify', reason: 'cancelled' }),
    ]
    expect(skipReason(events, 'notify')).toBe('cancelled')
    expect(skipReason([...events, ev(3, 'step.started', { step_id: 'notify' })], 'notify')).toBeNull()
    expect(eventAgent(events, 'notify')).toEqual({ agent: 'notifier', provider: 'kiro_cli' })
    expect(eventAgent(events, 'other')).toEqual({ agent: null, provider: null })
  })

  it('writes the params and the events as plain lines for Copy', () => {
    expect(stepParamsText(specStep('check', { agent: 'dlq-checker', params: { queue: 'orders-dlq', hours: '24' } }))).toBe(
      'agent: dlq-checker\nprovider: kiro_cli\ncall: handoff\nparams:\n  queue: orders-dlq\n  hours: 24',
    )
    expect(stepParamsText(specStep('a', { call: 'assign' }))).toBe('agent: a-agent\nprovider: kiro_cli\ncall: assign')
    const rows = labelledEvents([ev(1, 'step.started', { step_id: 'collect' }), ev(29, 'step.completed', { step_id: 'collect' })], at(0))
    expect(rows.map(r => [r.index, r.label])).toEqual([[0, 'Step started'], [1, 'Step completed in 28 s']])
    expect(eventsText(rows)).toBe(`${formatLocalTime(at(1))}  Step started\n${formatLocalTime(at(29))}  Step completed in 28 s`)
  })
})

// ── The Workflows home ────────────────────────────────────────────────────

describe('the Workflows home', () => {
  it('lists the runs as its main content, opens one on the run page, and Workflows goes back', async () => {
    stubFetch()
    const runs = [
      row('run-7f3c91a2d4e04b18', 'completed', { finished_at: at(72) }),
      row('run-0a1b2c3d4e5f6071', 'failed', { workflow_name: 'orders-audit', finished_at: at(13) }),
    ]
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue(runs)
    vi.spyOn(api, 'inspectWorkflowRun').mockImplementation(async id =>
      id === runs[0].run_id
        ? inspection('completed', [step('collect', 'completed'), step('check', 'completed'), step('report', 'completed')], { run_id: id })
        : inspection('failed', [step('scan', 'completed'), step('match', 'failed'), step('summarize', 'skipped', { attempts: 0 })], { run_id: id, workflow_name: 'orders-audit' }),
    )
    vi.spyOn(api, 'getWorkflowRunEvents').mockResolvedValue(EMPTY_PAGE)
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    render(<WorkflowsPanel />)
    const list = await screen.findByRole('list', { name: 'Workflow runs' })
    // The placeholder is gone, and the left column stays.
    expect(screen.queryByText(/Select a run to inspect/)).toBeNull()
    expect(screen.getByRole('button', { name: /New workflow/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Run a workflow/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Approve a plan id/ })).toBeInTheDocument()
    const first = within(list).getByTestId('run-row-run-7f3c91a2d4e04b18')
    expect(first).toHaveTextContent('Completed')
    expect(first).toHaveTextContent('nightly-check')
    expect(first).toHaveTextContent('run-7f3c91a2')
    expect(first).not.toHaveTextContent('run-7f3c91a2d4e04b18')
    expect(first).toHaveTextContent(formatRunStart(at(0)))
    expect(within(first).getByTestId('run-row-duration')).toHaveTextContent('1 min 12 s')
    await waitFor(() => expect(within(first).getByTestId('run-row-steps')).toHaveTextContent('3 of 3'))
    const second = within(list).getByTestId('run-row-run-0a1b2c3d4e5f6071')
    expect(second).toHaveTextContent('Failed')
    await waitFor(() => expect(within(second).getByTestId('run-row-steps')).toHaveTextContent('2 of 3'))

    fireEvent.click(first)
    expect(await screen.findByTestId('run-page')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New workflow/ })).toBeNull()
    expect(screen.getByRole('heading', { name: 'nightly-check' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back to Workflows' }))
    expect(screen.queryByTestId('run-page')).toBeNull()
    expect(useStore.getState().selectedRunId).toBeNull()
    expect(screen.getByRole('list', { name: 'Workflow runs' })).toBeInTheDocument()
    await flush()
  })

  it('shows the run page while the run loads, and an error with Try again when it fails', async () => {
    stubFetch()
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([row('run-1', 'completed')])
    vi.spyOn(api, 'getWorkflowRunEvents').mockResolvedValue(EMPTY_PAGE)
    let fail: (e: unknown) => void = () => {}
    const inspect = vi.spyOn(api, 'inspectWorkflowRun')
    inspect.mockImplementationOnce(() => new Promise(() => {})) // the home list's progress read
    inspect.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    inspect.mockResolvedValue(inspection('completed', [step('collect', 'completed')]))
    render(<WorkflowsPanel />)
    fireEvent.click(await screen.findByTestId('run-row-run-1'))
    const pending = screen.getByTestId('run-page-pending')
    expect(within(pending).getByRole('status')).toHaveTextContent('Loading run')
    expect(within(pending).getByRole('heading', { name: 'nightly-check' })).toBeInTheDocument()
    await act(async () => fail(new Error('502 Bad Gateway')))
    expect(within(pending).getByRole('alert')).toHaveTextContent('Could not load run run-1: 502 Bad Gateway')
    fireEvent.click(within(pending).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('run-page')).toBeInTheDocument()
    expect(useStore.getState().selectedRunError).toBeNull()
    await flush()
  })

  it('opens a run started from the launcher on the run page', async () => {
    stubFetch()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([
      { name: 'nightly-check', source_path: '/wf/nightly-check.yaml', mode: 'sequential', step_count: 3, description: '', indexed_at: '' },
    ])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    vi.spyOn(api, 'submitWorkflowRun').mockResolvedValue({ run_id: 'run-1', state: 'running' })
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    vi.spyOn(api, 'inspectWorkflowRun').mockResolvedValue(
      inspection('running', [step('collect', 'pending'), step('check', 'pending'), step('report', 'pending')]),
    )
    vi.spyOn(api, 'getWorkflowRunEvents').mockResolvedValue(EMPTY_PAGE)
    render(<WorkflowsPanel />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const run = await screen.findByRole('button', { name: /Run workflow/ })
    await waitFor(() => expect(run).toBeEnabled())
    fireEvent.click(run)
    expect(await screen.findByTestId('run-page')).toBeInTheDocument()
    expect(useStore.getState().selectedRunId).toBe('run-1')
    await flush()
  })

  it('reads a finished run\'s progress once and a live run\'s again, a few at a time', async () => {
    vi.useFakeTimers()
    const runs = [row('run-live', 'running'), row('run-done', 'completed', { finished_at: at(72) })]
    const inspect = vi.spyOn(api, 'inspectWorkflowRun').mockImplementation(async id =>
      id === 'run-live'
        ? inspection('running', [step('a', 'completed'), step('b', 'running'), step('c', 'pending')], { run_id: id })
        : inspection('completed', [step('a', 'completed'), step('b', 'completed')], { run_id: id }),
    )
    function Harness({ list }: { list: RunSummaryRow[] }) {
      const progress = useRunProgress(list)
      return <RunList runs={list} loading={false} error={null} onSelect={() => {}} progress={progress} />
    }
    render(<Harness list={runs} />)
    await tick()
    expect(within(screen.getByTestId('run-row-run-live')).getByTestId('run-row-steps')).toHaveTextContent('2 of 3')
    expect(within(screen.getByTestId('run-row-run-done')).getByTestId('run-row-steps')).toHaveTextContent('2 of 2')
    expect(inspect).toHaveBeenCalledTimes(2)
    inspect.mockClear()
    await tick(PROGRESS_REFRESH_MS)
    expect(inspect.mock.calls.map(c => c[0])).toEqual(['run-live'])
    cleanup()

    // Ten runs: no more than MAX_IN_FLIGHT reads at once.
    inspect.mockReset()
    inspect.mockImplementation(() => new Promise(() => {}))
    const many = Array.from({ length: 10 }, (_, i) => row(`run-${i}`, 'completed', { finished_at: at(5) }))
    render(<Harness list={many} />)
    await tick()
    expect(inspect).toHaveBeenCalledTimes(MAX_IN_FLIGHT)
  })

  it('ticks a live run\'s duration and says when a finished one is not known', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(T0 + 12_000)
    render(
      <RunList
        runs={[row('run-live', 'running'), row('run-odd', 'cancelled')]}
        loading={false} error={null} onSelect={() => {}}
      />,
    )
    const live = within(screen.getByTestId('run-row-run-live')).getByTestId('run-row-duration')
    expect(live).toHaveTextContent('12 s')
    await tick(3000)
    expect(live).toHaveTextContent('15 s')
    expect(within(screen.getByTestId('run-row-run-live')).getByText('Running').previousSibling).toHaveClass('motion-safe:animate-spin')
    expect(within(screen.getByTestId('run-row-run-odd')).getByTestId('run-row-duration')).toHaveTextContent('Unknown')
    // No progress read yet: the Steps cell stays empty.
    expect(within(screen.getByTestId('run-row-run-odd')).getByTestId('run-row-steps')).toHaveTextContent(/^$/)
  })
})

// ── The run bar ───────────────────────────────────────────────────────────

describe('the run bar', () => {
  it('goes back with a Workflows button, and has no run switcher', () => {
    stubFetch()
    const runs = [row('run-aaaaaaaa1111', 'completed'), row('run-bbbbbbbb2222', 'failed')]
    render(<RunDetail run={inspection('completed', [], { run_id: 'run-aaaaaaaa1111' })} allRuns={runs} />)
    const back = screen.getByRole('button', { name: 'Back to Workflows' })
    expect(back).toHaveTextContent('Workflows')
    expect(back).toHaveAttribute('title', 'Back to all workflows')
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('says how long a finished run took, or that it failed or was cancelled', () => {
    stubFetch()
    const { rerender } = render(
      <RunDetail run={inspection('completed', [], { finished_at: at(72) })} allRuns={[]} />,
    )
    expect(screen.getByTestId('run-state')).toHaveTextContent('Completed in 1 min 12 s')
    expect(screen.queryByTestId('run-connection')).toBeNull()
    rerender(<RunDetail run={inspection('failed', [])} allRuns={[]} />)
    expect(screen.getByTestId('run-state')).toHaveTextContent(/^Failed/)
    rerender(<RunDetail run={inspection('cancelled', [])} allRuns={[]} />)
    expect(screen.getByTestId('run-state')).toHaveTextContent(/^Cancelled/)
  })

  it('shows the running time and the connection hint while the run is live', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(T0 + 76_000)
    stubFetch()
    render(<RunDetail run={inspection('running', [])} allRuns={[]} />)
    expect(screen.getByTestId('run-state')).toHaveTextContent('Running · 1 min 16 s')
    expect(screen.getByTestId('run-connection')).toHaveTextContent('Reconnecting')
    act(() => useStore.getState().setFollowConnected(true))
    expect(screen.getByTestId('run-connection')).toHaveTextContent('Live')
    await tick()
  })

  it('cancels a live run', async () => {
    stubFetch()
    const cancel = vi.spyOn(api, 'cancelWorkflowRun').mockResolvedValue({ success: true, run_id: 'run-1' } as never)
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    render(<RunDetail run={inspection('running', [step('collect', 'pending')])} allRuns={[]} />)
    expect(screen.queryByRole('button', { name: /Resume/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('run-1'))
    await flush()
  })

  it('shows the failure detail of a failed run under the bar', () => {
    stubFetch()
    render(
      <RunDetail
        run={inspection('failed', [step('collect', 'completed'), step('check', 'failed', { attempts: 2, error_kind: 'timeout', terminal_id: 'ab12cd34' })])}
        allRuns={[]}
      />,
    )
    const detail = screen.getByRole('region', { name: 'Failure detail' })
    expect(detail).toHaveTextContent('Run failed')
    expect(detail).toHaveTextContent('Last stepcheck')
    expect(detail).toHaveTextContent('Failing attempt2')
    expect(detail).toHaveTextContent('Error kindtimeout')
    expect(detail).toHaveTextContent('Terminalab12cd34')
  })

  it('offers Approve & resume when Resume needs a new approval', async () => {
    stubFetch()
    vi.spyOn(api, 'resumeWorkflowRun').mockRejectedValue(
      Object.assign(new Error('403 Error'), { status: 403, kind: 'approval_required', detail: 'changed', detailMeta: { plan_id: 'plan-v1:abc' } }),
    )
    const approve = vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({ plan_id: 'plan-v1:abc', approved: true, changed: true })
    resetRunStore({ selectedRunId: 'run-1' })
    render(<RunDetail run={inspection('cancelled', [step('collect', 'completed')])} allRuns={[]} />)
    expect(screen.queryByRole('button', { name: /^Cancel$/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Resume/ }))
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('resuming needs a new approval')
    expect(banner).toHaveTextContent('plan-v1:abc')
    fireEvent.click(within(banner).getByRole('button', { name: /Approve & resume/ }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('plan-v1:abc'))
    await flush()
  })
})

describe('the More menu', () => {
  it('exports diagnostics, opens Compare runs, and asks before deleting the run', async () => {
    stubFetch()
    const exportSpy = vi.spyOn(api, 'getWorkflowRunDiagnostics').mockResolvedValue({} as never)
    const del = vi.spyOn(api, 'deleteWorkflowRun').mockResolvedValue(undefined as never)
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    resetRunStore({ selectedRunId: 'run-1' })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed')])} allRuns={[row('run-1', 'completed'), row('run-2', 'completed')]} />)
    const more = screen.getByRole('button', { name: 'More actions' })
    expect(more).toHaveAttribute('aria-haspopup', 'menu')
    expect(more).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(more)
    const menu = screen.getByRole('menu', { name: 'More actions' })
    expect(more).toHaveAttribute('aria-expanded', 'true')
    expect(within(menu).getAllByRole('menuitem').map(i => i.textContent)).toEqual(['Export diagnostics', 'Compare runs', 'Delete run'])
    expect(within(menu).getByRole('menuitem', { name: 'Export diagnostics' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(within(menu).getByRole('menuitem', { name: 'Compare runs' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'End' })
    expect(within(menu).getByRole('menuitem', { name: 'Delete run' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(within(menu).getByRole('menuitem', { name: 'Export diagnostics' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(within(menu).getByRole('menuitem', { name: 'Delete run' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(more).toHaveFocus()

    fireEvent.click(more)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Export diagnostics' }))
    await waitFor(() => expect(exportSpy).toHaveBeenCalledWith('run-1'))
    await waitFor(() => expect(useStore.getState().snackbar).toMatchObject({ type: 'success', message: 'Diagnostics exported' }))

    fireEvent.click(more)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Compare runs' }))
    const compare = screen.getByRole('dialog', { name: 'Compare runs' })
    expect(within(compare).getByText('Compare against')).toBeInTheDocument()
    fireEvent.click(within(compare).getByRole('button', { name: 'Close Compare runs' }))
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(more)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete run' }))
    expect(screen.queryByRole('menu')).toBeNull()
    const confirm = screen.getByRole('alertdialog')
    expect(confirm).toHaveTextContent(/permanently deletes/)
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('run-1'))
    await waitFor(() => expect(useStore.getState().selectedRunId).toBeNull())
  })

  it('closes on a click outside', () => {
    stubFetch()
    render(<RunDetail run={inspection('completed', [])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'More actions' }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
  })
})

// ── Follow ────────────────────────────────────────────────────────────────

describe('Follow the running step', () => {
  const three = (check: string, report: string) => [
    step('collect', 'completed', { result: result('Found 3 orders.') }),
    step('check', check),
    step('report', report),
  ]

  it('follows the running step, stops on a manual pick, and picks the running step again', async () => {
    stubFetch()
    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect' }),
        ev(5, 'step.completed', { step_id: 'collect', state: 'completed' }),
        ev(6, 'step.started', { step_id: 'check', state: 'running' }),
      ],
    })
    render(<RunDetail run={inspection('running', three('running', 'pending'))} allRuns={[]} />)
    const follow = screen.getByRole('switch', { name: 'Follow the running step' })
    expect(follow).toHaveAttribute('aria-checked', 'true')
    expect(consoleName()).toBe('Step check')
    expect(within(rail()).getByRole('button', { name: /^check/ })).toHaveAttribute('aria-current', 'step')
    // Picking the step Follow already shows changes nothing.
    fireEvent.click(within(rail()).getByRole('button', { name: /^check/ }))
    expect(follow).toHaveAttribute('aria-checked', 'true')

    // The next step starts: Follow moves to it.
    act(() => {
      useStore.getState().appendWorkflowEvent(ev(9, 'step.completed', { step_id: 'check', state: 'completed' }))
      useStore.getState().appendWorkflowEvent(ev(10, 'step.started', { step_id: 'report', state: 'running' }))
    })
    expect(consoleName()).toBe('Step report')
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true')

    // Picking another step turns Follow off and holds the pick.
    fireEvent.click(within(rail()).getByRole('button', { name: /^collect/ }))
    expect(follow).toHaveAttribute('aria-checked', 'false')
    expect(consoleName()).toBe('Step collect')
    act(() => useStore.getState().appendWorkflowEvent(ev(12, 'step.attempt.started', { step_id: 'report', attempt: 1 })))
    expect(consoleName()).toBe('Step collect')

    // Switching it on picks the running step again.
    fireEvent.click(follow)
    expect(follow).toHaveAttribute('aria-checked', 'true')
    expect(consoleName()).toBe('Step report')

    // Switching it off without a pick keeps the step it shows.
    fireEvent.click(follow)
    expect(follow).toHaveAttribute('aria-checked', 'false')
    expect(consoleName()).toBe('Step report')
    await flush()
  })

  it('is gone once the run has ended, and a failed run opens on its failed step', () => {
    stubFetch()
    render(
      <RunDetail
        run={inspection('failed', [step('collect', 'completed', { result: result('ok') }), step('check', 'failed', { error_kind: 'timeout' }), step('report', 'skipped', { attempts: 0 })])}
        allRuns={[]}
      />,
    )
    expect(screen.queryByRole('switch', { name: 'Follow the running step' })).toBeNull()
    expect(consoleName()).toBe('Step check')
    expect(screen.getByRole('tab', { name: 'Output' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: /Resume/ })).toBeInTheDocument()
  })

  it('a picked Result gives way to Follow when the run is resumed', () => {
    stubFetch()
    const { rerender } = render(<RunDetail run={inspection('cancelled', three('skipped', 'pending'))} allRuns={[]} />)
    fireEvent.click(within(screen.getByTestId('rail-result')).getByRole('button'))
    expect(screen.getByTestId('run-result-view')).toBeInTheDocument()
    rerender(<RunDetail run={inspection('running', three('running', 'pending'))} allRuns={[]} />)
    expect(screen.queryByTestId('run-result-view')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Follow the running step' })).toHaveAttribute('aria-checked', 'true')
    expect(consoleName()).toBe('Step check')
  })
})

// ── The step rail ─────────────────────────────────────────────────────────

describe('the step rail', () => {
  const FIVE: WorkflowSpec = {
    name: 'nightly-check',
    steps: ['collect', 'shape', 'check', 'notify', 'report'].map(id => specStep(id)),
  }

  it('shows a finished, a failed, a skipped and a not-run step of a run that ended', async () => {
    stubFetch(specRoute(FIVE))
    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect' }),
        ev(29, 'step.completed', { step_id: 'collect', state: 'completed' }),
        ev(30, 'step.started', { step_id: 'shape' }),
        ev(31, 'step.completed', { step_id: 'shape', state: 'completed' }),
        ev(32, 'step.started', { step_id: 'check' }),
        ev(50, 'step.failed', { step_id: 'check' }),
        ev(51, 'step.skipped', { step_id: 'notify', reason: 'upstream_halt' }),
        ev(52, 'run.failed'),
      ],
    })
    render(
      <RunDetail
        run={inspection('failed', [
          step('collect', 'completed', { result: result('Found 3 orders stuck in PROCESSING.\nTwo share a cause.\nA third line.') }),
          step('shape', 'completed', { output_json: '{"rows": 3}' }),
          step('check', 'failed', { attempts: 2, error_kind: 'timeout', error: 'Step timed out after 600 s\nTraceback (most recent call last)' }),
          step('notify', 'skipped', { attempts: 0 }),
          step('report', 'pending'),
        ])}
        allRuns={[]}
      />,
    )
    await flush()
    // Finished: a check, its duration, its agent, and the first two lines of its answer.
    expect(screen.getByTestId('rail-step-1')).toHaveAttribute('data-state', 'completed')
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('28 s')
    expect(within(screen.getByTestId('rail-step-1')).getByText('collect-agent')).toBeInTheDocument()
    expect(within(screen.getByTestId('rail-step-1')).getByText('· kiro_cli')).toBeInTheDocument()
    expect(screen.getByTestId('rail-answer-1')).toHaveTextContent('Found 3 orders stuck in PROCESSING. Two share a cause.')
    expect(screen.getByTestId('rail-answer-1')).not.toHaveTextContent('A third line.')
    expect(screen.getByTestId('rail-answer-2')).toHaveTextContent('{"rows":3}')
    // Failed: a cross, the error kind and the first line of the error.
    expect(screen.getByTestId('rail-step-3')).toHaveAttribute('data-state', 'failed')
    expect(screen.getByTestId('rail-detail-3')).toHaveTextContent('timeout · Step timed out after 600 s')
    expect(screen.getByTestId('rail-detail-3')).not.toHaveTextContent('Traceback')
    expect(screen.getByTestId('rail-chip-3')).toHaveTextContent('18 s')
    // Skipped, with why.
    expect(screen.getByTestId('rail-chip-4')).toHaveTextContent('Skipped')
    expect(screen.getByTestId('rail-detail-4')).toHaveTextContent('Skipped because an earlier step stopped the run.')
    // Never started, in a run that ended.
    expect(screen.getByTestId('rail-chip-5')).toHaveTextContent('Not run')
    expect(screen.getByTestId('rail-detail-5')).toHaveTextContent('The run ended before this step started.')
    expect(screen.getByTestId('rail-dot-5')).toHaveTextContent('5')
    // Each dot has its own shape: no dot reuses another state's icon.
    const icons = [1, 3, 4].map(n => screen.getByTestId(`rail-dot-${n}`).querySelector('svg')?.getAttribute('class'))
    expect(new Set(icons).size).toBe(3)
    // The line is green below finished steps, red below the failed one, grey after.
    expect(screen.getByTestId('rail-line-1')).toHaveClass('bg-emerald-500/70')
    expect(screen.getByTestId('rail-line-3')).toHaveClass('bg-red-500/60')
    expect(screen.getByTestId('rail-line-4')).toHaveClass('bg-gray-700')
    expect(screen.getByTestId('rail-progress')).toHaveTextContent('Steps · 3 of 5')
    // Each step names its state for a screen reader.
    expect(within(rail()).getByRole('button', { name: /^check.*failed$/ })).toBeInTheDocument()
    expect(within(rail()).getByRole('button', { name: /^report.*did not run$/ })).toBeInTheDocument()
    // The run's facts.
    const facts = screen.getByTestId('run-facts')
    expect(facts).toHaveTextContent(`Started${formatRunStart(at(0))}`)
    expect(facts).toHaveTextContent('Run idrun-1')
    expect(facts).toHaveTextContent('Tieryaml')
  })

  it('shows a running step with its attempt and params, and the waiting step after it', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(T0 + 48_000)
    stubFetch(specRoute())
    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect', ts: at(-30) }),
        ev(2, 'step.completed', { step_id: 'collect', state: 'completed', ts: at(-2) }),
        ev(3, 'step.started', { step_id: 'check', ts: at(0) }),
        ev(4, 'step.attempt.started', { step_id: 'check', attempt: 1, ts: at(0) }),
      ],
    })
    render(
      <RunDetail
        run={inspection('running', [step('collect', 'completed', { result: result('ok') }), step('check', 'running'), step('report', 'pending')], { started_at: at(-30) })}
        allRuns={[]}
      />,
    )
    await tick()
    expect(screen.getByTestId('rail-chip-2')).toHaveTextContent('0:48')
    await tick(1000)
    expect(screen.getByTestId('rail-chip-2')).toHaveTextContent('0:49')
    expect(screen.getByTestId('rail-detail-2')).toHaveTextContent('Attempt 1 of 2 · params: queue, hours')
    expect(screen.getByTestId('rail-chip-3')).toHaveTextContent('Waiting')
    expect(screen.getByTestId('rail-detail-3')).toHaveTextContent('Starts when check finishes.')
    expect(screen.getByTestId('rail-line-1')).toHaveClass('bg-emerald-500/70')
    expect(screen.getByTestId('rail-line-2')).toHaveClass('bg-blue-500/80')
    // The last step draws no line until the Result node exists.
    expect(screen.queryByTestId('rail-line-3')).toBeNull()
    expect(screen.queryByTestId('rail-result')).toBeNull()
    expect(screen.getByTestId('rail-progress')).toHaveTextContent('Steps · 2 of 3')
    expect(within(rail()).getByRole('button', { name: /^check/ })).toHaveAttribute('aria-current', 'step')
    expect(screen.getByTestId('rail-pulse-2')).toHaveClass('motion-safe:animate-pulse')
    expect(screen.getByTestId('run-facts')).toHaveTextContent('Duration1 min 19 s')
  })

  it('says the first step waits to start, a retried step\'s attempt, and a cancelled step', async () => {
    stubFetch(specRoute())
    const { unmount } = render(
      <RunDetail run={inspection('running', [step('collect', 'pending'), step('check', 'pending'), step('report', 'pending')])} allRuns={[]} />,
    )
    expect(screen.getByTestId('rail-detail-1')).toHaveTextContent('Waiting to start.')
    expect(screen.getByTestId('rail-detail-2')).toHaveTextContent('Starts when collect finishes.')
    expect(screen.getByTestId('rail-progress')).toHaveTextContent('Steps · 0 of 3')
    await flush()
    unmount()

    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect' }),
        ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 }),
        ev(3, 'step.attempt.failed', { step_id: 'collect', attempt: 1, error_kind: 'timeout' }),
        ev(4, 'step.attempt.started', { step_id: 'collect', attempt: 2 }),
      ],
    })
    const second = render(<RunDetail run={inspection('running', [step('collect', 'running'), step('check', 'pending')])} allRuns={[]} />)
    await flush()
    expect(screen.getByTestId('rail-step-1')).toHaveAttribute('data-state', 'retried')
    expect(screen.getByTestId('rail-detail-1')).toHaveTextContent('Attempt 2 of 4')
    expect(screen.getByTestId('rail-dot-1').querySelector('svg')).toHaveClass('motion-safe:animate-spin')
    second.unmount()

    resetRunStore({ wfEvents: [ev(1, 'step.started', { step_id: 'collect' }), ev(2, 'run.cancelled')] })
    render(<RunDetail run={inspection('cancelled', [step('collect', 'running'), step('check', 'pending')])} allRuns={[]} />)
    expect(screen.getByTestId('rail-chip-1')).toHaveTextContent('Cancelled')
    expect(screen.getByTestId('rail-detail-1')).toHaveTextContent('Stopped when the run was cancelled.')
    await flush()
  })
})

// ── The console ───────────────────────────────────────────────────────────

describe('the console', () => {
  const done = [
    step('collect', 'completed', { result: result('Found 3 rows.') }),
    step('check', 'completed', { result: result('Checked orders-dlq.') }),
    step('report', 'completed', { result: result('Wrote the report.') }),
  ]

  it('shows a step\'s params from the workflow file, with its agent, provider and call type', async () => {
    stubFetch(specRoute())
    render(<RunDetail run={inspection('completed', done)} allRuns={[]} />)
    await flush()
    fireEvent.click(within(rail()).getByRole('button', { name: /^check/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Params' }))
    const params = screen.getByTestId('step-params')
    expect(within(params).getByText('dlq-checker')).toBeInTheDocument()
    expect(within(params).getByText('kiro_cli')).toBeInTheDocument()
    expect(within(params).getByText('handoff')).toBeInTheDocument()
    const table = within(params).getByRole('table', { name: 'Params of step check' })
    expect(within(table).getAllByRole('row').map(r => r.textContent)).toEqual(['queueorders-dlq', 'hours24'])
    expect(screen.getByTestId('step-params-source')).toHaveTextContent('From the current workflow file.')
    fireEvent.click(within(rail()).getByRole('button', { name: /^report/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Params' }))
    expect(screen.getByTestId('step-params')).toHaveTextContent('This step has no params.')
  })

  it('says why a step has no params to show', async () => {
    stubFetch(url => (url.endsWith('/workflows/nightly-check') ? { status: 200, body: { name: 'nightly-check', source: 'x', path: '/wf/n.py' } } : undefined))
    const { unmount } = render(<RunDetail run={inspection('completed', done)} allRuns={[]} />)
    await flush()
    fireEvent.click(screen.getByRole('tab', { name: 'Params' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('This is a Python workflow, so its steps have no params in a workflow file.')
    unmount()

    stubFetch(specRoute({ name: 'nightly-check', steps: [specStep('collect')] }))
    render(<RunDetail run={inspection('completed', done)} allRuns={[]} />)
    await flush()
    fireEvent.click(screen.getByRole('tab', { name: 'Params' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('This step is not in the current workflow file.')
  })

  it('lists this step\'s events with their plain labels and outcome', () => {
    stubFetch()
    resetRunStore({
      wfEvents: [
        ev(1, 'step.started', { step_id: 'collect' }),
        ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 }),
        ev(3, 'terminal.created', { step_id: 'collect', terminal_id: 'ab12cd34' }),
        ev(29, 'step.completed', { step_id: 'collect', state: 'completed' }),
        ev(30, 'step.started', { step_id: 'report' }),
        ev(31, 'run.completed'),
      ],
    })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed', { result: result('Found 3 rows.') }), step('report', 'completed')])} allRuns={[]} />)
    expect(consoleName()).toBe('Step collect')
    fireEvent.click(screen.getByRole('tab', { name: 'Events' }))
    const items = within(screen.getByRole('list', { name: 'Events of step collect' })).getAllByRole('listitem')
    expect(items).toHaveLength(4)
    expect(items[0]).toHaveTextContent(`${formatLocalTime(at(1))}Step started`)
    expect(items[1]).toHaveTextContent('Attempt 1 started')
    expect(items[2]).toHaveTextContent('Terminal opened')
    expect(items[3]).toHaveTextContent('Step completed in 28 sCompleted')
    fireEvent.click(within(rail()).getByRole('button', { name: /^report/ }))
    fireEvent.click(screen.getByRole('tab', { name: 'Events' }))
    expect(within(screen.getByRole('list', { name: 'Events of step report' })).getAllByRole('listitem')).toHaveLength(1)
  })

  it('says when a step has no events yet', async () => {
    stubFetch()
    render(<RunDetail run={inspection('completed', [step('collect', 'completed')])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('tab', { name: 'Events' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('No events for this step yet.')
    await flush()
  })
})

describe('Copy', () => {
  it('copies the current tab\'s text and says so', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    stubFetch(url =>
      url.endsWith('/steps/collect/transcript')
        ? { status: 200, body: transcriptOf('$ collect\nFound 3 rows.') }
        : specRoute()(url),
    )
    resetRunStore({ wfEvents: [ev(1, 'step.started', { step_id: 'collect' }), ev(29, 'step.completed', { step_id: 'collect', state: 'completed' })] })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed', { terminal_id: 'ab12cd34', result: result('Found 3 rows.') })])} allRuns={[]} />)
    await flush()

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('Found 3 rows.'))
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
    expect(screen.getByTestId('console-copy-status')).toHaveTextContent('Copied the Output text')

    fireEvent.click(screen.getByRole('tab', { name: 'Params' }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('agent: orders-reader\nprovider: kiro_cli\ncall: handoff'))

    fireEvent.click(screen.getByRole('tab', { name: 'Events' }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(`${formatLocalTime(at(1))}  Step started\n${formatLocalTime(at(29))}  Step completed in 28 s`),
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }))
    await waitFor(() => expect(screen.getByTestId('step-terminal-text')).toHaveTextContent('Found 3 rows.'))
    // Copy turns on once the terminal hands its text to the console.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('$ collect\nFound 3 rows.'))
  })

  it('is off when the tab has nothing to copy, and says when copying fails', async () => {
    stubFetch()
    const { unmount } = render(<RunDetail run={inspection('running', [step('collect', 'pending')])} allRuns={[]} />)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeDisabled()
    unmount()
    // No Clipboard API and no execCommand, as in jsdom.
    render(<RunDetail run={inspection('completed', [step('collect', 'completed', { result: result('Found 3 rows.') })])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(await screen.findByRole('button', { name: 'Copy failed' })).toBeInTheDocument()
    await flush()
  })

  it('falls back to a hidden textarea and execCommand without the Clipboard API', async () => {
    const exec = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec })
    expect(await copyText('hello')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => false) })
    expect(await copyText('hello')).toBe(false)
    // A Clipboard API that refuses falls back too.
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec })
    expect(await copyText('hello')).toBe(true)
  })
})

// ── The events strip ──────────────────────────────────────────────────────

describe('the events strip', () => {
  const events = [
    ev(1, 'step.started', { step_id: 'collect' }),
    ev(2, 'step.attempt.started', { step_id: 'collect', attempt: 1 }),
    ev(3, 'terminal.created', { step_id: 'collect', terminal_id: 'ab12cd34' }),
    ev(29, 'step.completed', { step_id: 'collect', state: 'completed' }),
    ev(30, 'run.completed'),
  ]

  it('shows the newest three events, newest first, and All N events opens the timeline', async () => {
    stubFetch()
    resetRunStore({ wfEvents: events, selectedIndex: 4 })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed')])} allRuns={[]} />)
    const chips = screen.getAllByTestId('events-strip-chip')
    expect(chips.map(c => c.textContent)).toEqual([
      `${formatLocalTime(at(30))}Run completed in 30 s`,
      `${formatLocalTime(at(29))}Step completed in 28 s · collect`,
      `${formatLocalTime(at(3))}Terminal opened · collect`,
    ])
    expect(chips[0]).toHaveAttribute('data-newest', 'true')
    expect(chips[1]).not.toHaveAttribute('data-newest')
    expect(screen.queryByTestId('events-strip-gaps')).toBeNull()

    const all = screen.getByRole('button', { name: 'All 5 events' })
    all.focus()
    fireEvent.click(all)
    const dialog = screen.getByRole('dialog', { name: 'All events' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByRole('list', { name: 'Event timeline' })).toBeInTheDocument()
    expect(within(dialog).getByRole('slider', { name: /playback position/i })).toBeInTheDocument()
    // The pane under the timeline shows the selected event, a run-level one here.
    expect(within(dialog).getByTestId('terminal-no-step')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Close All events' })).toHaveFocus()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(all).toHaveFocus()
    await flush()
  })

  it('opens the timeline on a chip\'s event, closes on a click outside, and counts declared gaps', async () => {
    stubFetch()
    resetRunStore({ wfEvents: events, wfGaps: [{ after_seq: 3, before_seq: 29, missing_count: 2, reason: 'append_failed' }], selectedIndex: 4 })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed')])} allRuns={[]} />)
    expect(screen.getByTestId('events-strip-gaps')).toHaveTextContent('1 declared gap')
    fireEvent.click(screen.getAllByTestId('events-strip-chip')[2])
    expect(screen.getByTestId('timeline-event-3')).toHaveAttribute('aria-current', 'true')
    expect(screen.getByTestId('timeline-gap')).toBeInTheDocument()
    expect(useStore.getState().followLatest).toBe(false)
    fireEvent.click(screen.getByTestId('slide-over-backdrop'))
    expect(screen.queryByRole('dialog')).toBeNull()
    // The newest chip follows the newest event again.
    fireEvent.click(screen.getAllByTestId('events-strip-chip')[0])
    expect(useStore.getState()).toMatchObject({ followLatest: true, selectedIndex: 4 })
    await flush()
  })

  it('keeps focus inside the slide-over', async () => {
    stubFetch()
    resetRunStore({ wfEvents: events, selectedIndex: 4 })
    render(<RunDetail run={inspection('completed', [step('collect', 'completed')])} allRuns={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'All 5 events' }))
    const dialog = screen.getByRole('dialog', { name: 'All events' })
    const close = within(dialog).getByRole('button', { name: 'Close All events' })
    const buttons = within(dialog).getAllByRole('button')
    const last = buttons[buttons.length - 1]
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
    await flush()
  })

  it('says when there are no events yet', () => {
    stubFetch()
    render(<RunDetail run={inspection('running', [step('collect', 'pending')])} allRuns={[]} />)
    expect(screen.getByTestId('events-strip')).toHaveTextContent('No events yet.')
    expect(screen.getByRole('button', { name: 'All 0 events' })).toBeInTheDocument()
  })
})

// ── The Result node ───────────────────────────────────────────────────────

describe('the Result node', () => {
  it('appears once the run has ended and shows the result, the artifacts and run comparison', async () => {
    stubFetch(url => {
      if (url.endsWith('/workflows/runs/run-1/result')) {
        return { status: 200, body: { run_id: 'run-1', workflow_name: 'nightly-check', state: 'completed', started_at: at(0), steps: [] } }
      }
      if (url.endsWith('/workflows/runs/run-1/artifacts')) {
        return { status: 200, body: { run_id: 'run-1', artifacts: [{ input: 'report', path: '/tmp/report.md', available: true, size: 10 }] } }
      }
      return undefined
    })
    const { rerender } = render(<RunDetail run={inspection('running', [step('collect', 'running')])} allRuns={[]} />)
    expect(screen.queryByTestId('rail-result')).toBeNull()
    rerender(
      <RunDetail
        run={inspection('completed', [step('collect', 'completed', { result: result('ok') })], { finished_at: at(72) })}
        allRuns={[row('run-1', 'completed', { finished_at: at(72) }), row('run-2', 'completed')]}
      />,
    )
    const node = screen.getByTestId('rail-result')
    expect(within(node).getByTestId('rail-result-chip')).toHaveTextContent('Completed in 1 min 12 s')
    expect(screen.getByTestId('rail-line-1')).toHaveClass('bg-emerald-500/70')
    fireEvent.click(within(node).getByRole('button'))
    expect(within(node).getByRole('button')).toHaveAttribute('aria-current', 'step')
    expect(screen.queryByTestId('step-console')).toBeNull()
    const view = screen.getByTestId('run-result-view')
    expect(await within(view).findByText(/Final state:/)).toHaveTextContent('Final state: completed')
    expect(await within(view).findByRole('button', { name: /View report/ })).toBeInTheDocument()
    expect(within(view).getByRole('region', { name: 'Run comparison' })).toBeInTheDocument()
    expect(within(view).getByTestId('run-result-last-step')).toHaveTextContent('Last answer, from step collect')
    // Back to a step.
    fireEvent.click(within(rail()).getByRole('button', { name: /^collect/ }))
    expect(screen.queryByTestId('run-result-view')).toBeNull()
    expect(consoleName()).toBe('Step collect')
  })
})

// ── The console terminal ──────────────────────────────────────────────────

describe('the console terminal', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(T0)
  })

  it('says Watch only and when it was updated, and follows the output until the user scrolls up', async () => {
    const read = vi.spyOn(api, 'getWorkflowStepScreen').mockResolvedValue(screenOf('line 1\nline 2'))
    const onText = vi.fn()
    render(<StepTerminal variant="console" runId="run-1" stepId="check" state="running" terminalId="ab12cd34" runEnded={false} onText={onText} />)
    await tick()
    expect(screen.getByTestId('step-terminal')).toHaveAttribute('data-variant', 'console')
    expect(screen.getByText('terminal ab12cd34')).toBeInTheDocument()
    expect(screen.getByTestId('step-terminal-watch-only')).toHaveTextContent('Watch only')
    expect(screen.getByTestId('step-terminal-live')).toHaveTextContent('Live')
    expect(screen.getByTestId('step-terminal-updated')).toHaveTextContent('· updated 0 s ago')
    expect(onText).toHaveBeenLastCalledWith('line 1\nline 2')
    expect(screen.getByTestId('step-terminal-following')).toHaveTextContent('Following the output')
    expect(screen.queryByRole('button', { name: 'Jump to latest' })).toBeNull()

    // Scrolled up to read: the pill offers a jump back, and new output does not move the view.
    const pre = screen.getByTestId('step-terminal-text')
    Object.defineProperty(pre, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(pre, 'clientHeight', { configurable: true, value: 200 })
    pre.scrollTop = 100
    fireEvent.scroll(pre)
    expect(screen.queryByTestId('step-terminal-following')).toBeNull()
    read.mockResolvedValue(screenOf('line 1\nline 2\nline 3'))
    await tick(SCREEN_POLL_MS)
    expect(onText).toHaveBeenLastCalledWith('line 1\nline 2\nline 3')
    expect(pre.scrollTop).toBe(100)
    fireEvent.click(screen.getByRole('button', { name: 'Jump to latest' }))
    expect(pre.scrollTop).toBe(1000)
    expect(screen.getByTestId('step-terminal-following')).toBeInTheDocument()
  })

  it('offers Jump to latest in a saved transcript, and never says it is following one', async () => {
    vi.spyOn(api, 'getWorkflowStepTranscript').mockResolvedValue(transcriptOf('all done'))
    render(<StepTerminal variant="console" runId="run-1" stepId="check" state="completed" terminalId="ab12cd34" runEnded />)
    await tick()
    expect(screen.getByText('Saved transcript')).toBeInTheDocument()
    expect(screen.queryByTestId('step-terminal-live')).toBeNull()
    expect(screen.queryByTestId('step-terminal-following')).toBeNull()
    const pre = screen.getByTestId('step-terminal-text')
    Object.defineProperty(pre, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(pre, 'clientHeight', { configurable: true, value: 200 })
    pre.scrollTop = 0
    fireEvent.scroll(pre)
    fireEvent.click(screen.getByRole('button', { name: 'Jump to latest' }))
    expect(pre.scrollTop).toBe(1000)
    expect(screen.queryByRole('button', { name: 'Jump to latest' })).toBeNull()
  })

  it('shows its empty states in the console too', async () => {
    const { rerender } = render(
      <StepTerminal variant="console" runId="run-1" stepId="check" state="pending" terminalId={null} runEnded={false} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('This step has not started.')
    expect(screen.getByText('terminal')).toBeInTheDocument()
    vi.spyOn(api, 'getWorkflowStepTranscript').mockRejectedValue(apiError(400, 'malformed step id'))
    rerender(<StepTerminal variant="console" runId="run-1" stepId="check" state="completed" terminalId="ab12cd34" runEnded />)
    await tick()
    expect(screen.getByRole('alert')).toHaveTextContent('malformed step id')
  })
})

// ── The page height ───────────────────────────────────────────────────────

describe('the run page fills the window', () => {
  afterEach(() => {
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 768 })
  })

  it('reaches the bottom of the window, less its <main> padding, and follows a resize', () => {
    stubFetch()
    render(
      <main style={{ paddingBottom: '24px' }}>
        <RunDetail run={inspection('completed', [])} allRuns={[]} />
      </main>,
    )
    const page = screen.getByTestId('run-page')
    expect(page.style.height).toBe('744px')
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 900 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(page.style.height).toBe('876px')
    // A short window keeps a usable minimum; the page then scrolls.
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 300 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(page.style.height).toBe('480px')
  })
})
