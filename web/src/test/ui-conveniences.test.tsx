// Common actions in the web UI: prompt presets (and the optional deployment
// ui-presets.json), installing a profile by name or URL, the server health
// card, memory OKF export, and the confirm dialog's accessibility.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { api } from '../api'
import { loadPromptPresets, savePromptPresets, PROMPT_PRESETS_KEY } from '../components/PromptPresets'
import { HealthCard } from '../components/HealthCard'
import { ConfirmModal } from '../components/ConfirmModal'

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}
function fail(status: number, detail: unknown) {
  return { ok: false, status, statusText: 'Error', json: () => Promise.resolve({ detail }) }
}

describe('client methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.restoreAllMocks(); mockFetch.mockReset() })

  it('installProfile sends {source} alone, and {source,provider} when given', async () => {
    mockFetch.mockResolvedValueOnce(ok({ success: true, message: 'ok' }))
    await api.installProfile('reviewer')
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ source: 'reviewer' })
    mockFetch.mockResolvedValueOnce(ok({ success: true, message: 'ok' }))
    await api.installProfile('https://example.com/p.md', 'claude_code')
    expect(JSON.parse(mockFetch.mock.calls[1][1].body)).toEqual({ source: 'https://example.com/p.md', provider: 'claude_code' })
    expect(mockFetch.mock.calls[1][0]).toBe('/agents/profiles/install')
  })

  it('getUiPresets keeps well-formed entries and drops the rest', async () => {
    mockFetch.mockResolvedValueOnce(ok({
      prompts: [{ label: 'Summarize', text: 'Summarize the open changes' }, { label: 1 }],
      installs: [{ label: 'reviewer', source: 'https://example.com/reviewer.md', provider: 'claude_code' }, { source: 'no label' }],
    }))
    const p = await api.getUiPresets()
    expect(mockFetch.mock.calls[0][0]).toBe('/ui-presets.json')
    expect(p.prompts).toEqual([{ label: 'Summarize', text: 'Summarize the open changes' }])
    expect(p.installs).toEqual([{ label: 'reviewer', source: 'https://example.com/reviewer.md', provider: 'claude_code' }])
  })

  it('getUiPresets resolves to {} when the file is absent', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 404, statusText: 'Not Found', json: () => Promise.reject(new Error('x')) })
    expect(await api.getUiPresets()).toEqual({})
  })

  it('getHealth reads /health', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', version: '2.5.4' }))
    expect((await api.getHealth()).version).toBe('2.5.4')
    expect(mockFetch.mock.calls[0][0]).toBe('/health')
  })

  it('exportMemory sends scope, scope_id and redact and returns a blob', async () => {
    const blob = new Blob(['tar'])
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, blob: () => Promise.resolve(blob) })
    const r = await api.exportMemory({ scope: 'project', scopeId: 'p1', redact: true })
    expect(r.blob).toBe(blob)
    expect(r.filename).toBe('cao-memory-project.tar.gz')
    expect(mockFetch.mock.calls[0][0]).toBe('/memory/export?scope=project&scope_id=p1&redact=true')
  })

  it('exportMemory surfaces the server detail on a refusal', async () => {
    mockFetch.mockResolvedValueOnce(fail(400, "scope 'project' requires scope_id"))
    await expect(api.exportMemory({ scope: 'project' })).rejects.toMatchObject({ status: 400, detail: "scope 'project' requires scope_id" })
  })
})

describe('prompt-preset persistence helpers', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips presets through localStorage', () => {
    savePromptPresets([{ label: 'Summarize', text: 'Summarize the open changes' }])
    expect(loadPromptPresets()).toEqual([{ label: 'Summarize', text: 'Summarize the open changes' }])
    expect(localStorage.getItem(PROMPT_PRESETS_KEY)).toContain('Summarize')
  })

  it('returns [] when nothing is stored', () => {
    expect(loadPromptPresets()).toEqual([])
  })

  it('ignores malformed JSON and non-preset entries', () => {
    localStorage.setItem(PROMPT_PRESETS_KEY, 'not json')
    expect(loadPromptPresets()).toEqual([])
    localStorage.setItem(PROMPT_PRESETS_KEY, JSON.stringify([{ label: 'ok', text: 't' }, { label: 42 }, { text: 'no label' }, null]))
    expect(loadPromptPresets()).toEqual([{ label: 'ok', text: 't' }])
  })
})

describe('HealthCard', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('shows backend, components and version', async () => {
    vi.spyOn(api, 'getHealth').mockResolvedValue({ status: 'ok', terminal_backend: 'herdr', version: '2.5.4', components: { cao: 'ok', herdr: 'ok', claude: 'unavailable' } })
    render(<HealthCard />)
    expect(await screen.findByText('herdr')).toBeTruthy()
    expect(screen.getByText('v2.5.4')).toBeTruthy()
    expect(screen.getByText('claude: unavailable')).toBeTruthy()
  })
})

describe('ConfirmModal accessibility', () => {
  afterEach(() => cleanup())
  it('is an alertdialog labelled by its title and message', () => {
    render(<ConfirmModal open title="Shut down all sessions" message="This stops 5 sessions." onConfirm={() => {}} onCancel={() => {}} />)
    const dlg = screen.getByRole('alertdialog', { name: 'Shut down all sessions' })
    expect(dlg.getAttribute('aria-modal')).toBe('true')
    expect(dlg.getAttribute('aria-describedby')).toBe('confirm-modal-message')
  })
})
