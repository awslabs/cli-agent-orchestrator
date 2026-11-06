// Workspace view rules that need no DOM: attention flags, split layout, and
// the live-terminal pool's lifecycle.
import { describe, it, expect, vi } from 'vitest'
import { nextAttention, strongest, summaryStatus } from '../workspace/attention'
import {
  EMPTY_LAYOUT,
  Layout,
  closePane,
  openInFocused,
  openInSplit,
  retainTerminals,
  setPaneCount,
} from '../workspace/layout'
import { DEFAULT_POOL_SIZE, TerminalPool } from '../workspace/terminalPool'
import { stepFontSize } from '../workspace/workspaceStore'
import type { XtermSession } from '../components/terminal/xtermSession'

describe('nextAttention', () => {
  it('flags a finished turn only on PROCESSING -> IDLE/COMPLETED while unviewed', () => {
    expect(nextAttention('PROCESSING', 'IDLE', undefined, false)).toBe('done')
    expect(nextAttention('PROCESSING', 'completed', undefined, false)).toBe('done')
    expect(nextAttention('IDLE', 'IDLE', undefined, false)).toBeUndefined()
    expect(nextAttention('PROCESSING', 'IDLE', undefined, true)).toBeUndefined()
  })

  it('does not flag what finished before the page first saw it', () => {
    expect(nextAttention(undefined, 'IDLE', undefined, false)).toBeUndefined()
    expect(nextAttention(undefined, 'ERROR', undefined, false)).toBeUndefined()
  })

  it('flags waiting immediately, even on first sight, and drops it once answered', () => {
    expect(nextAttention(undefined, 'waiting_user_answer', undefined, false)).toBe('waiting')
    expect(nextAttention('WAITING_USER_ANSWER', 'PROCESSING', 'waiting', false)).toBeUndefined()
  })

  it('flags a move into ERROR, and keeps an unread flag until viewed', () => {
    expect(nextAttention('PROCESSING', 'ERROR', undefined, false)).toBe('error')
    expect(nextAttention('IDLE', 'IDLE', 'done', false)).toBe('done')
    expect(nextAttention('IDLE', 'IDLE', 'done', true)).toBeUndefined()
  })

  it('summarises by severity', () => {
    expect(strongest(['done', undefined, 'error'])).toBe('error')
    expect(strongest(['done', 'waiting', 'error'])).toBe('waiting')
    expect(summaryStatus(['idle', 'processing'])).toBe('PROCESSING')
    expect(summaryStatus(['idle', 'waiting_user_answer', 'error'])).toBe('WAITING_USER_ANSWER')
    expect(summaryStatus([])).toBeNull()
  })
})

describe('layout', () => {
  it('opens in the focused pane, and focuses an already visible terminal instead of duplicating it', () => {
    let l = openInFocused(EMPTY_LAYOUT, 'a')
    expect(l.panes).toEqual(['a'])
    l = openInSplit(l, 'b')
    expect(l).toEqual({ count: 2, panes: ['a', 'b'], focused: 1 })
    l = openInFocused(l, 'a')
    expect(l).toEqual({ count: 2, panes: ['a', 'b'], focused: 0 })
  })

  it('grows 1 -> 2 -> 4 on split and replaces the next pane when full', () => {
    let l: Layout = openInFocused(EMPTY_LAYOUT, 'a')
    l = openInSplit(l, 'b')
    l = openInSplit(l, 'c')
    expect(l.count).toBe(4)
    expect(l.panes).toEqual(['a', 'b', 'c', null])
    l = openInSplit(l, 'd')
    l = openInSplit(l, 'e')
    expect(l.panes).toEqual(['e', 'b', 'c', 'd'])
    expect(l.focused).toBe(0)
  })

  it('shrinking keeps the focused terminal', () => {
    const l: Layout = { count: 4, panes: ['a', 'b', 'c', 'd'], focused: 2 }
    expect(setPaneCount(l, 1)).toEqual({ count: 1, panes: ['c'], focused: 0 })
    expect(setPaneCount(l, 2)).toEqual({ count: 2, panes: ['c', 'a'], focused: 0 })
  })

  it('closing collapses a split that no longer needs it', () => {
    const l: Layout = { count: 2, panes: ['a', 'b'], focused: 1 }
    expect(closePane(l, 1)).toEqual({ count: 1, panes: ['a'], focused: 0 })
    const four: Layout = { count: 4, panes: ['a', 'b', 'c', 'd'], focused: 0 }
    expect(closePane(four, 3)).toEqual({ count: 4, panes: ['a', 'b', 'c', null], focused: 3 })
  })

  it('clears panes whose terminal is gone', () => {
    const l: Layout = { count: 2, panes: ['a', 'b'], focused: 0 }
    expect(retainTerminals(l, new Set(['a']))).toEqual({ count: 2, panes: ['a', null], focused: 0 })
    expect(retainTerminals(l, new Set(['a', 'b']))).toBe(l)
  })
})

describe('TerminalPool', () => {
  function fakeFactory() {
    const made: Record<string, Omit<XtermSession, 'closed'> & { closed: boolean; dispose: ReturnType<typeof vi.fn> }> = {}
    const factory = vi.fn((id: string, _fontSize?: number) => {
      const s = { terminalId: id, element: document.createElement('div'), closed: false, fit: vi.fn(), focus: vi.fn(), setFontSize: vi.fn(), dispose: vi.fn() }
      made[id] = s
      return s
    })
    return { factory, made }
  }

  it('reuses a live session across detach/attach and moves its element', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 3)
    const a = document.createElement('div')
    const b = document.createElement('div')
    pool.attach('t1', a)
    expect(a.contains(made.t1.element)).toBe(true)
    pool.detach('t1', a)
    expect(a.contains(made.t1.element)).toBe(false)
    // Parked under the marker that stops xtermSession fitting (and so resizing
    // the agent's tmux window) while off screen.
    expect(made.t1.element.closest('[data-cao-parked]')).not.toBeNull()
    pool.attach('t1', b)
    expect(b.contains(made.t1.element)).toBe(true)
    expect(made.t1.dispose).not.toHaveBeenCalled()
    pool.disposeAll()
  })

  it('a stale detach from a pane the terminal already left is ignored', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 3)
    const a = document.createElement('div')
    const b = document.createElement('div')
    pool.attach('t1', a)
    pool.attach('t1', b)
    pool.detach('t1', a)
    expect(b.contains(made.t1.element)).toBe(true)
    pool.disposeAll()
  })

  it('evicts the least recently used off-screen session past the cap, never an on-screen one', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 2)
    const pane = document.createElement('div')
    const pane2 = document.createElement('div')
    pool.attach('t1', pane)
    pool.attach('t2', pane2)
    pool.detach('t1', pane)
    pool.attach('t3', pane) // t1 is the only off-screen session: it goes
    expect(made.t1.dispose).toHaveBeenCalled()
    expect(made.t2.dispose).not.toHaveBeenCalled()
    expect(pool.size()).toBe(2)
    pool.disposeAll()
  })

  it('never evicts an on-screen session, even when it is the least recently used', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 2)
    const p1 = document.createElement('div')
    const p2 = document.createElement('div')
    const p3 = document.createElement('div')
    pool.attach('t1', p1) // oldest, and stays on screen
    pool.attach('t2', p2)
    pool.detach('t2', p2)
    pool.attach('t3', p2) // over the cap: t2 is the only off-screen session
    expect(made.t2.dispose).toHaveBeenCalled()
    pool.attach('t4', p3) // over the cap with everything on screen: nothing goes
    expect(made.t1.dispose).not.toHaveBeenCalled()
    expect(made.t3.dispose).not.toHaveBeenCalled()
    expect(pool.size()).toBe(3)
    pool.disposeAll()
  })

  it('keeps at most six live terminals by default', () => {
    const { factory } = fakeFactory()
    const pool = new TerminalPool(factory)
    const pane = document.createElement('div')
    for (let i = 0; i < 9; i++) {
      pool.attach(`t${i}`, pane)
      pool.detach(`t${i}`, pane)
    }
    expect(DEFAULT_POOL_SIZE).toBe(6)
    expect(pool.size()).toBe(6)
    pool.disposeAll()
  })

  it('replaces a session whose socket closed, so showing it again reconnects', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 3)
    const pane = document.createElement('div')
    pool.attach('t1', pane)
    const first = made.t1
    pool.detach('t1', pane)
    first.closed = true

    pool.attach('t1', pane)

    expect(factory).toHaveBeenCalledTimes(2)
    expect(first.dispose).toHaveBeenCalled()
    expect(made.t1).not.toBe(first)
    expect(pane.contains(made.t1.element)).toBe(true)
    pool.disposeAll()
  })

  it('retain disposes sessions whose terminal no longer exists', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 4)
    pool.attach('t1', document.createElement('div'))
    pool.attach('t2', document.createElement('div'))
    pool.retain(new Set(['t2']))
    expect(made.t1.dispose).toHaveBeenCalled()
    expect(pool.has('t1')).toBe(false)
    expect(pool.has('t2')).toBe(true)
    pool.disposeAll()
  })

  it('setFontSize reaches live sessions and is passed to ones created later', () => {
    const { factory, made } = fakeFactory()
    const pool = new TerminalPool(factory, 4)
    pool.attach('t1', document.createElement('div'))
    pool.setFontSize(11)
    expect(made.t1.setFontSize).toHaveBeenCalledWith(11)
    pool.attach('t2', document.createElement('div'))
    expect(factory).toHaveBeenLastCalledWith('t2', 11)
    pool.disposeAll()
  })
})

describe('stepFontSize', () => {
  it('steps through the allowed sizes and clamps at both ends', () => {
    expect(stepFontSize(14, -1)).toBe(13)
    expect(stepFontSize(14, 1)).toBe(16)
    expect(stepFontSize(9, -1)).toBe(9)
    expect(stepFontSize(18, 1)).toBe(18)
    expect(stepFontSize(15, 1)).toBe(14) // unknown value resets to the default
  })
})
