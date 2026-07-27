import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'

// #510 U4: api.ts wrappers for edit (PUT) and clone (from-content). URL/verb/body
// assertions only; validation/containment/overwrite-refusal are server (U1) rules.
describe('edit + clone API wrappers', () => {
  const mockFetch = vi.fn()

  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => vi.restoreAllMocks())

  function mockResponse(data: unknown, status = 200) {
    mockFetch.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: () => Promise.resolve(data),
    })
  }

  it('updateProfile PUTs to /agents/profiles/{name} url-encoded', async () => {
    const req = { content: '---\nname: my-agent\n---\nbody', provider: 'claude_code', model: 'opus' }
    mockResponse({ name: 'my-agent', source: 'local', path: '/store/my-agent.md' })
    const result = await api.updateProfile('my-agent', req)
    expect(result.source).toBe('local')
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/my-agent',
      expect.objectContaining({
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      }),
    )
  })

  it('createProfileFromContent POSTs to /agents/profiles/from-content', async () => {
    const req = { name: 'developer-copy', content: '---\nname: developer-copy\n---\nbody', provider: 'claude_code', model: 'opus' }
    mockResponse({ name: 'developer-copy', source: 'local', path: '/store/developer-copy.md' })
    const result = await api.createProfileFromContent(req)
    expect(result.name).toBe('developer-copy')
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/from-content',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(req) }),
    )
  })

  it('createProfileFromContent surfaces the overwrite-refusal 400 as detail', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: "A local profile named 'x' already exists." }),
    })
    await expect(
      api.createProfileFromContent({ name: 'x', content: 'c', provider: 'p', model: 'm' }),
    ).rejects.toMatchObject({ status: 400, detail: "A local profile named 'x' already exists." })
  })
})
