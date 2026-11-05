// One live xterm.js terminal bound to a CAO terminal's WebSocket.
//
// Shared by the full-screen TerminalView overlay and the Workspace panes so the
// theme, clipboard handling, input forwarding and resize protocol live in one
// place. The session owns its own DOM element: callers move that element into
// whatever container should show it. Re-parenting keeps the xterm buffer and the
// socket alive, which is what lets the Workspace switch agent tabs without
// reconnecting and losing scrollback.
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { terminalSocketUrl } from '../../api'

export interface XtermSession {
  readonly terminalId: string
  /** Host element; append it to a sized container to display the terminal. */
  readonly element: HTMLDivElement
  /** Refit to the current container size and tell the server the new size. */
  fit: () => void
  focus: () => void
  /** Change the font size; refits so rows/cols follow (and tmux with them). */
  setFontSize: (px: number) => void
  dispose: () => void
}

export const DEFAULT_FONT_SIZE = 14

/** Marks the Workspace pool's off-screen holder; sessions inside it never fit. */
export const PARKED_ATTR = 'data-cao-parked'

export const XTERM_THEME = {
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
}

export function createXtermSession(terminalId: string, fontSize = DEFAULT_FONT_SIZE): XtermSession {
  const element = document.createElement('div')
  element.style.position = 'absolute'
  element.style.inset = '0'

  const term = new Terminal({
    cursorBlink: true,
    fontSize,
    fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
    scrollback: 10000,
    theme: XTERM_THEME,
  })
  const fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  term.open(element)

  const ws = new WebSocket(terminalSocketUrl(terminalId))
  ws.binaryType = 'arraybuffer'

  const sendSize = () => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
    }
  }

  // Every fit is sent to tmux as this client's size, and tmux sizes the agent's
  // window from its clients. So never fit while detached, hidden (0x0), or
  // parked off screen by the Workspace pool (PARKED_ATTR on an ancestor):
  // any of those would shrink the agent's real window to a sliver.
  const fit = () => {
    if (!element.isConnected || element.clientWidth === 0 || element.clientHeight === 0) return
    if (element.closest(`[${PARKED_ATTR}]`)) return
    fitAddon.fit()
    sendSize()
  }

  ws.onopen = () => fit()
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data))
  }
  ws.onclose = () => {
    term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n')
  }

  // Copy selection to clipboard on mouse-up
  term.onSelectionChange(() => {
    const selection = term.getSelection()
    if (selection) navigator.clipboard.writeText(selection).catch(() => {})
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

  // onData handles ALL input including paste
  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data }))
  })

  // Debounced so a drag-resize does not flood the socket
  let resizeTimer: ReturnType<typeof setTimeout> | undefined
  const resizeObserver = new ResizeObserver(() => {
    clearTimeout(resizeTimer)
    resizeTimer = setTimeout(fit, 50)
  })
  resizeObserver.observe(element)

  let disposed = false
  return {
    terminalId,
    element,
    fit,
    focus: () => term.focus(),
    setFontSize: (px: number) => {
      if (term.options.fontSize === px) return
      term.options.fontSize = px
      fit()
    },
    dispose: () => {
      if (disposed) return
      disposed = true
      clearTimeout(resizeTimer)
      resizeObserver.disconnect()
      ws.close()
      term.dispose()
      element.remove()
    },
  }
}
