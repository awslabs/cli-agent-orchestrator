import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useShelf } from '../src/hooks/useShelf'
import { busy, mockBackend } from './helpers'
import { resetStub, stubState } from './sdk-stub'

function Probe({ onState }: { onState: (s: ReturnType<typeof useShelf>) => void }) {
  onState(useShelf())
  return null
}

let last: ReturnType<typeof useShelf>
const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
  document.dispatchEvent(new Event('visibilitychange'))
}
const tick = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('useShelf', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    resetStub()
    setHidden(false)
  })
  afterEach(() => {
    vi.useRealTimers()
    setHidden(false)
  })

  it('polls every 5 s and sends ?since= with the last revision', async () => {
    const b = mockBackend({ 'GET /shelf': () => ({ body: busy() }) })
    render(<Probe onState={(s) => (last = s)} />)
    await tick(10)
    expect(b.calls.map((c) => c.path)).toEqual(['/shelf'])
    expect(last.data?.counts.sessions).toBe(12)
    await tick(5000)
    expect(b.calls.at(-1)!.path).toBe('/shelf?since=1')
    await tick(5000)
    expect(b.calls).toHaveLength(3)
  })

  it('keeps the data when the backend answers unchanged', async () => {
    let n = 0
    mockBackend({ 'GET /shelf': () => ({ body: n++ === 0 ? busy() : { revision: 1, unchanged: true } }) })
    render(<Probe onState={(s) => (last = s)} />)
    await tick(10)
    await tick(5000)
    expect(last.data?.counts.sessions).toBe(12)
    expect(last.error).toBeNull()
  })

  it('makes no request while the document is hidden and resumes when visible', async () => {
    setHidden(true)
    const b = mockBackend({ 'GET /shelf': busy() })
    render(<Probe onState={(s) => (last = s)} />)
    await tick(20000)
    expect(b.calls).toHaveLength(0)
    setHidden(false)
    await tick(10)
    expect(b.calls).toHaveLength(1)
  })

  it('makes no request while useAppInfo().active is false', async () => {
    stubState.active = false
    const b = mockBackend({ 'GET /shelf': busy() })
    render(<Probe onState={(s) => (last = s)} />)
    await tick(20000)
    expect(b.calls).toHaveLength(0)
  })

  it('backs off after errors and recovers', async () => {
    let fail = true
    const b = mockBackend({ 'GET /shelf': () => (fail ? { status: 502, body: {} } : { body: busy() }) })
    render(<Probe onState={(s) => (last = s)} />)
    await tick(10)
    expect(last.error?.code).toBe('backend_unreachable')
    await tick(5000)
    expect(b.calls).toHaveLength(1) // backed off to 10 s
    await tick(5000)
    expect(b.calls).toHaveLength(2)
    fail = false
    await tick(20000)
    expect(last.error).toBeNull()
    expect(last.data).not.toBeNull()
  })

  it('stops polling on unmount', async () => {
    const b = mockBackend({ 'GET /shelf': busy() })
    const { unmount } = render(<Probe onState={(s) => (last = s)} />)
    await tick(10)
    unmount()
    await tick(30000)
    expect(b.calls).toHaveLength(1)
  })
})
