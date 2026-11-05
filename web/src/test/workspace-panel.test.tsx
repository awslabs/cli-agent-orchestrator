// Workspace panel: session sidebar, agent tabs, split panes, attention flags
// and the quick switcher, against a mocked API and a fake xterm factory (jsdom
// has no canvas, and the pool's behaviour is covered in workspace-logic).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act, within } from '@testing-library/react'

const created: string[] = []
const fontSizes: Record<string, number | undefined> = {}
vi.mock('../components/terminal/xtermSession', () => ({
  PARKED_ATTR: 'data-cao-parked',
  createXtermSession: (id: string, fontSize?: number) => {
    created.push(id)
    fontSizes[id] = fontSize
    const element = document.createElement('div')
    element.dataset.terminal = id
    return { terminalId: id, element, fit: () => {}, focus: () => {}, setFontSize: (px: number) => { fontSizes[id] = px }, dispose: () => element.remove() }
  },
}))

import { api, SessionDetail } from '../api'
import { useStore } from '../store'
import { useWorkspace } from '../workspace/workspaceStore'
import { WorkspacePanel } from '../components/WorkspacePanel'

function term(id: string, profile: string, status: string) {
  return {
    id, description: null, tmux_session: 's', tmux_window: id, provider: 'kiro_cli',
    agent_profile: profile, created_at: null, last_active: null, status,
  }
}

let details: Record<string, SessionDetail>

beforeEach(() => {
  created.length = 0
  details = {
    'cao-alpha': { session: { id: 'cao-alpha', name: 'cao-alpha', status: 'active' }, terminals: [term('a1', 'supervisor', 'processing'), term('a2', 'developer', 'idle')] },
    'cao-beta': { session: { id: 'cao-beta', name: 'cao-beta', status: 'active' }, terminals: [term('b1', 'reviewer', 'processing')] },
  }
  vi.spyOn(api, 'listSessions').mockImplementation(async () => Object.values(details).map(d => d.session))
  vi.spyOn(api, 'getSession').mockImplementation(async (name: string) => {
    if (!details[name]) throw Object.assign(new Error('404'), { status: 404 })
    return structuredClone(details[name])
  })
  useStore.setState({ sessions: [], connected: false })
  localStorage.clear()
  useWorkspace.getState().reset()
  useWorkspace.setState({ fontSize: 14 })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function poll() {
  await act(async () => {
    await useWorkspace.getState().poll(Object.keys(details))
  })
}

describe('WorkspacePanel', () => {
  it('lists sessions, selects the first, and opens its oldest agent in the pane', async () => {
    render(<WorkspacePanel />)
    const sidebar = await screen.findByRole('complementary', { name: 'Sessions' })
    await waitFor(() => expect(within(sidebar).getByText('cao-alpha')).toBeInTheDocument())
    expect(within(sidebar).getByText('cao-beta')).toBeInTheDocument()

    const tabs = await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    expect(within(tabs).getByRole('tab', { name: /supervisor/ })).toHaveAttribute('aria-selected', 'true')
    expect(within(tabs).getByRole('tab', { name: /developer/ })).toHaveAttribute('aria-selected', 'false')
    await waitFor(() => expect(created).toEqual(['a1']))
    expect(screen.getByTestId('pane-0').querySelector('[data-terminal="a1"]')).not.toBeNull()
  })

  it('click shows an agent in the focused pane; shift+click opens it in a split', async () => {
    render(<WorkspacePanel />)
    const tabs = await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    await waitFor(() => expect(created).toContain('a1'))

    fireEvent.click(within(tabs).getByRole('tab', { name: /developer/ }), { shiftKey: true })
    await waitFor(() => expect(screen.getByTestId('pane-1').querySelector('[data-terminal="a2"]')).not.toBeNull())
    expect(screen.getByTestId('pane-0').querySelector('[data-terminal="a1"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: '2 panes' })).toHaveAttribute('aria-pressed', 'true')

    // Closing the pane collapses back to one and never touches the agent.
    fireEvent.click(screen.getByRole('button', { name: 'Close pane 2' }))
    await waitFor(() => expect(screen.queryByTestId('pane-1')).toBeNull())
  })

  it('flags an off-screen agent that finished, and clears it once viewed', async () => {
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    await poll() // first sight of every status

    // b1 (other session, off screen) finishes; a1 (on screen) finishes too.
    details['cao-beta'].terminals[0].status = 'idle'
    details['cao-alpha'].terminals[0].status = 'idle'
    await poll()

    const sidebar = screen.getByRole('complementary', { name: 'Sessions' })
    const beta = within(sidebar).getByText('cao-beta').closest('button')!
    expect(beta.querySelector('[data-attention="done"]')).not.toBeNull()
    const alpha = within(sidebar).getByText('cao-alpha').closest('button')!
    expect(alpha.querySelector('[data-attention]')).toBeNull()
    expect(document.title).toMatch(/^\(1\)/)

    fireEvent.click(beta)
    await waitFor(() => expect(beta.querySelector('[data-attention]')).toBeNull())
    expect(document.title).not.toMatch(/^\(\d+\)/)
  })

  it('flags an agent waiting for input even on first sight', async () => {
    details['cao-beta'].terminals[0].status = 'waiting_user_answer'
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    await poll()
    const beta = within(screen.getByRole('complementary', { name: 'Sessions' })).getByText('cao-beta').closest('button')!
    await waitFor(() => expect(beta.querySelector('[data-attention="waiting"]')).not.toBeNull())
  })

  it('drops a deleted session and the panes showing its agents', async () => {
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    delete details['cao-alpha']
    useStore.setState({ sessions: [details['cao-beta'].session] })
    await poll()
    await waitFor(() => expect(screen.queryByRole('tablist', { name: 'Agents in cao-alpha' })).toBeNull())
  })

  it('Alt+K opens the switcher; Enter jumps to the agent across sessions', async () => {
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    fireEvent.keyDown(window, { key: 'k', code: 'KeyK', altKey: true })
    const dialog = await screen.findByRole('dialog', { name: 'Jump to agent' })
    const input = within(dialog).getByRole('textbox', { name: 'Search agents' })
    fireEvent.change(input, { target: { value: 'review' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const tabs = await screen.findByRole('tablist', { name: 'Agents in cao-beta' })
    expect(within(tabs).getByRole('tab', { name: /reviewer/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('a/A step the terminal font size for every pane and remember it', async () => {
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    await waitFor(() => expect(fontSizes.a1).toBe(14))

    fireEvent.click(screen.getByRole('button', { name: 'Smaller terminal text' }))
    fireEvent.click(screen.getByRole('button', { name: 'Smaller terminal text' }))
    await waitFor(() => expect(fontSizes.a1).toBe(12))
    expect(localStorage.getItem('cao.workspace.fontSize')).toBe('12')

    // A terminal opened afterwards starts at the chosen size.
    const tabs = screen.getByRole('tablist', { name: 'Agents in cao-alpha' })
    fireEvent.click(within(tabs).getByRole('tab', { name: /developer/ }), { shiftKey: true })
    await waitFor(() => expect(fontSizes.a2).toBe(12))

    fireEvent.click(screen.getByRole('button', { name: 'Larger terminal text' }))
    await waitFor(() => expect(fontSizes.a1).toBe(13))
    expect(fontSizes.a2).toBe(13)
  })

  it('disables a/A at the ends of the range', async () => {
    useWorkspace.setState({ fontSize: 9 })
    render(<WorkspacePanel />)
    await screen.findByRole('tablist', { name: 'Agents in cao-alpha' })
    expect(screen.getByRole('button', { name: 'Smaller terminal text' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Larger terminal text' })).not.toBeDisabled()
  })
})
