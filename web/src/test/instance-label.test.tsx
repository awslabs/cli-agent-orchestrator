import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import App from '../App'

// CAO_INSTANCE_LABEL reaches the header and the browser tab through GET /health.
// A local server and a cluster supervisor serve the same build, so the label is
// the only thing that tells two open UIs apart.

const okJson = (data: unknown) => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  json: () => Promise.resolve(data),
  text: () => Promise.resolve(JSON.stringify(data)),
})

function fetchWithHealth(health: unknown) {
  return vi.fn(async (url: string) => {
    if (url.includes('/health')) {
      if (health instanceof Error) throw health
      return okJson(health)
    }
    if (url.includes('/memory/status')) return okJson({ enabled: false })
    return okJson([])
  })
}

const HEALTH = { status: 'ok', terminal_backend: 'tmux', version: '2.5.5', components: { cao: 'ok' } }

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.title = 'CLI Agent Orchestrator'
})

describe('instance label', () => {
  it('shows the label beside the title and in the browser tab', async () => {
    vi.stubGlobal('fetch', fetchWithHealth({ ...HEALTH, instance_label: 'EKS cluster' }))
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('instance-label')).toHaveTextContent('EKS cluster'))
    expect(screen.getByRole('banner')).toContainElement(screen.getByTestId('instance-label'))
    expect(document.title).toBe('EKS cluster · CLI Agent Orchestrator')
  })

  it.each([
    ['null', { ...HEALTH, instance_label: null }],
    ['absent (older server)', HEALTH],
    ['blank', { ...HEALTH, instance_label: '   ' }],
    ['not a string', { ...HEALTH, instance_label: 42 }],
  ])('shows no label when it is %s', async (_name, health) => {
    const fetchMock = fetchWithHealth(health)
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/health'))).toBe(true))
    await act(async () => {})
    expect(screen.queryByTestId('instance-label')).toBeNull()
    expect(document.title).toBe('CLI Agent Orchestrator')
  })

  it('shows no label when /health cannot be read', async () => {
    vi.stubGlobal('fetch', fetchWithHealth(new Error('network down')))
    render(<App />)
    await act(async () => {})
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('CLI Agent Orchestrator')
    expect(screen.queryByTestId('instance-label')).toBeNull()
    expect(document.title).toBe('CLI Agent Orchestrator')
  })
})
