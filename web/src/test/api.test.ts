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

  it('createSession sends POST with opencode_cli provider', async () => {
    const terminal = { id: 't2', name: 'dev', provider: 'opencode_cli', session_name: 's2' }
    mockResponse(terminal)
    await api.createSession('opencode_cli', 'developer')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/sessions?provider=opencode_cli&agent_profile=developer'),
      expect.objectContaining({ method: 'POST' })
    )
  })

  it('addTerminalToSession sends POST with opencode_cli provider', async () => {
    const terminal = { id: 't3', name: 'dev', provider: 'opencode_cli', session_name: 's1' }
    mockResponse(terminal)
    await api.addTerminalToSession('s1', 'opencode_cli', 'developer')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/sessions/s1/terminals?provider=opencode_cli&agent_profile=developer'),
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

  it('createSession includes session name (url-encoded) when provided', async () => {
    mockResponse({ id: 't1' })
    await api.createSession('kiro_cli', 'developer', 'my session/1')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('session_name=my%20session%2F1'),
      expect.any(Object)
    )
  })

  it('createSession url-encodes provider and agent_profile', async () => {
    mockResponse({ id: 't1' })
    await api.createSession('kiro_cli', 'my agent/v2')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('agent_profile=my%20agent%2Fv2'),
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

  it('getMemoryStatus fetches /settings/memory', async () => {
    mockResponse({ enabled: true })
    const result = await api.getMemoryStatus()
    expect(result).toEqual({ enabled: true })
    expect(mockFetch).toHaveBeenCalledWith('/settings/memory', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('listMemories fetches /memory without filters', async () => {
    mockResponse([])
    await api.listMemories()
    expect(mockFetch).toHaveBeenCalledWith('/memory', expect.any(Object))
  })

  it('listMemories includes filters as query params', async () => {
    mockResponse([])
    await api.listMemories({ scope: 'project', type: 'reference', scopeId: 'my-proj', limit: 25 })
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory?scope=project&type=reference&scope_id=my-proj&limit=25',
      expect.any(Object)
    )
  })

  it('getMemory fetches /memory/{key} with scope and scope_id', async () => {
    mockResponse({ key: 'my-key', scope: 'session', scope_id: 's1', memory_type: 'user', tags: '', created_at: '', updated_at: '', content: 'hello' })
    const result = await api.getMemory('my-key', 'session', 's1')
    expect(result.content).toBe('hello')
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory/my-key?scope=session&scope_id=s1',
      expect.any(Object)
    )
  })

  it('getMemory encodes user strings in URL', async () => {
    mockResponse({})
    await api.getMemory('a b', 'project', 'p/1')
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory/a%20b?scope=project&scope_id=p%2F1',
      expect.any(Object)
    )
  })

  it('deleteMemory sends DELETE with scope and scope_id', async () => {
    mockResponse({ success: true })
    await api.deleteMemory('my-key', 'project', 'my-proj')
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory/my-key?scope=project&scope_id=my-proj',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('deleteMemory omits scope_id for global scope', async () => {
    mockResponse({ success: true })
    await api.deleteMemory('my-key', 'global')
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory/my-key?scope=global',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('clearMemories sends DELETE with scope and scope_id', async () => {
    mockResponse({ success: true, deleted_count: 3 })
    const result = await api.clearMemories('session', 's 1')
    expect(result.deleted_count).toBe(3)
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory?scope=session&scope_id=s%201',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('clearMemories omits scope_id for global scope', async () => {
    mockResponse({ success: true, deleted_count: 0 })
    await api.clearMemories('global')
    expect(mockFetch).toHaveBeenCalledWith(
      '/memory?scope=global',
      expect.objectContaining({ method: 'DELETE' })
    )
  })

  it('getGraph builds /graph/{provider} with scope + scope_id', async () => {
    mockResponse({ nodes: [], edges: [], meta: {} })
    await api.getGraph('memory', 'project', 'my-proj')
    expect(mockFetch).toHaveBeenCalledWith(
      '/graph/memory?scope=project&scope_id=my-proj',
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    )
  })

  it('getGraph omits scope_id when not given', async () => {
    mockResponse({ nodes: [], edges: [], meta: {} })
    await api.getGraph('memory', 'global')
    expect(mockFetch).toHaveBeenCalledWith('/graph/memory?scope=global', expect.any(Object))
  })

  it('exportGraph POSTs the sink/dest body with scope query params', async () => {
    mockResponse({ written_files: ['/v/a.md'], sink: 'obsidian', dest: 'global-vault' })
    const res = await api.exportGraph('memory', { sink: 'obsidian', dest: 'global-vault' }, 'global')
    expect(res.written_files).toEqual(['/v/a.md'])
    expect(mockFetch).toHaveBeenCalledWith(
      '/graph/memory/export?scope=global',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ options: {}, sink: 'obsidian', dest: 'global-vault' }),
      })
    )
  })

  it('getGraph surfaces server detail + status on error', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: "scope 'session' is private" }),
    })
    await expect(api.getGraph('memory', 'session')).rejects.toMatchObject({
      status: 400,
      detail: "scope 'session' is private",
    })
  })

  it('exportGraph surfaces the 422 secret-gate detail', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      json: () => Promise.resolve({ detail: "export rejected: secret pattern 'aws-key' detected" }),
    })
    await expect(
      api.exportGraph('memory', { sink: 'obsidian', dest: 'x' }, 'global')
    ).rejects.toMatchObject({ status: 422, detail: "export rejected: secret pattern 'aws-key' detected" })
  })
})
