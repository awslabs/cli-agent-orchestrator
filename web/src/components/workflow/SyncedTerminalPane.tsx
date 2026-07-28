// SyncedTerminalPane — terminal output synced to the selected playback event
// (#504 / U8, FR-7.3). Read-only, offset-ranged (U5) to the event's captured
// byte window.
//
// THE #769 SEAM — graceful degradation:
//   Events' `terminal_offset_start` / `terminal_offset_len` are currently ALWAYS
//   null (offset-capture emission is the unresolved #769 decision). This pane
//   therefore has exactly two branches:
//     1. offsets present (non-null start AND len) -> fetch the U5 range API and
//        render the output.
//     2. offsets null -> render a DOCUMENTED "sync pending (#769)" state.
//   It NEVER crashes and NEVER shows a silent blank. This is forward-compatible:
//   the moment #769 wires offset capture, branch 1 activates with ZERO change
//   here. See the paired test asserting BOTH branches.

import { useState, useEffect } from 'react'
import { Terminal as TermIcon, Loader2, Clock, AlertCircle } from 'lucide-react'
import { api, type WorkflowEvent, type ApiError } from '../../api'

interface SyncedTerminalPaneProps {
  event: WorkflowEvent | null
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

export function SyncedTerminalPane({ event }: SyncedTerminalPaneProps) {
  const [output, setOutput] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const offsetsPresent = hasTerminalOffsets(event)

  useEffect(() => {
    // Branch 2 (the current #769 reality): no offsets -> nothing to fetch. The
    // render below shows the documented degrade state; clear any stale output.
    if (!offsetsPresent || !event) {
      setOutput(null)
      setError(null)
      setLoading(false)
      return
    }

    // Branch 1: offsets present -> fetch the exact byte window via the U5 range
    // API. Guarded so a slow/failed fetch degrades to an error state, never a
    // crash or an infinite spinner.
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getTerminalOutputRange(
        event.terminal_id as string,
        event.terminal_offset_start as number,
        event.terminal_offset_len as number,
      )
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
  }, [event, offsetsPresent])

  // ── Branch 2 render: documented sync-pending degrade state (#769) ─────────
  if (!offsetsPresent) {
    return (
      <section
        aria-label="Terminal output pane"
        className="rounded-lg border border-gray-700/50 bg-gray-900/60 p-4"
      >
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <Clock size={16} className="text-gray-500 shrink-0" aria-hidden="true" />
          {/* No issue link here: this pane previously linked to
              github.com/anthropics/cli-agent-orchestrator/issues/769 — the wrong
              org (the repo is awslabs/) and not a real issue number. Rather than
              guess a replacement, state the condition plainly. */}
          <span>
            Terminal output sync pending — this run's events carry no terminal byte
            offsets yet, so there is no range to display.
          </span>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          This event carries no terminal byte range yet. Playback still works; the
          synced terminal view activates automatically once offset capture lands.
        </p>
      </section>
    )
  }

  // ── Branch 1 render: fetched, loading, or error over the U5 range ─────────
  return (
    <section
      aria-label={`Terminal output for ${event?.terminal_id ?? 'terminal'}`}
      className="rounded-lg border border-gray-700/50 bg-[#0d1117] overflow-hidden"
    >
      <header className="flex items-center gap-2 px-3 py-2 bg-gray-900 border-b border-gray-700/50">
        <TermIcon size={14} className="text-emerald-400 shrink-0" aria-hidden="true" />
        <span className="text-xs font-mono text-gray-300 truncate">{event?.terminal_id}</span>
        <span className="text-[10px] text-gray-500 ml-auto">
          bytes {event?.terminal_offset_start}–
          {(event?.terminal_offset_start ?? 0) + (event?.terminal_offset_len ?? 0)}
        </span>
      </header>
      <div className="p-3 min-h-[6rem]">
        {loading && (
          <div className="flex items-center gap-2 text-xs text-gray-500" role="status">
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
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
          <pre className="text-xs font-mono text-gray-300 whitespace-pre-wrap break-words max-h-64 overflow-auto">
            {output || <span className="text-gray-600 italic">No output in this range.</span>}
          </pre>
        )}
      </div>
    </section>
  )
}
