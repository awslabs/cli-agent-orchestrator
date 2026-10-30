// Fleet visibility in the web UI: the /fleet client methods, the Home fleet
// status card and the fleet workers table (the `cao fleet status` and
// `cao worker list/status` equivalents).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { api } from '../api'
import { FleetStatusCard } from '../components/FleetStatusCard'
import { FleetWorkers, stripAnsi } from '../components/FleetWorkers'

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}

describe('fleet client methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.unstubAllGlobals(); mockFetch.mockReset() })

  it('getFleetStatus reads /fleet/status', async () => {
    mockFetch.mockResolvedValueOnce(ok({ broker: 'http://127.0.0.1:9890', live: 2, cleanup_pending: 0, states: { leased: 2 } }))
    expect((await api.getFleetStatus()).live).toBe(2)
    expect(mockFetch).toHaveBeenCalledWith('/fleet/status', expect.any(Object))
  })

  it('surfaces 503 when the fleet broker is not reachable', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 503, statusText: 'Service Unavailable', json: () => Promise.resolve({ detail: 'No fleet configured.' }) })
    await expect(api.getFleetStatus()).rejects.toMatchObject({ status: 503 })
  })

  it('listFleetWorkers adds ?all=true only when asked; getFleetWorker encodes the id', async () => {
    mockFetch.mockResolvedValueOnce(ok({ broker: 'b', workers: [] }))
    await api.listFleetWorkers()
    expect(mockFetch.mock.calls[0][0]).toBe('/fleet/workers')
    mockFetch.mockResolvedValueOnce(ok({ broker: 'b', workers: [] }))
    await api.listFleetWorkers(true)
    expect(mockFetch.mock.calls[1][0]).toBe('/fleet/workers?all=true')
    mockFetch.mockResolvedValueOnce(ok({ worker_id: 'a/b', lease: null, terminal: null, terminal_error: null, last_output: null }))
    await api.getFleetWorker('a/b')
    expect(mockFetch.mock.calls[2][0]).toBe('/fleet/workers/a%2Fb')
  })
})

describe('FleetStatusCard', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows the summary', async () => {
    vi.spyOn(api, 'getFleetStatus').mockResolvedValue({ broker: 'http://127.0.0.1:9890', live: 3, cleanup_pending: 1, states: { leased: 3 } })
    render(<FleetStatusCard />)
    expect(await screen.findByText('3')).toBeTruthy()
    expect(screen.getByText('leased: 3')).toBeTruthy()
  })

  it('says the fleet is not reachable on 503', async () => {
    vi.spyOn(api, 'getFleetStatus').mockRejectedValue(Object.assign(new Error('503'), { status: 503, detail: 'No fleet configured' }))
    render(<FleetStatusCard />)
    expect(await screen.findByText('Fleet not reachable')).toBeTruthy()
    expect(screen.getByText('No fleet configured')).toBeTruthy()
  })

  it('hides itself when the first read is a 404 (older server)', async () => {
    const spy = vi.spyOn(api, 'getFleetStatus').mockRejectedValue(Object.assign(new Error('404'), { status: 404 }))
    const { container } = render(<FleetStatusCard />)
    await waitFor(() => expect(spy).toHaveBeenCalled())
    await waitFor(() => expect(container.querySelector('[aria-label="Fleet status"]')).toBeNull())
  })
})

describe('FleetWorkers', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('fetches nothing until opened', () => {
    const list = vi.spyOn(api, 'listFleetWorkers')
    render(<FleetWorkers />)
    expect(list).not.toHaveBeenCalled()
  })

  it('lists workers and shows one worker on Details, without terminal escapes', async () => {
    vi.spyOn(api, 'listFleetWorkers').mockResolvedValue({
      broker: 'http://127.0.0.1:9890',
      workers: [{
        worker_id: 'a1b2c3d4', state: 'leased', reason: null, agent_profile: 'developer',
        provider: 'claude_code', age_seconds: 42, workload_present: true,
        cleanup_pending: false, lease_tracked: true,
      }],
    })
    const detail = vi.spyOn(api, 'getFleetWorker').mockResolvedValue({
      worker_id: 'a1b2c3d4',
      lease: null,
      terminal: { id: 't1', name: 'dev', session_name: 's', agent_profile: 'developer', provider: 'claude_code', status: 'completed' },
      terminal_error: null,
      last_output: '\x1b[32mall tests pass\x1b[0m',
    })
    render(<FleetWorkers />)
    fireEvent.click(screen.getByRole('button', { name: /Fleet workers/ }))
    expect(await screen.findByText('a1b2c3d4')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    await waitFor(() => expect(detail).toHaveBeenCalledWith('a1b2c3d4'))
    expect(await screen.findByText('all tests pass')).toBeInTheDocument()
    expect(screen.getByText(/not in the broker's ledger/)).toBeInTheDocument()
  })

  it('reads a 503 as "no fleet reachable" and re-lists with settled leases on toggle', async () => {
    const list = vi.spyOn(api, 'listFleetWorkers')
      .mockRejectedValueOnce(Object.assign(new Error('503'), { status: 503, detail: 'No fleet configured.' }))
      .mockResolvedValue({ broker: 'b', workers: [] })
    render(<FleetWorkers />)
    fireEvent.click(screen.getByRole('button', { name: /Fleet workers/ }))
    expect(await screen.findByText(/No fleet reachable: No fleet configured\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox'))
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(true))
    expect(await screen.findByText('No workers.')).toBeInTheDocument()
  })
})

describe('stripAnsi', () => {
  it('removes colour, cursor and title sequences', () => {
    const raw = '\x1b]0;user@host\x07\x1b[1m\x1b[32muser@w\x1b[0m:\x1b[34m~/ws\x1b[0m$ \x1b[?2004hdone'
    expect(stripAnsi(raw)).toBe('user@w:~/ws$ done')
  })
  it('leaves plain text alone', () => {
    expect(stripAnsi('PASS 10/10\n')).toBe('PASS 10/10\n')
  })
})
