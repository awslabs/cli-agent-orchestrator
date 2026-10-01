import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { SettingsPanel } from '../components/SettingsPanel'
import { getToken, setToken } from '../auth'

const okJson = (data: unknown) => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  json: () => Promise.resolve(data),
  text: () => Promise.resolve(JSON.stringify(data)),
})
const mockFetch = vi.fn(async (url: string) => {
  if (url.includes('/settings/agent-dirs')) {
    return okJson({ agent_dirs: {}, extra_dirs: [], disabled_dirs: [] })
  }
  if (url.includes('/agents/profiles')) return okJson([])
  return okJson({})
})

describe('Settings › Server Access Token (#807)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.stubGlobal('fetch', mockFetch)
  })
  afterEach(() => {
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('saves a pasted token for the tab and clears it again', async () => {
    render(<SettingsPanel />)
    await waitFor(() => screen.getByTestId('access-token-card'))
    expect(screen.getByTestId('access-token-state').textContent).toMatch(/No token set/)

    fireEvent.change(screen.getByTestId('access-token-input'), { target: { value: '  tok-1  ' } })
    fireEvent.click(screen.getByTestId('access-token-save'))
    expect(getToken()).toBe('tok-1')
    expect(screen.getByTestId('access-token-state').textContent).toMatch(/A token is set/)
    expect((screen.getByTestId('access-token-input') as HTMLInputElement).value).toBe('')

    fireEvent.click(screen.getByTestId('access-token-clear'))
    expect(getToken()).toBeNull()
    expect(screen.getByTestId('access-token-state').textContent).toMatch(/No token set/)
  })

  it('reflects a token that was already captured from the URL fragment', async () => {
    setToken('from-fragment')
    render(<SettingsPanel />)
    await waitFor(() => screen.getByTestId('access-token-card'))
    expect(screen.getByTestId('access-token-state').textContent).toMatch(/A token is set/)
    expect((screen.getByTestId('access-token-input') as HTMLInputElement).type).toBe('password')
  })
})

describe('Settings › token card is reachable when the server rejects the settings read (#838 review)', () => {
  const unauthorized = { ok: false, status: 401, statusText: 'Unauthorized', json: () => Promise.resolve({ detail: 'no' }), text: () => Promise.resolve('{"detail":"no"}') }
  let calls: string[]
  const mockFetch401 = vi.fn(async (url: string, _init?: RequestInit) => {
    calls.push(url)
    return unauthorized
  })

  beforeEach(() => {
    sessionStorage.clear()
    calls = []
    vi.stubGlobal('fetch', mockFetch401)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('renders the token card even though settings never load, and retries after a token is saved', async () => {
    render(<SettingsPanel />)
    await waitFor(() => screen.getByTestId('access-token-card'))
    expect(screen.getByTestId('settings-loading').textContent).toMatch(/set the access token above/)
    const before = calls.filter(u => u.includes('/settings/agent-dirs')).length
    expect(before).toBeGreaterThan(0)

    fireEvent.change(screen.getByTestId('access-token-input'), { target: { value: 'tok-1' } })
    fireEvent.click(screen.getByTestId('access-token-save'))
    await waitFor(() => {
      expect(calls.filter(u => u.includes('/settings/agent-dirs')).length).toBeGreaterThan(before)
    })
    const retried = mockFetch401.mock.calls.filter(([u]) => String(u).includes('/settings/agent-dirs')).pop()
    expect(new Headers(retried?.[1]?.headers as HeadersInit | undefined).get('Authorization')).toBe('Bearer tok-1')
  })

  it('a failed token save is reported and does not claim a token is set', async () => {
    render(<SettingsPanel />)
    await waitFor(() => screen.getByTestId('access-token-card'))
    // jsdom's sessionStorage is a proxy; replace it with a fake whose write throws.
    vi.stubGlobal('sessionStorage', {
      getItem: () => null,
      setItem: () => {
        throw new DOMException('denied', 'SecurityError')
      },
      removeItem: () => {},
      clear: () => {},
      key: () => null,
      length: 0,
    })
    fireEvent.change(screen.getByTestId('access-token-input'), { target: { value: 'tok-1' } })
    fireEvent.click(screen.getByTestId('access-token-save'))
    expect(getToken()).toBeNull()
    expect(screen.getByTestId('access-token-state').textContent).toMatch(/No token set/)
    expect((screen.getByTestId('access-token-input') as HTMLInputElement).value).toBe('tok-1')
  })
})
