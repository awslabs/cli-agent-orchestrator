// The boot order the fleet routing depends on.
//
// Every path the dashboard fetches is rewritten to `/nodes/{name}<path>` when the
// panel is serving it, and the node comes from `/api/fleet`. So a request sent
// before discovery resolves carries no node, and the panel answers none of those
// paths — it 404s. The failure is quiet by construction: an optional stat's
// `.catch(() => {})` is the correct handler for a real outage, and an effect with
// empty deps never retries once the node lands, so a wrong value stays on screen
// for the life of the page.
//
// Sequencing App's own fetches behind `discoverFleet()` does not cover this: a
// child's mount effect runs on the same commit, long before that promise settles.
// The gate is that no fetching component mounts until `fleetReady`.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import App from '../App'
import { setActiveNode } from '../api'
import { useStore } from '../store'

// xterm needs a real canvas and is irrelevant here — the terminal is only
// reachable after a session is opened, which is well past boot.
vi.mock('../components/TerminalView', () => ({
  TerminalView: () => null,
}))

const mockFetch = vi.fn()

/** Paths fetched so far, in order. */
function calls(): string[] {
  return mockFetch.mock.calls.map(c => String(c[0]))
}

beforeEach(() => {
  mockFetch.mockReset()
  vi.stubGlobal('fetch', mockFetch)
  useStore.setState({ fleetNodes: [], activeNode: null, fleetReady: false, sessions: [] })
  setActiveNode(null)
})

afterEach(() => {
  setActiveNode(null)
  vi.restoreAllMocks()
})

function ok(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(data),
    text: () => Promise.resolve(JSON.stringify(data)),
  }
}

describe('boot order', () => {
  it('sends nothing but /api/fleet until discovery resolves', async () => {
    // Discovery that never settles: the window in which a mount-time fetch would
    // escape is held open for the whole test rather than raced against.
    let release: (v: unknown) => void = () => {}
    const pending = new Promise(resolve => {
      release = resolve
    })
    mockFetch.mockImplementation((url: string) => {
      if (String(url).includes('/api/fleet')) return pending.then(() => ok({ machines: [] }))
      return Promise.resolve(ok([]))
    })

    render(<App />)

    // Give React every chance to run child effects and flush microtasks.
    await waitFor(() => expect(calls().length).toBeGreaterThan(0))
    await Promise.resolve()

    const escaped = calls().filter(u => !u.includes('/api/fleet'))
    expect(escaped).toEqual([])

    release(null)
  })

  it('every request after discovery carries the node', async () => {
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/api/fleet')) {
        return Promise.resolve(
          ok({
            machines: [
              { name: 'alpha', label: 'alpha', host: '10.0.0.1', role: 'central', online: true, sessions: [] },
            ],
          }),
        )
      }
      if (u.includes('/settings/memory')) return Promise.resolve(ok({ enabled: false }))
      return Promise.resolve(ok([]))
    })

    render(<App />)

    // The profile count is the one that regressed: DashboardHome fetches it from
    // its own mount effect, so it is the first thing to escape an ungated boot.
    await waitFor(() => {
      expect(calls().some(u => u.includes('/agents/profiles'))).toBe(true)
    })

    for (const url of calls()) {
      if (url.includes('/api/fleet')) continue
      expect(url).toContain('/nodes/alpha/')
    }
  })

  it('still boots when no panel is serving it', async () => {
    // A cao-server 404s /api/fleet. Discovery must settle anyway, or the gate
    // below would hold the single-server case on "Loading..." forever.
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/api/fleet')) {
        return Promise.resolve({
          ok: false,
          status: 404,
          statusText: 'Not Found',
          json: () => Promise.resolve({}),
          text: () => Promise.resolve('{}'),
        })
      }
      if (u.includes('/settings/memory')) return Promise.resolve(ok({ enabled: false }))
      return Promise.resolve(ok([]))
    })

    render(<App />)

    await waitFor(() => expect(useStore.getState().fleetReady).toBe(true))
    // And the paths are byte-identical to the pre-fleet app: no prefix at all.
    await waitFor(() => expect(calls().some(u => u.includes('/agents/profiles'))).toBe(true))
    expect(calls().filter(u => u.includes('/nodes/'))).toEqual([])
  })

  it('refetches per-node data when the node changes', async () => {
    // The other half of the same bug. Gating the FIRST render is not enough:
    // an effect with empty deps runs once and never again, so a component that
    // stays mounted across a switch — the dashboard does — keeps showing the
    // previous node's numbers next to the new node's (empty) session list.
    // Observed as PROFILES 1 on an offline node with 0 SESSIONS.
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/api/fleet')) {
        return Promise.resolve(
          ok({
            machines: [
              { name: 'alpha', label: 'alpha', host: '10.0.0.1', role: 'central', online: true, sessions: [] },
              { name: 'beta', label: 'beta', host: '10.0.0.2', role: 'worker', online: true, sessions: [] },
            ],
          }),
        )
      }
      if (u.includes('/settings/memory')) return Promise.resolve(ok({ enabled: false }))
      return Promise.resolve(ok([]))
    })

    render(<App />)

    await waitFor(() => {
      expect(calls().some(u => u.includes('/nodes/alpha/agents/profiles'))).toBe(true)
    })

    await act(async () => {
      await useStore.getState().selectNode('beta')
    })

    // Asked of beta, not carried over from alpha.
    await waitFor(() => {
      expect(calls().some(u => u.includes('/nodes/beta/agents/profiles'))).toBe(true)
    })
  })

  it('holds the content area, not the whole page, while discovering', async () => {
    mockFetch.mockImplementation((url: string) => {
      if (String(url).includes('/api/fleet')) return new Promise(() => {})
      return Promise.resolve(ok([]))
    })

    render(<App />)

    // The header still paints — a participant sees the app, not a blank tab.
    expect(screen.getByText('CLI Agent Orchestrator')).toBeInTheDocument()
    expect(screen.getByText('Loading...')).toBeInTheDocument()
  })
})
