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
