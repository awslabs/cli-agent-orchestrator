import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
const respond = (data: unknown) => vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, text: async () => JSON.stringify(data) })))
afterEach(() => vi.unstubAllGlobals())
describe('strict activity API contracts', () => {
  it.each([undefined, null, {}, [{}], [{ id: 'a', name: 'a', status: 'active' }], [{ id: 'a', name: 'a', status: 'active', session_incarnation_id: '' }]])('rejects incomplete listing %j', async value => {
    respond(value)
    await expect(api.listSessions()).rejects.toThrow()
  })
  it('accepts a complete empty listing and explicitly unidentified legacy row', async () => {
    respond([])
    expect(await api.listSessions()).toEqual([])
    const row = { id: 'a', name: 'a', status: 'active', session_incarnation_id: null }
    respond([row])
    expect(await api.listSessions()).toEqual([row])
    respond([row, row])
    await expect(api.listSessions()).rejects.toThrow()
  })
  it('validates membership and encodes path/query values', async () => {
    const row = { id: 'a', name: 'a', status: 'active', session_incarnation_id: 'one' }
    respond({ session: row, terminals: [] })
    expect((await api.getSessionMembership('a/b', 'one&two')).terminals).toEqual([])
    expect(fetch).toHaveBeenCalledWith('/sessions/a%2Fb?view=membership&expected_incarnation_id=one%26two', expect.anything())
    for (const value of [null, { session: row }, { session: row, terminals: [{}] }, { session: row, terminals: [{ id: 't', session_incarnation_id: 'one' }, { id: 't', session_incarnation_id: 'one' }] }]) {
      respond(value)
      await expect(api.getSessionMembership('a', 'one')).rejects.toThrow()
    }
  })
  it('requires terminal identity but accepts absent, null, and unrecognized state', async () => {
    for (const status of [undefined, null, 'new-status']) {
      respond({ id: 't', session_name: 'a', session_incarnation_id: 'one', status })
      expect((await api.getTerminalStatusReading('t')).status).toBe(status)
    }
    respond({ id: 't', session_name: 'a', status: 'processing' })
    await expect(api.getTerminalStatusReading('t')).rejects.toThrow()
  })
})
