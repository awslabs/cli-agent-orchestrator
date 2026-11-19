// Regression tests for the two independent reviews. Each one failed before its fix; the
// describe title names the finding.
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'
import { busy, err, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

beforeEach(() => resetStub())

const card = (name: string) => screen.getByRole('article', { name: new RegExp(name) })

describe('F1 the CAO-down banner age keeps moving while the backend answers "unchanged"', () => {
  it('ages the snapshot from last_good_at on the page clock, not from snapshot_age_seconds', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
    try {
      vi.setSystemTime(new Date('2030-01-01T00:00:00Z'))
      const q = quiet()
      const down = {
        ...q,
        stale_snapshot: true,
        last_good_at: new Date(Date.parse(q.generated_at) - 190_000).toISOString(),
        snapshot_age_seconds: 190, // frozen: the backend answers "unchanged" from here on
        cao: { ...q.cao, reachable: false, error: { code: 'cao_unreachable', message: 'down', retryable: true } },
      }
      let n = 0
      mockBackend({ 'GET /shelf': () => ({ body: n++ === 0 ? down : { revision: down.revision, unchanged: true } }) })
      render(<App />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100)
      })
      expect(screen.getByRole('alert')).toHaveTextContent('3 min old')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2 * 3600 * 1000) // two hours of polls, every one a stub
      })
      expect(screen.getByRole('alert')).toHaveTextContent('2 h old')
      expect(screen.getByRole('alert')).not.toHaveTextContent('3 min old')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('F2 a session whose conductor was deleted', () => {
  const orphaned = () => {
    const d = busy()
    const s = d.sessions.find((x) => x.name === 'docs-refresh')!
    s.conductor_missing = true
    s.conductor = { ...s.conductor, role: 'helper', caller_id: 'deadbeef', orphan: true }
    return d
  }

  it('says there is no conductor, flags the first terminal as an orphan, and turns Ask off with a reason', async () => {
    mockBackend({ 'GET /shelf': orphaned() })
    render(<App />)
    await screen.findAllByRole('article')
    const c = card('docs-refresh')
    expect(c).toHaveTextContent('no conductor')
    expect(c).not.toHaveTextContent(/conductor \(/)
    expect(c).toHaveTextContent('caller no longer exists')
    expect(c).toHaveTextContent('ORPHAN')
    const ask = within(c).getByRole('button', { name: 'Ask' })
    expect(ask).toBeDisabled()
    expect(ask).toHaveAttribute('title', expect.stringContaining('no conductor'))
    expect(c).toHaveTextContent('Ask is off')
    // the other cards are untouched
    expect(within(card('payments-fix')).getByRole('button', { name: 'Ask' })).toBeEnabled()
  })

  it('a session with a conductor never says "no conductor"', async () => {
    mockBackend({ 'GET /shelf': busy() })
    render(<App />)
    await screen.findAllByRole('article')
    expect(card('payments-fix')).not.toHaveTextContent('no conductor')
  })
})

describe('F13 the subtitle says what the page is and claims nothing about conductors', () => {
  it('counts sessions and names the three questions the page answers', async () => {
    mockBackend({ 'GET /shelf': busy() })
    render(<App />)
    await screen.findAllByRole('article')
    expect(screen.getByText('12 CAO sessions at a glance: what needs you, what is working, what is parked.')).toBeInTheDocument()
    expect(screen.queryByText(/one conductor each/)).toBeNull()
  })
})

describe('F5 sessions with names the shelf cannot address are counted', () => {
  it('says how many are hidden and why', async () => {
    const d = quiet()
    d.counts.hidden_sessions = 2
    mockBackend({ 'GET /shelf': d })
    render(<App />)
    await screen.findAllByRole('article')
    expect(screen.getByText(/2 sessions hidden \(unsupported name\)/)).toBeInTheDocument()
  })

  it('says it in the singular, and also when nothing else is listed', async () => {
    const d = quiet()
    d.sessions = []
    d.counts = { ...d.counts, sessions: 0, hidden_sessions: 1 }
    mockBackend({ 'GET /shelf': d })
    render(<App />)
    expect(await screen.findByText(/1 session hidden \(unsupported name\)/)).toBeInTheDocument()
    expect(screen.getByText(/No CAO sessions yet/)).toBeInTheDocument()
  })

  it('says nothing when none are hidden', async () => {
    mockBackend({ 'GET /shelf': quiet() })
    render(<App />)
    await screen.findAllByRole('article')
    expect(screen.queryByText(/hidden \(unsupported name\)/)).toBeNull()
  })
})

describe('F6 an unreadable last_active is "age unknown", never "now"', () => {
  it('labels the card and the terminal', async () => {
    const d = quiet()
    const s = d.sessions.find((x) => x.name === 'payments-fix')!
    s.tier_reason = 'age_unknown'
    s.warm = false
    s.last_active = ''
    s.conductor.last_active = ''
    mockBackend({ 'GET /shelf': d })
    render(<App />)
    await screen.findAllByRole('article')
    const c = card('payments-fix')
    expect(c).toHaveTextContent('PARKED, age unknown')
    expect(c).toHaveTextContent('idle, age unknown')
    expect(c).not.toHaveTextContent('<1 min')
  })
})

describe('F8 Escape while stopping does not discard the result', () => {
  it('keeps the dialog until the result is shown, then Escape closes it', async () => {
    const user = userEvent.setup()
    let finish: (v: unknown) => void = () => {}
    const executing = new Promise((resolve) => (finish = resolve))
    const plan = {
      plan_id: 'p1',
      expires_at: '2026-11-15T12:05:00Z',
      truncated: false,
      sessions: [{ name: 'ci-flakes', tier: 'stale', last_active: '2026-11-10T08:00:00.000000Z', working: false, terminals: [{ id: 'aaaa0001', label: 'conductor', role: 'conductor', status: 'idle' }] }],
    }
    const b = mockBackend({
      'GET /shelf': quiet(),
      'POST /stop/plan': plan,
      'POST /stop/execute': async () => ({ body: await executing }),
    })
    render(<App />)
    await screen.findAllByRole('article')
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await user.click((await within(dlg).findAllByRole('checkbox'))[0])
    await user.click(within(dlg).getByRole('button', { name: /^Stop 1 session/ }))
    await waitFor(() => expect(b.writes().filter((w) => w.path === '/stop/execute')).toHaveLength(1))
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument() // still open: the stop is in flight
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await act(async () => finish({ results: [{ name: 'ci-flakes', outcome: 'stopped' }] }))
    expect(await within(screen.getByRole('dialog')).findByRole('list', { name: 'Results' })).toHaveTextContent('ci-flakes: stopped')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('F9 Ask says what CAO reported', () => {
  const askWith = async (delivery: string) => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet(), 'POST /sessions/payments-fix/ask': { accepted: true, terminal_id: 'b2ce9908', message_id: 7, delivery } })
    render(<App />)
    await screen.findAllByRole('article')
    const c = card('payments-fix')
    await user.click(within(c).getByRole('button', { name: 'Ask' }))
    await user.type(within(c).getByLabelText(/Message to the conductor/), 'how is it going{Enter}')
    return c
  }

  it('queued: not "Sent"', async () => {
    const c = await askWith('pending')
    expect(await within(c).findByText(/Message queued: the conductor of payments-fix is busy/)).toBeInTheDocument()
    expect(within(c).queryByRole('button', { name: 'Sent' })).toBeNull()
  })

  it('failed and unconfirmed are said as they are', async () => {
    expect(await within(await askWith('failed')).findByText(/CAO could not deliver the message/)).toBeInTheDocument()
  })

  it('unconfirmed', async () => {
    expect(await within(await askWith('unknown')).findByText(/delivery could not be confirmed/)).toBeInTheDocument()
  })
})

describe('F3 the Start dialog keeps one idempotency key across retries', () => {
  const profiles = { profiles: [{ name: 'developer', description: 'writes code', provider: 'claude_code' }] }

  it('sends the same key on a retry after a failure, and a new key from a new dialog', async () => {
    const user = userEvent.setup()
    let n = 0
    const b = mockBackend({
      'GET /shelf': quiet(),
      'GET /profiles': profiles,
      'POST /sessions': () => (n++ === 0 ? err(502, 'cao_timeout', 'CAO took too long') : { status: 201, body: { session: 'new-one', conductor_id: 'cafe0001' } }),
    })
    render(<App />)
    await screen.findAllByRole('article')
    const open = async () => {
      await user.click(screen.getByRole('button', { name: 'Start session' }))
      const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
      await within(dlg).findByRole('option', { name: /developer/ })
      await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work/area')
      return dlg
    }
    const dlg = await open()
    await user.click(within(dlg).getByRole('button', { name: 'Start' }))
    await within(dlg).findByRole('alert')
    await user.click(within(dlg).getByRole('button', { name: 'Start' })) // the retry
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const starts = b.writes().filter((w) => w.path === '/sessions')
    expect(starts).toHaveLength(2)
    expect(starts[0].body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
    expect(starts[1].body.idempotency_key).toBe(starts[0].body.idempotency_key)
    await open() // a new dialog is a new launch
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(b.writes().filter((w) => w.path === '/sessions')).toHaveLength(3))
    expect(b.writes().filter((w) => w.path === '/sessions')[2].body.idempotency_key).not.toBe(starts[0].body.idempotency_key)
  })
})

describe('L3 quick answers come from the backend, per provider', () => {
  it('an approval-looking last line of another provider is a text box, not Approve/Deny', async () => {
    mockBackend({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: 'claude_code', tail: ['APPROVAL_REQUIRED: push to origin?'], suggested: [] },
    })
    render(<App />)
    expect(await screen.findByLabelText(/Answer for tester/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })
})
