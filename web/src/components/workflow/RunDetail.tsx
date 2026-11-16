// RunDetail: the run page (#504 / U8), the Workflows tab's view of one run. It
// fills the window below the tab row:
//
// - RunBar: back to the Workflows home, the run's state, a switcher over the
//   runs of this workflow, Follow the running step, Cancel or Resume, and a
//   More menu (Export diagnostics, Compare runs, Delete run). Under it, when
//   they apply, the approve-and-resume prompt and the failure detail.
// - StepRail: the steps as a vertical stepper, then a Result node once the run
//   has ended, then the run's facts. The list scrolls on its own.
// - The console: StepConsole for the selected step (Terminal, Output, Params,
//   Events), or the run-level panels for Result (RunResultPanel, RunArtifacts,
//   RunComparison).
// - EventsStrip: the newest three events, and "All N events", which opens
//   WorkflowTimeline (playback, declared gaps) and the SyncedTerminalPane under
//   it in a slide-over.
//
// It live-follows the SSE stream while the run is non-terminal, and polls the
// inspection, as before. Step states come from the polled inspection and the
// followed events (runStepStates); call types, agents and params come from the
// workflow's spec, read best effort.
//
// Follow: while the run is live and the user has picked nothing, the console
// shows the running step and moves when the next one starts. Picking a step
// turns Follow off; turning it on picks the running step again. A run that has
// ended opens on the step that failed, else on its last step with an answer.
//
// The run page reads terminals only through the read-only screen and
// transcript routes; it never opens a terminal WebSocket.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Flag, ShieldCheck } from 'lucide-react'
import { api } from '../../api'
import type { RunInspection, RunSummaryRow, WorkflowSpecStep } from '../../api'
import { useStore } from '../../store'
import { useEventFollow } from './useEventFollow'
import { WorkflowTimeline } from './WorkflowTimeline'
import { SyncedTerminalPane } from './SyncedTerminalPane'
import { FailureDetail } from './FailureDetail'
import { RunArtifacts } from './RunArtifacts'
import { RunComparison } from './RunComparison'
import { RunResultPanel } from './RunResultPanel'
import { runFlowSteps, runStepStates, flowStateKey, flowStateView } from './WorkflowFlow'
import { eventTimeMs, latestStepStarts, stepActivity, stepTerminalId, formatDuration } from './runView'
import {
  finishedTarget,
  followTarget,
  labelledEvents,
  railSteps,
  runSeconds,
  stepProgress,
  type RunPick,
} from './runPage'
import { CHIP_TONES, SlideOver, useFillHeight } from './runPageParts'
import { RunBar } from './RunBar'
import { StepRail } from './StepRail'
import { StepConsole, type SpecStatus } from './StepConsole'
import { EventsStrip } from './EventsStrip'

interface RunDetailProps {
  run: RunInspection
  allRuns: RunSummaryRow[]
}

// A deliberate cross-language COPY of the Python source of truth,
// `workflow_journal._TERMINAL_RUN_STATES` (which api/main.py imports rather than
// redefining — see PR #526 review, N6). There is no import path from TS to
// Python, so this duplicate cannot be eliminated; keep it in step with the
// journal's set if a run state is ever added. The values are also part of the
// wire contract (`RunInspection.state`), so a drift here shows up as a live
// follower that never stops polling a finished run.
export const TERMINAL_RUN_STATES: ReadonlySet<string> = new Set(['completed', 'failed', 'cancelled'])

/** How often the detail re-reads a live run's inspection (the list polls every 10s). */
export const RUN_REFRESH_MS = 5000

type Slide = 'events' | 'compare' | null

export function RunDetail({ run, allRuns }: RunDetailProps) {
  const events = useStore(s => s.wfEvents)
  const gaps = useStore(s => s.wfGaps)
  const selectedIndex = useStore(s => s.selectedIndex)
  const setSelectedIndex = useStore(s => s.setSelectedIndex)
  const followLatest = useStore(s => s.followLatest)
  const followLatestEvent = useStore(s => s.followLatestEvent)
  const appendWorkflowEvent = useStore(s => s.appendWorkflowEvent)
  const addWorkflowGap = useStore(s => s.addWorkflowGap)
  const followConnected = useStore(s => s.followConnected)
  const setFollowConnected = useStore(s => s.setFollowConnected)
  const cancelWorkflowRun = useStore(s => s.cancelWorkflowRun)
  const resumeWorkflowRun = useStore(s => s.resumeWorkflowRun)
  const refreshSelectedRun = useStore(s => s.refreshSelectedRun)
  const selectWorkflowRun = useStore(s => s.selectWorkflowRun)
  const showSnackbar = useStore(s => s.showSnackbar)
  // Guards the cancel/resume buttons against double-submit while the request is
  // in flight (both actions re-fetch + re-select the run when they settle).
  const [acting, setActing] = useState(false)
  // Set when Resume came back approval_required (the script changed since its
  // plan was approved); offers the same human Approve step as the launcher.
  const [resumePlanId, setResumePlanId] = useState<string | null>(null)
  useEffect(() => setResumePlanId(null), [run.run_id])
  const resume = async () => {
    const outcome = await resumeWorkflowRun(run.run_id)
    setResumePlanId(outcome && 'approvalPlanId' in outcome ? outcome.approvalPlanId : null)
  }
  const approveAndResume = async () => {
    if (!resumePlanId) return
    try {
      await api.approveWorkflowPlan(resumePlanId)
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e?.detail || e?.message || 'Failed to approve plan' })
      return
    }
    await resume()
  }
  const act = (fn: () => Promise<unknown>) => async () => {
    setActing(true)
    try {
      await fn()
    } finally {
      setActing(false)
    }
  }

  // Live-follow only while the run has not gone terminal (a finished run has a
  // complete durable timeline already loaded; no stream to open).
  //
  // `run` is the inspection snapshot taken when the run was SELECTED and is
  // never reassigned, so on its own `run.state` can never observe the run
  // finishing — the follow loop would reconnect every 1.5s forever after
  // completion. The run LIST is polled every 10s, so we take the freshest state
  // from there and fall back to the snapshot when the row is absent.
  const liveRow = allRuns.find(r => r.run_id === run.run_id)
  const effectiveState = liveRow?.state ?? run.state
  // A run that vanished from the journal (deleted or retention-swept) is also
  // terminal for follow purposes; the server declares it via `event: run_absent`
  // and this latches so the stream is not re-opened.
  const [absent, setAbsent] = useState(false)
  const isTerminal = TERMINAL_RUN_STATES.has(effectiveState) || absent
  useEventFollow(
    isTerminal ? null : run.run_id,
    {
      onEvent: appendWorkflowEvent,
      onGap: addWorkflowGap,
      onConnectedChange: setFollowConnected,
      onAbsent: () => setAbsent(true),
    },
    { enabled: !isTerminal },
  )

  // Reset the absent latch when the selection changes to a different run.
  useEffect(() => setAbsent(false), [run.run_id])

  // Keep the snapshot itself current, not just the follow decision: steps settle
  // while the run is live (a script-tier run emits no events, so the follow
  // stream never carries them), and Cancel / Resume move a run between states.
  // Poll the inspection while the run is live, and re-read it once when the
  // polled list reports a state the snapshot has not caught up with.
  useEffect(() => {
    if (isTerminal) return
    const interval = setInterval(refreshSelectedRun, RUN_REFRESH_MS)
    return () => clearInterval(interval)
  }, [run.run_id, isTerminal, refreshSelectedRun])
  const liveState = liveRow?.state
  useEffect(() => {
    if (liveState && liveState !== run.state) refreshSelectedRun()
  }, [liveState, run.state, refreshSelectedRun])

  // The spec supplies each step's call type, agent, provider and params. Best
  // effort: a Python workflow has no steps here, and a deleted one is a 404.
  const [specSteps, setSpecSteps] = useState<WorkflowSpecStep[] | null>(null)
  const [specStatus, setSpecStatus] = useState<SpecStatus>('loading')
  useEffect(() => {
    let cancelled = false
    setSpecSteps(null)
    setSpecStatus('loading')
    api.getWorkflowSpec(run.workflow_name)
      .then(spec => {
        if (cancelled) return
        const steps = Array.isArray(spec?.steps) ? spec.steps : null
        setSpecSteps(steps)
        setSpecStatus(steps ? 'ready' : 'script')
      })
      .catch(() => {
        if (!cancelled) setSpecStatus('unavailable')
      })
    return () => { cancelled = true }
  }, [run.workflow_name])
  const flowSteps = useMemo(() => runFlowSteps(run, specSteps), [run, specSteps])
  const stepIds = useMemo(() => flowSteps.map(s => s.id), [flowSteps])
  const flowStates = useMemo(() => runStepStates(run, events), [run, events])

  // The steps in progress now, with their latest start time when the events
  // carry one. A run that has ended has none.
  const stepStarts = useMemo(() => latestStepStarts(events), [events])
  const running = useMemo(() => {
    const out: Record<string, number | null> = {}
    for (const [id, state] of Object.entries(flowStates)) {
      if (stepActivity(state, isTerminal) === 'running') out[id] = stepStarts[id] ?? null
    }
    return out
  }, [flowStates, stepStarts, isTerminal])

  const rail = useMemo(
    () => railSteps({ run, stepIds, states: flowStates, running, events, specSteps, runEnded: isTerminal }),
    [run, stepIds, flowStates, running, events, specSteps, isTerminal],
  )
  const progress = useMemo(
    () => stepProgress(rail.map(s => ({ state: s.state, attempts: s.inspection?.attempts }))),
    [rail],
  )
  const labelled = useMemo(() => labelledEvents(events, run.started_at), [events, run.started_at])

  // ── What the console shows ──────────────────────────────────────────────
  // A pick is tied to its run, so switching runs starts over. No pick means
  // Follow is on while the run is live; a Result pick only counts once the
  // run has ended, so a resumed run follows again.
  const [pick, setPick] = useState<{ runId: string; pick: RunPick } | null>(null)
  const rawPick = pick?.runId === run.run_id ? pick.pick : null
  const picked =
    rawPick?.kind === 'result'
      ? isTerminal ? rawPick : null
      : rawPick && stepIds.includes(rawPick.id) ? rawPick : null
  const following = !isTerminal && picked == null
  const autoId = isTerminal ? finishedTarget(run, stepIds, flowStates) : followTarget(stepIds, running, events, flowStates)
  const current: RunPick | null = picked ?? (autoId ? { kind: 'step', id: autoId } : null)

  const selectStep = (stepId: string) => {
    // The step Follow already shows: picking it changes nothing.
    if (following && current?.kind === 'step' && current.id === stepId) return
    setPick({ runId: run.run_id, pick: { kind: 'step', id: stepId } })
  }
  const selectResult = () => setPick({ runId: run.run_id, pick: { kind: 'result' } })
  const setFollow = (on: boolean) => {
    if (on) setPick(null)
    else if (current) setPick({ runId: run.run_id, pick: current })
  }

  const currentStep = current?.kind === 'step' ? rail.find(s => s.id === current.id) ?? null : null
  const terminalId = currentStep
    ? stepTerminalId(events, currentStep.id, currentStep.inspection?.terminal_id, currentStep.activity)
    : null
  const stepEvents = useMemo(
    () => (currentStep ? labelled.filter(row => row.event.step_id === currentStep.id) : []),
    [labelled, currentStep?.id],
  )

  /** The last step in run order that has an answer, so a halted run whose last
   * step was skipped still shows what its steps said. Falls back to the last step. */
  const lastAnswered = [...run.steps].reverse().find(s => s.result != null || s.output_json != null)
  const lastInspected = lastAnswered ?? (run.steps.length > 0 ? run.steps[run.steps.length - 1] : null)
  const lastStep = lastInspected ? { step: lastInspected, state: flowStates[lastInspected.id] ?? lastInspected.state } : null

  // ── The event timeline, in a slide-over ─────────────────────────────────
  const [slide, setSlide] = useState<Slide>(null)
  useEffect(() => setSlide(null), [run.run_id])
  const openEvents = (index: number | null) => {
    if (index != null) {
      if (index === events.length - 1) followLatestEvent()
      else setSelectedIndex(index)
    }
    setSlide('events')
  }
  const stepContext = (stepId: string) => {
    const step = run.steps.find(s => s.id === stepId) ?? null
    const state = flowStates[stepId] ?? step?.state ?? 'pending'
    return { state, terminalId: stepTerminalId(events, stepId, step?.terminal_id, stepActivity(state, isTerminal)) }
  }
  const selectedEvent = events[selectedIndex] ?? null
  const eventStep = selectedEvent?.step_id ? stepContext(selectedEvent.step_id) : null

  const startedMs = eventTimeMs(run.started_at)
  const seconds = runSeconds(run.started_at, liveRow?.finished_at ?? run.finished_at)

  const pageRef = useRef<HTMLDivElement>(null)
  const height = useFillHeight(pageRef)

  return (
    <div
      ref={pageRef}
      style={height != null ? { height } : undefined}
      className="flex min-h-[30rem] flex-col overflow-hidden rounded-xl border border-gray-800 bg-[#0a0d12]"
      data-testid="run-page"
    >
      <RunBar
        run={run}
        state={effectiveState}
        runEnded={isTerminal}
        connected={followConnected}
        startedMs={startedMs}
        seconds={seconds}
        onBack={() => void selectWorkflowRun(null)}
        follow={isTerminal ? null : { on: following, onChange: setFollow }}
        acting={acting}
        onCancel={isTerminal ? null : act(() => cancelWorkflowRun(run.run_id))}
        onResume={effectiveState === 'failed' || effectiveState === 'cancelled' ? act(resume) : null}
        onCompare={() => setSlide('compare')}
      />

      {(resumePlanId || run.state === 'failed') && (
        <div className="shrink-0 space-y-2 border-b border-gray-800 px-5 py-3">
          {resumePlanId && (
            <div role="alert" className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-amber-700/60 bg-amber-950/30 px-3 py-2">
              <div className="min-w-0 flex-1 space-y-0.5">
                <p className="text-sm text-amber-200">
                  The workflow changed since its plan was approved, so resuming needs a new approval.
                </p>
                <p className="text-xs text-gray-400">
                  Plan id: <code className="break-all font-mono text-gray-300">{resumePlanId}</code>
                </p>
              </div>
              <button
                type="button"
                disabled={acting}
                onClick={act(approveAndResume)}
                className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50"
              >
                <ShieldCheck size={14} aria-hidden="true" /> Approve &amp; resume
              </button>
            </div>
          )}
          <FailureDetail run={run} />
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(15rem,22.5rem)_minmax(0,1fr)]">
        <StepRail
          steps={rail}
          selected={current}
          onSelectStep={selectStep}
          onSelectResult={selectResult}
          runEnded={isTerminal}
          runState={effectiveState}
          runSeconds={seconds}
          progress={progress}
          startedAt={run.started_at}
          runId={run.run_id}
          tier={run.tier}
        />
        <section aria-label="Console" className="flex min-h-0 min-w-0 flex-col">
          <div className="flex min-h-0 flex-1 flex-col px-5 pb-4 pt-4">
            {current?.kind === 'result' ? (
              <section aria-label="Result of the run" className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1" data-testid="run-result-view">
                <header className="flex items-center gap-3">
                  <Flag size={16} aria-hidden="true" className="text-gray-400" />
                  <h2 className="text-base font-semibold text-white">Result</h2>
                  <span
                    className={`inline-flex h-6 items-center rounded-full px-2.5 text-xs font-semibold ${CHIP_TONES[flowStateKey(effectiveState)]}`}
                  >
                    {effectiveState === 'completed' && seconds != null
                      ? `Completed in ${formatDuration(seconds)}`
                      : flowStateView(effectiveState).text}
                  </span>
                </header>
                <RunResultPanel runId={run.run_id} terminal={isTerminal} state={effectiveState} lastStep={lastStep} />
                <RunArtifacts runId={run.run_id} terminal={isTerminal} />
                <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3">
                  <RunComparison runId={run.run_id} otherRuns={allRuns} />
                </div>
              </section>
            ) : currentStep ? (
              <StepConsole
                key={`${run.run_id}:${currentStep.id}`}
                runId={run.run_id}
                step={currentStep}
                terminalId={terminalId}
                runEnded={isTerminal}
                specStatus={specStatus}
                events={stepEvents}
              />
            ) : (
              <p className="text-sm text-gray-500" role="status">No steps recorded for this run.</p>
            )}
          </div>
          <EventsStrip rows={labelled} gaps={gaps} onOpen={openEvents} />
        </section>
      </div>

      {slide === 'events' && (
        <SlideOver title="All events" onClose={() => setSlide(null)} testId="events-slide-over">
          <div className="space-y-4">
            <WorkflowTimeline
              events={events}
              gaps={gaps}
              selectedIndex={selectedIndex}
              onSelectIndex={setSelectedIndex}
              runStartedAt={run.started_at}
              live={!isTerminal}
              following={followLatest}
              onLatest={followLatestEvent}
            />
            {/* The selected event's step terminal (byte-exact once offset capture lands) */}
            <SyncedTerminalPane
              event={selectedEvent}
              step={eventStep ? { state: eventStep.state, terminalId: eventStep.terminalId } : null}
              runEnded={isTerminal}
            />
          </div>
        </SlideOver>
      )}
      {slide === 'compare' && (
        <SlideOver title="Compare runs" onClose={() => setSlide(null)} testId="compare-slide-over">
          <RunComparison runId={run.run_id} otherRuns={allRuns} />
        </SlideOver>
      )}
    </div>
  )
}
