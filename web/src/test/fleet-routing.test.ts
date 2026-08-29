// Fleet routing: one dashboard drives either a single cao-server or a fleet.
//
// The invariant these tests exist to hold is that the single-node case is
// untouched — with no active node every request is byte-identical to what this
// app has always sent. The fleet case is the same paths with one prefix.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  api,
  fleet,
  setActiveNode,
  getActiveNode,
  nodePath,
  terminalSocketUrl,
  eventStreamUrl,
} from '../api'
import { useStore } from '../store'

const mockFetch = vi.fn()

function mockResponse(data: unknown, status = 200) {
  mockFetch.mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: () => Promise.resolve(data),
    text: () => Promise.resolve(JSON.stringify(data)),
  })
}

function node(name: string, extra: Record<string, unknown> = {}) {
  return { name, label: name, host: `10.0.0.${name.length}`, online: true, sessions: [], ...extra }
}

beforeEach(() => {
  mockFetch.mockReset()
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  // The active node is module state by design (fetch, the terminal socket and
  // the SSE loop all read it). Reset it so one test cannot route another.
  setActiveNode(null)
  vi.restoreAllMocks()
})

describe('node routing', () => {
  it('sends paths unchanged when there is no fleet', async () => {
    expect(getActiveNode()).toBeNull()
    mockResponse([])
    await api.listSessions()
    expect(mockFetch).toHaveBeenCalledWith('/sessions', expect.anything())
  })

  it('routes every path through the active node', async () => {
    setActiveNode('node-b')
    mockResponse([])
    await api.listSessions()
    expect(mockFetch).toHaveBeenCalledWith('/nodes/node-b/sessions', expect.anything())

    mockResponse({ output: '', mode: 'full' })
    await api.getTerminalOutput('t1')
    // the query string survives the rewrite
    expect(mockFetch).toHaveBeenLastCalledWith(
      '/nodes/node-b/terminals/t1/output?mode=full',
      expect.anything(),
    )
  })

  it('encodes the node name', () => {
    setActiveNode('worker/../admin')
    expect(nodePath('/sessions')).toBe('/nodes/worker%2F..%2Fadmin/sessions')
  })

  it('keeps the panel API off the node route', async () => {
    setActiveNode('node-b')
    mockResponse({ machines: [] })
    await fleet.list()
    // /api/fleet is the panel's own; no node serves it
    expect(mockFetch).toHaveBeenCalledWith('/api/fleet', expect.anything())
  })

  it('routes the terminal socket and the event stream too', () => {
    expect(terminalSocketUrl('t1')).toBe(`ws://${location.host}/terminals/t1/ws`)
    expect(eventStreamUrl('r1')).toBe('/workflows/runs/r1/events')
    expect(eventStreamUrl('r1', 7)).toBe('/workflows/runs/r1/events?after_seq=7')

    setActiveNode('node-c')
    expect(terminalSocketUrl('t1')).toBe(`ws://${location.host}/nodes/node-c/terminals/t1/ws`)
    expect(eventStreamUrl('r1', 7)).toBe('/nodes/node-c/workflows/runs/r1/events?after_seq=7')
  })
})

describe('fleet discovery', () => {
  beforeEach(() => {
    useStore.setState({ fleetNodes: [], activeNode: null, fleetReady: false, sessions: [] })
  })

  it('opens on the coordinating node', async () => {
    mockResponse({ machines: [node('worker-1', { role: 'worker' }), node('sup', { role: 'supervisor' })] })
    await useStore.getState().discoverFleet()
    expect(useStore.getState().activeNode).toBe('sup')
    // the route is live for the transports, not just for rendering
    expect(getActiveNode()).toBe('sup')
    expect(useStore.getState().fleetReady).toBe(true)
  })

  it('falls back to a node that answered', async () => {
    mockResponse({ machines: [node('a', { online: false }), node('b')] })
    await useStore.getState().discoverFleet()
    expect(useStore.getState().activeNode).toBe('b')
  })

  it('treats a missing panel API as the single-node case', async () => {
    mockResponse({ detail: 'not found' }, 404)
    await useStore.getState().discoverFleet()
    const state = useStore.getState()
    expect(state.fleetNodes).toEqual([])
    expect(state.activeNode).toBeNull()
    expect(getActiveNode()).toBeNull()
    // ready, so the app polls on rather than waiting for a fleet that is not there
    expect(state.fleetReady).toBe(true)
    // and not an error the user has to dismiss
    expect(state.snackbar).toBeNull()
  })
})

describe('switching node', () => {
  beforeEach(() => {
    useStore.setState({
      fleetNodes: [node('a'), node('b')],
      activeNode: 'a',
      sessions: [{ id: 's1', name: 's1', status: 'active' }],
      activeSession: 's1',
      terminalStatuses: { t1: 'IDLE' },
      workflowRuns: [{ run_id: 'r1' } as never],
      selectedRunId: 'r1',
      wfEvents: [{ seq: 1 } as never],
      snackbar: null,
    })
    setActiveNode('a')
  })

  it('drops the previous node data instead of relabelling it', async () => {
    mockResponse([]) // the new node's fetchSessions
    await useStore.getState().selectNode('b')
    const state = useStore.getState()
    expect(state.activeNode).toBe('b')
    expect(getActiveNode()).toBe('b')
    expect(state.activeSession).toBeNull()
    expect(state.terminalStatuses).toEqual({})
    expect(state.workflowRuns).toEqual([])
    expect(state.selectedRunId).toBeNull()
    expect(state.wfEvents).toEqual([])
    // and it asked the NEW node for its sessions
    expect(mockFetch).toHaveBeenCalledWith('/nodes/b/sessions', expect.anything())
  })

  it('is a no-op when the node is already active', async () => {
    await useStore.getState().selectNode('a')
    expect(mockFetch).not.toHaveBeenCalled()
    expect(useStore.getState().activeSession).toBe('s1')
  })

  it('moves off a node that left the fleet', async () => {
    mockResponse({ machines: [node('b')] }) // 'a' is gone
    mockResponse([]) // selectNode('b') -> fetchSessions
    await useStore.getState().refreshFleet()
    const state = useStore.getState()
    expect(state.activeNode).toBe('b')
    expect(state.snackbar?.message).toContain('a left the fleet')
  })

  it('keeps the last listing when the panel is unreachable', async () => {
    mockFetch.mockRejectedValueOnce(new Error('network'))
    await useStore.getState().refreshFleet()
    expect(useStore.getState().fleetNodes.map(n => n.name)).toEqual(['a', 'b'])
    expect(useStore.getState().activeNode).toBe('a')
  })
})
