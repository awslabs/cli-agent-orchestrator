import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'

// #510 U2: api.ts wrappers for the profile-management endpoints. These assert
// the wrapper hits the right URL/verb/body; ranking + validation correctness is
// a server (U1) guarantee proven by the Python suite.
describe('profile management API wrappers', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  function mockResponse(data: unknown, status = 200) {
    mockFetch.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: () => Promise.resolve(data),
    })
  }

  it('searchProfiles GETs /agents/profiles/search with an encoded query', async () => {
    mockResponse([])
    await api.searchProfiles('monitor sqs')
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/search?q=monitor%20sqs',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('searchProfiles appends limit when provided', async () => {
    mockResponse([])
    await api.searchProfiles('sqs', 3)
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/search?q=sqs&limit=3',
      expect.any(Object),
    )
  })

  it('searchProfiles returns the server payload UNCHANGED (no client re-sort)', async () => {
    // Server order is coverage → BM25 → name; a name-descending payload must be
    // returned exactly as received. If the wrapper (or a future edit) sorted, the
    // order below would change — this pins the "never re-sort" contract at the
    // api boundary.
    const serverOrder = [
      { name: 'zzz-full', description: '', capabilities: [], tags: [], role: null, source: 'local', coverage: 2, score: 2.7 },
      { name: 'aaa-partial', description: '', capabilities: [], tags: [], role: null, source: 'local', coverage: 1, score: 1.5 },
    ]
    mockResponse(serverOrder)
    const result = await api.searchProfiles('sqs monitor')
    expect(result).toEqual(serverOrder)
    expect(result.map(r => r.name)).toEqual(['zzz-full', 'aaa-partial'])
  })

  it('getProfile GETs /agents/profiles/{name} url-encoded', async () => {
    mockResponse({ name: 'dev', description: 'x' })
    const result = await api.getProfile('dev')
    expect(result).toEqual({ name: 'dev', description: 'x' })
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/dev',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('validateProfile POSTs the payload as JSON', async () => {
    const payload = { metadata: { name: 'dev' } }
    mockResponse({ valid: true, errors: [], warnings: [] })
    const result = await api.validateProfile(payload)
    expect(result).toEqual({ valid: true, errors: [], warnings: [] })
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/validate',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }),
    )
  })

  it('validateProfile surfaces server errors as ApiError detail', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: 'Could not parse profile content' }),
    })
    await expect(api.validateProfile({ content: 'bad' })).rejects.toMatchObject({
      status: 400,
      detail: 'Could not parse profile content',
    })
  })
})
