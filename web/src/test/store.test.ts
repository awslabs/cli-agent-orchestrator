import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useStore } from '../store'
import { api, SessionDetail } from '../api'

describe('Store', () => {
  beforeEach(() => {
    // Reset store state between tests
    useStore.setState({
      sessions: [],
      activeSession: null,
      activeSessionDetail: null,
      terminalStatuses: {},
      snackbar: null,
    })
  })

  it('has correct initial state', () => {
    const state = useStore.getState()
    expect(state.sessions).toEqual([])
    expect(state.activeSession).toBeNull()
    expect(state.activeSessionDetail).toBeNull()
    expect(state.terminalStatuses).toEqual({})
    expect(state.snackbar).toBeNull()
  })

  it('sets terminal status', () => {
    const { setTerminalStatus } = useStore.getState()
    setTerminalStatus('term-1', 'idle')
    expect(useStore.getState().terminalStatuses['term-1']).toBe('IDLE')
  })

  it('sets multiple terminal statuses independently', () => {
    const { setTerminalStatus } = useStore.getState()
    setTerminalStatus('term-1', 'idle')
    setTerminalStatus('term-2', 'processing')
    const statuses = useStore.getState().terminalStatuses
    expect(statuses['term-1']).toBe('IDLE')
    expect(statuses['term-2']).toBe('PROCESSING')
  })

  it('shows and clears snackbar', () => {
    const { showSnackbar } = useStore.getState()
    showSnackbar({ type: 'success', message: 'Test message' })
    expect(useStore.getState().snackbar).toEqual({ type: 'success', message: 'Test message' })

    useStore.setState({ snackbar: null })
    expect(useStore.getState().snackbar).toBeNull()
  })

  it('shows error snackbar', () => {
    const { showSnackbar } = useStore.getState()
    showSnackbar({ type: 'error', message: 'Something failed' })
    expect(useStore.getState().snackbar).toEqual({ type: 'error', message: 'Something failed' })
  })
})


describe('pending inbox count refresh', () => {
  const initial = useStore.getState()
  afterEach(() => {
    vi.restoreAllMocks()
    useStore.setState(initial, true)
  })
  it.each([3, 1, 0])('retains a count-only update from 2 to %s', async count => {
    const detail: SessionDetail = {
      session: { id: 'synthetic', name: 'synthetic', status: 'active' },
      terminals: [{ id: 'receiver', tmux_session: 'synthetic', tmux_window: 'worker',
        provider: 'codex', agent_profile: null, created_at: null, last_active: null,
        pending_inbox_count: 2 }],
    }
    vi.spyOn(api, 'getSession').mockResolvedValueOnce(detail).mockResolvedValueOnce({
      ...detail, terminals: [{ ...detail.terminals[0], pending_inbox_count: count }],
    })
    await useStore.getState().selectSession('synthetic')
    expect(useStore.getState().activeSessionDetail?.terminals[0].pending_inbox_count).toBe(2)
    await useStore.getState().selectSession('synthetic')
    expect(useStore.getState().activeSessionDetail?.terminals[0].pending_inbox_count).toBe(count)
  })
})
