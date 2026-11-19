import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'
import { backendPayload, busy, history, mockBackend, quiet } from './helpers'
import { resetStub, stubState } from './sdk-stub'

const prompt = {
  terminal_id: 'b94da5c6',
  status: 'waiting_user_answer',
  provider: 'mock_cli',
  tail: ['working...', 'APPROVAL_REQUIRED: push to origin? [y/n]'],
  suggested: [
    { label: 'Approve push', send: 'y' },
    { label: 'Deny', send: 'n' },
  ],
}

async function renderApp(routes: Record<string, unknown>) {
  const b = mockBackend(routes)
  const r = render(<App />)
  await waitFor(() => expect(b.calls.length).toBeGreaterThan(0))
  return { ...b, ...r }
}

beforeEach(() => resetStub())

describe('quiet day (P1)', () => {
  it('AC2.1 says nothing needs you, nothing is working, parked shows 3 cards, stale is hidden', async () => {
    await renderApp({ 'GET /shelf': quiet() })
    expect(await screen.findByText('Nothing needs you.')).toBeInTheDocument()
    expect(screen.getByText('Nothing is working.')).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(3)
    const stale = screen.getByRole('button', { name: /Stale, over 48 h/ })
    expect(stale).toHaveTextContent('Stale, over 48 h 9 (hidden)')
    expect(stale).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByRole('heading', { name: /^Stale/ })).toBeNull()
  })

  it('AC2.2 the honest line is built from counts.by_status', async () => {
    await renderApp({ 'GET /shelf': quiet() })
    const line = await screen.findByText(/Idle is not running/)
    expect(line.textContent).toBe('20 terminals: 0 processing, 0 waiting for you, 20 idle. Idle is not running.')
  })

  it('AC2.3 "running" is never a status label', async () => {
    const { container } = await renderApp({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt })
    await screen.findAllByRole('article')
    const text = container.textContent ?? ''
    // only the honest sentence may contain the word
    expect(text.replace('Idle is not running.', '')).not.toMatch(/running/i)
  })

  it('AC3.3 shows idle age words, never a status word "running", and a hollow dot for idle', async () => {
    const { container } = await renderApp({ 'GET /shelf': quiet() })
    await screen.findAllByRole('article')
    expect(container.querySelector('.cs-dot-idle')).not.toBeNull()
    expect(container.textContent).toMatch(/idle 2 h/)
  })

  it('shows the stale tier when its toggle is pressed and remembers it', async () => {
    const user = userEvent.setup()
    const { unmount } = await renderApp({ 'GET /shelf': quiet() })
    await user.click(await screen.findByRole('button', { name: /Stale, over 48 h/ }))
    expect(screen.getAllByRole('article')).toHaveLength(12)
    expect(screen.getByRole('button', { name: /Stale, over 48 h/ })).toHaveTextContent('Stale, over 48 h 9')
    expect(localStorage.getItem('cao-shelf:tiers')).toContain('"stale":true')
    unmount()
    mockBackend({ 'GET /shelf': quiet() })
    render(<App />)
    expect(await screen.findAllByRole('article')).toHaveLength(12)
  })
})

describe('busy day (P1)', () => {
  const routes = () => ({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt })

  it('AC1.1 first section is NEEDS YOU with payments-fix, the asker, about 4 min and quick answers', async () => {
    await renderApp(routes())
    const first = (await screen.findAllByRole('heading', { level: 2 }))[0]
    expect(first).toHaveTextContent('Needs you (1)')
    const card = screen.getByRole('article', { name: 'session payments-fix, needs you' })
    expect(within(card).getByText('NEEDS YOU')).toBeInTheDocument()
    expect(within(card).getByText('tester', { selector: 'b' })).toBeInTheDocument()
    expect(card.textContent).toMatch(/about 4 min/)
    expect(await within(card).findByRole('button', { name: 'Approve push' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Deny' })).toBeInTheDocument()
  })

  it('AC1.2 the nav badge shows the NEEDS YOU count', async () => {
    await renderApp(routes())
    await screen.findAllByRole('article')
    expect(stubState.badge.at(-1)).toBe(1)
  })

  it('AC1.2 the nav badge clears at zero', async () => {
    await renderApp({ 'GET /shelf': quiet() })
    await screen.findAllByRole('article')
    expect(stubState.badge.at(-1)).toBe(0)
  })

  it('AC1.3 announces a session that enters NEEDS YOU without moving focus', async () => {
    let n = 0
    const q = quiet()
    const b = busy()
    mockBackend({ 'GET /shelf': () => ({ body: n++ === 0 ? q : { ...b, revision: 2 } }), 'GET /terminals/b94da5c6/prompt': prompt })
    const user = userEvent.setup()
    render(<App />)
    await screen.findAllByRole('article')
    const btn = screen.getByRole('button', { name: /Parked/ })
    btn.focus()
    expect(screen.getByTestId('live-region')).toHaveTextContent('')
    // next poll
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(screen.getByTestId('live-region')).toHaveTextContent('payments-fix needs you'))
    expect(document.activeElement).toBe(btn)
    void user
  })

  it('AC3.1 shows the conductor as title bar with status and idle age, helpers as chips', async () => {
    await renderApp(routes())
    const card = await screen.findByRole('article', { name: 'session migrate-db, parked' })
    expect(card).toHaveTextContent('conductor (supervisor)')
    expect(card).toHaveTextContent('idle 17 h')
    const pay = screen.getByRole('article', { name: 'session payments-fix, needs you' })
    const chips = within(within(pay).getByRole('list', { name: 'helpers of payments-fix' })).getAllByRole('listitem')
    expect(chips.map((c) => c.textContent)).toEqual(['coder-1processing', 'testerwaiting about 4 min', 'reviewerwaiting about 4 min'.replace('waiting about 4 min', 'idle 6 min')])
  })

  it('AC3.1 expanding a card shows terminals with status, last_active and caller_id', async () => {
    const user = userEvent.setup()
    await renderApp(routes())
    const pay = await screen.findByRole('article', { name: 'session payments-fix, needs you' })
    await user.click(within(pay).getByRole('button', { name: /payments-fix/ }))
    const table = within(pay).getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(5)
    expect(table).toHaveTextContent('b94da5c6')
    expect(table).toHaveTextContent('none')
    expect(within(pay).getByRole('button', { name: /payments-fix/ })).toHaveAttribute('aria-expanded', 'true')
  })

  it('AC3.1 caps chips at 5 and shows +N', async () => {
    const d = busy()
    const s = d.sessions.find((x) => x.name === 'payments-fix')!
    const h = s.helpers[0]
    s.helpers = Array.from({ length: 8 }, (_, i) => ({ ...h, id: `aaaa000${i}`, label: `w-${i}` }))
    await renderApp({ 'GET /shelf': d, 'GET /terminals/b94da5c6/prompt': prompt })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    expect(within(within(pay).getByRole('list', { name: /helpers/ })).getAllByRole('listitem')).toHaveLength(6)
    expect(within(pay).getByRole('button', { name: /3 more helpers/ })).toHaveTextContent('+3')
  })

  it('AC3.1 in-flight line is worded honestly', async () => {
    await renderApp(routes())
    const doc = await screen.findByRole('article', { name: 'session docs-refresh, live' })
    expect(doc).toHaveTextContent('conductor working, waiting on helper writer-1')
    expect(doc.textContent).not.toMatch(/handoff/i)
  })

  it('AC3.3 stale cards carry the dimming class and full colour on focus is handled in CSS', async () => {
    const user = userEvent.setup()
    await renderApp(routes())
    await user.click(await screen.findByRole('button', { name: /Stale, over 48 h/ }))
    expect(screen.getByRole('article', { name: 'session ci-flakes, stale' })).toHaveClass('cs-k-stale')
    expect(screen.getByRole('article', { name: 'session migrate-db, parked' })).toHaveClass('cs-k-parked')
  })

  it('error status is a red NEEDS YOU card without an answer box', async () => {
    const d = busy()
    const s = d.sessions[0]
    s.tier_reason = 'error'
    s.asks = [{ ...s.asks[0], status: 'error' }]
    s.helpers[1].status = 'error'
    await renderApp({ 'GET /shelf': d })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    expect(pay).toHaveClass('cs-err')
    expect(pay).toHaveTextContent('NEEDS YOU, error')
    expect(pay).toHaveTextContent('tester reported an error')
    expect(within(pay).queryByRole('button', { name: 'Approve push' })).toBeNull()
  })
})

describe('trouble (AC9)', () => {
  const unreachable = (d: any, code = 'cao_unreachable') => ({
    ...d,
    stale_snapshot: true,
    // the page ages the snapshot from last_good_at on its own clock; snapshot_age_seconds is not printed
    last_good_at: new Date(Date.parse(d.generated_at) - 190_000).toISOString(),
    snapshot_age_seconds: 190,
    cao: { ...d.cao, reachable: false, error: { code, message: 'down', retryable: true } },
  })

  it('AC9.1 shows a banner with the snapshot age, dims cards and disables actions', async () => {
    await renderApp({ 'GET /shelf': unreachable(quiet()) })
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('CAO is not reachable at http://127.0.0.1:9889')
    expect(banner).toHaveTextContent('3 min old')
    expect(screen.getAllByRole('article').length).toBe(3)
    for (const b of screen.getAllByRole('button', { name: 'Ask' })) expect(b).toBeDisabled()
    for (const b of screen.getAllByRole('button', { name: 'Stop...' })) expect(b).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Start session' })).toBeDisabled()
    expect(screen.queryByText(/No CAO sessions/)).toBeNull()
  })

  it('AC9.1 with no snapshot it does NOT say "no sessions"', async () => {
    const q = quiet()
    await renderApp({
      'GET /shelf': { ...q, sessions: [], stale_snapshot: false, history_total: null, cao: { ...q.cao, reachable: false, error: { code: 'cao_unreachable', message: 'down', retryable: true } } },
    })
    expect(await screen.findByText(/Cannot reach CAO at http:\/\/127\.0\.0\.1:9889\. Start it with/)).toBeInTheDocument()
    expect(screen.queryByText(/No CAO sessions/)).toBeNull()
    expect(screen.getByTestId('empty-state-subtitle')).toHaveTextContent('Start it with cao-server')
  })

  // payloads the backend itself served for these failures (tests/test_cao_shelf_poller.py writes and pins them)
  const served = (name: string) => backendPayload(name)

  it('AC9.2 explains an auth-enabled CAO on the first read, and does not say "no sessions"', async () => {
    await renderApp({ 'GET /shelf': served('auth-first-read') })
    expect(await screen.findByText('This CAO needs a sign-in.')).toBeInTheDocument()
    expect(screen.getByTestId('empty-state-subtitle')).toHaveTextContent(/authentication/)
    expect(screen.queryByText(/No CAO sessions/)).toBeNull()
  })

  it('AC9.2 a 401 after good reads keeps the old cards under a banner, actions off', async () => {
    await renderApp({ 'GET /shelf': served('auth-after-good-reads') })
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent(/authentication/)
    expect(banner).toHaveTextContent(/cannot sign in/)
    for (const b of screen.getAllByRole('button', { name: 'Ask' })) expect(b).toBeDisabled()
    for (const b of screen.getAllByRole('button', { name: 'Stop...' })) expect(b).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Start session' })).toBeDisabled()
  })

  it('names the problem for an incompatible CAO on the first read', async () => {
    await renderApp({ 'GET /shelf': served('incompatible-first-read') })
    expect(await screen.findByTestId('empty-state-subtitle')).toHaveTextContent('/sessions is not a list')
    expect(screen.queryByText(/No CAO sessions/)).toBeNull()
  })

  it('names another CAO error after good reads, actions off', async () => {
    await renderApp({ 'GET /shelf': served('error-after-good-reads') })
    expect(await screen.findByRole('alert')).toHaveTextContent('CAO answered 500')
    expect(screen.getByRole('button', { name: 'Start session' })).toBeDisabled()
    for (const b of screen.getAllByRole('button', { name: 'Ask' })) expect(b).toBeDisabled()
  })

  it('AC9.3 does not poll while hidden', async () => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    const b = mockBackend({ 'GET /shelf': quiet() })
    render(<App />)
    await new Promise((r) => setTimeout(r, 30))
    expect(b.calls).toHaveLength(0)
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  })

  it('the backend itself being down is its own empty state, not "no sessions"', async () => {
    await renderApp({ 'GET /shelf': () => ({ status: 502, body: {} }) })
    expect(await screen.findByText('Cannot reach the CAO Shelf backend.')).toBeInTheDocument()
    expect(screen.queryByText(/No CAO sessions/)).toBeNull()
  })

  it('zero sessions with CAO reachable shows the Start button empty state', async () => {
    const q = quiet()
    await renderApp({ 'GET /shelf': { ...q, sessions: [], counts: { ...q.counts, sessions: 0, terminals: 0 } } })
    expect(await screen.findByText('No CAO sessions yet.')).toBeInTheDocument()
  })

  it('shows skeleton cards while loading', () => {
    mockBackend({ 'GET /shelf': () => new Promise(() => {}) })
    const { container } = render(<App />)
    expect(container.querySelectorAll('.cs-skel')).toHaveLength(3)
  })
})

describe('history and orphans (P2)', () => {
  it('AC3.2 flags an orphan with a dashed chip, the ORPHAN text and a caller-gone note; peers are marked', async () => {
    const user = userEvent.setup()
    await renderApp({ 'GET /shelf': busy() })
    await user.click(await screen.findByRole('button', { name: /Stale, over 48 h/ }))
    const orphanCard = screen.getByRole('article', { name: 'session search-tuning, stale' })
    expect(within(orphanCard).getByText('ORPHAN')).toBeInTheDocument()
    expect(orphanCard.querySelector('.cs-chip-orphan')).not.toBeNull()
    expect(orphanCard).toHaveTextContent('probe-1: caller no longer exists')
    expect(screen.getByRole('article', { name: 'session onboarding-flow, stale' })).toHaveTextContent('(peer)')
  })

  it('AC4.1 opens the history drawer with helper <id4>, rows and "last message N ago"', async () => {
    const user = userEvent.setup()
    const b = await renderApp({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': prompt,
      'GET /sessions/payments-fix/history': history('busy', 'payments-fix'),
    })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    await user.click(within(pay).getByRole('button', { name: 'History (3)' }))
    expect(await within(pay).findByText('rebuilt from inbox rows; their terminals no longer exist')).toBeInTheDocument()
    expect(pay).toHaveTextContent('helper b996')
    expect(pay).toHaveTextContent('3 rows, last message 2 h ago')
    expect(b.calls.some((c) => c.path === '/sessions/payments-fix/history')).toBe(true)
  })

  it('AC4.1 "inbox rows" fetches up to three rows, shown as text', async () => {
    const user = userEvent.setup()
    await renderApp({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': prompt,
      'GET /sessions/payments-fix/history': history('busy', 'payments-fix'),
      'GET /sessions/payments-fix/history/b9963ace/rows': { rows: [{ id: 1, created_at: '2026-11-15T10:24:00+00:00', status: 'delivered', text: 'DONE <b>bold</b>' }] },
    })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    await user.click(within(pay).getByRole('button', { name: 'History (3)' }))
    await user.click((await within(pay).findAllByRole('button', { name: 'inbox rows' }))[0])
    expect(await within(pay).findByText(/DONE <b>bold<\/b>/)).toBeInTheDocument()
    expect(pay.querySelector('b')?.textContent).not.toBe('bold')
  })

  it('AC4.2 says older helpers may be missing when the window was full', async () => {
    const user = userEvent.setup()
    await renderApp({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': prompt,
      'GET /sessions/payments-fix/history': { ...history('busy', 'payments-fix'), complete: false },
    })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    await user.click(within(pay).getByRole('button', { name: 'History (3)' }))
    expect(await within(pay).findByText(/older closed helpers may be missing/)).toBeInTheDocument()
  })

  it('AC4.3 lists external senders separately from helpers', async () => {
    const user = userEvent.setup()
    await renderApp({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': prompt,
      'GET /sessions/payments-fix/history': { ...history('busy', 'payments-fix'), external_senders: [{ sender_id: 'cao-ops-mcp', rows: 2 }] },
    })
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    await user.click(within(pay).getByRole('button', { name: 'History (3)' }))
    expect(await within(pay).findByText(/Other senders .*cao-ops-mcp \(2 rows\)/)).toBeInTheDocument()
    expect(pay).not.toHaveTextContent('helper cao-')
  })

  it('shows "none recorded" for a session without closed helpers', async () => {
    const user = userEvent.setup()
    await renderApp({
      'GET /shelf': quiet(),
      'GET /sessions/onboarding-flow/history': history('quiet', 'onboarding-flow'),
    })
    await user.click(await screen.findByRole('button', { name: /Stale, over 48 h/ }))
    const c = screen.getByRole('article', { name: /onboarding-flow/ })
    await user.click(within(c).getByRole('button', { name: /History/ }))
    expect(await within(c).findByText('none recorded')).toBeInTheDocument()
  })

  it('All history lists newest 8 and "and N older"', async () => {
    const user = userEvent.setup()
    const closed = Array.from({ length: 20 }, (_, i) => ({ session: 's' + i, terminal_id: (0x10000000 + i).toString(16), rows: 2, last_at: `2026-11-15T${String(10 - (i % 10)).padStart(2, '0')}:00:00+00:00` }))
    await renderApp({ 'GET /shelf': quiet(), 'GET /history': { closed, complete: true } })
    await user.click(await screen.findByRole('button', { name: /All history \(20 closed helpers\)/ }))
    const region = await screen.findByRole('region', { name: 'All history' })
    await waitFor(() => expect(within(region).getAllByRole('listitem')).toHaveLength(8))
    expect(region).toHaveTextContent('and 12 older')
  })
})

describe('ages keep moving while the backend answers "unchanged"', () => {
  it('moves the idle words on from last_active, with no new shelf body, and ignores a skewed browser clock', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    try {
      vi.setSystemTime(new Date('2030-01-01T00:00:00Z')) // a browser clock years away from the backend's
      let n = 0
      const b = mockBackend({
        'GET /shelf': () => ({ body: n++ === 0 ? quiet() : { revision: quiet().revision, unchanged: true } }),
      })
      render(<App />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100)
      })
      const card = () => screen.getByRole('article', { name: /payments-fix/ })
      expect(card().textContent).toMatch(/idle 2 h/)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3 * 3600 * 1000) // three hours of polls, every one a stub
      })
      expect(b.calls.filter((c) => c.path.startsWith('/shelf')).length).toBeGreaterThan(100)
      expect(card().textContent).toMatch(/idle 5 h/)
      expect(card().textContent).not.toMatch(/idle 2 h/)
    } finally {
      vi.useRealTimers()
    }
  })
})
