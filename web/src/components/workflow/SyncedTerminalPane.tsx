// SyncedTerminalPane — terminal output synced to the selected playback event
// (#504 / U8, FR-7.3). Read-only, offset-ranged (U5) to the event's captured
// byte window, with a tail fallback when no byte window exists.
//
// THE NULL-OFFSET SEAM — graceful degradation, THREE branches:
//   Events' `terminal_offset_start` / `terminal_offset_len` are currently ALWAYS
//   null: offset-capture emission is not wired yet (it needs a byte-position
//   API on the terminal log writer that does not exist today).
//     1. offsets present (non-null start AND len) -> fetch the U5 range API and
//        render the exact byte window.
//     2. offsets null BUT the event names a `terminal_id` -> fetch the
//        terminal's recent output and render its TAIL, labelled as a tail so the
//        degrade is visible and honest. This is the fallback the PR body
//        described; before the #526 review the branch rendered a static message
//        and fetched nothing, so the documented behaviour did not exist.
//     3. no `terminal_id` at all -> the documented sync-pending state. There is
//        genuinely nothing to fetch: no terminal to read from.
//   It NEVER crashes and NEVER shows a silent blank. Forward-compatible: the
//   moment offset capture is wired, branch 1 takes over with ZERO change here.
//   See the paired tests asserting ALL THREE branches.

import { useState, useEffect } from 'react'
import { Terminal as TermIcon, Loader2, Clock, AlertCircle } from 'lucide-react'
import { api, type WorkflowEvent, type ApiError } from '../../api'

interface SyncedTerminalPaneProps {
  event: WorkflowEvent | null
}

/**
 * CHARACTERS of tail shown in branch 2 (applied with `String.slice`, so the unit
 * is UTF-16 code units, not bytes).
 *
 * The value is taken from the 8 KiB the journal uses as its per-output capture cap
 * (`workflow_retention.OUTPUT_CAP_BYTES`) so the fallback shows roughly no more
 * than a captured window would have — but the two are NOT equivalent, and this
 * comment previously claimed they "match" (PR #526 review fix cycle 1). For
 * non-ASCII output 8192 characters is more than 8192 bytes. That is acceptable
 * here: this is a display bound on a read-only pane, not the durability cap.
 */
export const TAIL_MAX_CHARS = 8 * 1024

// Exported so a test can assert the branch decision directly (mutation guard).
export function hasTerminalOffsets(event: WorkflowEvent | null): boolean {
  return (
    !!event &&
    event.terminal_id != null &&
    event.terminal_offset_start != null &&
    event.terminal_offset_len != null
  )
}

/** True when there is a terminal to tail even though no byte window exists. */
export function canTailTerminal(event: WorkflowEvent | null): boolean {
  return !!event && event.terminal_id != null && !hasTerminalOffsets(event)
}

export function SyncedTerminalPane({ event }: SyncedTerminalPaneProps) {
  const [output, setOutput] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const offsetsPresent = hasTerminalOffsets(event)
  const tailable = canTailTerminal(event)

  useEffect(() => {
    // Branch 3: nothing to read from at all -> clear state; the render below
    // shows the documented sync-pending message.
    if (!event || (!offsetsPresent && !tailable)) {
      setOutput(null)
      setError(null)
      setLoading(false)
      return
    }

    // Branches 1 and 2 both reach an I/O boundary from a render effect, so both
    // are wrapped in the SAME catch: a failed fetch must degrade to a VISIBLE
    // error state, never a crash and never an infinite spinner.
    let cancelled = false
    setLoading(true)
    setError(null)

    const request = offsetsPresent
      ? // Branch 1 — the exact byte window via the U5 range API.
        api
          .getTerminalOutputRange(
            event.terminal_id as string,
            event.terminal_offset_start as number,
            event.terminal_offset_len as number,
          )
          .then(range => range.data)
      : // Branch 2 — no byte window: read the terminal's recent output and keep
        // its tail. This is NOT offset-exact (the header says so), so it must
        // never be presented as the window around the selected event.
        api
          .getTerminalOutput(event.terminal_id as string)
          .then(res => (res.output ?? '').slice(-TAIL_MAX_CHARS))

    request
      .then(data => {
        if (!cancelled) {
          setOutput(data)
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
  }, [event, offsetsPresent, tailable])

  // ── Branch 3 render: documented sync-pending state (no terminal to read) ───
  if (!offsetsPresent && !tailable) {
    return (
      <section
        aria-label="Terminal output pane"
        className="rounded-lg border border-gray-700/50 bg-gray-900/60 p-4"
      >
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <Clock size={16} className="text-gray-500 shrink-0" aria-hidden="true" />
          {/* Deliberately NO issue link: the number this pane once cited did not
              exist, under an org that was not this repo's. State the condition
              plainly instead of citing a tracker id that cannot be verified. */}
          <span>
            Terminal output sync pending — this event names no terminal, so there
            is nothing to read.
          </span>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          Playback still works; the synced terminal view activates automatically
          for events that carry a terminal, and becomes byte-exact once offset
          capture lands.
        </p>
      </section>
    )
  }

  // ── Branches 1 & 2 render: fetched, loading, or error ─────────────────────
  return (
    <section
      aria-label={`Terminal output for ${event?.terminal_id ?? 'terminal'}`}
      className="rounded-lg border border-gray-700/50 bg-[#0d1117] overflow-hidden"
    >
      <header className="flex items-center gap-2 px-3 py-2 bg-gray-900 border-b border-gray-700/50">
        <TermIcon size={14} className="text-emerald-400 shrink-0" aria-hidden="true" />
        <span className="text-xs font-mono text-gray-300 truncate">{event?.terminal_id}</span>
        {offsetsPresent ? (
          <span className="text-[10px] text-gray-500 ml-auto">
            bytes {event?.terminal_offset_start}–
            {(event?.terminal_offset_start ?? 0) + (event?.terminal_offset_len ?? 0)}
          </span>
        ) : (
          // The degrade must be VISIBLE: this is the terminal's recent tail, not
          // the window around the selected event, and saying so is the whole
          // point of implementing the fallback rather than faking precision.
          <span
            className="text-[10px] text-amber-400/90 ml-auto"
            data-testid="terminal-tail-notice"
            title="This event carries no byte offsets, so the pane shows the terminal's recent output tail instead of the exact window around this event."
          >
            recent tail (not synced to this event)
          </span>
        )}
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
            {output || (
              <span className="text-gray-600 italic">
                {offsetsPresent ? 'No output in this range.' : 'No terminal output yet.'}
              </span>
            )}
          </pre>
        )}
      </div>
    </section>
  )
}
