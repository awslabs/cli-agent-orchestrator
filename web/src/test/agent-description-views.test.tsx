import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, SessionDetail, Terminal } from '../api'
import { useStore } from '../store'
import { AgentPanel } from '../components/AgentPanel'
import { DashboardHome } from '../components/DashboardHome'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

let detail: SessionDetail
beforeEach(() => {
  detail = {
    session: { id: 'cao-demo', name: 'cao-demo', status: 'active' },
    terminals: [{ id: 'abcd1234', tmux_session: 'cao-demo', tmux_window: 'developer-abcd1234', provider: 'kiro_cli', agent_profile: 'developer', description: 'Original responsibility', created_at: null, last_active: null }],
  }
  useStore.setState({ sessions: [detail.session], activeSession: 'cao-demo', activeSessionDetail: detail, terminalStatuses: {} })
  vi.spyOn(api, 'getSession').mockImplementation(async () => structuredClone(detail))
  vi.spyOn(api, 'listProfiles').mockResolvedValue([])
  vi.spyOn(api, 'listProviders').mockResolvedValue([])
  vi.spyOn(api, 'getTerminalStatus').mockResolvedValue('IDLE')
  vi.spyOn(api, 'getWorkingDirectory').mockResolvedValue({ working_directory: null })
  vi.spyOn(api, 'updateTerminalDescription').mockImplementation(async (id, description) => {
    detail = { ...detail, terminals: detail.terminals.map(t => t.id === id ? { ...t, description } : t) }
    return { id, description } as Terminal
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

for (const view of ['Home', 'Agents']) {
  describe(`${view} descriptions`, () => {
    it('renders, saves, clears and revalidates the confirmed value', async () => {
      render(view === 'Home' ? <DashboardHome onNavigate={vi.fn()} /> : <AgentPanel />)
      await screen.findByText('Original responsibility')
      fireEvent.click(screen.getByRole('button', { name: 'Edit agent description' }))
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'New responsibility' } })
      fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
      await screen.findByText('New responsibility')
      expect(api.updateTerminalDescription).toHaveBeenCalledWith('abcd1234', 'New responsibility')
      fireEvent.click(screen.getByRole('button', { name: 'Edit agent description' }))
      fireEvent.change(screen.getByRole('textbox'), { target: { value: ' ' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(screen.queryByText('New responsibility')).toBeNull())
      expect(api.updateTerminalDescription).toHaveBeenLastCalledWith('abcd1234', null)
      expect(useStore.getState().activeSessionDetail?.terminals[0].description).toBeNull()
      expect(api.getSession).toHaveBeenCalledWith('cao-demo')
    })

    it('preserves a draft across the five second poll', async () => {
      vi.useFakeTimers()
      await act(async () => { render(view === 'Home' ? <DashboardHome onNavigate={vi.fn()} /> : <AgentPanel />) })
      expect(screen.getByText('Original responsibility')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Edit agent description' }))
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'My draft' } })
      detail = { ...detail, terminals: detail.terminals.map(t => ({ ...t, description: 'Other operator edit' })) }
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(screen.getByRole('textbox')).toHaveValue('My draft')
      fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
      expect(screen.getByText('Other operator edit')).toBeInTheDocument()
    })
  })
}
