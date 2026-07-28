import { useRef, useState } from 'react'
import type { StreamingEngine, TraceEntry } from '../lib/streaming'

interface StreamingPanelProps {
  engine: StreamingEngine
  provider: string
  agentProfile: string | null
  generationShort: string
  trace: TraceEntry[]
  /** Bumped by the parent's engine onChange hook so counts stay fresh. */
  tick: number
  onStop: () => void
  onClearTrace: () => void
}

/**
 * The §7.3 streaming surface: armed indicator + target line, the §6.2
 * capture surface, the bounded §6.5 trace, and the always-visible
 * Stop streaming control.
 *
 * Capture honesty (§6.2/§6.3): the capture surface is a focused div on the
 * deployed recorder mechanism (D8) — onKeyDown with preventDefault /
 * stopPropagation. xterm keeps no input handlers for managed panes, and
 * this surface never synthesizes key events from wheel or touch input:
 * scrolling keeps its deployed semantics and never becomes provider
 * keystrokes. Nothing here ever sends a websocket `input` frame (§6.6).
 */
export function StreamingPanel({
  engine,
  provider,
  agentProfile,
  generationShort,
  trace,
  onStop,
  onClearTrace,
}: StreamingPanelProps) {
  const [notice, setNotice] = useState('')
  const [imeActive, setImeActive] = useState(false)
  const captureRef = useRef<HTMLDivElement>(null)

  const onKeyDown = (event: React.KeyboardEvent) => {
    // While armed every key is the payload: none may reach the terminal,
    // the page, or the browser's own bindings.
    event.preventDefault()
    event.stopPropagation()
    // IME composition is never partially forwarded (§3.3); the
    // compositionstart handler shows the notice.
    if (event.nativeEvent.isComposing || imeActive) return
    const result = engine.handleKey({
      key: event.key,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      altKey: event.altKey,
      shiftKey: event.shiftKey,
    })
    setNotice(result.refused ?? '')
  }

  const onPaste = (event: React.ClipboardEvent) => {
    event.preventDefault()
    event.stopPropagation()
    // Clipboard images cannot stream — streaming is keystrokes only (§6.2).
    if (event.clipboardData.files.length > 0) {
      setNotice('clipboard images cannot be streamed — streaming is keystrokes only')
      return
    }
    const text = event.clipboardData.getData('text/plain')
    const result = engine.handlePaste(text)
    setNotice(result.refused ?? '')
  }

  const outcomeColor = (outcome: string): string => {
    if (outcome === 'accepted') return 'text-emerald-300'
    if (outcome === 'paused') return 'text-amber-300'
    if (outcome === 'refused-locally') return 'text-amber-300'
    return 'text-red-300'
  }

  return (
    <div className="space-y-2 rounded border-2 border-emerald-400 bg-gray-950 px-3 py-2">
      {/* Target line: provider / agent profile / short generation, armed
          indicator in a high-contrast active color, icon + text (never
          color-only), motion-safe animation only (§7.5). */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span aria-hidden="true" className="text-emerald-400 motion-safe:animate-pulse">●</span>
        <span className="font-semibold tracking-wide text-emerald-300">
          STREAMING TO {provider} / {agentProfile ?? 'default'} · gen {generationShort}
        </span>
        <span className="text-[10px] text-gray-400">
          advisory-exclusive: other automation may still write between batches
        </span>
      </div>

      <div
        ref={captureRef}
        tabIndex={0}
        role="textbox"
        aria-label="Streaming keystroke capture: typed keys are sent to the terminal"
        aria-multiline="false"
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        onCompositionStart={() => {
          setImeActive(true)
          setNotice('IME composition is not streamed; use the literal composer for composed text')
        }}
        onCompositionEnd={() => {
          setImeActive(false)
          setNotice('')
        }}
        className="min-h-[44px] w-full cursor-text rounded border border-emerald-600/60 bg-gray-900 px-3 py-2 text-sm text-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-400"
      >
        <span className="text-gray-400">
          {imeActive
            ? 'IME composition is not streamed…'
            : 'Type to stream keystrokes to the terminal — batches send on a short quiet timer, Enter submits…'}
        </span>
      </div>
      {notice && (
        <div className="text-[11px] text-amber-300" role="status">
          {notice}
        </div>
      )}

      <div
        role="status"
        aria-live="polite"
        aria-label="Streaming trace"
        className="max-h-32 space-y-0.5 overflow-y-auto font-mono text-[11px]"
      >
        {trace.length === 0 ? (
          <div className="text-gray-400">no batches yet</div>
        ) : (
          trace.map((entry, index) => (
            <div key={index} className="flex flex-wrap items-baseline gap-x-2">
              <span className="min-w-0 truncate text-gray-300">
                {entry.preview || '(no events)'}
              </span>
              <span className={outcomeColor(entry.outcome)}>
                {entry.outcome}
                {entry.reasonCode ? `/${entry.reasonCode}` : ''}
              </span>
              <span className="text-gray-400">
                ({entry.controlIdShort}) {entry.events} ev · {entry.bytes} B
              </span>
              {entry.note && <span className="text-gray-400">{entry.note}</span>}
            </div>
          ))
        )}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onClearTrace}
          className="min-h-[44px] min-w-[44px] rounded bg-gray-800 px-3 text-xs text-gray-200 transition-colors hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-500"
        >
          Clear trace
        </button>
        <button
          type="button"
          onClick={onStop}
          className="min-h-[44px] min-w-[44px] rounded bg-amber-700 px-3 text-xs font-medium text-white transition-colors hover:bg-amber-600 focus:outline-none focus:ring-2 focus:ring-amber-400"
        >
          Stop streaming
        </button>
      </div>
    </div>
  )
}
