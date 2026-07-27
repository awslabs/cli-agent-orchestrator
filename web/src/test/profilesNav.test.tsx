import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { api } from '../api'

// #510 U2 FR3: the Home "Profiles" stat card links to the Profiles tab, and the
// nav label reads "Profiles" (route key unchanged). These are label/nav-only
// changes — no store key / API path / module rename.

describe('DashboardHome — Profiles stat card link [FR3/AC3.2]', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue([
      { name: 'developer', description: 'Dev', source: 'built-in' },
    ] as any)
    vi.spyOn(api, 'getSession').mockResolvedValue({ session: { id: 's', name: 's', status: 'active' }, terminals: [] } as any)
    vi.spyOn(api, 'getTerminalStatus').mockResolvedValue('IDLE' as any)
  })

  afterEach(() => vi.restoreAllMocks())

  it('renders the Profiles stat card as a button that navigates to the agents tab', async () => {
    const onNavigate = vi.fn()
    render(<DashboardHome onNavigate={onNavigate} />)
    const card = await screen.findByLabelText('Go to Profiles tab')
    expect(card.tagName).toBe('BUTTON')
    fireEvent.click(card)
    expect(onNavigate).toHaveBeenCalledWith('agents')
  })

  it('shows the profile count from listProfiles on the card', async () => {
    const onNavigate = vi.fn()
    render(<DashboardHome onNavigate={onNavigate} />)
    await waitFor(() => expect(screen.getByText('1')).toBeInTheDocument())
    // The card is keyboard-activatable (a real <button>, focusable by default).
    const card = screen.getByLabelText('Go to Profiles tab')
    card.focus()
    expect(document.activeElement).toBe(card)
  })
})

describe('App nav label [FR3/AC3.1]', () => {
  afterEach(() => vi.restoreAllMocks())

  it('the tab list renders the label "Profiles" while keeping the agents route key', async () => {
    // Assert against the shipped App shell: the tab list must contain a
    // "Profiles" label and must NOT contain the old "Agents" label.
    vi.spyOn(api, 'listSessions').mockResolvedValue([])
    vi.spyOn(api, 'getMemoryStatus').mockResolvedValue({ enabled: false })
    const App = (await import('../App')).default
    render(<App />)
    // Header brand text is "CLI Agent Orchestrator" (contains "Agent"), so scope
    // the assertion to the tablist.
    const tablist = await screen.findByRole('tablist')
    expect(tablist).toHaveTextContent('Profiles')
    const tabLabels = Array.from(tablist.querySelectorAll('[role="tab"]')).map(t => t.textContent)
    expect(tabLabels.some(l => l?.includes('Profiles'))).toBe(true)
    expect(tabLabels.some(l => l === 'Agents')).toBe(false)
  })

  it('keeps the internal route key "agents": session badge shows and the tab renders AgentPanel', async () => {
    // The complement of the label test. Only the user-facing LABEL changed; the
    // route key stays 'agents'. Two things are keyed on that literal:
    //   1. the session-count badge (App.tsx: `t.key === 'agents' && sessions...`)
    //   2. the tab body (`tab === 'agents' && <AgentPanel/>`)
    // A future contributor "finishing the rename" by changing the key would pass
    // the label test above while breaking both — so pin them here.
    vi.spyOn(api, 'listSessions').mockResolvedValue([
      { id: 's1', name: 's1', status: 'active' },
      { id: 's2', name: 's2', status: 'active' },
    ] as any)
    vi.spyOn(api, 'getMemoryStatus').mockResolvedValue({ enabled: false })
    vi.spyOn(api, 'getSession').mockResolvedValue(
      { session: { id: 's1', name: 's1', status: 'active' }, terminals: [] } as any,
    )
    vi.spyOn(api, 'listProviders').mockResolvedValue([] as any)
    vi.spyOn(api, 'listProfiles').mockResolvedValue([] as any)

    const App = (await import('../App')).default
    render(<App />)
    const tablist = await screen.findByRole('tablist')
    const profilesTab = Array.from(tablist.querySelectorAll('[role="tab"]'))
      .find(t => t.textContent?.includes('Profiles')) as HTMLElement
    expect(profilesTab).toBeTruthy()

    // (1) The session badge (keyed on 'agents') renders the count on this tab —
    // proof the key is still 'agents', not a renamed 'profiles'.
    await waitFor(() => expect(profilesTab).toHaveTextContent('2'))

    // (2) Activating the tab renders AgentPanel — proven by ProfilesBrowser's
    // "Agent Profiles" heading (mounted at the top of AgentPanel). If the key had
    // been renamed, `tab === 'agents'` would never match and nothing would show.
    fireEvent.click(profilesTab)
    expect(await screen.findByText('Agent Profiles')).toBeInTheDocument()
  })
})
