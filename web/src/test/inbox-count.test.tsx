import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { api, SessionDetail, TerminalMeta } from '../api'
import { useStore } from '../store'
import { AgentPanel } from '../components/AgentPanel'
import { DashboardHome } from '../components/DashboardHome'

// Local mocks keep existing component tests independent of these panels.
vi.mock('../components/InboxPanel', () => ({
  InboxPanel: ({ terminalId }: { terminalId: string }) => <div role="dialog">Inbox receiver: {terminalId}</div>,
}))
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))
vi.mock('../components/OutputViewer', () => ({ OutputViewer: () => null }))
vi.mock('../components/HealthCard', () => ({ HealthCard: () => null }))
const session = { id: 'synthetic-session', name: 'synthetic-session', status: 'active' }
const initialStore = useStore.getState()
function terminal(id: string, count?: number): TerminalMeta {
  return { id, tmux_session: session.name, tmux_window: id, provider: 'codex',
    agent_profile: id, description: null, created_at: null, last_active: null,
    ...(count === undefined ? {} : { pending_inbox_count: count }),
  }
}
function detail(terminals: TerminalMeta[]): SessionDetail { return { session, terminals } }
async function settle() { await act(async () => { await vi.advanceTimersByTimeAsync(0) }) }
function assertCount(count?: number) {
  const visible = (count ?? 0) > 1
  const button = screen.getByRole('button', {
    name: visible ? `Inbox: ${count} pending messages` : 'Inbox',
  })
  if (visible) {
    expect(within(button).getByText(String(count), { exact: true })).toBeVisible()
    expect(within(button).queryByText('99+')).not.toBeInTheDocument()
  } else {
    expect(button).not.toHaveTextContent(/\d/)
    expect(button.querySelector('span[aria-hidden="true"]')).toBeNull()
  }
  expect(button.querySelector('svg')).not.toBeNull()
  return button
}
beforeEach(() => {
  vi.useFakeTimers()
  useStore.setState({ ...initialStore, sessions: [session], activeSession: session.name })
  vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Unexpected network request'))
  vi.spyOn(api, 'listProfiles').mockResolvedValue([])
  vi.spyOn(api, 'listProviders').mockResolvedValue([])
  vi.spyOn(api, 'getTerminalStatus').mockResolvedValue('idle')
  vi.spyOn(api, 'getWorkingDirectory').mockResolvedValue({ working_directory: null })
  vi.spyOn(api, 'getInboxMessages').mockResolvedValue([])
})
afterEach(() => {
  cleanup()
  try {
    expect(api.getInboxMessages).not.toHaveBeenCalled()
    expect(globalThis.fetch).not.toHaveBeenCalled()
  } finally {
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.restoreAllMocks()
    useStore.setState(initialStore, true)
  }
})
for (const view of ['Home', 'Agents'] as const) {
  describe(`${view} inbox count`, () => {
    async function mount(terminals: TerminalMeta[]) {
      vi.spyOn(api, 'getSession').mockResolvedValue(detail(terminals))
      useStore.setState({ activeSessionDetail: detail(terminals) })
      render(view === 'Home' ? <DashboardHome onNavigate={vi.fn()} /> : <AgentPanel />)
      await settle()
    }
    it.each([undefined, 0, 1, 2, 3, 123])('renders count %s with its accessible name', async count => {
      await mount([terminal('receiver-a', count)])
      const button = assertCount(count)
      if (view === 'Agents') expect(button).toHaveTextContent('Inbox')
    })
    it.each(['button', 'number'])('opens the second receiver through its %s', async target => {
      await mount([terminal('receiver-a', 2), terminal('receiver-b', 123)])
      const first = screen.getByRole('button', { name: 'Inbox: 2 pending messages' })
      const second = screen.getByRole('button', { name: 'Inbox: 123 pending messages' })
      expect(within(first).getByText('2')).toBeVisible()
      expect(within(second).getByText('123')).toBeVisible()
      expect(first.parentElement?.parentElement?.textContent).toContain('receiver-a')
      expect(second.parentElement?.parentElement?.textContent).toContain('receiver-b')
      fireEvent.click(target === 'button' ? second : within(second).getByText('123'))
      expect(screen.getByRole('dialog')).toHaveTextContent('Inbox receiver: receiver-b')
    })
    it('refreshes 1 → 2 → 3 → 2 → 1 → 0 without opening inbox', async () => {
      const select = vi.spyOn(useStore.getState(), 'selectSession')
      await mount([terminal('receiver-a', 1)])
      assertCount(1)
      select.mockClear()
      for (const count of [2, 3, 2, 1, 0]) {
        vi.mocked(api.getSession).mockResolvedValue(detail([terminal('receiver-a', count)]))
        await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
        assertCount(count)
        if (view === 'Agents') {
          expect(select).toHaveBeenCalledWith(session.name)
          expect(useStore.getState().activeSessionDetail?.terminals[0].pending_inbox_count).toBe(count)
          select.mockClear()
        }
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      }
    })
  })
}
