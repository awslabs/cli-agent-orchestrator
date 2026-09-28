// PlaybackTransport — play/pause/step/jump controls driving the client-side
// fold (#504 / U8, FR-7). Keyboard-operable buttons with SR labels; playback is
// a pure index walk over the seq-ordered events, so it is deterministic and
// never re-executes the run server-side.

import { useEffect, useRef } from 'react'
import { Play, Pause, SkipBack, SkipForward, ChevronFirst, ChevronLast } from 'lucide-react'

interface PlaybackTransportProps {
  eventCount: number
  selectedIndex: number
  playing: boolean
  onSetIndex: (index: number) => void
  onSetPlaying: (playing: boolean) => void
  /** Advance interval in ms while playing. */
  intervalMs?: number
}

export function PlaybackTransport({
  eventCount,
  selectedIndex,
  playing,
  onSetIndex,
  onSetPlaying,
  intervalMs = 800,
}: PlaybackTransportProps) {
  const max = Math.max(0, eventCount - 1)
  const timerRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined)

  // Auto-advance while playing; stop at the end (a run is finite).
  useEffect(() => {
    if (!playing || eventCount === 0) return
    timerRef.current = setInterval(() => {
      onSetIndex(Math.min(selectedIndex + 1, max))
    }, intervalMs)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [playing, selectedIndex, max, eventCount, intervalMs, onSetIndex])

  // Pause automatically once the transport reaches the last event.
  useEffect(() => {
    if (playing && selectedIndex >= max && eventCount > 0) onSetPlaying(false)
  }, [playing, selectedIndex, max, eventCount, onSetPlaying])

  const disabled = eventCount === 0
  const btn =
    'p-2 rounded-lg text-gray-300 bg-gray-800 hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed'

  return (
    <div className="flex items-center gap-2" role="group" aria-label="Playback transport">
      <button
        type="button"
        className={btn}
        onClick={() => onSetIndex(0)}
        disabled={disabled || selectedIndex === 0}
        aria-label="Jump to first event"
        title="First event (Home)"
      >
        <ChevronFirst size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        className={btn}
        onClick={() => onSetIndex(Math.max(0, selectedIndex - 1))}
        disabled={disabled || selectedIndex === 0}
        aria-label="Step back one event"
        title="Step back"
      >
        <SkipBack size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        className={btn}
        onClick={() => onSetPlaying(!playing)}
        disabled={disabled}
        aria-label={playing ? 'Pause playback' : 'Play events'}
        aria-pressed={playing}
        title={playing ? 'Pause' : 'Play'}
      >
        {playing ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}
      </button>
      <button
        type="button"
        className={btn}
        onClick={() => onSetIndex(Math.min(max, selectedIndex + 1))}
        disabled={disabled || selectedIndex >= max}
        aria-label="Step forward one event"
        title="Step forward"
      >
        <SkipForward size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        className={btn}
        onClick={() => onSetIndex(max)}
        disabled={disabled || selectedIndex >= max}
        aria-label="Jump to last event"
        title="Last event (End)"
      >
        <ChevronLast size={16} aria-hidden="true" />
      </button>
    </div>
  )
}
