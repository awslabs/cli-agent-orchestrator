// WorkflowTimeline — the ordered event index with playback (#504 / U8, FR-7).
//
// Renders the seq-ordered events; a DECLARED gap (a GapMarker the API returned)
// is a first-class HATCHED segment carrying the missing range — visually and
// semantically distinct from the empty/no-events state, and NEVER inferred from
// event numbering (BR-4). An ARIA live region announces the selected event as
// playback moves. Transport + ScrubBar drive the shared selectedIndex.
//
// Each row shows the event's local time, its step and a plain label, such as
// "Attempt 2 failed (timeout)" or "Step completed in 29 s" (runView's
// eventLabels). Only an outcome event carries an outcome badge, and the badge
// comes from the event's type: the `state` an event recorded is the step's
// state at that moment, so showing it made a finished run's first row still
// read "running". During a live run the selection follows the newest event
// until the user picks one; Latest jumps back and follows again.

import { Fragment, useMemo, useState } from 'react'
import { ChevronsDown } from 'lucide-react'
import type { WorkflowEvent, GapMarker } from '../../api'
import { PlaybackTransport } from './PlaybackTransport'
import { ScrubBar } from './ScrubBar'
import { GAP_CUE } from './stateCues'
import { flowStateView } from './WorkflowFlow'
import { eventLabels, eventOutcome, formatLocalTime } from './runView'

interface WorkflowTimelineProps {
  events: WorkflowEvent[]
  gaps: GapMarker[]
  selectedIndex: number
  onSelectIndex: (index: number) => void
  /** The run's start time, for a run outcome's duration when no run.started event was recorded. */
  runStartedAt?: string | null
  /** True while the run is live, so new events can still arrive. */
  live?: boolean
  /** True while the selection follows the newest event. */
  following?: boolean
  /** Select the newest event and follow new ones again. Without it there is no Latest button. */
  onLatest?: () => void
}

export function WorkflowTimeline({
  events,
  gaps,
  selectedIndex,
  onSelectIndex,
  runStartedAt,
  live = false,
  following = true,
  onLatest,
}: WorkflowTimelineProps) {
  const [playing, setPlaying] = useState(false)
  const [focusScrub, setFocusScrub] = useState(false)
  const labels = useMemo(() => eventLabels(events, runStartedAt), [events, runStartedAt])

  // An INTERIOR declared gap is keyed by the seq of the event it precedes (its
  // before_seq), so we can render it immediately above that event.
  const gapByBeforeSeq = new Map<number, GapMarker>()
  for (const g of gaps) gapByBeforeSeq.set(g.before_seq, g)
  const gapBeforeSeqs = gaps.map(g => g.before_seq)

  // A TRAILING declared gap ("the run ended and the last N events were lost")
  // carries before_seq = high_water + 1 — a sentinel that matches no stored
  // event — so keying only off event seqs dropped it from this list as well as
  // from the scrub bar (PR #526 review, BLOCKING). It renders AFTER the last
  // event, which is where the hole actually is.
  const lastSeq = events.length ? events[events.length - 1].seq : 0
  const trailingGaps = gaps.filter(g => g.before_seq > lastSeq)

  const handleJump = (index: number) => {
    setPlaying(false)
    onSelectIndex(index)
    setFocusScrub(true)
  }

  // Empty state — explicitly NOT the same as a declared gap.
  //
  // A run with NO stored events but a DECLARED trailing gap is the total-loss
  // shape: every append was swallowed, so the journal knows events existed and
  // were lost. Falling into the empty state here reported the most severe loss
  // the journal can express as the benign "nothing happened" (PR #526 review fix
  // cycle 1, BLOCKING) — exactly the distinction FR-3.3 / BR-4 exist to keep. So
  // the empty state requires BOTH no events and no declared gap.
  if (events.length === 0 && trailingGaps.length === 0) {
    return (
      <div
        className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-6 text-center text-sm text-gray-500"
        data-testid="timeline-empty"
      >
        No events recorded for this run yet.
      </div>
    )
  }

  // Total loss: no events survived, but the server declared the hole. Render the
  // declaration on its own — the transport/scrubber below index events, and there
  // are none to index.
  if (events.length === 0) {
    const lost = trailingGaps.reduce((n, g) => n + g.missing_count, 0)
    return (
      <div
        className="rounded-lg border border-amber-600/40 bg-amber-950/20 p-6 text-sm"
        data-testid="timeline-total-loss"
        role="alert"
      >
        <p className="font-medium text-amber-300">
          {GAP_CUE.glyph} {lost} event{lost === 1 ? '' : 's'} declared lost
        </p>
        <p className="mt-1 text-gray-400">
          No events were durably recorded for this run, but the journal shows{' '}
          {lost} {lost === 1 ? 'was' : 'were'} allocated — every append was lost.
          This is a data-loss report, not an idle run.
        </p>
      </div>
    )
  }

  const selected = events[selectedIndex]

  const latest = () => {
    setPlaying(false)
    // Following moves the selection on every new event, so the scrubber must
    // not take focus on each move.
    setFocusScrub(false)
    onLatest?.()
  }

  return (
    <div className="space-y-3">
      {/* Transport + scrubber */}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <PlaybackTransport
            eventCount={events.length}
            selectedIndex={selectedIndex}
            playing={playing}
            onSetIndex={i => {
              onSelectIndex(i)
              setFocusScrub(false)
            }}
            onSetPlaying={setPlaying}
          />
          {onLatest && live && !following && (
            <button
              type="button"
              onClick={latest}
              title="Select the newest event and follow new ones"
              className="inline-flex items-center gap-1 rounded-lg border border-emerald-700/60 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-900/30"
            >
              <ChevronsDown size={13} aria-hidden="true" /> Latest
            </button>
          )}
          {onLatest && following && live && (
            <span className="text-[11px] text-gray-500" data-testid="timeline-following">
              Following the newest event
            </span>
          )}
        </div>
        <ScrubBar
          events={events}
          gapBeforeSeqs={gapBeforeSeqs}
          gaps={gaps}
          selectedIndex={selectedIndex}
          onChange={handleJump}
          focusOnUpdate={focusScrub}
        />
      </div>

      {/* ARIA live region: announces the selected event as playback advances. */}
      <div aria-live="polite" className="sr-only" data-testid="timeline-live">
        {selected
          ? `Selected event ${selectedIndex + 1} of ${events.length}: ${labels[selectedIndex]}${
              selected.step_id ? `, step ${selected.step_id}` : ''
            }`
          : ''}
      </div>

      {/* Ordered event list — declared gaps render as hatched segments. */}
      <ol className="space-y-1" aria-label="Event timeline">
        {events.map((ev, i) => {
          const gap = gapByBeforeSeq.get(ev.seq)
          const outcome = eventOutcome(ev)
          const badge = outcome ? flowStateView(outcome) : null
          return (
            <Fragment key={ev.seq}>
              {/* A declared gap is its OWN <li> sibling, not a nested one: an
                  <li> is already implicitly role="listitem", so putting the
                  marker inside the event's <li> and giving it role="listitem"
                  too nested two listitems and confused assistive tech. As a
                  sibling <li> it is a real list item, which is also what keeps
                  its aria-label exposed (a bare roleless div's label is not). */}
              {gap && (
                <li
                  data-testid="timeline-gap"
                  aria-label={`${GAP_CUE.sr}: ${gap.missing_count} event(s) missing between seq ${gap.after_seq} and ${gap.before_seq}, reason ${gap.reason}`}
                  className="flex items-center gap-2 my-1 px-3 py-1.5 rounded-sm border border-yellow-600/40 text-xs text-yellow-300 bg-[repeating-linear-gradient(45deg,rgba(250,204,21,0.12),rgba(250,204,21,0.12)_6px,transparent_6px,transparent_12px)]"
                >
                  <GAP_CUE.Icon size={14} aria-hidden="true" className={GAP_CUE.color} />
                  <span className="font-medium">Declared gap</span>
                  <span aria-hidden="true">{GAP_CUE.glyph}</span>
                  <span className="text-yellow-400/80">
                    {gap.missing_count} missing (seq {gap.after_seq}→{gap.before_seq}) · {gap.reason}
                  </span>
                </li>
              )}
              <li>
                <button
                  type="button"
                  onClick={() => handleJump(i)}
                  aria-current={i === selectedIndex}
                  title={ev.event_type}
                  data-testid={`timeline-event-${ev.seq}`}
                  className={`w-full text-left flex items-center gap-3 px-3 py-1.5 rounded text-xs transition-colors ${
                    i === selectedIndex
                      ? 'bg-emerald-900/30 text-emerald-200 ring-1 ring-emerald-600/40'
                      : 'text-gray-400 hover:bg-gray-800/50'
                  }`}
                >
                  <span className="text-gray-600 font-mono tabular-nums w-10 shrink-0">#{ev.seq}</span>
                  <span className="text-gray-500 font-mono tabular-nums w-20 shrink-0" data-testid="timeline-time">
                    {formatLocalTime(ev.ts)}
                  </span>
                  <span className="text-gray-500 font-mono truncate w-28 shrink-0" data-testid="timeline-step">
                    {ev.step_id ?? ''}
                  </span>
                  <span className="truncate" data-testid="timeline-label">{labels[i]}</span>
                  {badge && (
                    <span
                      className={`ml-auto inline-flex shrink-0 items-center gap-1 ${badge.cue.color}`}
                      data-testid="timeline-outcome"
                    >
                      <badge.cue.Icon size={12} aria-hidden="true" />
                      <span aria-hidden="true">{badge.cue.glyph}</span>
                      <span>{badge.text}</span>
                    </span>
                  )}
                </button>
              </li>
            </Fragment>
          )
        })}
        {/* Trailing declared gap(s) — the run ended with its last append(s)
            swallowed. Same hatched, labelled <li> treatment as an interior gap
            (so the encoding is not color-only), positioned after the final
            event because that is where the hole is. */}
        {trailingGaps.map(gap => (
          <li
            key={`trailing-${gap.before_seq}`}
            data-testid="timeline-trailing-gap"
            aria-label={`${GAP_CUE.sr}: ${gap.missing_count} event(s) lost after seq ${gap.after_seq}; the run ended before they were recorded, reason ${gap.reason}`}
            className="flex items-center gap-2 my-1 px-3 py-1.5 rounded-sm border border-yellow-600/40 text-xs text-yellow-300 bg-[repeating-linear-gradient(45deg,rgba(250,204,21,0.12),rgba(250,204,21,0.12)_6px,transparent_6px,transparent_12px)]"
          >
            <GAP_CUE.Icon size={14} aria-hidden="true" className={GAP_CUE.color} />
            <span className="font-medium">Trailing gap</span>
            <span aria-hidden="true">{GAP_CUE.glyph}</span>
            <span className="text-yellow-400/80">
              {gap.missing_count} event(s) lost after seq {gap.after_seq} · {gap.reason}
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}
