import { beforeEach, describe, it, expect, vi } from 'vitest'
import { useStore } from '../store'

// Minimal EventSource stand-in: capture named listeners and let tests dispatch.
class FakeEventSource {
  static last: FakeEventSource
  url: string
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, (e: { data: string }) => void> = {}
  constructor(url: string) {
    this.url = url
    FakeEventSource.last = this
  }
  addEventListener(type: string, cb: (e: { data: string }) => void) {
    this.listeners[type] = cb
  }
  emit(type: string, data: unknown) {
    this.listeners[type]?.({ data: JSON.stringify(data) })
  }
  close() {}
}

beforeEach(() => {
  ;(globalThis as any).EventSource = FakeEventSource as any
  // fetchSessions (triggered by a flow frame) hits /sessions — stub it inert.
  ;(globalThis as any).fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => [],
    headers: { get: () => null },
  })
  useStore.setState({ terminalStatuses: {}, flowPulses: [], connected: false })
})

describe('connectStatusStream (SSE consumer)', () => {
  it('subscribes to the Runs stream at /events/runs', () => {
    useStore.getState().connectStatusStream()
    expect(FakeEventSource.last.url).toBe('/events/runs')
  })

  it('applies a status frame to terminalStatuses (normalized upper-case)', () => {
    useStore.getState().connectStatusStream()
    FakeEventSource.last.emit('status', { terminal_id: 'abc12345', status: 'processing' })
    expect(useStore.getState().terminalStatuses['abc12345']).toBe('PROCESSING')
  })

  it('records a flow frame as a pulse with sender/receiver/kind', () => {
    useStore.getState().connectStatusStream()
    FakeEventSource.last.emit('flow', { sender_id: 'aaaa1111', receiver_id: 'bbbb2222', kind: 'handoff' })
    const pulses = useStore.getState().flowPulses
    expect(pulses).toHaveLength(1)
    expect(pulses[0]).toMatchObject({ sender: 'aaaa1111', receiver: 'bbbb2222', kind: 'handoff' })
  })

  it('ignores malformed frames (REST reconcile stays the safety net)', () => {
    useStore.getState().connectStatusStream()
    FakeEventSource.last.listeners['status']({ data: 'not-json' })
    FakeEventSource.last.listeners['flow']({ data: '{bad' })
    expect(Object.keys(useStore.getState().terminalStatuses)).toHaveLength(0)
    expect(useStore.getState().flowPulses).toHaveLength(0)
  })

  it('flips connected on open and back on error', () => {
    useStore.getState().connectStatusStream()
    FakeEventSource.last.onopen?.()
    expect(useStore.getState().connected).toBe(true)
    FakeEventSource.last.onerror?.()
    expect(useStore.getState().connected).toBe(false)
  })

  it('prunes pulses older than 30s', () => {
    useStore.getState().connectStatusStream()
    // Seed a stale pulse directly, then push a fresh one via the stream.
    useStore.setState({
      flowPulses: [{ id: 1, sender: 'x', receiver: 'y', kind: 'message', ts: Date.now() - 60_000 }],
    })
    FakeEventSource.last.emit('flow', { sender_id: 'a', receiver_id: 'b', kind: 'message' })
    const pulses = useStore.getState().flowPulses
    expect(pulses).toHaveLength(1)
    expect(pulses[0].sender).toBe('a')
  })
})
