import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { captureTokenFromFragment, getToken, setToken, withAuth, tokenQuery } from '../auth'
import { api, terminalSocketUrl, eventStreamUrl } from '../api'

describe('auth token for an auth-enabled server (#807)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    history.replaceState(null, '', '/')
  })
  afterEach(() => {
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('has no token by default', () => {
    expect(getToken()).toBeNull()
    expect(withAuth({ Accept: 'x' })).toEqual({ Accept: 'x' })
    expect(withAuth(undefined)).toBeUndefined()
    expect(tokenQuery('token', false)).toBe('')
  })

  it('captures #token= into sessionStorage and strips it from the URL', () => {
    history.replaceState(null, '', '/?keep=1#token=abc.def&other=2')
    expect(captureTokenFromFragment()).toBe(true)
    expect(getToken()).toBe('abc.def')
    expect(location.search).toBe('?keep=1')
    expect(location.hash).toBe('#other=2')
    expect(location.href).not.toContain('abc.def')
  })

  it('leaves a URL without a token alone', () => {
    history.replaceState(null, '', '/#view=agents')
    expect(captureTokenFromFragment()).toBe(false)
    expect(getToken()).toBeNull()
    expect(location.hash).toBe('#view=agents')
  })

  it('setToken stores, trims and clears', () => {
    setToken('  t1  ')
    expect(getToken()).toBe('t1')
    setToken('')
    expect(getToken()).toBeNull()
    setToken('t2')
    setToken(null)
    expect(getToken()).toBeNull()
  })

  it('withAuth adds the bearer to any header shape and keeps the rest', () => {
    setToken('t1')
    expect(withAuth({ Accept: 'text/event-stream' })).toEqual({
      accept: 'text/event-stream',
      authorization: 'Bearer t1',
    })
    expect(withAuth(undefined)).toEqual({ authorization: 'Bearer t1' })
    expect(withAuth([['X-A', '1']])).toEqual({ 'x-a': '1', authorization: 'Bearer t1' })
  })

  it('fetchJSON sends the bearer once a token is set, and nothing extra before', async () => {
    const mockFetch = vi.fn()
    vi.stubGlobal('fetch', mockFetch)
    const ok = () => ({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve([]), text: () => Promise.resolve('[]') })

    mockFetch.mockResolvedValueOnce(ok())
    await api.listSessions()
    expect(mockFetch.mock.calls[0][1].headers).toBeUndefined()

    setToken('t1')
    mockFetch.mockResolvedValueOnce(ok())
    await api.listSessions()
    const headers = new Headers(mockFetch.mock.calls[1][1].headers)
    expect(headers.get('Authorization')).toBe('Bearer t1')
  })

  it('the terminal WebSocket URL carries ?token= only when a token is set', () => {
    expect(terminalSocketUrl('abc')).toMatch(/\/terminals\/abc\/ws$/)
    setToken('t 1')
    expect(terminalSocketUrl('abc')).toMatch(/\/terminals\/abc\/ws\?token=t%201$/)
  })

  it('the event stream URL carries access_token= after any after_seq', () => {
    expect(eventStreamUrl('r1')).toBe('/workflows/runs/r1/events')
    expect(eventStreamUrl('r1', 5)).toBe('/workflows/runs/r1/events?after_seq=5')
    setToken('t1')
    expect(eventStreamUrl('r1')).toBe('/workflows/runs/r1/events?access_token=t1')
    expect(eventStreamUrl('r1', 5)).toBe('/workflows/runs/r1/events?after_seq=5&access_token=t1')
  })
})
