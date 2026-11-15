// SyncedTerminalPane: the terminal of the selected timeline event's step
// (#504 / U8, FR-7.3). Read-only. Three branches:
//
//   1. The event carries a byte window (non-null `terminal_offset_start` and
//      `terminal_offset_len`): fetch that exact window through the U5 range
//      API. Offset capture is not wired yet, so events carry null offsets
//      today; this branch takes over unchanged the moment they populate.
//   2. The event belongs to a step: show the step's terminal through the step
//      routes (StepTerminal), the live screen while the step runs and the saved
//      transcript after. That is the step's terminal now, not as it was at the
//      selected event, and the pane says so.
//   3. A run-level event, or no event: there is no terminal, and the pane says
//      so instead of showing a blank.
//
// It never opens a WebSocket and never attaches to the terminal.

import { useState, useEffect } from 'react'
import { Terminal as TermIcon, Loader2, Clock, AlertCircle } from 'lucide-react'
import { api, type WorkflowEvent, type ApiError } from '../../api'
import { SPIN_CLASS } from './stateCues'
import { StepTerminal } from './StepTerminal'

/** What the pane knows about the selected event's step. */
export interface PaneStep {
  /** The step's live state, as the Flow shows it. */
  state: string
  /** The terminal the step's view reads (runView's stepTerminalId). */
  terminalId: string | null
}

interface SyncedTerminalPaneProps {
  event: WorkflowEvent | null
  /**
   * The event's step. Without it the step is read as settled, with the
   * event's own terminal id.
   */
  step?: PaneStep | null
  /** True once the run has ended. */
  runEnded?: boolean
}

// Exported so a test can assert the branch decision directly (mutation guard).
export function hasTerminalOffsets(event: WorkflowEvent | null): boolean {
  return (
    !!event &&
    event.terminal_id != null &&
    event.terminal_offset_start != null &&
    event.terminal_offset_len != null
  )
}

export function SyncedTerminalPane({ event, step, runEnded = false }: SyncedTerminalPaneProps) {
  if (event && hasTerminalOffsets(event)) return <OffsetWindow event={event} />

  if (event?.step_id) {
    const context = step ?? { state: 'completed', terminalId: event.terminal_id ?? null }
    return (
      <div className="space-y-1">
        <p className="text-[11px] text-amber-400/90" data-testid="terminal-step-notice">
          The terminal of step <span className="font-mono">{event.step_id}</span> now, not as it was at this event.
        </p>
        <StepTerminal
          runId={event.run_id}
          stepId={event.step_id}
          state={context.state}
          terminalId={context.terminalId}
          runEnded={runEnded}
        />
      </div>
    )
  }

  return (
    <section
      aria-label="Terminal output pane"
      className="rounded-lg border border-gray-700/50 bg-gray-900/60 p-4"
      data-testid="terminal-no-step"
    >
      <div className="flex items-center gap-2 text-sm text-gray-400">
        <Clock size={16} className="text-gray-500 shrink-0" aria-hidden="true" />
        <span>
          {event
            ? 'This event belongs to the run, not to a step, so it has no terminal.'
            : "Select an event to see its step's terminal."}
        </span>
      </div>
    </section>
  )
}

/** Branch 1: the exact byte window an event carries, through the U5 range API. */
function OffsetWindow({ event }: { event: WorkflowEvent }) {
  const [output, setOutput] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const terminalId = event.terminal_id as string
  const start = event.terminal_offset_start as number
  const length = event.terminal_offset_len as number

  useEffect(() => {
    // A failed fetch degrades to a VISIBLE error state, never a crash and never
    // an infinite spinner.
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getTerminalOutputRange(terminalId, start, length)
      .then(range => {
        if (!cancelled) {
          setOutput(range.data)
          setLoading(false)
        }
      })
      .catch((e: ApiError) => {
        if (!cancelled) {
          setError(e?.detail || e?.message || 'Failed to read terminal output')
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [terminalId, start, length])

  return (
    <section
      aria-label={`Terminal output for ${terminalId}`}
      className="rounded-lg border border-gray-700/50 bg-[#0d1117] overflow-hidden"
    >
      <header className="flex items-center gap-2 px-3 py-2 bg-gray-900 border-b border-gray-700/50">
        <TermIcon size={14} className="text-emerald-400 shrink-0" aria-hidden="true" />
        <span className="text-xs font-mono text-gray-300 truncate">{terminalId}</span>
        <span className="text-[10px] text-gray-500 ml-auto">
          bytes {start}–{start + length}
        </span>
      </header>
      <div className="p-3 min-h-24">
        {loading && (
          <div className="flex items-center gap-2 text-xs text-gray-500" role="status">
            <Loader2 size={14} className={SPIN_CLASS} aria-hidden="true" />
            Loading terminal output…
          </div>
        )}
        {error && !loading && (
          <div className="flex items-center gap-2 text-xs text-red-400" role="alert">
            <AlertCircle size={14} aria-hidden="true" />
            {error}
          </div>
        )}
        {!loading && !error && (
          <pre className="text-xs font-mono text-gray-300 whitespace-pre-wrap wrap-break-word max-h-64 overflow-auto">
            {output || <span className="text-gray-600 italic">No output in this range.</span>}
          </pre>
        )}
      </div>
    </section>
  )
}
