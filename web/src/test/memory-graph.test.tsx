// Tests for the Memory panel's List⇄Graph toggle and the graph sub-view.
//
// Sigma needs a real WebGL2 context which jsdom lacks, so `sigma` is mocked
// (mirroring cao_mcp_apps/src/graph/GraphView.test.tsx): the fake records the
// graphology graph it was constructed with and lets tests simulate clickNode.
// Assertions target the graph data + wired handlers + api calls, not canvas
// pixels.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { useStore } from '../store'

type AnyHandler = (payload: any) => void

const { FakeSigma, getLastSigma, resetLastSigma } = vi.hoisted(() => {
  let last: any
  // Fake camera records enable/disable calls so drag tests can assert the
  // camera was disabled during a drag and re-enabled after.
  class FakeCamera {
    enabled = true
    enableCalls = 0
    disableCalls = 0
    enable() {
      this.enabled = true
      this.enableCalls++
      return this
    }
    disable() {
      this.enabled = false
      this.disableCalls++
      return this
    }
  }
  class FakeSigmaImpl {
    graph: any
    container: HTMLElement
    handlers: Record<string, AnyHandler[]> = {}
    killed = false
    camera = new FakeCamera()
    constructor(graph: unknown, container: HTMLElement) {
      this.graph = graph
      this.container = container
      last = this
    }
    on(event: string, handler: AnyHandler) {
      ;(this.handlers[event] ??= []).push(handler)
    }
    emit(event: string, payload: any) {
      for (const h of this.handlers[event] ?? []) h(payload)
    }
    getCamera() {
      return this.camera
    }
    // Identity conversion is fine for tests — we only assert that whatever
    // coords come out of moveBody get written onto the node.
    viewportToGraph({ x, y }: { x: number; y: number }) {
      return { x, y }
    }
    getGraph() {
      return this.graph
    }
    kill() {
      this.killed = true
    }
  }
  return {
    FakeSigma: FakeSigmaImpl,
    getLastSigma: () => last,
    resetLastSigma: () => {
      last = undefined
    },
  }
})

vi.mock('sigma', () => ({ default: FakeSigma }))

// eslint-disable-next-line import/first
import { MemoryPanel } from '../components/MemoryPanel'

const MEMORIES = [
  {
    key: 'project-conventions',
    scope: 'project',
    scope_id: 'my-proj',
    memory_type: 'project',
    tags: 'style',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-10T00:00:00Z',
  },
]

const GRAPH = {
  nodes: [
    { id: 'hub1', kind: 'topic', label: 'Hub', status: 'active', attrs: { is_hub: true } },
    { id: 'orphan1', kind: 'topic', label: 'Orphan', status: 'active', attrs: { is_orphan: true } },
    { id: 'n3', kind: 'topic', label: 'Plain', status: 'active', attrs: {} },
  ],
  edges: [
    { source: 'hub1', target: 'orphan1', type: 'contradiction', attrs: {} },
    { source: 'hub1', target: 'n3', type: 'relates_to', attrs: {} },
  ],
  meta: {},
}

describe('MemoryPanel — List⇄Graph toggle & graph view', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    // Clear call history between tests — mockFetch is module-level, so a
    // negative assertion (e.g. "no /memory/ read after a drag") would
    // otherwise see calls from an earlier test.
    mockFetch.mockClear()
    vi.stubGlobal('fetch', mockFetch)
  })

  afterEach(() => {
    cleanup()
    resetLastSigma()
    useStore.setState({ snackbar: null })
    vi.restoreAllMocks()
  })

  // The scope CustomSelect renders its selected label ("All scopes") as button
  // text — but that same string also appears in the graph scope-guard message,
  // so target the FIRST match (the select trigger, rendered before the guard).
  function selectGlobalScope() {
    fireEvent.click(screen.getAllByText('All scopes')[0])
    // The dropdown option is a <button>; "global" also appears as text in the
    // guard message, so disambiguate by role.
    fireEvent.click(screen.getByRole('button', { name: 'global' }))
  }

  // Route mock responses by URL + method so a component that fires list + graph
  // + detail fetches gets the right body for each.
  function routeFetch(handler: (url: string, opts?: any) => { status: number; body: unknown }) {
    mockFetch.mockImplementation((url: string, opts?: any) => {
      const { status, body } = handler(url, opts)
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        statusText: status === 200 ? 'OK' : 'Error',
        json: () => Promise.resolve(body),
      })
    })
  }

  it('defaults to List view and toggles to Graph', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      return { status: 200, body: MEMORIES }
    })
    render(<MemoryPanel />)
    // List is the default view.
    expect(await screen.findByText('project-conventions')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    // Graph tab now selected; list row gone from the DOM.
    await waitFor(() => expect(screen.getByRole('tab', { name: /graph/i })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByText('project-conventions')).not.toBeInTheDocument()
  })

  it('shows the friendly scope guard for All scopes and does NOT fetch the graph', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    // scopeFilter defaults to '' (All scopes) → guard, no graph fetch.
    expect(await screen.findByText(/Pick/i)).toBeInTheDocument()
    expect(mockFetch.mock.calls.some(c => String(c[0]).startsWith('/graph/'))).toBe(false)
  })

  it('fetches + renders the graph for global scope with hub/orphan/contradiction styling', async () => {
    let graphUrl = ''
    routeFetch(url => {
      if (url.startsWith('/graph/')) {
        graphUrl = url
        return { status: 200, body: GRAPH }
      }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()

    await waitFor(() => expect(getLastSigma()).toBeDefined())
    expect(graphUrl).toBe('/graph/memory?scope=global')

    const graph = getLastSigma()!.graph as import('graphology').default
    expect(graph.getNodeAttributes('hub1').size).toBe(12)
    expect(graph.getNodeAttributes('orphan1').color).toBe('#9ca3af')
    expect(graph.getNodeAttributes('n3').color).toBe('#2563eb')
    expect(graph.getEdgeAttributes(graph.edge('hub1', 'orphan1')).color).toBe('#dc2626')
    expect(graph.getEdgeAttributes(graph.edge('hub1', 'n3')).color).toBe('#94a3b8')
    // Every node has a finite position (Sigma throws otherwise).
    graph.forEachNode(n => {
      expect(Number.isFinite(graph.getNodeAttribute(n, 'x'))).toBe(true)
      expect(Number.isFinite(graph.getNodeAttribute(n, 'y'))).toBe(true)
    })
  })

  it('clicking a node calls api.getMemory and shows its content as plain text', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      if (url.startsWith('/memory/hub1')) {
        return {
          status: 200,
          body: { key: 'hub1', scope: 'global', scope_id: null, memory_type: 'user', tags: '', created_at: '', updated_at: '2026-06-10T00:00:00Z', content: 'HUB CONTENT <script>x</script>' },
        }
      }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()
    await waitFor(() => expect(getLastSigma()).toBeDefined())

    getLastSigma()!.emit('clickNode', { node: 'hub1' })

    // Content rendered verbatim as text (no HTML interpretation): the literal
    // <script> string is present as text, proving plain-text rendering.
    const body = await screen.findByText(/HUB CONTENT/)
    expect(body.textContent).toContain('<script>x</script>')
    expect(body.querySelector('script')).toBeNull()
    // The memory detail fetch used the graph's scope.
    expect(mockFetch.mock.calls.some(c => String(c[0]) === '/memory/hub1?scope=global')).toBe(true)
  })

  it('Export button calls api.exportGraph and snackbars the written path', async () => {
    let exportBody: any
    routeFetch((url, opts) => {
      if (url.startsWith('/graph/memory/export')) {
        exportBody = JSON.parse(opts.body)
        return { status: 200, body: { written_files: ['/vaults/global-vault/hub1.md'], sink: 'obsidian', dest: 'global-vault' } }
      }
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()
    await waitFor(() => expect(getLastSigma()).toBeDefined())

    fireEvent.click(screen.getByText('Export to Obsidian'))

    // Feedback goes through the shared store snackbar (rendered by <App>, not
    // this isolated panel), so assert on the store state.
    await waitFor(() => expect(useStore.getState().snackbar).toBeTruthy())
    const snack = useStore.getState().snackbar!
    expect(snack.type).toBe('success')
    expect(snack.message).toMatch(/Exported 1 note/)
    expect(snack.message).toContain('global-vault')
    expect(exportBody).toEqual({ options: {}, sink: 'obsidian', dest: 'global-vault' })
  })

  it('dragging a node updates its x/y, toggles the camera, and does NOT read the memory', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()
    await waitFor(() => expect(getLastSigma()).toBeDefined())

    const sigma = getLastSigma()!
    const graph = sigma.graph as import('graphology').default

    // Simulate a real drag: downNode → moveBody → upStage.
    let sigmaDefaultPrevented = false
    const original = { preventDefault: vi.fn(), stopPropagation: vi.fn() }
    sigma.emit('downNode', { node: 'hub1' })
    // Camera must be disabled for the duration of the drag so panning doesn't
    // fight the node move.
    expect(sigma.camera.enabled).toBe(false)
    expect(sigma.camera.disableCalls).toBe(1)

    sigma.emit('moveBody', {
      event: {
        x: 123,
        y: 456,
        preventSigmaDefault: () => {
          sigmaDefaultPrevented = true
        },
        original,
      },
    })
    // The node moved to the (identity-converted) pointer coords.
    expect(graph.getNodeAttribute('hub1', 'x')).toBe(123)
    expect(graph.getNodeAttribute('hub1', 'y')).toBe(456)
    // The drag suppressed Sigma's own pan handling.
    expect(sigmaDefaultPrevented).toBe(true)
    expect(original.preventDefault).toHaveBeenCalled()
    expect(original.stopPropagation).toHaveBeenCalled()

    sigma.emit('upStage', { event: { x: 123, y: 456 } })
    // Camera re-enabled after the drag ends.
    expect(sigma.camera.enabled).toBe(true)
    expect(sigma.camera.enableCalls).toBe(1)

    // A trailing clickNode after a drag must NOT open the topic (no read).
    sigma.emit('clickNode', { node: 'hub1' })
    // Give any (unexpected) async read a tick to fire.
    await Promise.resolve()
    expect(mockFetch.mock.calls.some(c => String(c[0]).startsWith('/memory/'))).toBe(false)
    // No side panel content appeared.
    expect(screen.queryByText(/Loading/)).not.toBeInTheDocument()
  })

  it('a plain clickNode (no drag) still reads the memory', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      if (url.startsWith('/memory/hub1')) {
        return {
          status: 200,
          body: { key: 'hub1', scope: 'global', scope_id: null, memory_type: 'user', tags: '', created_at: '', updated_at: '2026-06-10T00:00:00Z', content: 'PLAIN CLICK CONTENT' },
        }
      }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()
    await waitFor(() => expect(getLastSigma()).toBeDefined())

    // downNode + upNode with NO moveBody in between = a click, not a drag.
    const sigma = getLastSigma()!
    sigma.emit('downNode', { node: 'hub1' })
    sigma.emit('upNode', { node: 'hub1' })
    sigma.emit('clickNode', { node: 'hub1' })

    expect(await screen.findByText('PLAIN CLICK CONTENT')).toBeInTheDocument()
    expect(mockFetch.mock.calls.some(c => String(c[0]) === '/memory/hub1?scope=global')).toBe(true)
  })

  it('project scope sends scope_id on the graph fetch', async () => {
    let graphUrl = ''
    routeFetch(url => {
      if (url.startsWith('/graph/')) {
        graphUrl = url
        return { status: 200, body: GRAPH }
      }
      // The list fetch returns a project memory so MemoryPanel discovers a
      // default scope_id ('my-proj') for the project graph.
      return { status: 200, body: MEMORIES }
    })
    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    // Pick the project scope; the panel defaults graphScopeId from MEMORIES.
    fireEvent.click(screen.getAllByText('All scopes')[0])
    fireEvent.click(screen.getByRole('button', { name: 'project' }))

    await waitFor(() => expect(getLastSigma()).toBeDefined())
    // project → scope_id IS carried on the request.
    expect(graphUrl).toBe('/graph/memory?scope=project&scope_id=my-proj')
  })

  it('switching project→global drops the stale scope_id (never rides along on global)', async () => {
    const graphUrls: string[] = []
    routeFetch(url => {
      if (url.startsWith('/graph/')) {
        graphUrls.push(url)
        return { status: 200, body: GRAPH }
      }
      return { status: 200, body: MEMORIES }
    })
    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))

    // First select project so a scope_id ('my-proj') lands in shared state.
    fireEvent.click(screen.getAllByText('All scopes')[0])
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    await waitFor(() => expect(graphUrls.some(u => u.includes('scope=project'))).toBe(true))
    expect(graphUrls.some(u => u === '/graph/memory?scope=project&scope_id=my-proj')).toBe(true)

    // Now switch to global. graphScopeId ('my-proj') is still in state, but the
    // effectiveScopeId guard must OMIT it — global has no scope_id.
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'global' }))

    await waitFor(() => expect(graphUrls.some(u => u.includes('scope=global'))).toBe(true))
    const globalUrls = graphUrls.filter(u => u.includes('scope=global'))
    // Every global fetch is scope-only — no leaked project scope_id.
    for (const u of globalUrls) {
      expect(u).toBe('/graph/memory?scope=global')
      expect(u).not.toContain('scope_id')
    }
  })

  it('a connection failure renders the corrected :9889 message (no 9894, no stray URL)', async () => {
    // First graph fetch rejects like connection-refused: no HTTP status.
    mockFetch.mockImplementation((url: string) => {
      if (String(url).startsWith('/graph/')) {
        return Promise.reject(new TypeError('Failed to fetch'))
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve([]) })
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()

    const errBox = await screen.findByTestId('graph-error')
    expect(errBox.textContent).toMatch(/:9889/)
    expect(errBox.textContent).toMatch(/cao-server/)
    expect(errBox.textContent).not.toMatch(/9894/)
    expect(errBox.textContent).not.toMatch(/127\.0\.0\.1/)
  })

  it('a graph-fetch timeout (AbortError) renders the timeout message mentioning :9889', async () => {
    mockFetch.mockImplementation((url: string) => {
      if (String(url).startsWith('/graph/')) {
        const err = new Error('The operation was aborted.')
        err.name = 'AbortError'
        return Promise.reject(err)
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve([]) })
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()

    const errBox = await screen.findByTestId('graph-error')
    expect(errBox.textContent).toMatch(/timed out/i)
    expect(errBox.textContent).toMatch(/:9889/)
    expect(errBox.textContent).not.toMatch(/9894/)
  })

  it('a stale graph fetch (scope switched mid-flight) does NOT overwrite the current view', async () => {
    // Two graphs so we can tell which scope's data landed. The FIRST fetch
    // (project) is held open until AFTER the second (global) resolves, so the
    // late project resolution must be ignored by the latest-wins guard.
    const PROJECT_GRAPH = {
      nodes: [{ id: 'proj-only', kind: 'topic', label: 'Proj', status: 'active', attrs: {} }],
      edges: [],
      meta: {},
    }
    let releaseProject: (v: unknown) => void = () => {}
    const projectGate = new Promise(res => {
      releaseProject = res
    })
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.startsWith('/graph/memory?scope=project')) {
        // Hold the project fetch until the test releases it.
        return projectGate.then(() => ({
          ok: true,
          status: 200,
          statusText: 'OK',
          json: () => Promise.resolve(PROJECT_GRAPH),
        }))
      }
      if (u.startsWith('/graph/memory?scope=global')) {
        return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(GRAPH) })
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(MEMORIES) })
    })

    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))

    // Start the (slow) project fetch.
    fireEvent.click(screen.getAllByText('All scopes')[0])
    fireEvent.click(screen.getByRole('button', { name: 'project' }))

    // Switch to global before the project fetch resolves; global resolves first
    // and renders its graph.
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'global' }))
    await waitFor(() => expect(getLastSigma()).toBeDefined())
    const graphAfterGlobal = getLastSigma()!.graph as import('graphology').default
    expect(graphAfterGlobal.hasNode('hub1')).toBe(true)
    expect(graphAfterGlobal.hasNode('proj-only')).toBe(false)

    // Now let the stale project fetch resolve — it must NOT clobber global.
    releaseProject(undefined)
    await Promise.resolve()
    await Promise.resolve()

    const graphNow = getLastSigma()!.graph as import('graphology').default
    // Still global's graph — the late project resolution was ignored.
    expect(graphNow.hasNode('hub1')).toBe(true)
    expect(graphNow.hasNode('proj-only')).toBe(false)
    // No error was set by the stale request, and the canvas is still shown.
    expect(screen.queryByTestId('graph-error')).toBeNull()
    expect(screen.getByTestId('graph-canvas')).toBeInTheDocument()
  })

  it('a stale graph fetch that REJECTS does NOT clobber the current view with an error (catch-path guard)', async () => {
    // Companion to the success case above, but the held project fetch REJECTS
    // (500) after global has already rendered. The catch-path isStale() guard
    // (fetchGraph :141) must swallow it: no setError, no setView(null). Without
    // that guard, the late rejection would blow away global's graph and show an
    // error box.
    let releaseProject: (v: unknown) => void = () => {}
    const projectGate = new Promise(res => {
      releaseProject = res
    })
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.startsWith('/graph/memory?scope=project')) {
        // Held open; when released, resolve to a non-OK response so fetchJSON
        // throws an ApiError (exercising fetchGraph's catch).
        return projectGate.then(() => ({
          ok: false,
          status: 500,
          statusText: 'Server Error',
          json: () => Promise.resolve({ detail: 'boom' }),
        }))
      }
      if (u.startsWith('/graph/memory?scope=global')) {
        return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(GRAPH) })
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(MEMORIES) })
    })

    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))

    // Start the (slow, doomed) project fetch, then switch to global before it
    // settles. Global resolves first and renders its graph.
    fireEvent.click(screen.getAllByText('All scopes')[0])
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'global' }))
    await waitFor(() => expect(getLastSigma()).toBeDefined())
    const graphAfterGlobal = getLastSigma()!.graph as import('graphology').default
    expect(graphAfterGlobal.hasNode('hub1')).toBe(true)
    expect(graphAfterGlobal.hasNode('proj-only')).toBe(false)

    // Now let the stale project fetch REJECT — the catch guard must ignore it.
    releaseProject(undefined)
    // Flush the reject → res.json() → throw → catch → finally microtask chain.
    await new Promise(resolve => setTimeout(resolve, 0))
    await new Promise(resolve => setTimeout(resolve, 0))

    // No error box appeared and global's graph is untouched.
    expect(screen.queryByTestId('graph-error')).toBeNull()
    expect(screen.getByTestId('graph-canvas')).toBeInTheDocument()
    const graphNow = getLastSigma()!.graph as import('graphology').default
    expect(graphNow.hasNode('hub1')).toBe(true)
    expect(graphNow.hasNode('proj-only')).toBe(false)
  })

  it('a stale fetch settling does NOT flip loading off while the current fetch is still pending (finally-path guard)', async () => {
    // The stale project fetch settles while the current global fetch is STILL
    // in flight. fetchGraph's finally-path isStale() guard (:170) must leave
    // loading=true so the spinner keeps showing. Without that guard, the stale
    // finally would setLoading(false) and mask the pending global spinner.
    let releaseProject: (v: unknown) => void = () => {}
    const projectGate = new Promise(res => {
      releaseProject = res
    })
    // Global never resolves during this test — it stays pending so its spinner
    // is the state under test.
    const globalGate = new Promise<never>(() => {})
    mockFetch.mockImplementation((url: string) => {
      const u = String(url)
      if (u.startsWith('/graph/memory?scope=project')) {
        return projectGate.then(() => ({
          ok: true,
          status: 200,
          statusText: 'OK',
          json: () => Promise.resolve(GRAPH),
        }))
      }
      if (u.startsWith('/graph/memory?scope=global')) {
        return globalGate
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(MEMORIES) })
    })

    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))

    // Start project (fetch A, loading=true), then switch to global (fetch B,
    // still pending). Both leave loading=true; the spinner is showing.
    fireEvent.click(screen.getAllByText('All scopes')[0])
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'project' }))
    fireEvent.click(screen.getByRole('button', { name: 'global' }))
    await waitFor(() => expect(screen.getByTestId('graph-loading')).toBeInTheDocument())

    // Settle the STALE project fetch — its finally must NOT clear loading.
    releaseProject(undefined)
    await new Promise(resolve => setTimeout(resolve, 0))
    await new Promise(resolve => setTimeout(resolve, 0))

    // Global is still pending, so the spinner is still up: the stale finally
    // did not mask it.
    expect(screen.getByTestId('graph-loading')).toBeInTheDocument()
    // And the stale success payload did not render a graph either.
    expect(getLastSigma()).toBeUndefined()
  })

  it('422 secret-gate export shows the pattern detail and NO content', async () => {
    routeFetch(url => {
      if (url.startsWith('/graph/memory/export')) {
        return { status: 422, body: { detail: "export rejected: secret pattern 'aws-access-key-id' detected" } }
      }
      if (url.startsWith('/graph/')) return { status: 200, body: GRAPH }
      return { status: 200, body: [] }
    })
    render(<MemoryPanel />)
    await screen.findByText('No memories stored.')
    fireEvent.click(screen.getByRole('tab', { name: /graph/i }))
    selectGlobalScope()
    await waitFor(() => expect(getLastSigma()).toBeDefined())

    fireEvent.click(screen.getByText('Export to Obsidian'))

    await waitFor(() => expect(useStore.getState().snackbar).toBeTruthy())
    const snack = useStore.getState().snackbar!
    expect(snack.type).toBe('error')
    expect(snack.message).toMatch(/secret gate/i)
    // Only the server's pattern-name detail is surfaced — never content.
    expect(snack.message).toContain('aws-access-key-id')
    expect(snack.message).toMatch(/Nothing was written/i)
  })
})
