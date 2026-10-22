import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentPanel } from '../components/AgentPanel'
import { api, Session, SessionDetail } from '../api'
import { useStore } from '../store'
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))
vi.mock('../components/OutputViewer', () => ({ OutputViewer: () => null }))
const initial = useStore.getState()
const rows: Session[] = ['a', 'b', 'c', 'd'].map(id => ({ id, name: id, status: 'active', session_incarnation_id: `${id}-1` }))
const detail: SessionDetail = { session: rows[0], terminals: [] }
beforeEach(() => {
  vi.useFakeTimers()
  useStore.setState({ ...initial, sessions: rows, activeSession: 'a', activeSessionDetail: detail })
  vi.spyOn(api, 'listSessions').mockResolvedValue(rows)
  vi.spyOn(api, 'listProfiles').mockResolvedValue([])
  vi.spyOn(api, 'listProviders').mockResolvedValue([])
  vi.spyOn(api, 'getSession').mockResolvedValue(detail)
  vi.spyOn(api, 'getSessionMembership').mockImplementation(async (id, incarnation) => ({ session: { id, name: id, status: 'active', session_incarnation_id: incarnation }, terminals: [{ id: `${id}-t`, session_incarnation_id: incarnation }] }))
  vi.spyOn(api, 'getTerminalStatusReading').mockImplementation(async id => ({ id, session_name: id[0], session_incarnation_id: `${id[0]}-1`, status: id[0] === 'b' ? 'WAITING_USER_ANSWER' : 'COMPLETED' }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); useStore.setState(initial, true) })
const flush = async () => act(async () => { await vi.advanceTimersByTimeAsync(0) })
const row = (id: string) => screen.getByText(id, { exact: true }).closest('div.flex.items-center.justify-between') as HTMLElement

describe('Agents Sessions badges', () => {
  it('reads collapsed/never-selected/filtered rows without visiting Home or changing selection', async () => {
    render(<AgentPanel />)
    await flush()
    expect(within(row('b')).getByText('Awaiting Input')).toBeVisible()
    expect(within(row('c')).getByText('Completed')).toBeVisible()
    expect(useStore.getState().activeSession).toBe('a')
    expect(useStore.getState().activeSessionDetail).toEqual(detail)
    expect(api.getSession).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByPlaceholderText('Filter sessions...'), { target: { value: 'a' } })
    expect(screen.queryByText('b', { exact: true })).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(8)
    expect(api.getSession).toHaveBeenCalledTimes(1)
    expect(useStore.getState().activeSession).toBe('a')
  })
  it('keeps reading every row when all sessions are collapsed', async () => {
    useStore.setState({ activeSession: null, activeSessionDetail: null })
    render(<AgentPanel />)
    await flush()
    expect(api.getSession).not.toHaveBeenCalled()
    expect(api.getSessionMembership).toHaveBeenCalledTimes(4)
    expect(within(row('b')).getAllByText('Awaiting Input')).toHaveLength(1)
    expect(useStore.getState().activeSession).toBeNull()
  })
  it('selection A to B to A never restarts the background reader or changes its results', async () => {
    vi.mocked(api.getSession).mockImplementation(async name => ({ session: rows.find(r => r.id === name)!, terminals: [] }))
    render(<AgentPanel />)
    await flush()
    fireEvent.click(row('b'))
    await flush()
    expect(useStore.getState().activeSession).toBe('b')
    fireEvent.click(row('a'))
    await flush()
    expect(useStore.getState().activeSession).toBe('a')
    expect(useStore.getState().activeSessionDetail?.session.id).toBe('a')
    expect(api.getSessionMembership).toHaveBeenCalledTimes(4)
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(4)
    expect(within(row('b')).getByText('Awaiting Input')).toBeVisible()
  })

  it.each([null, undefined])('preserves visible legacy terminal badges with identity %s while the aggregate stays Unknown', async identity => {
    const legacy = { ...rows[0], session_incarnation_id: identity }
    const legacyDetail: SessionDetail = { session: legacy, terminals: [{ id: 'a-t', tmux_session: 'a', tmux_window: 'a-t', provider: 'codex', agent_profile: 'developer', description: null, created_at: null, last_active: null }] }
    useStore.setState({ sessions: [legacy], activeSessionDetail: legacyDetail })
    vi.mocked(api.listSessions).mockResolvedValue([legacy])
    vi.mocked(api.getSession).mockResolvedValue(legacyDetail)
    vi.spyOn(api, 'getWorkingDirectory').mockResolvedValue({ working_directory: null })
    const individual = vi.spyOn(api, 'getTerminalStatus').mockResolvedValue('PROCESSING')
    render(<AgentPanel />)
    await flush()
    expect(within(row('a')).getByText('Unknown')).toBeVisible()
    expect(screen.getByText('Processing')).toBeVisible()
    individual.mockResolvedValue('WAITING_USER_ANSWER')
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(screen.getByText('Awaiting Input')).toBeVisible()
    expect(within(row('a')).getByText('Unknown')).toBeVisible()
    expect(individual).toHaveBeenCalledTimes(2)
    expect(api.getSessionMembership).not.toHaveBeenCalled()
    expect(api.getTerminalStatusReading).not.toHaveBeenCalled()
  })

  it('stops legacy polling on identity promotion and rejects the pending legacy callback', async () => {
    const legacy = { ...rows[0], session_incarnation_id: null }
    const terminal = { id: 'a-t', tmux_session: 'a', tmux_window: 'a-t', provider: 'codex', agent_profile: 'developer', description: null, created_at: null, last_active: null }
    const legacyDetail: SessionDetail = { session: legacy, terminals: [terminal] }
    useStore.setState({ sessions: [legacy], activeSessionDetail: legacyDetail })
    vi.mocked(api.listSessions).mockResolvedValue([legacy])
    vi.mocked(api.getSession).mockResolvedValue(legacyDetail)
    vi.spyOn(api, 'getWorkingDirectory').mockResolvedValue({ working_directory: null })
    let resolve!: (status: string) => void
    const individual = vi.spyOn(api, 'getTerminalStatus').mockReturnValue(new Promise(done => { resolve = done }))
    const view = render(<AgentPanel />)
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(individual).toHaveBeenCalledTimes(1) // Pending legacy reads never overlap.
    const verifiedDetail = { session: rows[0], terminals: [terminal] }
    vi.mocked(api.listSessions).mockResolvedValue([rows[0]])
    vi.mocked(api.getSession).mockResolvedValue(verifiedDetail)
    act(() => useStore.setState({ sessions: [rows[0]], activeSessionDetail: verifiedDetail }))
    await flush()
    act(() => resolve('WAITING_USER_ANSWER'))
    await flush()
    expect(screen.queryByText('Awaiting Input')).toBeNull()
    expect(screen.getAllByText('Completed')).toHaveLength(2) // Session and terminal use the verified reader.
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(individual).toHaveBeenCalledTimes(1)
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(2)
    view.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(individual).toHaveBeenCalledTimes(1)
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(2)
  })

  it('ignores a legacy callback after collapsing the selected session and cleans up its timer', async () => {
    const legacy = { ...rows[0], session_incarnation_id: null }
    const legacyDetail: SessionDetail = { session: legacy, terminals: [{ id: 'a-t', tmux_session: 'a', tmux_window: 'a-t', provider: 'codex', agent_profile: 'developer', description: null, created_at: null, last_active: null }] }
    useStore.setState({ sessions: [legacy], activeSessionDetail: legacyDetail })
    vi.mocked(api.listSessions).mockResolvedValue([legacy])
    vi.mocked(api.getSession).mockResolvedValue(legacyDetail)
    vi.spyOn(api, 'getWorkingDirectory').mockResolvedValue({ working_directory: null })
    let resolve!: (status: string) => void
    const individual = vi.spyOn(api, 'getTerminalStatus').mockReturnValue(new Promise(done => { resolve = done }))
    render(<AgentPanel />)
    await flush()
    fireEvent.click(row('a'))
    await flush()
    act(() => resolve('WAITING_USER_ANSWER'))
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(useStore.getState().activeSession).toBeNull()
    expect(screen.queryByText('Awaiting Input')).toBeNull()
    expect(within(row('a')).getByText('Unknown')).toBeVisible()
    expect(individual).toHaveBeenCalledTimes(1)
  })

})
