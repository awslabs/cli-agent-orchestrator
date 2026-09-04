import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import App from '../App'
import { ProfilesPanel, SEARCH_DEBOUNCE_MS } from '../components/ProfilesPanel'

// fetchJSON reads the body via res.text() and parses it itself, so a mock
// must supply text() (same convention as settingsPanel.test.tsx).
const okJson = (data: unknown) => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  json: () => Promise.resolve(data),
  text: () => Promise.resolve(JSON.stringify(data)),
})

const errJson = (status: number, detail: string) => ({
  ok: false,
  status,
  statusText: 'Error',
  json: () => Promise.resolve({ detail }),
  text: () => Promise.resolve(JSON.stringify({ detail })),
})

const CATALOG = [
  { name: 'developer', description: 'Writes code', source: 'local' },
  { name: 'analyst', description: 'Analyzes data', source: 'built-in', duplicated_in: ['custom'] },
  { name: 'reviewer', description: 'Reviews PRs', source: 'kiro' },
  // Catalog order for the search rows (z->a->m) deliberately differs from
  // both their ranked order (m->a->z) and alphabetical (a->m->z).
  { name: 'zzz-agent', description: 'Z', source: 'local' },
  { name: 'aaa-agent', description: 'A', source: 'built-in' },
  { name: 'mid-agent', description: 'M', source: 'kiro' },
]

// Deliberately NOT alphabetical and NOT catalog order: the server's ranked
// order is the contract; the client must render it verbatim.
// Three rows chosen so the ranked order (m->a->z) differs from BOTH
// alphabetical (a->m->z) AND catalog order (z->a->m): a client that re-sorts
// by any plausible key, or falls through to the catalog, fails the ordering
// test rather than passing by coincidence.
const SEARCH_RESULTS = [
  { name: 'mid-agent', description: 'M', capabilities: ['review'], tags: [], role: '', source: 'kiro', coverage: 3, score: 3.4 },
  { name: 'aaa-agent', description: 'A', capabilities: [], tags: ['data'], role: '', source: 'built-in', coverage: 2, score: 2.2 },
  { name: 'zzz-agent', description: 'Z', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1.1 },
]

const DETAIL = {
  name: 'analyst',
  description: 'Analyzes data',
  provider: 'kiro_cli',
  model: 'claude-sonnet-4',
  tags: ['data', 'reports'],
  capabilities: ['analyze sqs metrics'],
}

/**
 * URL-routing fetch mock. Order matters: the static /search sub-path is
 * matched before the generic catalog route, mirroring the backend's own
 * route-declaration-order constraint.
 */
function routedFetch(overrides: Record<string, (url: string) => any> = {}) {
  return vi.fn(async (url: string) => {
    for (const [needle, handler] of Object.entries(overrides)) {
      if (url.includes(needle)) return handler(url)
    }
    if (url.includes('/agents/profiles/search')) return okJson(SEARCH_RESULTS)
    if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson(DETAIL)
    if (url.includes('/agents/profiles')) return okJson(CATALOG)
    if (url.includes('/memory/status')) return okJson({ enabled: false })
    if (url.includes('/sessions')) return okJson([])
    return okJson([])
  })
}

afterEach(() => vi.restoreAllMocks())

describe('Profiles tab navigation (stage 1)', () => {
  beforeEach(() => vi.stubGlobal('fetch', routedFetch()))

  it('renders the Profiles tab between Home and Agents', async () => {
    render(<App />)
    const tabs = screen.getAllByRole('tab').map(t => t.textContent)
    const home = tabs.findIndex(t => t?.includes('Home'))
    const profiles = tabs.findIndex(t => t?.includes('Profiles'))
    const agents = tabs.findIndex(t => t?.includes('Agents'))
    expect(profiles).toBe(home + 1)
    expect(agents).toBe(profiles + 1)
  })

  it('opens the Profiles panel when the tab is clicked', async () => {
    render(<App />)
    fireEvent.click(screen.getByRole('tab', { name: /profiles/i }))
    expect(await screen.findByRole('searchbox', { name: /search profiles/i })).toBeInTheDocument()
  })

  it('navigates to the Profiles tab from the Home stat card', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /open profiles tab/i }))
    expect(await screen.findByRole('searchbox', { name: /search profiles/i })).toBeInTheDocument()
  })

  it('Alt+2 selects Profiles and Alt+3 selects Agents (the renumbered shortcuts)', async () => {
    // Inserting the Profiles tab shifted Alt+N for every later tab; this
    // pins the new mapping beyond DOM order.
    render(<App />)
    fireEvent.keyDown(window, { key: '2', altKey: true })
    expect(screen.getByRole('tab', { name: /profiles/i })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(window, { key: '3', altKey: true })
    expect(screen.getByRole('tab', { name: /agents/i })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /profiles/i })).toHaveAttribute('aria-selected', 'false')
  })
})

describe('ProfilesPanel — list and detail (stage 2)', () => {
  beforeEach(() => vi.stubGlobal('fetch', routedFetch()))

  it('lists the catalog in server order with source badges', async () => {
    render(<ProfilesPanel />)
    const list = await screen.findByRole('listbox', { name: /profile list/i })
    const options = within(list).getAllByRole('option')
    expect(options.map(o => o.textContent)).toEqual([
      expect.stringContaining('developer'),
      expect.stringContaining('analyst'),
      expect.stringContaining('reviewer'),
      expect.stringContaining('zzz-agent'),
      expect.stringContaining('aaa-agent'),
      expect.stringContaining('mid-agent'),
    ])
    expect(within(options[0]).getByText('local')).toBeInTheDocument()
    expect(within(options[1]).getByText('built-in')).toBeInTheDocument()
  })

  it('shows detail fields and the duplicated_in warning on selection', async () => {
    render(<ProfilesPanel />)
    fireEvent.click(await screen.findByRole('option', { name: /analyst/i }))
    const detail = await screen.findByTestId('profile-detail')
    await waitFor(() => expect(within(detail).getByText('kiro_cli')).toBeInTheDocument())
    expect(within(detail).getByText('claude-sonnet-4')).toBeInTheDocument()
    expect(within(detail).getByText('reports')).toBeInTheDocument()
    expect(within(detail).getByText('analyze sqs metrics')).toBeInTheDocument()
    expect(within(detail).getByText(/also defined in: custom/i)).toBeInTheDocument()
  })

  it('shows the duplicated_in warning when the profile is reached through SEARCH', async () => {
    // Search results carry no duplicated_in; the panel resolves the shadowing
    // metadata from the catalog by name, so the amber banner renders for both
    // row types (#692 review: it was invisible via search).
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/search': () => okJson([
        { name: 'analyst', description: 'Analyzes data', capabilities: [], tags: [], role: '', source: 'built-in', coverage: 1, score: 1.0 },
      ]),
    }))
    vi.useFakeTimers()
    try {
      render(<ProfilesPanel />)
      await act(async () => {})
      fireEvent.change(screen.getByRole('searchbox', { name: /search profiles/i }), { target: { value: 'analyst' } })
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
      fireEvent.click(screen.getByRole('option', { name: /analyst/i }))
      await act(async () => {})
      expect(within(screen.getByTestId('profile-detail')).getByText(/also defined in: custom/i)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('surfaces a detail load failure without breaking the list', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/analyst': () => errJson(500, 'detail exploded'),
    }))
    render(<ProfilesPanel />)
    fireEvent.click(await screen.findByRole('option', { name: /analyst/i }))
    const detail = await screen.findByTestId('profile-detail')
    await waitFor(() => expect(within(detail).getByRole('alert')).toHaveTextContent('detail exploded'))
    // The list stays intact and another selection still works
    fireEvent.click(screen.getByRole('option', { name: /developer/i }))
    await waitFor(() => expect(within(screen.getByTestId('profile-detail')).getByText('kiro_cli')).toBeInTheDocument())
  })

  it('shows a loading state while the catalog is in flight', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    render(<ProfilesPanel />)
    expect(screen.getByTestId('catalog-loading')).toBeInTheDocument()
  })

  it('surfaces a catalog API error', async () => {
    vi.stubGlobal('fetch', routedFetch({ '/agents/profiles': () => errJson(500, 'store exploded') }))
    render(<ProfilesPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('store exploded')
  })
})

describe('ProfilesPanel — search debounce and ordering (stage 2)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  const searchCalls = (mock: ReturnType<typeof vi.fn>) =>
    mock.mock.calls.filter(([url]) => String(url).includes('/agents/profiles/search'))

  it('issues exactly one search request per keystroke burst, with the final query', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    const box = screen.getByRole('searchbox', { name: /search profiles/i })

    // Burst of keystrokes inside the debounce window
    fireEvent.change(box, { target: { value: 'r' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS - 50))
    fireEvent.change(box, { target: { value: 're' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS - 50))
    fireEvent.change(box, { target: { value: 'review' } })
    expect(searchCalls(mock)).toHaveLength(0) // nothing fired mid-burst

    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS))
    const calls = searchCalls(mock)
    expect(calls).toHaveLength(1)
    expect(String(calls[0][0])).toContain('q=review')
  })

  it('renders results in server rank order, never re-sorted', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    fireEvent.change(screen.getByRole('searchbox', { name: /search profiles/i }), { target: { value: 'data' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))

    const list = screen.getByRole('listbox', { name: /profile list/i })
    const options = within(list).getAllByRole('option')
    // Ranked order m->a->z: distinct from alphabetical (a->m->z) AND catalog
    // (z->a->m), so neither re-sorting nor catalog fallthrough can pass.
    expect(options.map(o => o.textContent)).toEqual([
      expect.stringContaining('mid-agent'),
      expect.stringContaining('aaa-agent'),
      expect.stringContaining('zzz-agent'),
    ])
  })

  it('returns to the full catalog when the query is cleared, without a search call', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    const box = screen.getByRole('searchbox', { name: /search profiles/i })
    fireEvent.change(box, { target: { value: 'data' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
    expect(searchCalls(mock)).toHaveLength(1)

    fireEvent.change(box, { target: { value: '' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
    expect(searchCalls(mock)).toHaveLength(1) // no extra call for the empty query
    const list = screen.getByRole('listbox', { name: /profile list/i })
    expect(within(list).getAllByRole('option')).toHaveLength(CATALOG.length)
  })

  it('shows an empty-results message distinct from an empty catalog', async () => {
    const mock = routedFetch({ '/agents/profiles/search': () => okJson([]) })
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    fireEvent.change(screen.getByRole('searchbox', { name: /search profiles/i }), { target: { value: 'zzz' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
    expect(screen.getByText(/no profiles match this search/i)).toBeInTheDocument()
  })

  it('surfaces a search API error', async () => {
    const mock = routedFetch({ '/agents/profiles/search': () => errJson(500, 'search backend down') })
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    fireEvent.change(screen.getByRole('searchbox', { name: /search profiles/i }), { target: { value: 'x' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
    expect(screen.getByRole('alert')).toHaveTextContent('search backend down')
  })
})

describe('ProfilesPanel — stale in-flight responses are discarded (#692 review)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a search response landing after the box was cleared does not restore filtered rows', async () => {
    // Gate the search response so it can be released AFTER the clear -- the
    // discard branch (`seq !== searchSeq.current`) is only reachable with a
    // genuinely reordered resolution.
    let releaseSearch!: () => void
    const gate = new Promise<ReturnType<typeof okJson>>(res => {
      releaseSearch = () => res(okJson(SEARCH_RESULTS))
    })
    const mock = routedFetch({ '/agents/profiles/search': () => gate })
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    const box = screen.getByRole('searchbox', { name: /search profiles/i })
    await act(async () => {}) // catalog fetch settles
    const list = screen.getByRole('listbox', { name: /profile list/i })

    // Type, let the debounce fire -> request in flight, response gated
    fireEvent.change(box, { target: { value: 'mid' } })
    await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
    expect(mock.mock.calls.filter(([u]) => String(u).includes('/search'))).toHaveLength(1)

    // Clear the box while the request is still in flight
    fireEvent.change(box, { target: { value: '' } })
    await act(async () => {})
    expect(within(list).getAllByRole('option')).toHaveLength(CATALOG.length)

    // The stale response lands: it must be dropped, not re-filter the list
    await act(async () => { releaseSearch() })
    expect(within(list).getAllByRole('option')).toHaveLength(CATALOG.length)
    expect(box).toHaveValue('')
    // No spinner or error left behind by the discarded response
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('round-4 review: mutation vs search/catalog ordering (#692)', () => {
  it('creating under an ACTIVE search clears the query and selects the new profile', async () => {
    // The visible rows are `results ?? catalog`; refreshing only the catalog
    // left the stale results in place, so the created profile was invisible
    // and the detail pane read "Select a profile…" under a success snackbar
    // (haofeif's round-4 P2 probe).
    let created = false
    const NEW_ROW = { name: 'fresh-agent', description: 'Newly made', source: 'local' }
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/agents/profiles/search')) return okJson([
        { name: 'old-agent', description: 'Old', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1.0 },
      ])
      if (url.includes('/agents/profiles/schema')) return okJson({
        type: 'object', required: ['name'],
        properties: { name: { type: 'string' }, description: { type: 'string' } },
      })
      if (url.includes('/agents/profiles/templates')) return okJson([])
      if (url.includes('/agents/providers')) return okJson([])
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') { created = true; return okJson({ name: 'fresh-agent', warnings: [] }) }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson({ name: 'fresh-agent', description: 'Newly made' })
      if (url.includes('/agents/profiles')) return okJson(created ? [...CATALOG, NEW_ROW] : CATALOG)
      return okJson([])
    }))
    vi.useFakeTimers()
    try {
      render(<ProfilesPanel />)
      await act(async () => {})
      // Activate a search whose results will NOT contain the new profile
      const box = screen.getByRole('searchbox', { name: /search profiles/i })
      fireEvent.change(box, { target: { value: 'old' } })
      await act(() => vi.advanceTimersByTimeAsync(400))
      expect(screen.getByRole('option', { name: /old-agent/ })).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: /new profile/i }))
      await act(async () => {}) // modal open-effect fetches settle
      fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
      await act(async () => {})
      fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'fresh-agent' } })
      fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
      await act(async () => {})

      // Search cleared, new row visible AND selected
      expect(box).toHaveValue('')
      expect(screen.getByRole('option', { name: /fresh-agent/ })).toBeInTheDocument()
      await act(async () => {})
      expect(within(screen.getByTestId('profile-detail')).getByText('Newly made')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a slow mount catalog load cannot overwrite a post-create refresh', async () => {
    // Catalog loads now carry a monotonic token: previously the mount
    // snapshot resolving AFTER the create-triggered refresh replaced the
    // fresh catalog, deleting the just-created row (haofeif's round-4 P2).
    let releaseMount!: () => void
    const NEW_ROW = { name: 'fresh-agent', description: 'Newly made', source: 'local' }
    let catalogCall = 0
    let created = false
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/agents/profiles/schema')) return okJson({
        type: 'object', required: ['name'], properties: { name: { type: 'string' } },
      })
      if (url.includes('/agents/profiles/templates')) return okJson([])
      if (url.includes('/agents/providers')) return okJson([])
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') { created = true; return okJson({ name: 'fresh-agent', warnings: [] }) }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson({ name: 'fresh-agent', description: 'Newly made' })
      if (url.includes('/agents/profiles')) {
        catalogCall++
        if (catalogCall === 1) return new Promise<any>(res => { releaseMount = () => res(okJson(CATALOG)) })
        return okJson(created ? [...CATALOG, NEW_ROW] : CATALOG)
      }
      return okJson([])
    }))
    render(<ProfilesPanel />)
    await act(async () => {}) // mount load in flight, gated

    fireEvent.click(screen.getByRole('button', { name: /new profile/i }))
    await act(async () => {}) // modal open-effect fetches settle
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'fresh-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})
    expect(screen.getByRole('option', { name: /fresh-agent/ })).toBeInTheDocument()

    // The stale mount snapshot lands last: it must be discarded
    await act(async () => { releaseMount() })
    expect(screen.getByRole('option', { name: /fresh-agent/ })).toBeInTheDocument()
  })
})

describe('round-5 review: settled-state and navigation ownership (#692)', () => {
  it("a search for A resolving during B's debounce window never installs A's rows", async () => {
    // searchSeq previously advanced when the debounced request STARTED, so a
    // response for prior query A landing inside B's 300ms debounce window
    // still matched the sequence and installed A's rows under B's query
    // (haofeif's round-5 P2). The generation now advances on every query
    // change, before the timer.
    let releaseA!: () => void
    const A_ROWS = [{ name: 'alpha-hit', description: 'A only', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1.0 }]
    const B_ROWS = [{ name: 'beta-hit', description: 'B only', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1.0 }]
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/agents/profiles/search')) {
        if (url.includes('q=alpha')) return new Promise<any>(res => { releaseA = () => res(okJson(A_ROWS)) })
        return okJson(B_ROWS)
      }
      if (url.includes('/agents/profiles')) return okJson(CATALOG)
      return okJson([])
    }))
    vi.useFakeTimers()
    try {
      render(<ProfilesPanel />)
      await act(async () => {})
      const box = screen.getByRole('searchbox', { name: /search profiles/i })
      fireEvent.change(box, { target: { value: 'alpha' } })
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10)) // A in flight, gated

      fireEvent.change(box, { target: { value: 'beta' } }) // B's debounce running
      // A settles INSIDE B's debounce window: it must be discarded
      await act(async () => { releaseA() })
      expect(screen.queryByRole('option', { name: /alpha-hit/ })).not.toBeInTheDocument()

      // B's own request proceeds normally and its rows land
      await act(() => vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 10))
      expect(screen.getByRole('option', { name: /beta-hit/ })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: /alpha-hit/ })).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('navigation performed while the post-create refresh is pending wins over the older clear-and-select', async () => {
    // The create modal closes without awaiting handleCreated, so the panel is
    // interactive while refreshCatalog() is pending. Typing a new search in
    // that window used to be clobbered when the older continuation resolved:
    // it cleared the fresh query and force-selected the saved profile
    // (haofeif's round-5 P2). The continuation now yields to any navigation
    // performed after the mutation.
    let releaseRefresh!: () => void
    const NEW_ROW = { name: 'fresh-agent', description: 'Newly made', source: 'local' }
    let catalogCall = 0
    let created = false
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/agents/profiles/search')) return new Promise(() => {}) // user's search stays in flight
      if (url.includes('/agents/profiles/schema')) return okJson({
        type: 'object', required: ['name'], properties: { name: { type: 'string' } },
      })
      if (url.includes('/agents/profiles/templates')) return okJson([])
      if (url.includes('/agents/providers')) return okJson([])
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') { created = true; return okJson({ name: 'fresh-agent', warnings: [] }) }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson({ name: 'fresh-agent', description: 'Newly made' })
      if (url.includes('/agents/profiles')) {
        catalogCall++
        if (catalogCall > 1) return new Promise<any>(res => { releaseRefresh = () => res(okJson([...CATALOG, NEW_ROW])) })
        return okJson(CATALOG)
      }
      return okJson([])
    }))
    vi.useFakeTimers()
    try {
      render(<ProfilesPanel />)
      await act(async () => {})

      fireEvent.click(screen.getByRole('button', { name: /new profile/i }))
      await act(async () => {})
      fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
      await act(async () => {})
      fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'fresh-agent' } })
      fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
      await act(async () => {}) // POST settles; post-create refresh now in flight, gated

      // User navigates while the refresh is pending
      const box = screen.getByRole('searchbox', { name: /search profiles/i })
      fireEvent.change(box, { target: { value: 'reviewer' } })
      await act(async () => {})

      // The older continuation resolves: it must NOT clear the new query or
      // select the created profile over the user's navigation
      await act(async () => { releaseRefresh() })
      expect(box).toHaveValue('reviewer')
      expect(screen.queryByTestId('profile-detail')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('round-6 review: post-edit detail reload vs navigation ownership (#692)', () => {
  it('newer navigation does not suppress the post-edit detail reload', async () => {
    // The round-5 navigation guard protected the forced selection but sat
    // ABOVE setDetailReload, so a search keystroke while the post-save
    // refresh was pending invalidated the guard and skipped the reload --
    // stranding a still-selected edited profile's pane on pre-edit
    // provider/model/tags indefinitely (haofeif's round-6 P2). The detail
    // revision now advances unconditionally: the server document changed no
    // matter who owns navigation.
    let releaseRefresh!: () => void
    let edited = false
    const SOURCE = '---\nname: developer\ndescription: Writes code\n---\n\nBody.\n'
    let catalogCall = 0
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      const u = String(url)
      if (u.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (u.includes('/agents/profiles/search')) return new Promise(() => {}) // user's search stays in flight
      if (u.includes('/source')) return okJson({ name: 'developer', content: SOURCE })
      if (u.endsWith('/agents/profiles/developer') && opts?.method === 'PUT') { edited = true; return okJson({ name: 'developer', warnings: [] }) }
      if (/\/agents\/profiles\/[^/?]+$/.test(u)) return okJson({
        name: 'developer', description: 'Writes code', provider: 'kiro_cli',
        model: edited ? 'post-edit-model' : 'pre-edit-model', tags: [], capabilities: [],
      })
      if (u.includes('/agents/profiles')) {
        catalogCall++
        if (catalogCall > 1) return new Promise<any>(res => { releaseRefresh = () => res(okJson(CATALOG)) })
        return okJson(CATALOG)
      }
      return okJson([])
    }))
    vi.useFakeTimers()
    try {
      render(<ProfilesPanel />)
      await act(async () => {})
      fireEvent.click(screen.getByRole('option', { name: /developer/ }))
      await act(async () => {})
      expect(within(screen.getByTestId('profile-detail')).getByText('pre-edit-model')).toBeInTheDocument()

      // In-place edit through the real editor modal
      fireEvent.click(within(screen.getByTestId('profile-detail')).getByRole('button', { name: /edit/i }))
      await act(async () => {})
      const editor = screen.getByRole('textbox', { name: /profile source/i })
      fireEvent.change(editor, { target: { value: SOURCE.replace('Body.', 'New body.') } })
      fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
      await act(async () => {}) // PUT settles; post-save refresh now in flight, gated

      // User navigates while the refresh is pending: this must NOT suppress
      // the detail reload for the edited (still-selected) profile
      fireEvent.change(screen.getByRole('searchbox', { name: /search profiles/i }), { target: { value: 'developer' } })
      await act(async () => {})

      await act(async () => { releaseRefresh() })
      await act(async () => {})
      expect(within(screen.getByTestId('profile-detail')).getByText('post-edit-model')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
