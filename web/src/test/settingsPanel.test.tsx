import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { SettingsPanel } from '../components/SettingsPanel'

const AGENT_STORE = '/home/u/.aws/cli-agent-orchestrator/agent-store'
const DEFAULTS = {
  kiro_cli: '/home/u/.kiro/agents',
  claude_code: AGENT_STORE,
  codex: AGENT_STORE, // shares a path with claude_code -> must render once
  cao_installed: '/home/u/.aws/cli-agent-orchestrator/agent-context',
}

describe('SettingsPanel — directory enable/disable (GH #280/#281)', () => {
  let state: any
  let posts: any[]

  const mockFetch = vi.fn(async (url: string, opts?: any) => {
    const method = opts?.method || 'GET'
    if (url.includes('/settings/agent-dirs') && method === 'POST') {
      const body = JSON.parse(opts.body)
      posts.push(body)
      if (body.extra_dirs !== undefined) state.extra_dirs = body.extra_dirs
      if (body.disabled_dirs !== undefined) state.disabled_dirs = body.disabled_dirs
      return { ok: true, status: 200, json: () => Promise.resolve(state) }
    }
    if (url.includes('/settings/agent-dirs')) {
      return { ok: true, status: 200, json: () => Promise.resolve(state) }
    }
    if (url.includes('/agents/profiles')) {
      return {
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve([
            { name: 'dev', description: '', source: 'custom', duplicated_in: ['built-in'] },
            { name: 'analyst', description: '', source: 'built-in', duplicated_in: [] },
          ]),
      }
    }
    return { ok: true, status: 200, json: () => Promise.resolve({}) }
  })

  beforeEach(() => {
    state = { agent_dirs: { ...DEFAULTS }, extra_dirs: ['/team/a'], disabled_dirs: [] }
    posts = []
    vi.stubGlobal('fetch', mockFetch)
  })
  afterEach(() => vi.restoreAllMocks())

  it('lists defaults (deduped) + custom dirs, profile count, and duplicate note', async () => {
    render(<SettingsPanel />)
    expect(await screen.findByText('/home/u/.kiro/agents')).toBeInTheDocument()
    expect(screen.getByText('/team/a')).toBeInTheDocument()
    // agent-store shared by two providers renders exactly once
    expect(screen.getAllByText(AGENT_STORE)).toHaveLength(1)
    expect(await screen.findByTestId('profile-count')).toHaveTextContent('2 profiles discovered')
    expect(await screen.findByTestId('dup-note')).toBeInTheDocument()
  })

  it('disabling a directory POSTs it in disabled_dirs and flips the switch off', async () => {
    render(<SettingsPanel />)
    await screen.findByText('/team/a')

    const toggle = screen.getByRole('switch', { name: 'Enable /team/a' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)

    await waitFor(() =>
      expect(posts.some(p => p.disabled_dirs?.includes('/team/a'))).toBe(true)
    )
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: 'Enable /team/a' })).toHaveAttribute(
        'aria-checked',
        'false'
      )
    )
  })

  it('disabling a built-in default persists it (no more silent reappear — #281)', async () => {
    render(<SettingsPanel />)
    await screen.findByText('/home/u/.kiro/agents')
    fireEvent.click(screen.getByRole('switch', { name: 'Enable /home/u/.kiro/agents' }))
    await waitFor(() =>
      expect(posts.some(p => p.disabled_dirs?.includes('/home/u/.kiro/agents'))).toBe(true)
    )
  })

  it('removing a custom dir POSTs extra_dirs without it', async () => {
    render(<SettingsPanel />)
    await screen.findByText('/team/a')
    fireEvent.click(screen.getByRole('button', { name: 'Remove /team/a' }))
    await waitFor(() =>
      expect(
        posts.some(p => Array.isArray(p.extra_dirs) && !p.extra_dirs.includes('/team/a'))
      ).toBe(true)
    )
  })

  it('built-in defaults cannot be removed (no remove button)', async () => {
    render(<SettingsPanel />)
    await screen.findByText('/home/u/.kiro/agents')
    expect(screen.queryByRole('button', { name: 'Remove /home/u/.kiro/agents' })).toBeNull()
  })
})
