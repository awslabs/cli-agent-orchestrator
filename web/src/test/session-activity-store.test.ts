import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, Session } from '../api'
import { useStore } from '../store'
const row: Session = { id: 'cao-a', name: 'cao-a', status: 'active', session_incarnation_id: 'one' }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
beforeEach(() => useStore.setState({ sessions: [], connected: false, activeSession: null, activeSessionDetail: null }))
afterEach(() => vi.restoreAllMocks())
describe('strict listing store', () => {
  it('deduplicates pending reads, preserves rows on failure, accepts confirmed empty after reconnect', async () => {
    const pending = deferred<Session[]>()
    vi.spyOn(api, 'listSessions').mockReturnValueOnce(pending.promise)
    const first = useStore.getState().fetchSessions()
    const second = useStore.getState().fetchSessions()
    expect(api.listSessions).toHaveBeenCalledTimes(1)
    pending.resolve([row])
    await Promise.all([first, second])
    vi.mocked(api.listSessions).mockRejectedValueOnce(new Error('offline'))
    await useStore.getState().fetchSessions()
    expect(useStore.getState().sessions).toEqual([row])
    expect(useStore.getState().connected).toBe(false)
    vi.mocked(api.listSessions).mockResolvedValueOnce([])
    await useStore.getState().fetchSessions()
    expect(useStore.getState().sessions).toEqual([])
  })
  it('queues one post-delete read after the pending transport finishes, without resurrecting rows', async () => {
    const secondRow = { ...row, id: 'cao-b', name: 'cao-b' }
    useStore.setState({ sessions: [row, secondRow], connected: true })
    const pending = deferred<Session[]>()
    const replacement = deferred<Session[]>()
    const reads = vi.spyOn(api, 'listSessions').mockReturnValueOnce(pending.promise).mockReturnValueOnce(replacement.promise)
    vi.spyOn(api, 'deleteSession').mockResolvedValue({ success: true, deleted: ['cao-a', 'cao-b'], errors: [] })
    const listing = useStore.getState().fetchSessions()
    const firstDelete = useStore.getState().deleteSession('cao-a')
    const secondDelete = useStore.getState().deleteSession('cao-b')
    // Both DELETE continuations run while the old GET remains pending.
    await Promise.resolve()
    const concurrentPoll = useStore.getState().fetchSessions()
    const beforeOldSettles = reads.mock.calls.length
    expect(useStore.getState().sessions).toEqual([])
    pending.resolve([row, secondRow])
    await Promise.resolve()
    await Promise.resolve()
    expect(useStore.getState().sessions).toEqual([])
    replacement.resolve([])
    await Promise.all([listing, firstDelete, secondDelete, concurrentPoll])
    expect(beforeOldSettles).toBe(1)
    expect(reads).toHaveBeenCalledTimes(2)
    expect(useStore.getState().sessions).toEqual([])
  })
  it('refreshes after delete even when it settles in the same microtask turn as the old GET', async () => {
    const pending = deferred<Session[]>()
    const deletion = deferred<Awaited<ReturnType<typeof api.deleteSession>>>()
    const reads = vi.spyOn(api, 'listSessions').mockReturnValueOnce(pending.promise).mockResolvedValueOnce([])
    vi.spyOn(api, 'deleteSession').mockReturnValueOnce(deletion.promise)
    const listing = useStore.getState().fetchSessions()
    const deleting = useStore.getState().deleteSession('cao-a')
    pending.resolve([row])
    deletion.resolve({ success: true, deleted: ['cao-a'], errors: [] })
    await Promise.all([listing, deleting])
    expect(reads).toHaveBeenCalledTimes(2)
    expect(useStore.getState().sessions).toEqual([])
  })

})
