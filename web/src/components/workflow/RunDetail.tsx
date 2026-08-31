// RunDetail — the selected run's inspection + playback surface (#504 / U8).
// Composes: StepSummary, WorkflowTimeline (playback transport + scrubber +
// declared-gap segments), SyncedTerminalPane (offset-synced to the selected
// event, degrading gracefully on the null-offset seam), FailureDetail,
// RunComparison, and the diagnostics/delete actions. Live-follows the SSE
// stream while the run is non-terminal.

import { useEffect, useState } from 'react'
import { Loader2, Radio } from 'lucide-react'
import type { RunInspection, RunSummaryRow } from '../../api'
import { useStore } from '../../store'
import { useEventFollow } from './useEventFollow'
import { StepSummary } from './StepSummary'
import { WorkflowTimeline } from './WorkflowTimeline'
import { SyncedTerminalPane } from './SyncedTerminalPane'
import { FailureDetail } from './FailureDetail'
import { RunComparison } from './RunComparison'
import { DiagnosticsExportButton } from './DiagnosticsExportButton'
import { DeleteRunButton } from './DeleteRunButton'
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
          <DiagnosticsExportButton runId={run.run_id} />
          <DeleteRunButton runId={run.run_id} workflowName={run.workflow_name} />
        </div>
      </div>

      <FailureDetail run={run} />

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
