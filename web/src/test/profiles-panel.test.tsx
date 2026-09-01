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
