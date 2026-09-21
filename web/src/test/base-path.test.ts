import { describe, it, expect, vi, afterEach } from 'vitest'
import { terminalSocketUrl, eventStreamUrl } from '../api'

// Two properties, and the first one is the one that matters for existing
// deployments: at the default base every URL these builders produce is
// character-for-character what the hardcoded root-absolute paths produced
// before they existed. The REST side is already covered by api.test.ts, which
// asserts the exact paths fetch() is called with.

describe('URL building at the default base', () => {
  it('terminalSocketUrl matches the pre-prefix construction', () => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    expect(terminalSocketUrl('term-1')).toBe(
      `${protocol}//${location.host}/terminals/term-1/ws`
    )
  })

  it('eventStreamUrl matches the pre-prefix construction', () => {
    expect(eventStreamUrl('run-1')).toBe('/workflows/runs/run-1/events')
    expect(eventStreamUrl('run-1', 42)).toBe('/workflows/runs/run-1/events?after_seq=42')
  })

  it('eventStreamUrl still encodes the run id', () => {
    expect(eventStreamUrl('a/b')).toBe('/workflows/runs/a%2Fb/events')
  })

  it('a zero cursor is sent, not dropped', () => {
    // `after_seq=0` is meaningful (replay from the start), so the guard is
    // `!= null` rather than a truthiness test.
    expect(eventStreamUrl('run-1', 0)).toBe('/workflows/runs/run-1/events?after_seq=0')
  })
})

describe('URL building under a path prefix', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('prefixes both runtime transports', async () => {
    // BASE is read once at module load, so the stub has to be in place before
    // the re-import.
    vi.stubEnv('BASE_URL', '/cao/')
    vi.resetModules()
    const api = await import('../api')

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    expect(api.terminalSocketUrl('term-1')).toBe(
      `${protocol}//${location.host}/cao/terminals/term-1/ws`
    )
    expect(api.eventStreamUrl('run-1', 42)).toBe(
      '/cao/workflows/runs/run-1/events?after_seq=42'
    )
  })

  it('prefixes REST calls', async () => {
    vi.stubEnv('BASE_URL', '/cao/')
    vi.resetModules()
    const api = await import('../api')

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve([]),
      text: () => Promise.resolve('[]'),
    })
    vi.stubGlobal('fetch', mockFetch)
    await api.api.listSessions()
    expect(mockFetch).toHaveBeenCalledWith(
      '/cao/sessions',
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    )
    vi.unstubAllGlobals()
  })
})
