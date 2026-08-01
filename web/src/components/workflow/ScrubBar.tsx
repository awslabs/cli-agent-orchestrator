// ScrubBar — an accessible playback scrubber over the event index (#504 / U8,
// FR-7). A native ARIA slider (role="slider") so it is keyboard-operable
// (Arrow/Home/End) and screen-reader announced out of the box. Focus is managed
// on the thumb; the value text names the selected event, not just its number.

import { useRef, useEffect } from 'react'
import type { WorkflowEvent, GapMarker } from '../../api'

interface ScrubBarProps {
  events: WorkflowEvent[]
  /** Seqs at which a DECLARED gap precedes the event (hatched tick markers). */
  gapBeforeSeqs: number[]
  /**
   * The declared gaps themselves. Needed because a TRAILING gap's before_seq is
   * a sentinel (high_water + 1) that matches no event, so it cannot be rendered
   * from `gapBeforeSeqs` alone — it needs its own missing_count/after_seq to
   * describe the hole. Optional so existing callers keep working.
   */
  gaps?: GapMarker[]
  selectedIndex: number
  onChange: (index: number) => void
  /** When true, move focus to the thumb (e.g. after a jump). */
  focusOnUpdate?: boolean
}

export function ScrubBar({
  events,
  gapBeforeSeqs,
  gaps,
  selectedIndex,
  onChange,
  focusOnUpdate,
}: ScrubBarProps) {
  const thumbRef = useRef<HTMLDivElement>(null)
  const max = Math.max(0, events.length - 1)
  const selected = events[selectedIndex]
  const gapSet = new Set(gapBeforeSeqs)

  // PR #526 review (BLOCKING): a TRAILING declared gap — "the run ended and the
  // last N events were lost" — carries before_seq = high_water + 1, a sentinel
  // that by construction matches NO stored event. Rendering ticks only inside
  // events.map() therefore dropped it entirely: the most severe loss the API can
  // declare was invisible on the scrub bar. Any gap sitting past the last
  // event's seq is rendered as its own tick at the END of the bar.
  const lastSeq = events.length ? events[events.length - 1].seq : 0
  const trailingGaps = (gaps ?? []).filter(g => g.before_seq > lastSeq)

  useEffect(() => {
    if (focusOnUpdate) thumbRef.current?.focus()
  }, [focusOnUpdate, selectedIndex])

  const handleKey = (e: React.KeyboardEvent) => {
    let next = selectedIndex
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = selectedIndex + 1
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = selectedIndex - 1
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = max
    else if (e.key === 'PageUp') next = selectedIndex + 5
    else if (e.key === 'PageDown') next = selectedIndex - 5
    else return
    e.preventDefault()
    onChange(Math.min(Math.max(0, next), max))
  }

  // "N event(s) lost after event M" — the trailing hole in words. Every tick
  // here is aria-hidden (the slider owns the accessible name), so this same
  // sentence is appended to aria-valuetext below: the declaration must not be
  // reachable only by sighted hover over a hatched tick.
  const trailingText = trailingGaps
    .map(
      g =>
        `Trailing gap: ${g.missing_count} event(s) lost after event ${g.after_seq}` +
        ` (${g.reason})`
    )
    .join('. ')

  const baseValueText = selected
    ? `Event ${selectedIndex + 1} of ${events.length}: ${selected.event_type}${
        selected.step_id ? ` (${selected.step_id})` : ''
      }`
    : 'No events'
  const valueText = trailingText ? `${baseValueText}. ${trailingText}` : baseValueText

  return (
    <div className="w-full">
      <div
        ref={thumbRef}
        role="slider"
        tabIndex={events.length ? 0 : -1}
        aria-label="Playback position"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={selectedIndex}
        aria-valuetext={valueText}
        onKeyDown={handleKey}
        className="relative h-6 w-full rounded bg-gray-800 focus:outline-none focus:ring-2 focus:ring-emerald-500 cursor-pointer"
      >
        {/* Progress fill */}
        <div
          className="absolute top-0 left-0 h-full rounded bg-emerald-600/40"
          style={{ width: max === 0 ? '100%' : `${(selectedIndex / max) * 100}%` }}
        />
        {/* Event ticks — a declared gap is a HATCHED tick (non-color: a striped
            fill + title), distinct from a normal event tick. */}
        {events.map((ev, i) => {
          const isGap = gapSet.has(ev.seq)
          const left = max === 0 ? 0 : (i / max) * 100
          return (
            <button
              key={ev.seq}
              type="button"
              tabIndex={-1}
              aria-hidden="true"
              onClick={() => onChange(i)}
              title={
                isGap
                  ? `Declared gap before event ${i + 1} (${ev.event_type})`
                  : `Event ${i + 1}: ${ev.event_type}`
              }
              className={`absolute top-0 h-full w-1 -translate-x-1/2 ${
                isGap ? 'bg-[repeating-linear-gradient(45deg,#facc15,#facc15_2px,transparent_2px,transparent_4px)]' : 'bg-gray-600'
              } ${i === selectedIndex ? 'ring-1 ring-emerald-400' : ''}`}
              style={{ left: `${left}%` }}
            />
          )
        })}
        {/* Trailing gap tick — pinned to the END of the bar, hatched with the
            SAME striped fill as an interior gap (non-color encoding, no
            color-only signal). Not a jump target: there is no event to select,
            so it is a static marker rather than a button. */}
        {trailingGaps.map(g => (
          <div
            key={`trailing-${g.before_seq}`}
            data-testid="scrub-trailing-gap"
            aria-hidden="true"
            title={`Trailing gap: ${g.missing_count} event(s) lost after event ${g.after_seq} (${g.reason})`}
            className="absolute top-0 right-0 h-full w-1.5 bg-[repeating-linear-gradient(45deg,#facc15,#facc15_2px,transparent_2px,transparent_4px)]"
          />
        ))}
      </div>
      <p className="mt-1 text-[11px] text-gray-500">{valueText}</p>
    </div>
  )
}
