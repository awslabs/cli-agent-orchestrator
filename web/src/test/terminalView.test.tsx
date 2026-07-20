import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { TerminalView } from '../components/TerminalView'

// xterm renders nothing meaningful under jsdom, so replace it with a minimal
// fake whose `element` is a real DOM node. TerminalView dispatches synthetic
// `wheel` events onto that element, which is exactly the boundary we assert on.
// Defined via vi.hoisted so the (hoisted) vi.mock factory below can reference it.
const { wheelEvents, termRegistry, FakeTerminal } = vi.hoisted(() => {
  const wheelEvents: WheelEvent[] = []
  const termRegistry: { current: FakeTerminal | null } = { current: null }

  class FakeTerminal {
    rows = 24
    cols = 80
    element: HTMLDivElement

    constructor(_opts: unknown) {
      this.element = document.createElement('div')
      this.element.addEventListener('wheel', (e) => wheelEvents.push(e as WheelEvent))
      termRegistry.current = this
    }

    loadAddon() {}
    open(parent: HTMLElement) {
      parent.appendChild(this.element)
    }
    onData() {}
    onSelectionChange() {}
    attachCustomKeyEventHandler() {}
    getSelection() {
      return ''
    }
    focus() {}
    write() {}
    dispose() {}
  }

  return { wheelEvents, termRegistry, FakeTerminal }
})

vi.mock('@xterm/xterm/css/xterm.css', () => ({}))
vi.mock('@xterm/xterm', () => ({ Terminal: FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit() {}
  },
}))

// TerminalView opens a WebSocket and observes resizes; jsdom has neither.
class FakeWebSocket {
  static OPEN = 1
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onclose: (() => void) | null = null
  constructor(public url: string) {}
  send() {}
  close() {}
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

// jsdom lacks TouchEvent/Touch; craft a plain event carrying the `touches`
// shape the handlers read.
function touch(type: string, clientYs: number[]): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(ev, 'touches', {
    value: clientYs.map((clientY) => ({ clientY })),
    configurable: true,
  })
  return ev
}

function terminalContainer(): HTMLElement {
  // FakeTerminal.open() appends its element to the ref'd container div.
  const el = termRegistry.current?.element.parentElement
  if (!el) throw new Error('terminal container not mounted')
  return el
}

// Under jsdom there is no layout, so el.clientHeight is 0 and rowHeight() falls
// back to DEFAULT_LINE_HEIGHT = round(TERMINAL_FONT_SIZE * 1.2) = round(14 * 1.2)
// = 17px. Every notch below therefore corresponds to 17px of finger travel.
const LINE_H = 17

describe('TerminalView touch scrolling', () => {
  beforeEach(() => {
    wheelEvents.length = 0
    termRegistry.current = null
    ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = FakeResizeObserver
  })

  afterEach(() => {
    cleanup()
  })

  it('emits exactly one line-mode notch per row of travel', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    // Swipe up by exactly 3 rows of travel.
    el.dispatchEvent(touch('touchstart', [200]))
    el.dispatchEvent(touch('touchmove', [200 - 3 * LINE_H]))

    expect(wheelEvents).toHaveLength(3)
    // Line mode (bypasses xterm 6's trackpad damping), one line per notch.
    expect(wheelEvents.every((e) => e.deltaMode === WheelEvent.DOM_DELTA_LINE)).toBe(true)
    expect(wheelEvents.every((e) => Math.abs(e.deltaY) === 1)).toBe(true)
    // Finger up scrolls toward newer output → positive deltaY.
    expect(wheelEvents.every((e) => e.deltaY > 0)).toBe(true)
  })

  it('scrolls the opposite direction when the finger moves down', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    el.dispatchEvent(touch('touchstart', [100]))
    el.dispatchEvent(touch('touchmove', [100 + 3 * LINE_H])) // finger down 3 rows

    expect(wheelEvents).toHaveLength(3)
    expect(wheelEvents.every((e) => e.deltaMode === WheelEvent.DOM_DELTA_LINE)).toBe(true)
    expect(wheelEvents.every((e) => e.deltaY === -1)).toBe(true)
  })

  it('accumulates sub-row moves and emits one notch when they cross a row', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    // Two moves of ~half a row each: neither alone crosses a row, together they do.
    const half = Math.ceil(LINE_H / 2) // 9px
    el.dispatchEvent(touch('touchstart', [200]))

    el.dispatchEvent(touch('touchmove', [200 - half])) // 9px < 17px
    expect(wheelEvents).toHaveLength(0)

    el.dispatchEvent(touch('touchmove', [200 - 2 * half])) // 18px total ≥ 17px
    expect(wheelEvents).toHaveLength(1)
    expect(wheelEvents[0].deltaMode).toBe(WheelEvent.DOM_DELTA_LINE)
    expect(wheelEvents[0].deltaY).toBe(1)
  })

  it('preventsDefault on touchmove so the page does not pan/rubber-band', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    el.dispatchEvent(touch('touchstart', [200]))
    const move = touch('touchmove', [100])
    el.dispatchEvent(move)

    expect(move.defaultPrevented).toBe(true)
  })

  it('ignores multi-finger gestures', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    el.dispatchEvent(touch('touchstart', [200, 220]))
    el.dispatchEvent(touch('touchmove', [100, 120]))

    expect(wheelEvents.length).toBe(0)
  })

  it('resets on touchcancel so a stray move after it does not scroll', () => {
    render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    el.dispatchEvent(touch('touchstart', [200]))
    el.dispatchEvent(touch('touchmove', [200 - 3 * LINE_H]))
    expect(wheelEvents.length).toBe(3)

    // Gesture cancelled (e.g. palm rejection): state must reset.
    el.dispatchEvent(touch('touchcancel', []))
    wheelEvents.length = 0

    // A move with no fresh touchstart is ignored (touchY is null again).
    el.dispatchEvent(touch('touchmove', [100]))
    expect(wheelEvents.length).toBe(0)
  })

  it('stops dispatching after unmount (listeners cleaned up)', () => {
    const { unmount } = render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    unmount()
    wheelEvents.length = 0
    el.dispatchEvent(touch('touchstart', [200]))
    el.dispatchEvent(touch('touchmove', [100]))

    expect(wheelEvents.length).toBe(0)
  })
})
