import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { X, Terminal as TermIcon } from 'lucide-react'
import { api, type ApiError } from '../api'
import {
  applyKeyToRecording,
  previewToken,
  sequenceTextBytes,
  MAX_SEQUENCE_EVENTS,
  MAX_SEQUENCE_TEXT_BYTES,
  type SequenceEvent,
} from '../lib/sequenceRecorder'

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
const CONTROL_UNSUPPORTED_STATUSES = new Set([404, 405, 501])
const CONTROL_AMBIGUOUS_STATUSES = new Set([408, 425, 500, 502, 503, 504])

function isWheelMouseReport(data: string): boolean {
  const sgr = /^\x1b\[<(\d+);\d+;\d+[Mm]$/.exec(data)
  if (sgr) return (Number(sgr[1]) & 64) === 64

  // Legacy X10 mouse reports encode the button and coordinates as three
  // bytes after ESC [ M. Keep this narrow so printable input and paste stay
  // blocked on managed transcript panes.
  if (data.length === 6 && data.startsWith('\x1b[M')) {
    const button = data.charCodeAt(3) - 32
    return button >= 0 && (button & 64) === 64
  }
  return false
}

export function TerminalView({ terminalId, provider, agentProfile, onClose }: TerminalViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const managedRef = useRef<boolean | null>(null)
  const [managed, setManaged] = useState(false)
  const [generation, setGeneration] = useState<string | undefined>()
  const [executionMode, setExecutionMode] = useState<string | undefined>()
  const [nativeControlSupported, setNativeControlSupported] = useState(false)
  const [nativeControlResolved, setNativeControlResolved] = useState(false)
  const [message, setMessage] = useState('')
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [controlBusy, setControlBusy] = useState(false)
  const [controlStatus, setControlStatus] = useState('')
  const [sequenceSupported, setSequenceSupported] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recordedEvents, setRecordedEvents] = useState<SequenceEvent[]>([])
  const [recordNotice, setRecordNotice] = useState('')

  useEffect(() => {
    managedRef.current = null
    api.getManagedControl(terminalId)
      .then(result => {
        managedRef.current = result.managed
        setManaged(result.managed)
        setGeneration(result.generation)
        setExecutionMode(result.execution_mode)
        setNativeControlSupported(false)
        setNativeControlResolved(result.execution_mode !== 'native_tui')
        if (result.managed && result.execution_mode === 'native_tui') {
          api.getControlInputCapabilities()
            .then(capabilities => {
              setNativeControlSupported(
                capabilities.execution_modes.includes('native_tui')
                && capabilities.literal_write === true
                && capabilities.bracketed_paste === false
                && capabilities.enter_required === true,
              )
              // The structured-sequence recorder is v3-only: a server that
              // does not advertise it gets the literal bar alone, and the
              // recorder's absence is stated rather than silently missing.
              setSequenceSupported(
                (capabilities.request_schema_versions ?? []).includes(3),
              )
              setNativeControlResolved(true)
            })
            .catch(() => {
              setNativeControlSupported(false)
              setNativeControlResolved(true)
              setControlStatus('native control capability unavailable')
            })
        }
      })
      .catch(() => {
        // Unknown control identity is not proof this is an ordinary TUI.
        // Keep terminal input fail-closed rather than pasting into a possibly
        // managed bridge pane when the identity lookup is unavailable.
        managedRef.current = null
        setManaged(false)
        setGeneration(undefined)
        setExecutionMode(undefined)
        setNativeControlSupported(false)
        setNativeControlResolved(false)
        setControlStatus('terminal control identity unavailable')
      })
  }, [terminalId])

  const runManagedOperation = async (body: {
    action: string
    message?: string
    config_id?: string
    value?: string
    instruction?: string
  }) => {
    const operationId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`${body.action}: submitting…`)
    try {
      const response = await api.beginManagedOperation(terminalId, {
        ...body,
        operation_id: operationId,
        generation,
      })
      const state = String(response.receipt.state || 'unknown')
      const reason = response.receipt.reason_code || response.receipt.reason_detail
      setControlStatus(`${body.action}: ${state}${reason ? ` — ${String(reason)}` : ''}`)
      if (body.action === 'route-query') {
        const result = response.receipt.result as Record<string, unknown> | undefined
        if (result?.model) setModel(String(result.model))
        if (result?.effort) setEffort(String(result.effort))
      }
      if (body.action === 'follow-up' && response.success) setMessage('')
    } catch (error) {
      // The request may have crossed the provider boundary before the HTTP
      // response was lost. Query the same durable operation; never invent a
      // second ID and repeat a potentially accepted effect.
      try {
        const reconciled = await api.queryManagedOperation(
          terminalId,
          operationId,
          generation,
        )
        const state = String(reconciled.receipt.state || 'unknown')
        const reason = reconciled.receipt.reason_code || reconciled.receipt.reason_detail
        setControlStatus(
          `${body.action}: ${state}${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `${body.action}: response unavailable; operation ${operationId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  const runNativeControl = async (text: string, label: string) => {
    const controlId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`${label}: submitting… (${controlId})`)
    try {
      const identity = await api.getControlIdentity(terminalId)
      const expectedIdentity = Object.fromEntries(
        [
          'terminal_id',
          'terminal_incarnation',
          'terminal_generation',
          'pane_birth_id',
          'provider_process_id',
          'provider',
          'native_session_id',
          'execution_mode',
          'session_name',
        ].map(key => [key, identity[key] ?? null]),
      )
      const response = await api.sendControlInput(terminalId, {
        control_id: controlId,
        text,
        enter: true,
        expected_identity: expectedIdentity,
      })
      const outcome = String(response.outcome || 'unknown')
      const reason = response.reason_code || response.reason_detail
      setControlStatus(
        `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
      )
      if (label === 'send' && outcome === 'accepted') setMessage('')
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        setControlStatus(
          `${label}: unsupported (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        setControlStatus(
          `${label}: refused (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      try {
        const response = await api.queryControlInput(controlId)
        const outcome = String(response.outcome || 'unknown')
        const reason = response.reason_code || response.reason_detail
        setControlStatus(
          `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `${label}: response unavailable; control ${controlId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  // --- Structured key-sequence recorder (cond-0175) ------------------------
  //
  // Record captures the ordered, representable events; the preview renders
  // them readably; send submits the exact structured events through the
  // arbiter with terminal/pane/process/generation re-proof (the same
  // expected-identity re-fetch the literal send uses) and a fresh request
  // id; cancel before send writes nothing — no request is ever sent on a
  // discard.

  const startRecording = () => {
    setRecordedEvents([])
    setRecordNotice('')
    setRecording(true)
  }

  const stopRecording = () => {
    setRecording(false)
    setRecordNotice('')
  }

  const cancelRecording = () => {
    // Discard is purely local: no request id is minted and nothing is
    // sent, so a cancelled sequence provably wrote nothing.
    setRecording(false)
    setRecordedEvents([])
    setRecordNotice('')
    setControlStatus('sequence: cancelled — nothing was sent')
  }

  const onRecorderKeyDown = (event: React.KeyboardEvent) => {
    // While recording, every key is the payload: none of them may reach
    // the terminal, the page, or the browser's own bindings.
    event.preventDefault()
    event.stopPropagation()
    const result = applyKeyToRecording(recordedEvents, {
      key: event.key,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      altKey: event.altKey,
    })
    if (result.refused) {
      setRecordNotice(result.refused)
      return
    }
    setRecordNotice('')
    setRecordedEvents(result.events)
  }

  const runNativeSequence = async (events: SequenceEvent[]) => {
    const controlId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`sequence: submitting… (${controlId})`)
    try {
      const identity = await api.getControlIdentity(terminalId)
      const expectedIdentity = Object.fromEntries(
        [
          'terminal_id',
          'terminal_incarnation',
          'terminal_generation',
          'pane_birth_id',
          'provider_process_id',
          'provider',
          'native_session_id',
          'execution_mode',
          'session_name',
        ].map(key => [key, identity[key] ?? null]),
      )
      const response = await api.sendControlInput(terminalId, {
        control_id: controlId,
        events,
        expected_identity: expectedIdentity,
      })
      const outcome = String(response.outcome || 'unknown')
      const reason = response.reason_code || response.reason_detail
      const perEvent = Array.isArray(response.events)
        ? ` [${(response.events as Array<Record<string, unknown>>)
            .map(entry => String(entry.outcome ?? '—'))
            .join(', ')}]`
        : ''
      setControlStatus(
        `sequence: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}${perEvent}`,
      )
      if (outcome === 'accepted') {
        setRecording(false)
        setRecordedEvents([])
      }
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        setControlStatus(
          `sequence: unsupported (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        setControlStatus(
          `sequence: refused (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      // The response may have crossed the pane boundary before it was
      // lost. Resolve by the exact control id; never re-send the events.
      try {
        const response = await api.queryControlInput(controlId)
        const outcome = String(response.outcome || 'unknown')
        const reason = response.reason_code || response.reason_detail
        setControlStatus(
          `sequence: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `sequence: response unavailable; control ${controlId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

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
      // A managed pane is a rendered view of a private provider RPC stream.
      // Its stdin is deliberately not a provider input channel; controls use
      // the exact generation-bound API above.  Hold input while managed status
      // is unresolved so an early paste cannot leak into the wrong transport.
      if (
        ws.readyState === WebSocket.OPEN
        && (
          managedRef.current === false
          || (managedRef.current === true && isWheelMouseReport(data))
        )
      ) {
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

  const nativeManaged = managed && executionMode === 'native_tui'
  const acpManaged = managed && executionMode === 'acp'

  return (
    <div className="fixed inset-0 z-50 flex flex-col" style={{ background: '#0d1117' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-700/50 shrink-0">
        <div className="flex items-center gap-3">
          <TermIcon size={16} className="text-emerald-400" />
          <span className="text-sm font-mono text-gray-300">{terminalId}</span>
          {provider && <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{provider}</span>}
          {agentProfile && <span className="text-xs text-emerald-400 bg-emerald-900/30 px-2 py-0.5 rounded">{agentProfile}</span>}
          {managed && (
            <span className="text-xs text-cyan-300 bg-cyan-900/30 px-2 py-0.5 rounded">
              {nativeManaged
                ? 'Managed native TUI · identity-bound controls'
                : acpManaged
                  ? 'Managed ACP · read-only transcript'
                  : 'Managed mode unknown · controls unavailable'}
            </span>
          )}
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
      {nativeManaged && nativeControlSupported && (
        <div className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-2 space-y-2">
          <div className="flex gap-2">
            <input
              value={message}
              onChange={event => setMessage(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && message.trim() && !controlBusy) {
                  void runNativeControl(message.trim(), 'send')
                }
              }}
              placeholder="Send literal text to the native composer…"
              className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !message.trim()}
              onClick={() => void runNativeControl(message.trim(), 'send')}
              className="rounded bg-emerald-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Send
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runNativeControl('/compact', 'compact')}
              className="rounded bg-indigo-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Compact
            </button>
          </div>
          <div className="flex items-center gap-2 text-[11px] text-gray-500">
            <span>Cancel, route, effort, and resume controls are unavailable for native TUI sessions.</span>
            <span className="min-w-0 truncate">{controlStatus}</span>
          </div>
          {sequenceSupported && (
            <div className="space-y-1 border-t border-gray-800 pt-2">
              <div className="flex items-center gap-2">
                {!recording ? (
                  <button
                    disabled={controlBusy}
                    onClick={startRecording}
                    className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
                  >
                    Record
                  </button>
                ) : (
                  <button
                    disabled={controlBusy}
                    onClick={stopRecording}
                    className="rounded bg-amber-700 px-3 py-1 text-xs text-white disabled:opacity-40"
                  >
                    Stop
                  </button>
                )}
                {recording && (
                  <div
                    tabIndex={0}
                    onKeyDown={onRecorderKeyDown}
                    ref={element => element?.focus()}
                    className="min-w-0 flex-1 rounded border border-amber-600/60 bg-gray-900 px-3 py-1 text-xs text-amber-200 focus:outline-none"
                  >
                    Recording — press keys now ({recordedEvents.length}/{MAX_SEQUENCE_EVENTS}{' '}
                    events, {sequenceTextBytes(recordedEvents)}/{MAX_SEQUENCE_TEXT_BYTES} B)
                  </div>
                )}
                {!recording && recordedEvents.length > 0 && (
                  <div className="min-w-0 flex-1 truncate rounded border border-gray-700 bg-gray-900 px-3 py-1 font-mono text-xs text-gray-200">
                    {recordedEvents.map((event, index) => (
                      <span key={index} className="mr-2">
                        {previewToken(event)}
                      </span>
                    ))}
                  </div>
                )}
                <button
                  disabled={controlBusy || recording || recordedEvents.length === 0}
                  onClick={() => void runNativeSequence(recordedEvents)}
                  className="rounded bg-emerald-700 px-3 py-1 text-xs text-white disabled:opacity-40"
                >
                  Send sequence
                </button>
                <button
                  disabled={controlBusy || (!recording && recordedEvents.length === 0)}
                  onClick={cancelRecording}
                  className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
              {recordNotice && (
                <div className="text-[11px] text-amber-300">{recordNotice}</div>
              )}
              <div className="text-[10px] text-gray-600">
                Records exact representable events only: Escape, Ctrl+C, Ctrl+S (steer chord),
                Enter, Backspace, and printable text. Comma, plus, and backslash are ordinary
                text. Terminal protocols cannot express arbitrary simultaneous physical-key
                combinations; a combination this surface cannot represent is refused with a
                message, never approximated. Cancel before send writes nothing.
              </div>
            </div>
          )}
          {nativeControlResolved && !sequenceSupported && (
            <div className="text-[10px] text-gray-600">
              Sequence recording needs control-input schema v3; this server offers the literal
              control only.
            </div>
          )}
        </div>
      )}
      {nativeManaged && nativeControlResolved && !nativeControlSupported && (
        <div className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-2 text-[11px] text-amber-300">
          Native control input is unsupported by this server; no dashboard actions are available.
          <span className="ml-2 text-gray-500">{controlStatus}</span>
        </div>
      )}
      {acpManaged && (
        <div className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-2 space-y-2">
          <div className="flex gap-2">
            <input
              value={message}
              onChange={event => setMessage(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && message.trim() && !controlBusy) {
                  void runManagedOperation({ action: 'follow-up', message: message.trim() })
                }
              }}
              placeholder="Send a provider-native follow-up…"
              className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !message.trim()}
              onClick={() => void runManagedOperation({ action: 'follow-up', message: message.trim() })}
              className="rounded bg-emerald-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Send
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'cancel' })}
              className="rounded bg-amber-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Cancel turn
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'compact' })}
              className="rounded bg-indigo-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Compact
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'route-query' })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Query route
            </button>
            <input
              value={model}
              onChange={event => setModel(event.target.value)}
              placeholder="model"
              className="w-56 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !model.trim()}
              onClick={() => void runManagedOperation({ action: 'route-set', config_id: 'model', value: model.trim() })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Set model
            </button>
            <input
              value={effort}
              onChange={event => setEffort(event.target.value)}
              placeholder="effort"
              className="w-32 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !effort.trim()}
              onClick={() => void runManagedOperation({ action: 'route-set', config_id: 'thinking', value: effort.trim() })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Set effort
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'resume-status' })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Resume status
            </button>
            <span className="min-w-0 truncate text-[11px] text-gray-500">{controlStatus}</span>
          </div>
        </div>
      )}
      {/* Terminal — absolute positioning gives xterm.js real pixel dimensions to measure */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
      </div>
    </div>
  )
}
