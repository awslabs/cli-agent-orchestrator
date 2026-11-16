import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentPanel } from '../components/AgentPanel'
import { api } from '../api'
import { useStore } from '../store'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))
const profiles = [
  { name: 'code-reviewer', description: 'Reviews code', source: 'built-in' },
  { name: 'developer', description: 'Writes code', source: 'local' },
]
const create = vi.fn().mockResolvedValue(undefined)
beforeEach(() => {
  create.mockClear()
  useStore.setState({ sessions: [], activeSession: null, activeSessionDetail: null, createSession: create })
  vi.spyOn(api, 'listProviders').mockResolvedValue([])
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })
const open = () => { fireEvent.click(screen.getByRole('button', { name: /Spawn Agent/i })) }

describe('AgentPanel profile search', () => {
  it('selects a filtered profile and creates with exact form values; no match retains selection', async () => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue(profiles)
    render(<AgentPanel />)
    open()
    fireEvent.click(await screen.findByRole('button', { name: 'Agent Profile' }))
    let search = screen.getByRole('combobox', { name: 'Agent Profile' })
    fireEvent.change(search, { target: { value: 'review code' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(create).not.toHaveBeenCalled()
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    fireEvent.keyDown(search, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Agent Profile' }))
    search = screen.getByRole('combobox')
    fireEvent.change(search, { target: { value: 'missing' } })
    expect(screen.getByRole('status')).toHaveTextContent('No profiles match')
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(screen.getByRole('button', { name: 'Agent Profile' })).toHaveTextContent('code-reviewer')
    fireEvent.change(screen.getByPlaceholderText(/my-session/), { target: { value: 'session-a' } })
    fireEvent.change(screen.getByPlaceholderText(/path\/to\/project/), { target: { value: '/tmp/demo' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Spawn Agent' })[1])
    await waitFor(() => expect(create).toHaveBeenCalledWith('kiro_cli', 'code-reviewer', '/tmp/demo', 'session-a'))
  })
  it('quick pick updates the selected profile', async () => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue(profiles)
    render(<AgentPanel />); open()
    fireEvent.click(await screen.findByRole('button', { name: /developer.*Local/ }))
    expect(screen.getByRole('button', { name: 'Agent Profile' })).toHaveTextContent('developer')
  })
  it.each(['empty', 'error'])('retains manual fallback for %s catalogue', async mode => {
    vi.spyOn(api, 'listProfiles').mockImplementation(() => mode === 'empty' ? Promise.resolve([]) : Promise.reject(new Error('failed')))
    render(<AgentPanel />); open()
    const input = await screen.findByRole('textbox', { name: 'Agent Profile' })
    fireEvent.change(input, { target: { value: 'manual-profile' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Spawn Agent' })[1])
    await waitFor(() => expect(create).toHaveBeenCalledWith('kiro_cli', 'manual-profile', undefined, undefined))
  })
  it('shows loading while the catalogue is pending', async () => {
    vi.spyOn(api, 'listProfiles').mockReturnValue(new Promise(() => {}))
    render(<AgentPanel />); open()
    expect(screen.getByText('Loading profiles...')).toBeInTheDocument()
    await waitFor(() => expect(api.listProviders).toHaveBeenCalled())
  })
})
