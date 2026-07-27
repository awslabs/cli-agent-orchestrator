import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { ProfilesBrowser } from '../components/ProfilesBrowser'
import { api } from '../api'

// #510 U2: ProfilesBrowser interaction tests. The component holds no ranking or
// validation logic — it renders whatever the (mocked) server returns. The key
// assertions: search renders results in SERVER ORDER (no client re-sort), an
// empty query shows the grouped list without calling search, unloadable rows
// are badged + View-only, and the validate panel splits errors/warnings/valid.

const GROUPED: any[] = [
  { name: 'developer', description: 'Dev agent', source: 'built-in', loadable: true, tags: [], capabilities: [] },
  { name: 'reviewer', description: 'Review agent', source: 'built-in', loadable: true, tags: [], capabilities: [] },
  { name: 'my-local', description: 'Local agent', source: 'local', loadable: true, tags: [], capabilities: [] },
  { name: 'broken-one', description: 'Broken', source: 'local', loadable: false, tags: [], capabilities: [] },
]

// Server search order is coverage → BM25 → name; this payload is intentionally
// NOT name-sorted so a client-side re-sort would be detectable.
const SEARCH_RESULTS: any[] = [
  { name: 'zzz-full', description: 'sqs monitor', capabilities: [], tags: [], role: null, source: 'local', coverage: 2, score: 2.74 },
  { name: 'aaa-partial', description: 'sqs', capabilities: [], tags: [], role: null, source: 'built-in', coverage: 1, score: 1.51 },
]

describe('ProfilesBrowser', () => {
  let searchSpy: any

  beforeEach(() => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue(GROUPED as any)
    searchSpy = vi.spyOn(api, 'searchProfiles').mockResolvedValue(SEARCH_RESULTS as any)
    vi.spyOn(api, 'getProfile').mockResolvedValue({
      name: 'developer', description: 'Dev agent', role: 'developer', provider: 'claude_code', model: 'opus',
    } as any)
    vi.spyOn(api, 'validateProfile').mockResolvedValue({ valid: true, errors: [], warnings: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('renders the grouped list from listProfiles on mount', async () => {
    render(<ProfilesBrowser />)
    expect(await screen.findByTestId('grouped-list')).toBeInTheDocument()
    expect(screen.getByText('developer')).toBeInTheDocument()
    expect(screen.getByText('my-local')).toBeInTheDocument()
    // Grouped by source: both a Built-in and a Local group header appear.
    expect(screen.getByText(/Built-in \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Local \(2\)/)).toBeInTheDocument()
  })

  it('badges an unloadable profile and keeps it View-only (no Edit)', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    // The unloadable row shows the badge...
    expect(screen.getByText('unloadable')).toBeInTheDocument()
    // ...and no Edit/Clone action is offered anywhere in phase-1 (View only).
    expect(screen.queryByText('Edit')).not.toBeInTheDocument()
    expect(screen.queryByText('Clone')).not.toBeInTheDocument()
    // Every row (including unloadable) still offers View.
    expect(screen.getAllByText('View').length).toBe(GROUPED.length)
  })

  it('does not call searchProfiles for an empty/whitespace query', async () => {
    vi.useFakeTimers()
    render(<ProfilesBrowser />)
    // flush the mount fetch
    await vi.runOnlyPendingTimersAsync()
    const input = screen.getByLabelText('Search agent profiles')
    fireEvent.change(input, { target: { value: '   ' } })
    await vi.advanceTimersByTimeAsync(300)
    expect(searchSpy).not.toHaveBeenCalled()
    // The grouped list is still shown (no ranked results panel).
    expect(screen.queryByTestId('search-results')).not.toBeInTheDocument()
    expect(screen.getByTestId('grouped-list')).toBeInTheDocument()
  })

  it('renders search results in SERVER ORDER (no client re-sort)', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    const input = screen.getByLabelText('Search agent profiles')
    fireEvent.change(input, { target: { value: 'sqs monitor' } })

    const resultsBox = await screen.findByTestId('search-results')
    await waitFor(() => expect(searchSpy).toHaveBeenCalledWith('sqs monitor'))
    // The DOM order must equal the server payload order, NOT name-ascending.
    const names = within(resultsBox).getAllByText(/-(full|partial)/).map(n => n.textContent)
    expect(names).toEqual(['zzz-full', 'aaa-partial'])
  })

  it('renders EQUAL-score results verbatim (no client tie-break re-sort)', async () => {
    // The prior test uses distinct scores, so it only catches a re-sort that
    // reorders by score. This one pins the tie-break branch (AC1.2): two hits
    // with IDENTICAL coverage+score are delivered name-DESCENDING. The server
    // owns the name-ascending tie-break; if the client re-sorted (by score OR
    // by name) it would flip these — so verbatim render is the only pass.
    searchSpy.mockResolvedValueOnce([
      { name: 'zulu-tie', description: '', capabilities: [], tags: [], role: null, source: 'local', coverage: 1, score: 1.5 },
      { name: 'alpha-tie', description: '', capabilities: [], tags: [], role: null, source: 'local', coverage: 1, score: 1.5 },
    ] as any)
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.change(screen.getByLabelText('Search agent profiles'), { target: { value: 'tie' } })
    const resultsBox = await screen.findByTestId('search-results')
    const names = within(resultsBox).getAllByText(/-tie/).map(n => n.textContent)
    expect(names).toEqual(['zulu-tie', 'alpha-tie'])
  })

  it('shows the relative score indicator, not a percentage', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.change(screen.getByLabelText('Search agent profiles'), { target: { value: 'sqs' } })
    await screen.findByTestId('search-results')
    // score rendered as a bare number (2.74), never a "%".
    expect(screen.getByText('2.74')).toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('clear button restores the grouped list', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.change(screen.getByLabelText('Search agent profiles'), { target: { value: 'sqs' } })
    await screen.findByTestId('search-results')
    fireEvent.click(screen.getByLabelText('Clear search'))
    expect(await screen.findByTestId('grouped-list')).toBeInTheDocument()
    expect(screen.queryByTestId('search-results')).not.toBeInTheDocument()
  })

  it('surfaces a search error in an inline banner', async () => {
    searchSpy.mockRejectedValueOnce(Object.assign(new Error('500'), { detail: 'boom' }))
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.change(screen.getByLabelText('Search agent profiles'), { target: { value: 'sqs' } })
    expect(await screen.findByText(/Search failed: boom/)).toBeInTheDocument()
  })
})

describe('ProfilesBrowser — ProfileDetail + ValidatePanel', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue(GROUPED as any)
    vi.spyOn(api, 'searchProfiles').mockResolvedValue([])
    vi.spyOn(api, 'getProfile').mockResolvedValue({
      name: 'developer', description: 'Dev agent', role: 'developer', provider: 'claude_code', model: 'opus',
    } as any)
  })

  afterEach(() => vi.restoreAllMocks())

  async function openDeveloperDetail() {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    const devRow = screen.getByTestId('profile-row-developer')
    fireEvent.click(within(devRow).getByText('View'))
    await screen.findByText('Validate')
  }

  it('built-in profile shows read-only + Clone-to-customize, no Edit', async () => {
    await openDeveloperDetail()
    // U4 wires the Clone action; built-ins never expose Edit (FR6).
    expect(screen.getByText('Clone to customize')).toBeInTheDocument()
    expect(screen.getByText(/Built-in · read-only/)).toBeInTheDocument()
    expect(screen.queryByText(/^Edit$/)).not.toBeInTheDocument()
  })

  it('validate errors block save and list [error] messages', async () => {
    vi.spyOn(api, 'validateProfile').mockResolvedValue({
      valid: false,
      errors: ['[error] name: does not match pattern'],
      warnings: [],
    })
    await openDeveloperDetail()
    fireEvent.click(screen.getByText('Validate'))
    expect(await screen.findByText(/save blocked/)).toBeInTheDocument()
    expect(screen.getByText('[error] name: does not match pattern')).toBeInTheDocument()
  })

  it('warnings-only validation is allowed (save not blocked)', async () => {
    vi.spyOn(api, 'validateProfile').mockResolvedValue({
      valid: true,
      errors: [],
      warnings: ['[warn] role custom is not built-in'],
    })
    await openDeveloperDetail()
    fireEvent.click(screen.getByText('Validate'))
    expect(await screen.findByText(/save allowed/)).toBeInTheDocument()
    expect(screen.getByText('[warn] role custom is not built-in')).toBeInTheDocument()
    expect(screen.queryByText(/save blocked/)).not.toBeInTheDocument()
  })

  it('valid profile shows a success state', async () => {
    vi.spyOn(api, 'validateProfile').mockResolvedValue({ valid: true, errors: [], warnings: [] })
    await openDeveloperDetail()
    fireEvent.click(screen.getByText('Validate'))
    expect(await screen.findByText(/Valid — no issues/)).toBeInTheDocument()
  })

  it('validate sends the parsed metadata without the system_prompt body', async () => {
    const validateSpy = vi.spyOn(api, 'validateProfile').mockResolvedValue({ valid: true, errors: [], warnings: [] })
    vi.spyOn(api, 'getProfile').mockResolvedValue({
      name: 'developer', description: 'Dev', system_prompt: 'SECRET BODY', provider: 'claude_code',
    } as any)
    await openDeveloperDetail()
    fireEvent.click(screen.getByText('Validate'))
    await waitFor(() => expect(validateSpy).toHaveBeenCalled())
    const payload = validateSpy.mock.calls[0][0]
    expect(payload.metadata).toBeDefined()
    expect(payload.metadata).not.toHaveProperty('system_prompt')
    expect(payload.metadata?.name).toBe('developer')
  })
})
