// RunList — the workflow-run list (#504 / U8, FR-10.3), the main content of
// the Workflows home. Consumes #505's FINAL RunSummaryRow shape (run_id,
// workflow_name, state, tier, started_at, finished_at, current_step_id) — read,
// not built here. Renders the loading / empty / error states explicitly.
//
// One row per run: its state icon and label, the workflow, the short run id,
// when it started, how long it ran (ticking while live) and how far it got,
// such as "2 of 3" (useRunProgress reads that from each run's inspection). A
// row click opens the run page.

import { Loader2, AlertCircle, Inbox, ChevronRight } from 'lucide-react'
import type { RunSummaryRow } from '../../api'
import { Elapsed, runCue, SPIN_CLASS } from './stateCues'
import { eventTimeMs, formatDuration } from './runView'
import { formatRunStart, runSeconds, shortRunId, type StepProgress } from './runPage'

interface RunListProps {
  runs: RunSummaryRow[]
  /** The run the page shows, if any. */
  selectedRunId?: string | null
  loading: boolean
  error: string | null
  onSelect: (runId: string) => void
  /** Each run's step progress, when it has been read. */
  progress?: Record<string, StepProgress>
}

const LIVE_STATES = new Set(['running', 'pending'])
const COLUMNS = 'grid-cols-[7.5rem_minmax(0,1fr)_7.5rem_9.5rem_6rem_4.5rem_1rem]'

function Duration({ run }: { run: RunSummaryRow }) {
  const seconds = runSeconds(run.started_at, run.finished_at)
  if (seconds != null) return <>{formatDuration(seconds)}</>
  const start = eventTimeMs(run.started_at)
  if (LIVE_STATES.has(run.state) && start != null) return <Elapsed since={start} />
  return <span className="text-gray-600">Unknown</span>
}

export function RunList({ runs, selectedRunId = null, loading, error, onSelect, progress = {} }: RunListProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-500 py-8 justify-center" role="status">
        <Loader2 size={16} className="animate-spin" aria-hidden="true" />
        Loading runs…
      </div>
    )
  }
  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-red-400 py-8 justify-center" role="alert">
        <AlertCircle size={16} aria-hidden="true" />
        {error}
      </div>
    )
  }
  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 text-sm text-gray-500 py-12" data-testid="runlist-empty">
        <Inbox size={24} className="text-gray-600" aria-hidden="true" />
        No workflow runs yet.
      </div>
    )
  }

  return (
    <div>
      <div
        aria-hidden="true"
        className={`grid ${COLUMNS} gap-3 border-b border-gray-800 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-gray-500`}
      >
        <span>State</span>
        <span>Workflow</span>
        <span>Run</span>
        <span>Started</span>
        <span>Duration</span>
        <span>Steps</span>
        <span />
      </div>
      <ul aria-label="Workflow runs" className="divide-y divide-gray-800/70">
        {runs.map(run => {
          const cue = runCue(run.state)
          const active = run.run_id === selectedRunId
          const steps = progress[run.run_id]
          return (
            <li key={run.run_id}>
              <button
                type="button"
                onClick={() => onSelect(run.run_id)}
                aria-current={active ? 'true' : undefined}
                title={`Open run ${run.run_id}`}
                data-testid={`run-row-${run.run_id}`}
                className={`grid w-full ${COLUMNS} items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-gray-800/40 focus:outline-hidden focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500 ${
                  active ? 'bg-emerald-900/20' : ''
                }`}
              >
                <span className={`inline-flex min-w-0 items-center gap-1.5 text-xs font-medium ${cue.color}`}>
                  <cue.Icon size={14} aria-hidden="true" className={run.state === 'running' ? SPIN_CLASS : undefined} />
                  <span className="truncate">{cue.label}</span>
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-gray-100">{run.workflow_name}</span>
                  <span className="block text-[11px] text-gray-500">{run.tier}</span>
                </span>
                <span className="truncate font-mono text-xs text-gray-400">{shortRunId(run.run_id)}</span>
                <span className="truncate text-xs tabular-nums text-gray-400">{formatRunStart(run.started_at) || 'Unknown'}</span>
                <span className="truncate text-xs tabular-nums text-gray-400" data-testid="run-row-duration">
                  <Duration run={run} />
                </span>
                <span className="text-xs tabular-nums text-gray-400" data-testid="run-row-steps">
                  {steps && steps.total > 0 ? `${steps.reached} of ${steps.total}` : ''}
                </span>
                <ChevronRight size={14} aria-hidden="true" className="text-gray-600" />
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
