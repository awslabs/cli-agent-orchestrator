import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'

describe('Workflow API methods (#504 / U8)', () => {
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
      text: () => Promise.resolve(JSON.stringify(data)),
    })
  }

  it('listWorkflowRuns fetches /workflows/runs (consumes #505 RunSummaryRow)', async () => {
    const rows = [
      {
        run_id: 'r1',
        workflow_name: 'wf',
        state: 'completed',
        tier: 'yaml',
        started_at: '2026-07-27T00:00:00Z',
        finished_at: '2026-07-27T00:01:00Z',
        current_step_id: null,
      },
    ]
    mockResponse(rows)
    const result = await api.listWorkflowRuns()
    expect(result).toEqual(rows)
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs', expect.any(Object))
  })

  it('inspectWorkflowRun fetches /workflows/runs/{id}', async () => {
    mockResponse({ run_id: 'r1', workflow_name: 'wf', state: 'running', started_at: '', tier: 'yaml', steps: [] })
    await api.inspectWorkflowRun('r1')
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1', expect.any(Object))
  })

  it('getWorkflowRunEvents omits after_seq when not given', async () => {
    mockResponse({ events: [], gaps: [], next_after_seq: null })
    await api.getWorkflowRunEvents('r1')
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1/events', expect.any(Object))
  })

  it('getWorkflowRunEvents includes after_seq as the replay cursor', async () => {
    mockResponse({ events: [], gaps: [], next_after_seq: null })
    await api.getWorkflowRunEvents('r1', 42)
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1/events?after_seq=42', expect.any(Object))
  })

  it('compareWorkflowRuns builds the ?against= query (url-encoded)', async () => {
    mockResponse({ baseline_run_id: 'r1', compare_run_id: 'r2', steps: [], output_diffs: [] })
    await api.compareWorkflowRuns('r1', 'r 2')
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1/compare?against=r%202', expect.any(Object))
  })

  it('getWorkflowRunDiagnostics fetches the diagnostics bundle', async () => {
    mockResponse({ spec_id: 's', spec_content_hash: 'h', inputs: '', events: [], gaps: [], step_outcomes: [], environment: { providers: [], agent_profiles: [], engines: [] }, references: { terminals: [], artifacts: [] }, excerpts: [], capture_enabled: false })
    await api.getWorkflowRunDiagnostics('r1')
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1/diagnostics', expect.objectContaining({ timeoutMs: 30000 }))
  })

  it('deleteWorkflowRun sends DELETE and returns undefined on 204', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 204,
      statusText: 'No Content',
      // A real 204 body is empty; both readers reflect that.
      json: () => Promise.reject(new Error('no body')),
      text: () => Promise.resolve(''),
    })
    const result = await api.deleteWorkflowRun('r1')
    expect(result).toBeUndefined()
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/r1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('getTerminalOutputRange builds the U5 offset/length query', async () => {
    mockResponse({ terminal_id: 't1', offset: 10, length: 20, data: 'hello' })
    const res = await api.getTerminalOutputRange('t1', 10, 20)
    expect(res.data).toBe('hello')
    expect(mockFetch).toHaveBeenCalledWith('/terminals/t1/output/range?offset=10&length=20', expect.any(Object))
  })

  it('surfaces ApiError status + detail on a compare 404', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      json: () => Promise.resolve({ detail: "unknown run 'r2' (compare target)" }),
    })
    await expect(api.compareWorkflowRuns('r1', 'r2')).rejects.toMatchObject({
      status: 404,
      detail: "unknown run 'r2' (compare target)",
    })
  })
})

// ── PR #526 review remediation: empty-body handling ────────────────────────
// The comment on fetchJSON advertised "204 No Content (or any empty body)"
// but the code only special-cased 204 — any other success with an empty body
// (notably a proxy stripping the body on this SSE-adjacent surface) still hit
// res.json() and threw. These guard the implemented behaviour: reverting to
// `if (res.status === 204) return undefined; return res.json()` makes the
// 200-empty and whitespace cases go red (res.json() rejects on an empty body).
describe('fetchJSON empty-body handling (#526)', () => {
  const mockFetch = vi.fn()
  beforeEach(() => {
    mockFetch.mockReset()
    vi.stubGlobal('fetch', mockFetch)
  })
  afterEach(() => vi.unstubAllGlobals())

  /** A response whose body is `body`; json() rejects as a real empty body would. */
  function bodyResponse(body: string, status = 200) {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status,
      statusText: 'OK',
      json: () => Promise.reject(new SyntaxError('Unexpected end of JSON input')),
      text: () => Promise.resolve(body),
    })
  }

  it('returns undefined for a 200 with a body stripped to empty by a proxy', async () => {
    bodyResponse('')
    await expect(api.listWorkflowRuns()).resolves.toBeUndefined()
  })

  it('returns undefined for a whitespace-only body', async () => {
    bodyResponse('\n  \n')
    await expect(api.listWorkflowRuns()).resolves.toBeUndefined()
  })

  it('still returns undefined on a 204 with an empty body', async () => {
    bodyResponse('', 204)
    await expect(api.deleteWorkflowRun('r1')).resolves.toBeUndefined()
  })

  it('still parses a normal non-empty JSON body', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve([{ run_id: 'r1' }]),
      text: () => Promise.resolve('[{"run_id":"r1"}]'),
    })
    await expect(api.listWorkflowRuns()).resolves.toEqual([{ run_id: 'r1' }])
  })

  it('still throws on a malformed (non-empty, non-JSON) success body', async () => {
    bodyResponse('<html>gateway</html>')
    await expect(api.listWorkflowRuns()).rejects.toThrow(SyntaxError)
  })
})
