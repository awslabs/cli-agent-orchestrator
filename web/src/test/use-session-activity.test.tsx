import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, Session } from '../api'
import { useSessionActivity } from '../hooks/useSessionActivity'

const sessions: Session[] = ['a', 'b', 'c'].map(id => ({ id, name: id, status: 'active', session_incarnation_id: `${id}-1` }))
const membership = (name: string, incarnation: string, ids = [`${name}-t`]) => ({
  session: { id: name, name, status: 'active', session_incarnation_id: incarnation },
  terminals: ids.map(id => ({ id, session_incarnation_id: incarnation })),
})
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

describe('Agents session activity reader', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.spyOn(api, 'getSessionMembership').mockImplementation(async (name, incarnation) => membership(name, incarnation))
    vi.spyOn(api, 'getTerminalStatusReading').mockImplementation(async id => ({ id, session_name: id.split('-')[0], session_incarnation_id: `${id.split('-')[0]}-1`, status: 'PROCESSING' }))
  })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('reads every session immediately with 3s/5s polls and cleans up on unmount', async () => {
    const fetch = vi.fn(async () => {})
    const { result, unmount } = renderHook(() => useSessionActivity(sessions, fetch))
    await flush()
    expect(result.current.sessionStatuses).toEqual({ a: 'PROCESSING', b: 'PROCESSING', c: 'PROCESSING' })
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(3)
    expect(fetch).toHaveBeenCalledOnce()
    await tick(3000)
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(6)
    expect(api.getSessionMembership).toHaveBeenCalledTimes(3)
    await tick(2000)
    expect(api.getSessionMembership).toHaveBeenCalledTimes(6)
    unmount()
    await tick(10000)
    expect(api.getSessionMembership).toHaveBeenCalledTimes(6)
  })

  it('retains failures without renewing age and expires with no successful network response', async () => {
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), vi.fn(async () => {})))
    await flush()
    vi.mocked(api.getSessionMembership).mockRejectedValue(new Error('Offline'))
    vi.mocked(api.getTerminalStatusReading).mockRejectedValue(new Error('Offline'))
    await tick(119999)
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    await tick(1)
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN')
  })

  it('repeated identical status renews only its observation; membership can expire independently', async () => {
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), vi.fn(async () => {})))
    await flush()
    vi.mocked(api.getSessionMembership).mockRejectedValue(new Error('Offline'))
    await tick(120000)
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN')
    expect(result.current.terminalStatuses['a-t']).toBe('PROCESSING')
  })

  it('unknown readings replace known state; priority resolves on the next observation', async () => {
    vi.mocked(api.getSessionMembership).mockImplementation(async (n, i) => membership(n, i, ['a-t', 'a-u']))
    vi.mocked(api.getTerminalStatusReading).mockImplementation(async id => ({ id, session_name: 'a', session_incarnation_id: 'a-1', status: id === 'a-t' ? 'WAITING_USER_ANSWER' : 'PROCESSING' }))
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), vi.fn(async () => {})))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('WAITING_USER_ANSWER')
    // Preserve the other working member; a valid null must clear the old wait.
    vi.mocked(api.getTerminalStatusReading).mockImplementation(async id => ({ id, session_name: 'a', session_incarnation_id: 'a-1', status: id === 'a-t' ? null : 'PROCESSING' }))
    await tick(3000)
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    expect(result.current.terminalStatuses['a-t']).toBe('UNKNOWN')
  })

  it('does not overlap slow requests, and ignores removed member callbacks after ABA', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.getTerminalStatusReading>>>()
    vi.mocked(api.getTerminalStatusReading).mockReturnValue(pending.promise)
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), vi.fn(async () => {})))
    await flush()
    await tick(6000)
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(1)
    vi.mocked(api.getSessionMembership).mockImplementation(async (n, i) => membership(n, i, []))
    await tick(4000)
    expect(result.current.sessionStatuses.a).toBe('IDLE')
    vi.mocked(api.getSessionMembership).mockImplementation(async (n, i) => membership(n, i))
    const replacement = deferred<Awaited<ReturnType<typeof api.getTerminalStatusReading>>>()
    vi.mocked(api.getTerminalStatusReading).mockReturnValueOnce(replacement.promise)
    await tick(5000)
    // The newly added member has its own pending request/generation.
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(1)
    act(() => pending.resolve({ id: 'a-t', session_name: 'a', session_incarnation_id: 'a-1', status: 'COMPLETED' }))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN')
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(2)
    act(() => replacement.resolve({ id: 'a-t', session_name: 'a', session_incarnation_id: 'a-1', status: 'PROCESSING' }))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
  })

  it('rejects stale incarnation, unmount, and confirmed mutation responses', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.getSessionMembership>>>()
    vi.mocked(api.getSessionMembership).mockReturnValueOnce(pending.promise)
    const { result, rerender, unmount } = renderHook(({ rows }) => useSessionActivity(rows, vi.fn(async () => {})), { initialProps: { rows: sessions.slice(0, 1) } })
    rerender({ rows: [{ ...sessions[0], session_incarnation_id: 'a-2' }] })
    await flush()
    act(() => pending.resolve(membership('a', 'a-1', [])))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN') // terminal identity contradicts a-2
    unmount()
    await flush()
  })

  it('invalidates confirmed mutations and ignores membership response from before the mutation', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.getSessionMembership>>>()
    vi.mocked(api.getSessionMembership).mockReturnValueOnce(pending.promise)
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), vi.fn(async () => {})))
    act(() => result.current.invalidate('a'))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN')
    expect(api.getSessionMembership).toHaveBeenCalledTimes(1)
    act(() => pending.resolve(membership('a', 'a-1', [])))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
  })
  it('a pending key does not delay other sessions and identical successful readings renew TTL', async () => {
    vi.mocked(api.getSessionMembership).mockImplementation(async (n, i) => n === 'b' ? new Promise(() => {}) : membership(n, i))
    const { result } = renderHook(() => useSessionActivity(sessions, vi.fn(async () => {})))
    await flush()
    expect(result.current.sessionStatuses).toEqual({ a: 'PROCESSING', b: 'UNKNOWN', c: 'PROCESSING' })
    await tick(121000)
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    expect(result.current.sessionStatuses.c).toBe('PROCESSING')
    expect(vi.mocked(api.getSessionMembership).mock.calls.filter(([n]) => n === 'b')).toHaveLength(1)
  })

  it('ignores an old response processed after suspension, and restarts fresh on remount', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.getTerminalStatusReading>>>()
    vi.mocked(api.getTerminalStatusReading).mockReturnValueOnce(pending.promise)
    const read = vi.fn(async () => {})
    const { result, unmount } = renderHook(() => useSessionActivity(sessions.slice(0, 1), read))
    await flush()
    vi.setSystemTime(Date.now() + 120000)
    act(() => window.dispatchEvent(new Event('focus')))
    act(() => pending.resolve({ id: 'a-t', session_name: 'a', session_incarnation_id: 'a-1', status: 'WAITING_USER_ANSWER' }))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('UNKNOWN')
    unmount()
    const next = renderHook(() => useSessionActivity(sessions.slice(0, 1), read))
    await flush()
    expect(next.result.current.sessionStatuses.a).toBe('PROCESSING')
    next.unmount()
  })

  it('ignores membership ABA callbacks when a session is removed and returns with the same key', async () => {
    const old = deferred<Awaited<ReturnType<typeof api.getSessionMembership>>>()
    vi.mocked(api.getSessionMembership).mockReturnValueOnce(old.promise)
    const fetch = vi.fn(async () => {})
    const { result, rerender } = renderHook(({ rows }) => useSessionActivity(rows, fetch), { initialProps: { rows: sessions.slice(0, 1) } })
    rerender({ rows: [] })
    rerender({ rows: sessions.slice(0, 1) })
    expect(api.getSessionMembership).toHaveBeenCalledTimes(1)
    act(() => old.resolve(membership('a', 'a-1', [])))
    await flush()
    // A queued new-generation read follows the old transport; its data wins.
    expect(api.getSessionMembership).toHaveBeenCalledTimes(2)
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
  })

  it('ignores old 409 failures after a confirmed mutation without invalidating the replacement', async () => {
    let reject!: (error: unknown) => void
    vi.mocked(api.getSessionMembership).mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    const fetch = vi.fn(async () => {})
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), fetch))
    act(() => result.current.invalidate('a'))
    act(() => reject(Object.assign(new Error('Old incarnation conflict'), { status: 409 })))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    expect(fetch).toHaveBeenCalledTimes(1) // Only the mount refresh, no stale error reconciliation.
  })

  it('ignores old terminal 404 after removal and leaves current members intact', async () => {
    let reject!: (error: unknown) => void
    vi.mocked(api.getTerminalStatusReading).mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    const fetch = vi.fn(async () => {})
    const { result } = renderHook(() => useSessionActivity(sessions.slice(0, 1), fetch))
    await flush()
    vi.mocked(api.getSessionMembership).mockImplementation(async (n, i) => membership(n, i, ['a-new']))
    await tick(5000)
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    const requests = vi.mocked(api.getSessionMembership).mock.calls.length
    act(() => reject(Object.assign(new Error('Old terminal gone'), { status: 404 })))
    await flush()
    expect(result.current.sessionStatuses.a).toBe('PROCESSING')
    expect(result.current.terminalStatuses['a-t']).toBeUndefined()
    expect(api.getSessionMembership).toHaveBeenCalledTimes(requests)
  })

  it('never applies callbacks or launches follow-up work from a previous mount', async () => {
    const old = deferred<Awaited<ReturnType<typeof api.getSessionMembership>>>()
    vi.mocked(api.getSessionMembership).mockReturnValueOnce(old.promise)
    const fetch = vi.fn(async () => {})
    const first = renderHook(() => useSessionActivity(sessions.slice(0, 1), fetch))
    first.unmount()
    const second = renderHook(() => useSessionActivity(sessions.slice(0, 1), fetch))
    await flush()
    expect(second.result.current.sessionStatuses.a).toBe('PROCESSING')
    const statuses = vi.mocked(api.getTerminalStatusReading).mock.calls.length
    act(() => old.resolve(membership('a', 'a-1', ['a-obsolete'])))
    await flush()
    expect(second.result.current.sessionStatuses.a).toBe('PROCESSING')
    expect(api.getTerminalStatusReading).toHaveBeenCalledTimes(statuses)
    expect(fetch).toHaveBeenCalledTimes(2)
    second.unmount()
  })

})
