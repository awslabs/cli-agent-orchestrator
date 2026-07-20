import { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { X, Terminal as TermIcon } from 'lucide-react'

interface TerminalViewProps {
  terminalId: string
  provider?: string
  agentProfile?: string | null
  onClose: () => void
}

const TERMINAL_FONT_SIZE = 14

// Fallback row height (px) used only when the container has not been laid out
// yet, so a touch delta can still be turned into whole wheel notches.
const DEFAULT_LINE_HEIGHT = Math.round(TERMINAL_FONT_SIZE * 1.2)

export function TerminalView({ terminalId, provider, agentProfile, onClose }: TerminalViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: TERMINAL_FONT_SIZE,
      fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
      scrollback: 10000,
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
        cursor: '#58a6ff',
        selectionBackground: '#264f78',
        black: '#0d1117',
        red: '#ff7b72',
        green: '#3fb950',
        yellow: '#d29922',
        blue: '#58a6ff',
        magenta: '#bc8cff',
        cyan: '#39d353',
        white: '#c9d1d9',
      },
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    // Touch-scroll support. xterm.js core forwards DOM `wheel` events as
    // mouse-wheel escape sequences whenever the attached app is in
    // mouse-tracking mode (here: tmux with `mouse on`), which is why a desktop
    // mouse wheel scrolls the terminal. xterm has no touch handling, so a finger
    // swipe on a phone produces no wheel event and nothing scrolls. Translate a
    // single-finger vertical swipe into synthetic `wheel` events dispatched onto
    // xterm's root element, driving that same already-working path so the swipe
    // produces the same kind of mouse-wheel report the server already acts on.
    // (Equivalent in effect to a desktop wheel — not necessarily byte-identical,
    // since coordinates and delta encoding differ; see the caveats below.)
    //
    // Emit line-mode deltas (DOM_DELTA_LINE, one notch per row of travel) rather
    // than pixel deltas: xterm 6 treats a small pixel-mode wheel delta as a
    // trackpad and damps it to ~0.3 of a line, which would make touch scrolling
    // roughly 3x too slow. The line-mode path bypasses that damping, so one row
    // of finger travel scrolls one line.
    //
    // Scope/caveats (kept deliberately minimal; both worth noting upstream):
    //   - Mouse-tracking only. This scrolls when the attached app is in
    //     mouse-tracking mode (tmux `mouse on`, or alt-buffer apps that report
    //     the wheel). In a plain shell the synthetic event reaches no listener —
    //     xterm's own scrollback viewport listens on a descendant element, not
    //     term.element — so a swipe does nothing there.
    //   - Single pane. The synthetic wheel carries default coordinates (col 1,
    //     row 1), so a multi-pane tmux layout routes every swipe to the
    //     top-left pane rather than the pane under the finger.
    // Desktop behavior is untouched (touch-only listeners).
    const wheelTarget = term.element ?? el

    // Row height in px, so a swipe distance maps to a matching number of wheel
    // notches. Derived from the live geometry (no private xterm internals);
    // falls back to a font-size estimate before the first layout.
    const rowHeight = (): number => {
      const rows = term.rows
      const height = el.clientHeight
      return rows > 0 && height > 0 ? height / rows : DEFAULT_LINE_HEIGHT
    }

    let touchY: number | null = null
    let scrollAcc = 0

    const onTouchStart = (ev: TouchEvent) => {
      if (ev.touches.length !== 1) {
        touchY = null
        return
      }
      touchY = ev.touches[0].clientY
      scrollAcc = 0
    }

    const onTouchMove = (ev: TouchEvent) => {
      if (touchY === null || ev.touches.length !== 1) return
      const y = ev.touches[0].clientY
      // Finger moving up (clientY decreasing) scrolls toward newer output,
      // matching a downward mouse-wheel notch.
      scrollAcc += touchY - y
      touchY = y
      const lineH = rowHeight()
      while (Math.abs(scrollAcc) >= lineH) {
        const dir = scrollAcc > 0 ? 1 : -1
        // One line-mode notch per row of accumulated travel. The pixel
        // accumulator (lineH per notch) sets the cadence; DOM_DELTA_LINE keeps
        // xterm 6 from damping the delta as a trackpad gesture.
        wheelTarget.dispatchEvent(
          new WheelEvent('wheel', {
            deltaY: dir,
            deltaMode: WheelEvent.DOM_DELTA_LINE,
            bubbles: true,
            cancelable: true,
          })
        )
        scrollAcc -= dir * lineH
      }
      // Stop the browser from panning the page / rubber-banding instead.
      ev.preventDefault()
    }

    const endTouch = () => {
      touchY = null
      scrollAcc = 0
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', endTouch, { passive: true })
    el.addEventListener('touchcancel', endTouch, { passive: true })

    // Connect WebSocket
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${location.host}/terminals/${terminalId}/ws`)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      // Fit once the connection is live so we send correct dimensions
      fitAddon.fit()
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
      }
    }

    ws.onclose = () => {
      term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n')
    }

    // Copy selection to clipboard on mouse-up
    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) {
        navigator.clipboard.writeText(selection).catch(() => {})
      }
    })

    // Ctrl+Shift+C to copy selection
    term.attachCustomKeyEventHandler((e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        const selection = term.getSelection()
        if (selection) navigator.clipboard.writeText(selection).catch(() => {})
        return false
      }
      return true
    })

    // onData handles ALL input including paste — xterm.js
    // receives pasted text through the browser's input system
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }))
      }
    })

    // Handle resize — debounce to avoid flooding
    let resizeTimer: ReturnType<typeof setTimeout>
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
        }
      }, 50)
    })
    resizeObserver.observe(el)

    // Initial fit after layout settles
    const initialFit = requestAnimationFrame(() => {
      fitAddon.fit()
    })

    term.focus()

    return () => {
      cancelAnimationFrame(initialFit)
      clearTimeout(resizeTimer)
      resizeObserver.disconnect()
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', endTouch)
      el.removeEventListener('touchcancel', endTouch)
      ws.close()
      term.dispose()
    }
  }, [terminalId])

  return (
    <div className="fixed inset-0 z-50 flex flex-col" style={{ background: '#0d1117' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-700/50 shrink-0">
        <div className="flex items-center gap-3">
          <TermIcon size={16} className="text-emerald-400" />
          <span className="text-sm font-mono text-gray-300">{terminalId}</span>
          {provider && <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{provider}</span>}
          {agentProfile && <span className="text-xs text-emerald-400 bg-emerald-900/30 px-2 py-0.5 rounded">{agentProfile}</span>}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] text-gray-600">Click X to close</span>
          <button
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-white transition-colors rounded"
            title="Close terminal"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      {/* Terminal — absolute positioning gives xterm.js real pixel dimensions to measure */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
      </div>
    </div>
  )
}
