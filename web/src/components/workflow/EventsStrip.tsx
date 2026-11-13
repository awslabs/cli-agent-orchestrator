// EventsStrip: the row under the run page's console. The run's three newest
// events as chips, newest first and highlighted, each with its local time and
// runView's plain label. "All N events" opens the full timeline in a
// slide-over; a chip opens it on that event. A declared gap is counted here
// too, so a run that lost events says so without opening the timeline.

import { AlertTriangle, ChevronRight } from 'lucide-react'
import type { GapMarker } from '../../api'
import { formatLocalTime } from './runView'
import type { LabelledEvent } from './runPage'

export interface EventsStripProps {
  /** Every event of the run, labelled, in seq order. */
  rows: LabelledEvent[]
  gaps: GapMarker[]
  /** Open the timeline, on an event's index, or as it is when null. */
  onOpen: (index: number | null) => void
}

export function EventsStrip({ rows, gaps, onOpen }: EventsStripProps) {
  const newest = rows.slice(-3).reverse()
  const count = rows.length
  return (
    <div
      className="flex h-12 shrink-0 items-center gap-2.5 border-t border-gray-800 bg-[#0b1017] px-5 text-xs text-gray-400"
      data-testid="events-strip"
    >
      <span className="shrink-0 text-[10.5px] font-semibold uppercase tracking-wider text-gray-500">Events</span>
      <div className="flex min-w-0 flex-1 gap-2.5 overflow-hidden">
        {newest.length === 0 ? (
          <span className="text-gray-500">No events yet.</span>
        ) : (
          newest.map((row, i) => (
            <button
              key={row.event.seq}
              type="button"
              onClick={() => onOpen(row.index)}
              title={`${row.label}${row.event.step_id ? ` · ${row.event.step_id}` : ''}`}
              aria-label={`${i === 0 ? 'Newest event' : 'Event'}: ${row.label}${row.event.step_id ? `, step ${row.event.step_id}` : ''}, at ${formatLocalTime(row.event.ts)}`}
              data-testid="events-strip-chip"
              data-newest={i === 0 ? 'true' : undefined}
              className={`inline-flex h-[26px] items-center gap-2 rounded-md border bg-[#0f151d] px-2.5 hover:bg-gray-800/60 ${
                i === 0 ? 'max-w-[24rem] shrink-0 border-blue-400/45 text-blue-100' : 'min-w-0 max-w-[19rem] border-gray-800 text-gray-300'
              }`}
            >
              <span className="shrink-0 font-mono text-[11px] tabular-nums text-gray-500">{formatLocalTime(row.event.ts)}</span>
              <span className="truncate">
                {row.label}
                {row.event.step_id ? ` · ${row.event.step_id}` : ''}
              </span>
            </button>
          ))
        )}
      </div>
      {gaps.length > 0 && (
        <span
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-yellow-600/40 px-2 py-0.5 text-yellow-300"
          data-testid="events-strip-gaps"
        >
          <AlertTriangle size={12} aria-hidden="true" />
          {gaps.length === 1 ? '1 declared gap' : `${gaps.length} declared gaps`}
        </span>
      )}
      <button
        type="button"
        onClick={() => onOpen(null)}
        className="inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-gray-300 hover:bg-gray-800/60 hover:text-white"
      >
        {count === 1 ? 'All 1 event' : `All ${count} events`}
        <ChevronRight size={13} aria-hidden="true" />
      </button>
    </div>
  )
}
