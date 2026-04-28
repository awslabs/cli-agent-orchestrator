import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'

describe('API wrapper', () => {
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

  it('listSessions fetches /sessions', async () => {
    const sessions = [{ id: 's1', name: 'test', status: 'active' }]
    mockResponse(sessions)
    const result = await api.listSessions()
    expect(result).toEqual(sessions)
    expect(mockFetch).toHaveBeenCalledWith('/sessions', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('listProfiles fetches /agents/profiles', async () => {
    const profiles = [{ name: 'dev', description: 'Developer', source: 'built-in' }]
    mockResponse(profiles)
    const result = await api.listProfiles()
    expect(result).toEqual(profiles)
  })

  it('listProviders fetches /agents/providers', async () => {
    const providers = [
      { name: 'kiro_cli', binary: 'kiro-cli', installed: true },
      { name: 'opencode_cli', binary: 'opencode', installed: false },
    ]
    mockResponse(providers)
    const result = await api.listProviders()
    expect(result).toEqual(providers)
  })

  it('createSession sends POST with params', async () => {
    const terminal = { id: 't1', name: 'dev', provider: 'kiro_cli', session_name: 's1' }
    mockResponse(terminal)
    await api.createSession('kiro_cli', 'developer')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/sessions?provider=kiro_cli&agent_profile=developer'),
      expect.objectContaining({ method: 'POST' })
    )
  })

  it('createSession includes working directory when provided', async () => {
    mockResponse({ id: 't1' })
    await api.createSession('kiro_cli', 'developer', undefined, '/home/user/project')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('working_directory='),
      expect.any(Object)
    )
  })

  it('deleteSession sends DELETE', async () => {
    mockResponse({ success: true, deleted: [], errors: [] })
    await api.deleteSession('s1')
    expect(mockFetch).toHaveBeenCalledWith('/sessions/s1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('sendInput sends POST with message', async () => {
    mockResponse({ success: true })
    await api.sendInput('t1', 'hello')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/terminals/t1/input?message=hello'),
      expect.objectContaining({ method: 'POST' })
    )
  })

  it('getTerminalOutput fetches with mode', async () => {
    mockResponse({ output: 'test output', mode: 'last' })
    const result = await api.getTerminalOutput('t1', 'last')
    expect(result.output).toBe('test output')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/terminals/t1/output?mode=last'),
      expect.any(Object)
    )
  })

  it('listFlows fetches /flows', async () => {
    const flows = [{ name: 'test-flow', schedule: '0 9 * * *', enabled: true }]
    mockResponse(flows)
    const result = await api.listFlows()
    expect(result).toEqual(flows)
  })

  it('createFlow sends POST with JSON body', async () => {
    const flow = { name: 'new-flow', schedule: '0 9 * * *', agent_profile: 'dev', prompt_template: 'Do stuff' }
    mockResponse(flow)
    await api.createFlow(flow)
    expect(mockFetch).toHaveBeenCalledWith(
      '/flows',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(flow),
      })
    )
  })

  it('enableFlow sends POST', async () => {
    mockResponse({ success: true })
    await api.enableFlow('my-flow')
    expect(mockFetch).toHaveBeenCalledWith('/flows/my-flow/enable', expect.objectContaining({ method: 'POST' }))
  })

  it('disableFlow sends POST', async () => {
    mockResponse({ success: true })
    await api.disableFlow('my-flow')
    expect(mockFetch).toHaveBeenCalledWith('/flows/my-flow/disable', expect.objectContaining({ method: 'POST' }))
  })

  it('runFlow sends POST with long timeout', async () => {
    mockResponse({ executed: true })
    await api.runFlow('my-flow')
    expect(mockFetch).toHaveBeenCalledWith('/flows/my-flow/run', expect.objectContaining({ method: 'POST' }))
  })

  it('deleteFlow sends DELETE', async () => {
    mockResponse({ success: true })
    await api.deleteFlow('my-flow')
    expect(mockFetch).toHaveBeenCalledWith('/flows/my-flow', expect.objectContaining({ method: 'DELETE' }))
  })

  it('throws on non-OK response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: () => Promise.resolve({}),
    })
    await expect(api.listSessions()).rejects.toThrow('500 Internal Server Error')
  })

  it('exitTerminal sends POST', async () => {
    mockResponse({ success: true })
    await api.exitTerminal('t1')
    expect(mockFetch).toHaveBeenCalledWith('/terminals/t1/exit', expect.objectContaining({ method: 'POST' }))
  })

  it('deleteTerminal sends DELETE', async () => {
    mockResponse({ success: true })
    await api.deleteTerminal('t1')
    expect(mockFetch).toHaveBeenCalledWith('/terminals/t1', expect.objectContaining({ method: 'DELETE' }))
  })
})
