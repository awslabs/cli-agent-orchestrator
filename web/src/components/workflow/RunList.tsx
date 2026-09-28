// RunList — the workflow-run list (#504 / U8, FR-10.3). Consumes #505's FINAL
// RunSummaryRow shape (run_id, workflow_name, state, tier, started_at,
// finished_at, current_step_id) — read, not built here. Renders the
// loading / empty / error states explicitly; a row click selects the run.

import { Loader2, AlertCircle, Inbox } from 'lucide-react'
import type { RunSummaryRow } from '../../api'
import { runCue } from './stateCues'

interface RunListProps {
  runs: RunSummaryRow[]
  selectedRunId: string | null
  loading: boolean
  error: string | null
  onSelect: (runId: string) => void
}

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export function RunList({ runs, selectedRunId, loading, error, onSelect }: RunListProps) {
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
    <ul className="space-y-1" aria-label="Workflow runs">
      {runs.map(run => {
        const cue = runCue(run.state)
        const active = run.run_id === selectedRunId
        return (
          <li key={run.run_id}>
            <button
              type="button"
              onClick={() => onSelect(run.run_id)}
              aria-current={active}
              className={`w-full text-left px-3 py-2 rounded-lg transition-colors ${
                active ? 'bg-emerald-900/30 ring-1 ring-emerald-600/40' : 'hover:bg-gray-800/50'
              }`}
            >
              <div className="flex items-center gap-2">
                <span className={`inline-flex items-center gap-1 ${cue.color}`}>
                  <cue.Icon size={13} aria-hidden="true" />
                  <span aria-hidden="true">{cue.glyph}</span>
                  <span className="sr-only">{cue.sr}</span>
                </span>
                <span className="text-sm font-medium text-gray-200 truncate">{run.workflow_name}</span>
                <span className="text-[10px] text-gray-500 bg-gray-800 px-1.5 py-0.5 rounded ml-auto">
                  {run.tier}
                </span>
              </div>
              <div className="flex items-center gap-2 mt-0.5 text-[11px] text-gray-500 font-mono">
                <span className="truncate">{run.run_id.slice(0, 12)}</span>
                <span>·</span>
                <span>{cue.label}</span>
                <span>·</span>
                <span>{formatTime(run.started_at)}</span>
              </div>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
