// RunDetail — the selected run's inspection + playback surface (#504 / U8).
// Composes: StepSummary, WorkflowTimeline (playback transport + scrubber +
// declared-gap segments), SyncedTerminalPane (offset-synced to the selected
// event, degrading gracefully on the null-offset seam), FailureDetail,
// RunComparison, and the diagnostics/delete actions. Live-follows the SSE
// stream while the run is non-terminal.

import { useEffect, useState } from 'react'
import { Loader2, Radio, Ban, RotateCcw, ShieldCheck } from 'lucide-react'
import { api } from '../../api'
import type { RunInspection, RunSummaryRow } from '../../api'
import { useStore } from '../../store'
import { useEventFollow } from './useEventFollow'
import { StepSummary } from './StepSummary'
import { WorkflowTimeline } from './WorkflowTimeline'
import { SyncedTerminalPane } from './SyncedTerminalPane'
import { FailureDetail } from './FailureDetail'
import { RunArtifacts } from './RunArtifacts'
import { RunComparison } from './RunComparison'
import { DiagnosticsExportButton } from './DiagnosticsExportButton'
import { DeleteRunButton } from './DeleteRunButton'
import { RunResultPanel } from './RunResultPanel'
import { runCue } from './stateCues'

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
const TERMINAL_RUN_STATES = new Set(['completed', 'failed', 'cancelled'])

export function RunDetail({ run, allRuns }: RunDetailProps) {
  const events = useStore(s => s.wfEvents)
  const gaps = useStore(s => s.wfGaps)
  const selectedIndex = useStore(s => s.selectedIndex)
  const setSelectedIndex = useStore(s => s.setSelectedIndex)
  const appendWorkflowEvent = useStore(s => s.appendWorkflowEvent)
  const addWorkflowGap = useStore(s => s.addWorkflowGap)
  const followConnected = useStore(s => s.followConnected)
  const setFollowConnected = useStore(s => s.setFollowConnected)
  const cancelWorkflowRun = useStore(s => s.cancelWorkflowRun)
  const resumeWorkflowRun = useStore(s => s.resumeWorkflowRun)
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

  const cue = runCue(run.state)
  const selectedEvent = events[selectedIndex] ?? null

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`inline-flex items-center gap-1.5 ${cue.color}`}>
          <cue.Icon size={16} aria-hidden="true" />
          <span aria-hidden="true">{cue.glyph}</span>
          <span className="font-medium">{cue.label}</span>
          <span className="sr-only">{cue.sr}</span>
        </span>
        <h2 className="text-base font-semibold text-white">{run.workflow_name}</h2>
        <span className="text-xs font-mono text-gray-500">{run.run_id}</span>
        {!isTerminal && (
          <span
            className={`inline-flex items-center gap-1 text-[11px] ${
              followConnected ? 'text-emerald-400' : 'text-gray-500'
            }`}
            title={followConnected ? 'Live-following' : 'Reconnecting…'}
          >
            {followConnected ? (
              <Radio size={12} aria-hidden="true" />
            ) : (
              <Loader2 size={12} className="animate-spin" aria-hidden="true" />
            )}
            {followConnected ? 'Live' : 'Reconnecting'}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {!isTerminal && (
            <button
              type="button"
              disabled={acting}
              onClick={async () => { setActing(true); try { await cancelWorkflowRun(run.run_id) } finally { setActing(false) } }}
              className="inline-flex items-center gap-1 rounded-lg border border-amber-700/60 px-2 py-1 text-xs text-amber-300 hover:bg-amber-900/30 disabled:opacity-50"
            >
              <Ban size={13} aria-hidden="true" /> Cancel
            </button>
          )}
          {(effectiveState === 'failed' || effectiveState === 'cancelled') && (
            <button
              type="button"
              disabled={acting}
              onClick={async () => { setActing(true); try { await resume() } finally { setActing(false) } }}
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-700/60 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-900/30 disabled:opacity-50"
            >
              <RotateCcw size={13} aria-hidden="true" /> Resume
            </button>
          )}
          <DiagnosticsExportButton runId={run.run_id} />
          <DeleteRunButton runId={run.run_id} workflowName={run.workflow_name} />
        </div>
      </div>

      {resumePlanId && (
        <div role="alert" className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 space-y-2">
          <p className="text-sm text-amber-200">
            The workflow changed since its plan was approved, so resuming needs a new approval.
          </p>
          <p className="text-xs text-gray-400">
            Plan id: <code className="break-all font-mono text-gray-300">{resumePlanId}</code>
          </p>
          <button
            type="button"
            disabled={acting}
            onClick={async () => { setActing(true); try { await approveAndResume() } finally { setActing(false) } }}
            className="inline-flex items-center gap-2 rounded-lg bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50"
          >
            <ShieldCheck size={14} aria-hidden="true" /> Approve &amp; resume
          </button>
        </div>
      )}

      <FailureDetail run={run} />

      <RunResultPanel runId={run.run_id} terminal={isTerminal} state={effectiveState} />
      <RunArtifacts runId={run.run_id} terminal={isTerminal} />

      {/* Steps */}
      <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Steps</h3>
        <StepSummary run={run} />
      </div>

      {/* Timeline + playback */}
      <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Timeline</h3>
        <WorkflowTimeline
          events={events}
          gaps={gaps}
          selectedIndex={selectedIndex}
          onSelectIndex={setSelectedIndex}
        />
      </div>

      {/* Synced terminal (the null-offset graceful-degrade seam) */}
      <SyncedTerminalPane event={selectedEvent} />

      {/* Comparison */}
      <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3">
        <RunComparison runId={run.run_id} otherRuns={allRuns} />
      </div>
    </div>
  )
}
