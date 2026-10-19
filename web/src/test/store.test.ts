import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useStore } from '../store'
import { api, SessionDetail, SESSION_DESCRIPTION_READ_ATTEMPTS } from '../api'

describe('Store', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
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

  it('preserves session details when polling fails', async () => {
    const detail = { session: { id: 'cao-demo', name: 'cao-demo', status: 'active' }, terminals: [] } as SessionDetail
    useStore.setState({ activeSession: 'cao-demo', activeSessionDetail: detail })
    vi.spyOn(api, 'getSession').mockRejectedValue(new Error('Offline'))
    await useStore.getState().selectSession('cao-demo')
    expect(useStore.getState().activeSessionDetail).toEqual(detail)
  })

  it('clears the detail of a deleted session on a 404 poll', async () => {
    const detail: SessionDetail = { session: { id: 'cao-gone', name: 'cao-gone', status: 'active' }, terminals: [] }
    useStore.setState({ activeSession: 'cao-gone', activeSessionDetail: detail })
    vi.spyOn(api, 'getSession').mockRejectedValue(Object.assign(new Error('Gone'), { status: 404 }))
    await useStore.getState().selectSession('cao-gone')
    expect(useStore.getState().activeSessionDetail).toBeNull()
    expect(useStore.getState().activeSession).toBe('cao-gone')
  })

  it('ignores a late 404 after another session has loaded', async () => {
    let fail!: (error: unknown) => void
    const detail: SessionDetail = { session: { id: 'cao-new', name: 'cao-new', status: 'active' }, terminals: [] }
    vi.spyOn(api, 'getSession')
      .mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
      .mockResolvedValueOnce(detail)
    const selecting = useStore.getState().selectSession('cao-old')
    await useStore.getState().selectSession('cao-new')
    fail(Object.assign(new Error('Gone'), { status: 404 }))
    await selecting
    expect(useStore.getState().activeSessionDetail).toEqual(detail)
  })

  it.each(['cao-demo', 'cao-other'])('bounds revalidations during continuous edits with %s detail', async previous => {
    const detail: SessionDetail = { session: { id: previous, name: previous, status: 'active' }, terminals: [] }
    useStore.setState({ activeSession: 'cao-demo', activeSessionDetail: detail })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, text: async () => JSON.stringify({ id: 'abcd1234', description: 'New' }) }))
    const read = vi.spyOn(api, 'getSession').mockImplementation(async () => {
      await api.updateTerminalDescription('abcd1234', 'New')
      // Guard the regression test itself against the old unbounded recursion.
      if (read.mock.calls.length > 5) throw new Error('Too many reads')
      return detail
    })
    await useStore.getState().selectSession('cao-demo')
    expect(read).toHaveBeenCalledTimes(SESSION_DESCRIPTION_READ_ATTEMPTS)
    expect(useStore.getState().activeSessionDetail).toEqual(previous === 'cao-demo' ? detail : null)
  })

  it('ignores an earlier response after another session is selected', async () => {
    let finish!: (detail: SessionDetail) => void
    vi.spyOn(api, 'getSession').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const selecting = useStore.getState().selectSession('cao-old')
    await useStore.getState().selectSession(null)
    finish({ session: { id: 'cao-old', name: 'cao-old', status: 'active' }, terminals: [] })
    await selecting
    expect(useStore.getState().activeSessionDetail).toBeNull()
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
        provider: 'codex', agent_profile: null, description: null, created_at: null, last_active: null,
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
