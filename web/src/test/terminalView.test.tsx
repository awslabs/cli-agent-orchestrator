import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
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
    dataHandler: ((data: string) => void) | null = null

    constructor(_opts: unknown) {
      this.element = document.createElement('div')
      this.element.addEventListener('wheel', (e) => wheelEvents.push(e as WheelEvent))
      termRegistry.current = this
    }

    loadAddon() {}
    open(parent: HTMLElement) {
      parent.appendChild(this.element)
    }
    onData(handler: (data: string) => void) {
      this.dataHandler = handler
    }
    emitData(data: string) {
      this.dataHandler?.(data)
    }
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
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: string[] = []
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
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

beforeEach(() => {
  wheelEvents.length = 0
  termRegistry.current = null
  FakeWebSocket.instances.length = 0
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = FakeResizeObserver
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('TerminalView touch scrolling', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ managed: false }),
    }))
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
    expect(wheelEvents).toHaveLength(3)

    // Gesture cancelled (e.g. palm rejection): state must reset.
    el.dispatchEvent(touch('touchcancel', []))
    wheelEvents.length = 0

    // A move with no fresh touchstart is ignored (touchY is null again).
    el.dispatchEvent(touch('touchmove', [100]))
    expect(wheelEvents).toHaveLength(0)
  })

  it('stops dispatching after unmount (listeners cleaned up)', () => {
    const { unmount } = render(<TerminalView terminalId="t-1" onClose={() => {}} />)
    const el = terminalContainer()

    unmount()
    wheelEvents.length = 0
    el.dispatchEvent(touch('touchstart', [200]))
    el.dispatchEvent(touch('touchmove', [100]))

    expect(wheelEvents).toHaveLength(0)
  })
})

describe('TerminalView native literal control', () => {
  it('routes native Send through identity-bound control input; the header has no Compact button (§7.1)', async () => {
    const requests: Array<{ url: string; body?: Record<string, unknown> }> = []
    let controlNumber = 0
    vi.stubGlobal('crypto', {
      randomUUID: () => `control-${++controlNumber}`,
    })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      requests.push({ url, body })
      let response: Record<string, unknown>
      if (url.endsWith('/managed-control')) {
        response = {
          managed: true,
          generation: 'generation-1',
          execution_mode: 'native_tui',
        }
      } else if (url.endsWith('/control-input/capabilities')) {
        response = {
          protocol: 'cao-control-input-v1',
          execution_modes: ['native_tui'],
          literal_write: true,
          bracketed_paste: false,
          enter_required: true,
        }
      } else if (url.endsWith('/control-identity')) {
        response = {
          terminal_id: 't-native',
          terminal_incarnation: 'incarnation-1',
          terminal_generation: 'generation-1',
          pane_birth_id: '%7',
          provider_process_id: '42@start',
          provider: 'kimi_cli',
          native_session_id: 'session-1',
          execution_mode: 'native_tui',
          session_name: 'cao-test',
          pane: { pane_id: '%7' },
        }
      } else {
        response = {
          control_id: body?.control_id,
          outcome: 'accepted',
        }
      }
      return {
        ok: true,
        json: async () => response,
      } as Response
    }))

    render(<TerminalView terminalId="t-native" onClose={() => {}} />)

    expect(
      await screen.findByText('Managed native TUI · identity-bound controls'),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel turn' })).toBeNull()

    fireEvent.change(
      screen.getByPlaceholderText('Send a message to the native composer…'),
      { target: { value: 'continue the review' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => {
      const control = requests.find(request => request.url.endsWith('/control-input'))
      expect(control?.body?.text).toBe('continue the review')
      expect(control?.body?.enter).toBe(true)
      expect(control?.body?.payload_class).toBeUndefined()
      expect(control?.body?.expected_identity).toEqual({
        terminal_id: 't-native',
        terminal_incarnation: 'incarnation-1',
        terminal_generation: 'generation-1',
        pane_birth_id: '%7',
        provider_process_id: '42@start',
        provider: 'kimi_cli',
        native_session_id: 'session-1',
        execution_mode: 'native_tui',
        session_name: 'cao-test',
      })
    })
    await waitFor(() => {
      expect(
        (screen.getByPlaceholderText(
          'Send a message to the native composer…',
        ) as HTMLInputElement).value,
      ).toBe('')
    })

    // §7.1: the standalone Compact button is removed — Compact is a built-in
    // favorite macro now, not a header control.
    expect(screen.queryByRole('button', { name: 'Compact' })).toBeNull()
  })

  it('forwards only mouse-wheel reports from a managed transcript', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const response = url.endsWith('/managed-control')
        ? { managed: true, execution_mode: 'native_tui' }
        : url.endsWith('/control-input/capabilities')
          ? {
              protocol: 'cao-control-input-v1',
              execution_modes: ['native_tui'],
              literal_write: true,
              bracketed_paste: false,
              enter_required: true,
            }
          : {}
      return {
        ok: true,
        json: async () => response,
      } as Response
    }))

    render(<TerminalView terminalId="t-native" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    const terminal = termRegistry.current
    const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    if (!terminal || !socket) throw new Error('terminal websocket not mounted')

    terminal.emitData('x')
    terminal.emitData('ordinary multi-character paste')
    terminal.emitData('\x1b[<0;1;1M')
    terminal.emitData('\x1b[<64;12;8M')
    terminal.emitData('\x1b[M`!!')

    expect(socket.sent.map(message => JSON.parse(message))).toEqual([
      { type: 'input', data: '\x1b[<64;12;8M' },
      { type: 'input', data: '\x1b[M`!!' },
    ])
  })

  it('surfaces a typed native-control refusal without querying or retrying', async () => {
    const requests: Array<{ url: string; method?: string }> = []
    vi.stubGlobal('crypto', { randomUUID: () => 'control-refused' })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, method: init?.method })
      if (url.endsWith('/managed-control')) {
        return {
          ok: true,
          json: async () => ({ managed: true, execution_mode: 'native_tui' }),
        } as Response
      }
      if (url.endsWith('/control-input/capabilities')) {
        return {
          ok: true,
          json: async () => ({
            protocol: 'cao-control-input-v1',
            execution_modes: ['native_tui'],
            literal_write: true,
            bracketed_paste: false,
            enter_required: true,
          }),
        } as Response
      }
      if (url.endsWith('/control-identity')) {
        return {
          ok: true,
          json: async () => ({
            terminal_id: 't-native',
            terminal_generation: 'generation-1',
            execution_mode: 'native_tui',
          }),
        } as Response
      }
      if (url.endsWith('/control-input') && init?.method === 'POST') {
        return {
          ok: true,
          json: async () => ({
            control_id: 'control-refused',
            outcome: 'refused',
            reason_code: 'terminal-generation-stale',
          }),
        } as Response
      }
      throw new Error(`unexpected request: ${url}`)
    }))

    render(<TerminalView terminalId="t-native" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')
    fireEvent.change(
      screen.getByPlaceholderText('Send a message to the native composer…'),
      { target: { value: 'continue' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(
      await screen.findByText(
        'send: refused (control-refused) — terminal-generation-stale',
      ),
    ).toBeTruthy()
    expect(requests.filter(request => request.method === 'POST')).toHaveLength(1)
    expect(
      requests.some(request => request.url.endsWith('/control-input/control-refused')),
    ).toBe(false)
  })

  it('reconciles an ambiguous HTTP status by the same control id without retrying', async () => {
    const requests: Array<{ url: string; method?: string }> = []
    vi.stubGlobal('crypto', { randomUUID: () => 'control-ambiguous' })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, method: init?.method })
      if (url.endsWith('/managed-control')) {
        return {
          ok: true,
          json: async () => ({ managed: true, execution_mode: 'native_tui' }),
        } as Response
      }
      if (url.endsWith('/control-input/capabilities')) {
        return {
          ok: true,
          json: async () => ({
            protocol: 'cao-control-input-v1',
            execution_modes: ['native_tui'],
            literal_write: true,
            bracketed_paste: false,
            enter_required: true,
          }),
        } as Response
      }
      if (url.endsWith('/control-identity')) {
        return {
          ok: true,
          json: async () => ({
            terminal_id: 't-native',
            terminal_generation: 'generation-1',
            execution_mode: 'native_tui',
          }),
        } as Response
      }
      if (url.endsWith('/control-input') && init?.method === 'POST') {
        return {
          ok: false,
          status: 425,
          statusText: 'Too Early',
          json: async () => ({ detail: 'response lost after request write' }),
        } as Response
      }
      if (url.endsWith('/control-input/control-ambiguous')) {
        return {
          ok: true,
          json: async () => ({
            control_id: 'control-ambiguous',
            outcome: 'ambiguous',
            reason_code: 'response-loss-unresolved',
          }),
        } as Response
      }
      throw new Error(`unexpected request: ${url}`)
    }))

    render(<TerminalView terminalId="t-native" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')
    fireEvent.change(
      screen.getByPlaceholderText('Send a message to the native composer…'),
      { target: { value: 'continue' } },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(
      await screen.findByText(
        'send: ambiguous (control-ambiguous) — response-loss-unresolved',
      ),
    ).toBeTruthy()
    expect(requests.filter(request => request.method === 'POST')).toHaveLength(1)
    expect(
      requests.filter(request => request.url.endsWith('/control-input/control-ambiguous')),
    ).toHaveLength(1)
  })

  it('offers no actions when a managed execution mode is unresolved', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      const response = url.endsWith('/managed-control')
        ? { managed: true, execution_mode: 'future_mode' }
        : {}
      return {
        ok: true,
        json: async () => response,
      } as Response
    }))

    render(<TerminalView terminalId="t-unknown" onClose={() => {}} />)

    expect(
      await screen.findByText('Managed mode unknown · controls unavailable'),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Compact' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel turn' })).toBeNull()
  })
})

// ── Lane B: streaming, macros, capability degradation (§6, §7, §3.5) ────

const FULL_KEYS = [
  'Backspace', 'C-c', 'C-s', 'Delete', 'Down', 'End', 'Enter', 'Escape',
  'Home', 'Insert', 'Left', 'PageDown', 'PageUp', 'Right', 'Tab', 'Up',
]

const KIMI_COMPACT_EVENTS = [
  { type: 'text', text: '/compact' },
  { type: 'key', key: 'Enter' },
]
const KIMI_STOP_EVENTS = [{ type: 'key', key: 'Escape' }]

interface MacroFixture {
  id: string
  name: string
  description: string | null
  scope: Record<string, unknown>
  events: Array<Record<string, unknown>>
  favorite: boolean
  origin: 'builtin' | 'user'
  mutable: boolean
  builtin_kind?: 'compact' | 'stop'
  created_at: string | null
  updated_at: string | null
}

function builtinFixture(kind: 'compact' | 'stop'): MacroFixture {
  return {
    id: `builtin:kimi_cli:${kind}`,
    name: kind === 'compact' ? 'Compact' : 'Stop',
    description: null,
    scope: { kind: 'provider', provider: 'kimi_cli' },
    events: kind === 'compact' ? KIMI_COMPACT_EVENTS : KIMI_STOP_EVENTS,
    favorite: true,
    origin: 'builtin',
    mutable: false,
    builtin_kind: kind,
    created_at: null,
    updated_at: null,
  }
}

function userFixture(overrides: Partial<MacroFixture>): MacroFixture {
  return {
    id: 'user-1',
    name: 'Model K2.7',
    description: null,
    scope: { kind: 'provider', provider: 'kimi_cli' },
    events: [
      { type: 'text', text: '/model' },
      { type: 'key', key: 'Enter' },
    ],
    favorite: true,
    origin: 'user',
    mutable: true,
    created_at: null,
    updated_at: null,
    ...overrides,
  }
}

interface StubOptions {
  streamingBlock?: boolean
  commandControls?: boolean
  providerControls?: boolean
  steerChords?: string[]
  macrosStatus?: number
  macros?: MacroFixture[]
  schemaVersions?: number[]
  sequenceKeys?: string[]
  // r15 (§6.7): whether the per-terminal, build-exact identity block
  // advertises interactive streaming. Default false — the honest
  // old-server shape, under which armed batches never declare.
  interactiveStreaming?: boolean
  // When true, the first control-input POST answers a pane-busy refusal
  // carrying the wire `detail` field (never `reason_detail`) — the r15
  // normalization case; later POSTs answer accepted.
  firstBatchBusy?: boolean
}

/**
 * A fetch stub shaped like the §3.5 new-server responses: full key set,
 * streaming, provider_controls (kimi), command_controls, and a /macros
 * library. Options remove blocks to exercise the §3.5 old-server rows.
 */
function stubLaneBFetch(
  requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }>,
  options: StubOptions = {},
) {
  const {
    streamingBlock = true,
    commandControls = true,
    providerControls = true,
    steerChords = ['C-s'],
    macrosStatus = 200,
    macros = [builtinFixture('compact'), builtinFixture('stop'), userFixture({})],
    schemaVersions = [1, 2, 3, 4],
    sequenceKeys = FULL_KEYS,
    interactiveStreaming = false,
    firstBatchBusy = false,
  } = options
  let controlNumber = 0
  let postNumber = 0
  vi.stubGlobal('crypto', { randomUUID: () => `control-${++controlNumber}` })
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    requests.push({ url, method: init?.method, body })
    if (url.endsWith('/managed-control')) {
      return {
        ok: true,
        json: async () => ({ managed: true, generation: 'generation-1', execution_mode: 'native_tui' }),
      } as Response
    }
    if (url.endsWith('/control-input/capabilities')) {
      return {
        ok: true,
        json: async () => ({
          protocol: 'cao-control-input-v1',
          execution_modes: ['native_tui'],
          literal_write: true,
          bracketed_paste: false,
          enter_required: true,
          request_schema_versions: schemaVersions,
          sequence: {
            event_types: ['chord', 'key', 'text'],
            keys: sequenceKeys,
            max_events: 32,
            max_text_bytes: 512,
          },
          ...(streamingBlock
            ? { streaming: { supported: true, max_in_flight: 1, coalesce_window_ms: 200 } }
            : {}),
          ...(providerControls
            ? {
                provider_controls: {
                  kimi_cli: {
                    compact: { events: KIMI_COMPACT_EVENTS },
                    stop: { events: KIMI_STOP_EVENTS },
                    steer_chords: steerChords,
                    dispatch_grace_ms: 5000,
                  },
                },
              }
            : {}),
          ...(commandControls ? { command_controls: { composer_nonempty_guard: true } } : {}),
        }),
      } as Response
    }
    if (url.endsWith('/control-identity')) {
      return {
        ok: true,
        json: async () => ({
          terminal_id: 't-native',
          terminal_incarnation: 'incarnation-1',
          terminal_generation: 'generation-1',
          pane_birth_id: '%7',
          provider_process_id: '42@start',
          provider: 'kimi_cli',
          native_session_id: 'session-1',
          execution_mode: 'native_tui',
          session_name: 'cao-test',
          control_input: {
            schema_versions: [1, 2, 3, 4],
            sequence: { keys: FULL_KEYS, max_events: 32, max_text_bytes: 512 },
            ...(providerControls
              ? {
                  provider_controls: {
                    kimi_cli: {
                      steer_chords: steerChords,
                      dispatch_grace_ms: 5000,
                      ...(interactiveStreaming ? { interactive_streaming: { supported: true } } : {}),
                    },
                  },
                }
              : {}),
          },
        }),
      } as Response
    }
    if (url.includes('/macros')) {
      if (macrosStatus !== 200) {
        return {
          ok: false,
          status: macrosStatus,
          statusText: 'Not Found',
          json: async () => ({ detail: 'not found' }),
        } as Response
      }
      return { ok: true, json: async () => ({ macros }) } as Response
    }
    if (url.endsWith('/control-input') && init?.method === 'POST') {
      postNumber += 1
      if (firstBatchBusy && postNumber === 1) {
        return {
          ok: true,
          json: async () => ({
            control_id: body?.control_id,
            outcome: 'refused',
            reason_code: 'pane-busy',
            detail:
              'the receiver is processing, not idle; a composer-class sequence is ' +
              'readiness-gated and nothing was written',
          }),
        } as Response
      }
      return {
        ok: true,
        json: async () => ({ control_id: body?.control_id, outcome: 'accepted', events: body?.events }),
      } as Response
    }
    if (url.includes('/control-input/')) {
      return {
        ok: true,
        json: async () => ({ outcome: 'ambiguous', reason_code: 'response-lost' }),
      } as Response
    }
    throw new Error(`unexpected request: ${url}`)
  }))
}

function controlInputPosts(
  requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }>,
) {
  return requests.filter(r => r.url.endsWith('/control-input') && r.method === 'POST')
}

describe('TerminalView Lane B dashboard (§6, §7)', () => {
  it('arms streaming, captures a fused utterance, shows the trace, and Stop restores the draft', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests)
    render(<TerminalView terminalId="t-native" provider="kimi_cli" agentProfile="spec-writer-k3" onClose={() => {}} />)

    await screen.findByText('Managed native TUI · identity-bound controls')
    // The draft survives arm/disarm (§6.1).
    fireEvent.change(screen.getByPlaceholderText('Send a message to the native composer…'), {
      target: { value: 'keep me' },
    })
    // The favorite strip renders in server order with a count badge.
    await screen.findByText('Model K2.7')
    expect(screen.getByRole('button', { name: /Macros/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    expect(
      screen.getByText(/STREAMING TO kimi_cli \/ spec-writer-k3 · gen genera/),
    ).toBeTruthy()

    fireEvent.keyDown(capture, { key: 'h' })
    fireEvent.keyDown(capture, { key: 'i' })
    fireEvent.keyDown(capture, { key: 'Enter' })

    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      // Enter-after-text fusion: one request carries text+Enter (§6.3).
      expect(posts[0].body?.events).toEqual([
        { type: 'text', text: 'hi' },
        { type: 'key', key: 'Enter' },
      ])
      // Streaming NEVER declares command-class (§4.1).
      expect(posts[0].body?.payload_class).toBeUndefined()
      // The identity pinned at arm is bound to the batch (§6.3 step 4).
      expect(posts[0].body?.expected_identity).toMatchObject({
        terminal_id: 't-native',
        terminal_generation: 'generation-1',
      })
    })

    // The trace records the accepted batch.
    expect(await screen.findByText('accepted')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Stop streaming' }))
    expect(await screen.findByText(/Streaming disarmed: operator stopped streaming/)).toBeTruthy()
    // The composer returns with its draft preserved.
    expect(
      (screen.getByPlaceholderText('Send a message to the native composer…') as HTMLInputElement).value,
    ).toBe('keep me')
  })

  it('§6.7: armed batches declare payload_class "interactive" when the per-terminal block advertises it', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { interactiveStreaming: true })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" agentProfile="spec-writer-k3" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    fireEvent.keyDown(capture, { key: 'h' })
    fireEvent.keyDown(capture, { key: 'i' })
    fireEvent.keyDown(capture, { key: 'Enter' })

    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      // The declaration rides the same POST body; the wire sequence is
      // otherwise unchanged (§6.7 — text+Enter fusion intact).
      expect(posts[0].body?.payload_class).toBe('interactive')
      expect(posts[0].body?.events).toEqual([
        { type: 'text', text: 'hi' },
        { type: 'key', key: 'Enter' },
      ])
    })
  })

  it('§6.7: no declaration without the per-terminal block, and the arm notice says why', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests) // interactiveStreaming defaults to the honest absent shape
    render(<TerminalView terminalId="t-native" provider="kimi_cli" agentProfile="spec-writer-k3" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    // The old-server fallback is stated, never a speculative bypass.
    await screen.findByText(/interactive declaration unavailable on this server/)
    fireEvent.keyDown(capture, { key: 'x' })
    fireEvent.keyDown(capture, { key: 'Enter' })
    await waitFor(() => expect(controlInputPosts(requests)).toHaveLength(1))
    expect(controlInputPosts(requests)[0].body?.payload_class).toBeUndefined()
  })

  it('r15: a pane-busy carrying the wire `detail` field pauses instead of disarming', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { firstBatchBusy: true })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" agentProfile="spec-writer-k3" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    fireEvent.keyDown(capture, { key: 'h' })
    fireEvent.keyDown(capture, { key: 'i' })
    fireEvent.keyDown(capture, { key: 'Enter' })

    // Before r15 this exact wire shape (detail, never reason_detail) took
    // the fail-closed "unrecognized" disarm on the live path. Now the
    // batch pauses on the turn-gate discriminator and retries once.
    await screen.findByText(/provider busy/)
    expect(screen.queryByText(/Streaming disarmed/)).toBeNull()
    await waitFor(() => expect(controlInputPosts(requests)).toHaveLength(2), { timeout: 4000 })
    // No disarm followed the explainable sequence either.
    expect(screen.queryByText(/Streaming disarmed/)).toBeNull()
  })

  it('§6.6: the armed capture surface sends zero websocket input frames', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests)
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    fireEvent.keyDown(capture, { key: 'x' })
    fireEvent.keyDown(capture, { key: 'Enter' })

    await waitFor(() => expect(controlInputPosts(requests)).toHaveLength(1))
    const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    expect(socket.sent.filter(frame => frame.includes('"input"'))).toHaveLength(0)
  })

  it('gates chords on the per-terminal advertised set: unadvertised Ctrl+S is refused locally, zero POSTs', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { steerChords: [] })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    fireEvent.keyDown(capture, { key: 's', ctrlKey: true })

    // The refusal is shown inline AND recorded in the trace (§6.2).
    expect(
      (await screen.findAllByText(/not admitted for this terminal's provider and build/))
        .length,
    ).toBeGreaterThan(0)
    expect(controlInputPosts(requests)).toHaveLength(0)
  })

  it('sends an advertised chord as a chord event', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { steerChords: ['C-s'] })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)
    await screen.findByText('Managed native TUI · identity-bound controls')

    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    fireEvent.keyDown(capture, { key: 's', ctrlKey: true })

    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      expect(posts[0].body?.events).toEqual([{ type: 'chord', chord: 'C-s' }])
    })
  })

  it('sends a favorite-strip macro as one v3 request; user macros never set payload_class', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests)
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    const macroButton = await screen.findByRole('button', { name: 'Model K2.7' })
    fireEvent.click(macroButton)

    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      expect(posts[0].body?.events).toEqual([
        { type: 'text', text: '/model' },
        { type: 'key', key: 'Enter' },
      ])
      expect(posts[0].body?.payload_class).toBeUndefined()
      expect(posts[0].body?.expected_identity).toMatchObject({ terminal_id: 't-native' })
    })
  })

  it('the Compact built-in declares payload_class "command" only when command_controls is advertised', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { commandControls: true })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Compact' }))
    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      expect(posts[0].body?.payload_class).toBe('command')
      expect(posts[0].body?.events).toEqual(KIMI_COMPACT_EVENTS)
    })
  })

  it('without command_controls the Compact built-in sends no payload_class and states why (§4.1 rule 4)', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { commandControls: false })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    expect(
      await screen.findByText('prefill-concatenation guard unavailable on this server'),
    ).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Compact' }))
    await waitFor(() => {
      const posts = controlInputPosts(requests)
      expect(posts).toHaveLength(1)
      expect(posts[0].body?.payload_class).toBeUndefined()
    })
  })

  it('opens the macro library modal (layout smoke) and closes it again', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests)
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /Macros/ }))
    expect(await screen.findByRole('dialog')).toBeTruthy()
  })
})

describe('TerminalView Lane B old-server degradation (§3.5)', () => {
  it('v3 absent: macros/streaming hidden behind the stated notice', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { schemaVersions: [1, 2], streamingBlock: false })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    expect(
      await screen.findByText(/Macros and streaming need control-input schema v3/),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Macros/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Streaming' })).toHaveProperty('disabled', true)
  })

  it('streaming block absent: the toggle is disabled with "server predates streaming"', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { streamingBlock: false })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    expect(await screen.findByText(/this server predates streaming/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Streaming' })).toHaveProperty('disabled', true)
  })

  it('key set incomplete: the streaming toggle stays disabled', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, {
      sequenceKeys: ['Backspace', 'C-c', 'C-s', 'Enter', 'Escape'],
    })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    expect(await screen.findByText(/this server predates streaming/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Streaming' })).toHaveProperty('disabled', true)
  })

  it('/macros 404: the library UI is hidden behind a notice', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { macrosStatus: 404 })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    expect(
      await screen.findByText('The macro library is unavailable on this server.'),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Macros/ })).toBeNull()
  })

  it('provider_controls absent: built-ins hidden, user macros still available', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown> }> = []
    stubLaneBFetch(requests, { providerControls: false })
    render(<TerminalView terminalId="t-native" provider="kimi_cli" onClose={() => {}} />)

    // The user favorite still renders in the strip…
    expect(await screen.findByRole('button', { name: 'Model K2.7' })).toBeTruthy()
    // …but the synthesized built-ins are hidden (§3.5).
    expect(screen.queryByRole('button', { name: 'Compact' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })
})
