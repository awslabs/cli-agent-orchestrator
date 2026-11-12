// createXtermSession against fake xterm/fit/WebSocket: it must never send a size
// while detached, hidden or parked (tmux sizes the agent's window from it), and
// it must report a closed socket so the pool can reconnect.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const fit = vi.fn()
vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    rows = 24
    cols = 80
    options: Record<string, unknown> = {}
    loadAddon() {}
    open() {}
    write() {}
    focus() {}
    dispose() {}
    onSelectionChange() {}
    onData() {}
    attachCustomKeyEventHandler() {}
    getSelection() { return '' }
  },
}))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit = fit } }))
vi.mock('@xterm/xterm/css/xterm.css', () => ({}))

class FakeSocket {
  static OPEN = 1
  static last: FakeSocket
  readyState = 1
  binaryType = ''
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((e: unknown) => void) | null = null
  constructor(public url: string) { FakeSocket.last = this }
  send(data: string) { this.sent.push(data) }
  close() {}
}

import { createXtermSession, PARKED_ATTR } from '../components/terminal/xtermSession'

function sized(el: HTMLElement, w: number, h: number) {
  Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => w })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => h })
}

beforeEach(() => {
  fit.mockReset()
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

const resizes = () => FakeSocket.last.sent.filter(m => JSON.parse(m).type === 'resize')

describe('createXtermSession', () => {
  it('fits and sends its size once it is shown in a sized container', () => {
    const s = createXtermSession('t1')
    document.body.appendChild(s.element)
    sized(s.element, 800, 400)

    s.fit()

    expect(fit).toHaveBeenCalledTimes(1)
    expect(resizes()).toHaveLength(1)
    s.dispose()
  })

  it('never fits while detached or 0x0', () => {
    const s = createXtermSession('t1')
    s.fit() // detached
    document.body.appendChild(s.element)
    sized(s.element, 0, 0)
    s.fit() // hidden

    expect(fit).not.toHaveBeenCalled()
    expect(resizes()).toHaveLength(0)
    s.dispose()
  })

  it('never fits while parked, even at a real size', () => {
    const holder = document.createElement('div')
    holder.setAttribute(PARKED_ATTR, '')
    document.body.appendChild(holder)
    const s = createXtermSession('t1')
    holder.appendChild(s.element)
    sized(s.element, 800, 400)

    s.fit()
    FakeSocket.last.onopen?.()

    expect(fit).not.toHaveBeenCalled()
    expect(resizes()).toHaveLength(0)
    s.dispose()
  })

  it('reports a closed socket', () => {
    const s = createXtermSession('t1')
    expect(s.closed).toBe(false)

    FakeSocket.last.onclose?.()

    expect(s.closed).toBe(true)
    s.dispose()
  })
})
