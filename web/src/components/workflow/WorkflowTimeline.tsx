// WorkflowTimeline — the ordered event index with playback (#504 / U8, FR-7).
//
// Renders the seq-ordered events; a DECLARED gap (a GapMarker the API returned)
// is a first-class HATCHED segment carrying the missing range — visually and
// semantically distinct from the empty/no-events state, and NEVER inferred from
// event numbering (BR-4). An ARIA live region announces the selected event as
// playback moves. Transport + ScrubBar drive the shared selectedIndex.

import { useState } from 'react'
import type { WorkflowEvent, GapMarker } from '../../api'
import { PlaybackTransport } from './PlaybackTransport'
import { ScrubBar } from './ScrubBar'
import { GAP_CUE } from './stateCues'

interface WorkflowTimelineProps {
  events: WorkflowEvent[]
  gaps: GapMarker[]
  selectedIndex: number
  onSelectIndex: (index: number) => void
}

export function WorkflowTimeline({
  events,
  gaps,
  selectedIndex,
  onSelectIndex,
}: WorkflowTimelineProps) {
  const [playing, setPlaying] = useState(false)
  const [focusScrub, setFocusScrub] = useState(false)

  // A declared gap is keyed by the seq of the event it precedes (its
  // before_seq), so we can render it immediately above that event.
  const gapByBeforeSeq = new Map<number, GapMarker>()
  for (const g of gaps) gapByBeforeSeq.set(g.before_seq, g)
  const gapBeforeSeqs = gaps.map(g => g.before_seq)

  const handleJump = (index: number) => {
    setPlaying(false)
    onSelectIndex(index)
    setFocusScrub(true)
  }

  // Empty state — explicitly NOT the same as a declared gap.
  if (events.length === 0) {
    return (
      <div
        className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-6 text-center text-sm text-gray-500"
        data-testid="timeline-empty"
      >
        No events recorded for this run yet.
      </div>
    )
  }

  const selected = events[selectedIndex]

  return (
    <div className="space-y-3">
      {/* Transport + scrubber */}
      <div className="flex flex-col gap-2">
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
        <ScrubBar
          events={events}
          gapBeforeSeqs={gapBeforeSeqs}
          selectedIndex={selectedIndex}
          onChange={handleJump}
          focusOnUpdate={focusScrub}
        />
      </div>

      {/* ARIA live region: announces the selected event as playback advances. */}
      <div aria-live="polite" className="sr-only" data-testid="timeline-live">
        {selected
          ? `Selected event ${selectedIndex + 1} of ${events.length}: ${selected.event_type}${
              selected.step_id ? `, step ${selected.step_id}` : ''
            }${selected.state ? `, state ${selected.state}` : ''}`
          : ''}
      </div>

      {/* Ordered event list — declared gaps render as hatched segments. */}
      <ol className="space-y-1" aria-label="Event timeline">
        {events.map((ev, i) => {
          const gap = gapByBeforeSeq.get(ev.seq)
          return (
            <li key={ev.seq}>
              {gap && (
                <div
                  data-testid="timeline-gap"
                  role="listitem"
                  aria-label={`${GAP_CUE.sr}: ${gap.missing_count} event(s) missing between seq ${gap.after_seq} and ${gap.before_seq}, reason ${gap.reason}`}
                  className="flex items-center gap-2 my-1 px-3 py-1.5 rounded border border-yellow-600/40 text-xs text-yellow-300 bg-[repeating-linear-gradient(45deg,rgba(250,204,21,0.12),rgba(250,204,21,0.12)_6px,transparent_6px,transparent_12px)]"
                >
                  <GAP_CUE.Icon size={14} aria-hidden="true" className={GAP_CUE.color} />
                  <span className="font-medium">Declared gap</span>
                  <span aria-hidden="true">{GAP_CUE.glyph}</span>
                  <span className="text-yellow-400/80">
                    {gap.missing_count} missing (seq {gap.after_seq}→{gap.before_seq}) · {gap.reason}
                  </span>
                </div>
              )}
              <button
                type="button"
                onClick={() => handleJump(i)}
                aria-current={i === selectedIndex}
                className={`w-full text-left flex items-center gap-3 px-3 py-1.5 rounded text-xs font-mono transition-colors ${
                  i === selectedIndex
                    ? 'bg-emerald-900/30 text-emerald-200 ring-1 ring-emerald-600/40'
                    : 'text-gray-400 hover:bg-gray-800/50'
                }`}
              >
                <span className="text-gray-600 tabular-nums w-10 shrink-0">#{ev.seq}</span>
                <span className="truncate">{ev.event_type}</span>
                {ev.step_id && <span className="text-gray-500 truncate">{ev.step_id}</span>}
                {ev.state && <span className="text-gray-500 ml-auto">{ev.state}</span>}
              </button>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
