import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  StreamingEngine,
  STREAMING_GATE_RETRY_MS,
  TRACE_LIMIT,
  type SendResult,
  type TraceEntry,
} from '../lib/streaming'
import type { SequenceEvent } from '../lib/sequenceRecorder'

const plain = (key: string) => ({ key, ctrlKey: false, metaKey: false, altKey: false })
const ctrl = (key: string) => ({ key, ctrlKey: true, metaKey: false, altKey: false })

const KIMI_CHORDS: ReadonlySet<string> = new Set(['C-s'])
const NO_CHORDS: ReadonlySet<string> = new Set()

const accepted: SendResult = { kind: 'resolved', result: { outcome: 'accepted' } }
const refused = (reasonCode: string, reasonDetail = ''): SendResult => ({
  kind: 'resolved',
  result: { outcome: 'refused', reasonCode, reasonDetail },
})

interface Harness {
  engine: StreamingEngine
  sent: Array<{ controlId: string; events: SequenceEvent[] }>
  deferreds: Array<{ resolve: (result: SendResult) => void }>
  trace: TraceEntry[]
  disarmReasons: Array<{ reason: string; reasonCode?: string }>
  resolveNext: (result: SendResult) => Promise<void>
}

function makeHarness(options?: {
  dispatchGraceMs?: number
  chords?: ReadonlySet<string>
  declareInteractive?: boolean
}): Harness {
  const sent: Harness['sent'] = []
  const deferreds: Harness['deferreds'] = []
  const disarmReasons: Harness['disarmReasons'] = []
  const harness: Harness = {
    engine: undefined as unknown as StreamingEngine,
    sent,
    deferreds,
    trace: [],
    disarmReasons,
    resolveNext: async (result: SendResult) => {
      const deferred = deferreds.shift()
      if (!deferred) throw new Error('no in-flight batch to resolve')
      deferred.resolve(result)
      // Let the engine's .then chain settle.
      await Promise.resolve()
      await Promise.resolve()
    },
  }
  let idCounter = 0
  const engine = new StreamingEngine(
    {
      coalesceWindowMs: 200,
      dispatchGraceMs: options?.dispatchGraceMs,
      declareInteractive: options?.declareInteractive,
      advertisedChords: options?.chords ?? KIMI_CHORDS,
      mintId: () => `control-${(idCounter += 1)}`,
    },
    {
      onSendBatch: (controlId, events) => {
        sent.push({ controlId, events })
        return new Promise<SendResult>(resolve => deferreds.push({ resolve }))
      },
      onTrace: trace => {
        harness.trace = trace
      },
      onDisarm: (reason, reasonCode) => {
        disarmReasons.push({ reason, reasonCode })
      },
    },
  )
  harness.engine = engine
  return harness
}

function typeText(engine: StreamingEngine, text: string) {
  for (const char of text) {
    const result = engine.handleKey(plain(char))
    expect(result.refused).toBeUndefined()
  }
}

describe('streaming batching (§6.3)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('coalesces typed text on the quiet timer', () => {
    const h = makeHarness()
    typeText(h.engine, 'hi')
    expect(h.sent).toHaveLength(0)
    vi.advanceTimersByTime(199)
    expect(h.sent).toHaveLength(0)
    vi.advanceTimersByTime(1)
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([{ type: 'text', text: 'hi' }])
  })

  it('fuses a trailing Enter with pending text into one request', () => {
    const h = makeHarness()
    typeText(h.engine, '/model')
    h.engine.handleKey(plain('Enter'))
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([
      { type: 'text', text: '/model' },
      { type: 'key', key: 'Enter' },
    ])
  })

  it('sends a bare Enter as its own batch immediately', () => {
    const h = makeHarness()
    h.engine.handleKey(plain('Enter'))
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([{ type: 'key', key: 'Enter' }])
  })

  it('treats a non-text event after text as a flush boundary', async () => {
    const h = makeHarness()
    typeText(h.engine, 'hi')
    h.engine.handleKey(plain('ArrowUp'))
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([{ type: 'text', text: 'hi' }])
    vi.advanceTimersByTime(200)
    expect(h.sent).toHaveLength(1) // the boundary batch is still in flight
    await h.resolveNext(accepted)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([{ type: 'key', key: 'Up' }])
  })

  it('flushes a pending text event at 48 chars', () => {
    const h = makeHarness()
    typeText(h.engine, 'x'.repeat(47))
    expect(h.sent).toHaveLength(0)
    typeText(h.engine, 'y')
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([{ type: 'text', text: `${'x'.repeat(47)}y` }])
  })

  it('seals the open batch at the 32-event cap and keeps typing', async () => {
    const h = makeHarness()
    for (let i = 0; i < 32; i += 1) h.engine.handleKey(plain('ArrowUp'))
    expect(h.sent).toHaveLength(0)
    h.engine.handleKey(plain('ArrowUp'))
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toHaveLength(32)
    vi.advanceTimersByTime(200)
    expect(h.sent).toHaveLength(1) // in flight; the 33rd key waits its turn
    await h.resolveNext(accepted)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([{ type: 'key', key: 'Up' }])
  })

  it('serializes: input during a flight forms the next batch in order', async () => {
    const h = makeHarness()
    typeText(h.engine, 'a')
    h.engine.handleKey(plain('Enter'))
    expect(h.sent).toHaveLength(1)
    typeText(h.engine, 'b')
    h.engine.handleKey(plain('Enter'))
    expect(h.sent).toHaveLength(1) // one batch in flight
    await h.resolveNext(accepted)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([
      { type: 'text', text: 'b' },
      { type: 'key', key: 'Enter' },
    ])
  })

  it('refuses over-budget paste with a trace-visible message, no partial send', () => {
    const h = makeHarness()
    h.engine.handlePaste('x'.repeat(40))
    const result = h.engine.handlePaste('y'.repeat(480)) // 40 + 480 > 512
    expect(result.refused).toMatch(/refused rather than truncated/)
    const last = h.trace[h.trace.length - 1]
    expect(last.outcome).toBe('refused-locally')
    vi.advanceTimersByTime(200)
    expect(h.sent).toHaveLength(1) // only the accepted first paste
    expect(h.sent[0].events).toEqual([{ type: 'text', text: 'x'.repeat(40) }])
  })
})

describe('streaming capture honesty (§6.2, §10.4 Sol P1-2)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('refuses an unadvertised chord locally: zero POSTs, trace-visible', () => {
    const h = makeHarness({ chords: NO_CHORDS })
    const result = h.engine.handleKey(ctrl('s'))
    expect(result.refused).toMatch(/not admitted/)
    vi.advanceTimersByTime(1000)
    expect(h.sent).toHaveLength(0)
    const last = h.trace[h.trace.length - 1]
    expect(last.outcome).toBe('refused-locally')
    expect(last.controlIdShort).toBe('—')
  })

  it('refuses arbitrary Ctrl+letters absent from the advertised set', () => {
    const h = makeHarness({ chords: KIMI_CHORDS })
    expect(h.engine.handleKey(ctrl('x')).refused).toMatch(/not admitted/)
    vi.advanceTimersByTime(1000)
    expect(h.sent).toHaveLength(0)
  })

  it('admits an advertised chord and sends it as a chord event', () => {
    const h = makeHarness({ chords: KIMI_CHORDS })
    expect(h.engine.handleKey(ctrl('s')).refused).toBeUndefined()
    vi.advanceTimersByTime(200)
    expect(h.sent).toHaveLength(1)
    expect(h.sent[0].events).toEqual([{ type: 'chord', chord: 'C-s' }])
  })
})

describe('streaming grace pacing (§6.3 step 3, §10.4)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('withholds composer-class batches while the dispatch grace runs', async () => {
    const h = makeHarness({ dispatchGraceMs: 5000 })
    typeText(h.engine, 'hi')
    h.engine.handleKey(plain('Enter'))
    await h.resolveNext(accepted) // stamps graceUntil = now + 5000
    typeText(h.engine, 'next')
    vi.advanceTimersByTime(200) // t=200: quiet timer seals the batch
    expect(h.sent).toHaveLength(1) // withheld during grace
    vi.advanceTimersByTime(4799) // t=4999: still inside the window
    expect(h.sent).toHaveLength(1)
    vi.advanceTimersByTime(1) // t=5000: window expires, batch releases
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([{ type: 'text', text: 'next' }])
  })

  it('lets interrupt-only batches through during the grace', async () => {
    const h = makeHarness({ dispatchGraceMs: 5000 })
    typeText(h.engine, 'hi')
    h.engine.handleKey(plain('Enter'))
    await h.resolveNext(accepted)
    h.engine.handleKey(plain('Escape'))
    vi.advanceTimersByTime(200)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([{ type: 'key', key: 'Escape' }])
  })

  it('sends the next declared-interactive composer batch immediately after an accepted Enter (§6.7)', async () => {
    const h = makeHarness({ dispatchGraceMs: 5000, declareInteractive: true })
    typeText(h.engine, 'hi')
    h.engine.handleKey(plain('Enter'))
    await h.resolveNext(accepted)
    typeText(h.engine, 'next')
    vi.advanceTimersByTime(200) // quiet timer seals the batch
    // The server no longer refuses declared batches on the dispatch grace,
    // so no grace is armed and the composer-class batch sends immediately
    // instead of waiting out the 5000 ms window.
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].events).toEqual([{ type: 'text', text: 'next' }])
  })
})

describe('streaming timers under browser receiver semantics (§10.4 Sol P1 regression)', () => {
  // Browsers require the timer intrinsics to be invoked with the global as
  // their receiver. The engine stores its defaults on `this.config` and calls
  // them as methods (`this.config.setTimeoutFn(...)`), so a bare `setTimeout`
  // default runs with the config object as `this` — Chrome rejects that with
  // `TypeError: Illegal invocation`, which killed the quiet timer on the
  // first captured printable key (no batch, no trace). Node's timers never
  // check the receiver, so every test above is blind to it; this harness
  // wraps the globals with the browser's receiver check.
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout

  function browserReceiverCheck<T extends (...args: never[]) => unknown>(fn: T): T {
    const wrapped = function (this: unknown, ...args: unknown[]) {
      // Sloppy-mode/direct calls arrive with undefined or the global itself;
      // anything else (e.g. a config object) is the browser's illegal case.
      if (this !== undefined && this !== globalThis) {
        throw new TypeError('Illegal invocation')
      }
      return Reflect.apply(fn, undefined, args)
    }
    return wrapped as unknown as T
  }

  beforeEach(() => {
    globalThis.setTimeout = browserReceiverCheck(realSetTimeout)
    globalThis.clearTimeout = browserReceiverCheck(realClearTimeout)
  })
  afterEach(() => {
    globalThis.setTimeout = realSetTimeout
    globalThis.clearTimeout = realClearTimeout
  })

  it('first printable capture has no exception, forms a batch and a trace on the control route', async () => {
    const sent: Array<{ controlId: string; events: SequenceEvent[] }> = []
    const deferreds: Array<{ resolve: (result: SendResult) => void }> = []
    let trace: TraceEntry[] = []
    let idCounter = 0
    const engine = new StreamingEngine(
      {
        coalesceWindowMs: 5,
        advertisedChords: NO_CHORDS,
        mintId: () => `control-${(idCounter += 1)}`,
        // No setTimeoutFn/clearTimeoutFn: the engine must bind its defaults.
      },
      {
        onSendBatch: (controlId, events) => {
          sent.push({ controlId, events })
          return new Promise<SendResult>(resolve => deferreds.push({ resolve }))
        },
        onTrace: next => {
          trace = next
        },
        onDisarm: () => {},
      },
    )

    // The first printable key must not throw (the P1 repro), and the quiet
    // timer must arm so the batch flushes on the coalesce window alone.
    expect(() => engine.handleKey(plain('a'))).not.toThrow()
    await new Promise(resolve => realSetTimeout(resolve, 40))
    expect(sent).toHaveLength(1)
    expect(sent[0].events).toEqual([{ type: 'text', text: 'a' }])
    // Only the identity-bound control route carries the batch: the engine is
    // DOM-free and its sole egress is onSendBatch with the minted control id.
    expect(sent[0].controlId).toBe('control-1')

    deferreds.shift()?.resolve(accepted)
    await Promise.resolve()
    await Promise.resolve()
    expect(trace.map(entry => entry.outcome)).toEqual(['accepted'])
    expect(trace[0].controlIdShort).toBe('control-')

    // The cancel path (Enter-after-text fusion calls clearTimeout) is covered
    // by the same receiver check: no throw, the fused batch sends at once.
    expect(() => engine.handleKey(plain('b'))).not.toThrow()
    expect(() => engine.handleKey(plain('Enter'))).not.toThrow()
    expect(sent).toHaveLength(2)
    expect(sent[1].events).toEqual([
      { type: 'text', text: 'b' },
      { type: 'key', key: 'Enter' },
    ])
    engine.disarm('test done')
  })
})

describe('streaming pause and disarm (§6.4)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function sendFused(h: Harness, text: string) {
    typeText(h.engine, text)
    h.engine.handleKey(plain('Enter'))
    expect(h.sent).toHaveLength(1)
  }

  it('pause — dispatch grace: one scheduled re-attempt with a fresh control_id', async () => {
    const h = makeHarness({ dispatchGraceMs: 5000 })
    await sendFused(h, 'hi')
    await h.resolveNext(refused('pane-busy', 'pane is inside its dispatch grace window'))
    expect(h.disarmReasons).toHaveLength(0)
    const paused = h.trace[h.trace.length - 1]
    expect(paused.outcome).toBe('paused')
    expect(paused.note).toMatch(/paused \(dispatch grace\)/)
    // The re-attempt fires when the advertised grace window expires (clock
    // skew backstop: local estimate unknown → full advertised window).
    expect(h.sent).toHaveLength(1)
    vi.advanceTimersByTime(5000)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].controlId).not.toBe(h.sent[0].controlId)
    expect(h.sent[1].events).toEqual(h.sent[0].events)
    await h.resolveNext(accepted)
    expect(h.disarmReasons).toHaveLength(0)
  })

  it('pause — dispatch grace: a second refusal disarms with the reason', async () => {
    const h = makeHarness({ dispatchGraceMs: 5000 })
    await sendFused(h, 'hi')
    await h.resolveNext(refused('pane-busy', 'pane is inside its dispatch grace window'))
    vi.advanceTimersByTime(5000)
    expect(h.sent).toHaveLength(2)
    await h.resolveNext(refused('pane-busy', 'pane is inside its dispatch grace window'))
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/re-attempt was also refused/)
    expect(h.engine.isDisarmed).toBe(true)
  })

  it('pause — readiness gate: one re-attempt after the client-pinned 1000 ms', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext(refused('pane-busy', 'provider is not idle'))
    expect(h.disarmReasons).toHaveLength(0)
    const paused = h.trace[h.trace.length - 1]
    expect(paused.note).toMatch(/provider busy/)
    vi.advanceTimersByTime(STREAMING_GATE_RETRY_MS - 1)
    expect(h.sent).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(h.sent).toHaveLength(2)
    expect(h.sent[1].controlId).not.toBe(h.sent[0].controlId)
  })

  it('disarm — lease contention is concurrent input, never a pause', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext(refused('pane-busy', 'input lease is held by another process'))
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/concurrent input/)
    vi.advanceTimersByTime(5000)
    expect(h.sent).toHaveLength(1) // no re-attempt
  })

  it('disarm — a pane-busy matching no pinned discriminator fails closed', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext(refused('pane-busy', 'some entirely new wording'))
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/unrecognized detail/)
  })

  it('disarm — every other refusal reason', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext(refused('stale-generation', 'generation moved'))
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/refused: stale-generation/)
    expect(h.disarmReasons[0].reasonCode).toBe('stale-generation')
  })

  it('disarm — ambiguous is terminal, never resent', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext({ kind: 'resolved', result: { outcome: 'ambiguous', reasonCode: 'response-lost' } })
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/ambiguous/)
    vi.advanceTimersByTime(10000)
    expect(h.sent).toHaveLength(1)
  })

  it('disarm — unsupported and unknown typed outcomes behave identically', async () => {
    for (const outcome of ['unsupported', 'mystery-outcome']) {
      const h = makeHarness()
      typeText(h.engine, 'hi')
      h.engine.handleKey(plain('Enter'))
      await h.resolveNext({ kind: 'resolved', result: { outcome } })
      expect(h.disarmReasons).toHaveLength(1)
      expect(h.engine.isDisarmed).toBe(true)
      vi.advanceTimersByTime(10000)
      expect(h.sent).toHaveLength(1)
    }
  })

  it('disarm — reconciliation failure', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    await h.resolveNext({ kind: 'reconcile-failed' })
    expect(h.disarmReasons).toHaveLength(1)
    expect(h.disarmReasons[0].reason).toMatch(/reconciliation query failed/)
  })

  // Sol P2-1 acceptance (§10.4): atomic disarm — input arriving during a
  // refused in-flight batch is discarded with the quiet timer cancelled;
  // no second POST; the trace retains only metadata.
  it('atomic disarm discards pending input and cancels the quiet timer', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    typeText(h.engine, 'pending words')
    h.engine.handleKey(plain('ArrowUp'))
    expect(h.engine.pendingEventCount).toBeGreaterThan(0)
    await h.resolveNext(refused('pane-dead', 'pane is gone'))
    expect(h.engine.isDisarmed).toBe(true)
    expect(h.engine.pendingEventCount).toBe(0)
    vi.advanceTimersByTime(10000)
    expect(h.sent).toHaveLength(1) // nothing typed after the disarm was sent
    // The trace retains the refused batch and the disarm note only.
    expect(h.trace.map(entry => entry.outcome)).toEqual(['refused', 'disarmed'])
    expect(h.trace[1].events).toBe(0)
    // Further input is refused in place.
    expect(h.engine.handleKey(plain('x')).refused).toMatch(/disarmed/)
  })

  it('disarm keeps the in-flight reconciliation running to completion', async () => {
    const h = makeHarness()
    await sendFused(h, 'hi')
    typeText(h.engine, 'more')
    // Operator/environment disarm while the batch is still in flight.
    h.engine.disarm('output websocket closed')
    expect(h.engine.isDisarmed).toBe(true)
    await h.resolveNext(accepted)
    // The settled batch is trace metadata; nothing further sends.
    expect(h.trace.map(entry => entry.outcome)).toEqual(['disarmed', 'accepted'])
    vi.advanceTimersByTime(10000)
    expect(h.sent).toHaveLength(1)
  })

  it('keeps the trace bounded at 50 batches', async () => {
    const h = makeHarness()
    for (let i = 0; i < TRACE_LIMIT + 5; i += 1) {
      h.engine.handleKey(plain('Enter'))
      await h.resolveNext(accepted)
    }
    expect(h.trace.length).toBeLessThanOrEqual(TRACE_LIMIT)
  })
})
