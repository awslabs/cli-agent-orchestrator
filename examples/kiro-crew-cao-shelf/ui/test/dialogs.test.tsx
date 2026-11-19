import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { busy, err, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

const prompt = (suggested: Array<{ label: string; send: string }> = [{ label: 'Approve push', send: 'y' }, { label: 'Deny', send: 'n' }]) => ({
  terminal_id: 'b94da5c6',
  status: 'waiting_user_answer',
  provider: 'mock_cli',
  tail: ['APPROVAL_REQUIRED: push to origin? [y/n]'],
  suggested,
})

const stalePlan = (names: string[], extra: Record<string, unknown> = {}) => ({
  plan_id: 'plan-1',
  expires_at: '2026-11-15T12:05:00Z',
  sessions: names.map((n, i) => ({
    name: n,
    tier: 'stale',
    last_active: `2026-11-1${i}T08:00:00.000000Z`,
    terminals: [
      { id: 'aaaa000' + i, label: 'conductor', role: 'conductor', status: 'idle' },
      { id: 'bbbb000' + i, label: 'coder-' + i, role: 'helper', status: 'idle' },
    ],
    working: false,
  })),
  ...extra,
})

beforeEach(() => resetStub())

async function boot(routes: Record<string, unknown>) {
  const b = mockBackend({ 'GET /shelf': quiet(), ...routes })
  render(<App />)
  await screen.findAllByRole('article')
  return b
}

describe('ask (AC5)', () => {
  it('AC5.1 opens a labelled box, sends to /sessions/{name}/ask with the conductor id the user saw', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /sessions/payments-fix/ask': { accepted: true, terminal_id: 'b2ce9908', message_id: 1, delivery: 'delivered' } })
    const card = screen.getByRole('article', { name: /payments-fix/ })
    await user.click(within(card).getByRole('button', { name: 'Ask' }))
    const box = within(card).getByLabelText('Message to the conductor of payments-fix')
    expect(box).toHaveFocus()
    await user.type(box, 'status please{Enter}')
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ method: 'POST', path: '/sessions/payments-fix/ask', body: { message: 'status please', conductor_id: 'b2ce9908' } })
    expect(Object.keys(b.writes()[0].body).sort()).toEqual(['conductor_id', 'message'])
    expect(await within(card).findByRole('button', { name: 'Sent' })).toBeInTheDocument()
    expect(within(card).getByRole('status')).toHaveTextContent('Message delivered to the conductor of payments-fix.')
  })

  it('AC5.1 Escape closes the form without sending', async () => {
    const user = userEvent.setup()
    const b = await boot({})
    const card = screen.getByRole('article', { name: /payments-fix/ })
    await user.click(within(card).getByRole('button', { name: 'Ask' }))
    await user.type(within(card).getByLabelText(/Message to the conductor/), 'x{Escape}')
    expect(within(card).queryByLabelText(/Message to the conductor/)).toBeNull()
    expect(b.writes()).toHaveLength(0)
  })

  it('AC5.2 tells the user the session changed and nothing was sent', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /sessions/payments-fix/ask': () => err(409, 'conflict_changed', 'changed') })
    const card = screen.getByRole('article', { name: /payments-fix/ })
    await user.click(within(card).getByRole('button', { name: 'Ask' }))
    await user.type(within(card).getByLabelText(/Message to the conductor/), 'hi{Enter}')
    expect(await within(card).findByRole('alert')).toHaveTextContent('This session changed, reload')
    expect(within(card).getByRole('alert')).toHaveTextContent('Nothing was sent')
  })

  it('shows the backend message for other failures inline', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /sessions/payments-fix/ask': () => err(502, 'cao_unreachable', 'CAO refused the connection') })
    const card = screen.getByRole('article', { name: /payments-fix/ })
    await user.click(within(card).getByRole('button', { name: 'Ask' }))
    await user.type(within(card).getByLabelText(/Message to the conductor/), 'hi{Enter}')
    expect(await within(card).findByRole('alert')).toHaveTextContent('CAO refused the connection')
  })
})

describe('answer (AC6)', () => {
  const ready = (extra: Record<string, unknown> = {}) => ({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt(), ...extra })

  it('AC6.1 loads the prompt tail behind a disclosure, as text', async () => {
    const user = userEvent.setup()
    mockBackend({ ...ready(), 'GET /terminals/b94da5c6/prompt': { ...prompt(), tail: ['<script>alert(1)</script>', 'push to origin?'] } })
    const { container } = render(<App />)
    const card = await screen.findByRole('article', { name: /payments-fix/ })
    expect(await within(card).findByText(/push to origin\?/)).toBeInTheDocument()
    expect(within(card).queryByText(/<script>/)).toBeNull()
    await user.click(within(card).getByRole('button', { name: 'Show prompt' }))
    expect(within(card).getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
  })

  it('AC6.1 quick buttons only when suggested; otherwise a text box and Send answer', async () => {
    const user = userEvent.setup()
    const b = mockBackend({ ...ready(), 'GET /terminals/b94da5c6/prompt': prompt([]), 'POST /terminals/b94da5c6/answer': { delivered: true, terminal_id: 'b94da5c6' } })
    render(<App />)
    const card = await screen.findByRole('article', { name: /payments-fix/ })
    expect(within(card).queryByRole('button', { name: 'Approve push' })).toBeNull()
    await user.type(await within(card).findByLabelText('Answer for tester'), 'go ahead')
    await user.click(within(card).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/terminals/b94da5c6/answer', body: { answer: 'go ahead', expect_status: 'waiting_user_answer' } })
  })

  it('AC6.3 an answer goes only to /terminals/{id}/answer, never an inbox or message route', async () => {
    const user = userEvent.setup()
    const b = mockBackend({ ...ready(), 'POST /terminals/b94da5c6/answer': { delivered: true, terminal_id: 'b94da5c6' } })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Approve push' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ method: 'POST', path: '/terminals/b94da5c6/answer', body: { answer: 'y', expect_status: 'waiting_user_answer' } })
    expect(b.calls.every((c) => !/inbox|message|ask/.test(c.path))).toBe(true)
  })

  it('AC6.2 a terminal that is no longer waiting says so', async () => {
    const user = userEvent.setup()
    mockBackend({ ...ready({ 'POST /terminals/b94da5c6/answer': () => err(409, 'not_waiting', 'no', { status: 'idle' }) }) })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Approve push' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('tester is no longer waiting. Nothing was sent.')
  })

  it('actions are disabled while CAO is unreachable', async () => {
    const d = busy()
    d.cao = { ...d.cao, reachable: false, error: { code: 'cao_unreachable', message: 'x', retryable: true } }
    d.stale_snapshot = true
    mockBackend({ ...ready({ 'GET /shelf': d }) })
    render(<App />)
    expect(await screen.findByRole('button', { name: 'Approve push' })).toBeDisabled()
  })
})

describe('start (AC7)', () => {
  const profiles = { profiles: [{ name: 'developer', description: 'writes code', provider: 'claude_code' }, { name: 'supervisor' }] }

  it('AC7.1 sends nothing until Start; required cwd; optional fields omitted', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'GET /profiles': profiles, 'POST /sessions': { status: 201, body: { session: 'new-one', conductor_id: 'cafe0001' } } })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    await within(dlg).findByRole('option', { name: /developer/ })
    expect(b.writes()).toHaveLength(0)
    const startBtn = within(dlg).getByRole('button', { name: 'Start' })
    expect(startBtn).toBeDisabled()
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work/area')
    expect(startBtn).toBeEnabled()
    await user.click(startBtn)
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/sessions', body: { agent_profile: 'developer', working_directory: '/work/area' } })
    expect(Object.keys(b.writes()[0].body).sort()).toEqual(['agent_profile', 'idempotency_key', 'working_directory'])
    expect(b.writes()[0].body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('AC7.1 Cancel and Escape send nothing; a bad name blocks Start', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'GET /profiles': profiles })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog')
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/w')
    await user.type(within(dlg).getByLabelText('Session name (optional)'), 'bad name!')
    expect(within(dlg).getByRole('button', { name: 'Start' })).toBeDisabled()
    // the form holds input, so Escape is refused (design section 4, guardAccidentalDismiss); Cancel closes
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await user.click(within(dlg).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes()).toHaveLength(0)
  })

  it('AC7.2 shows the CAO reason on failure and keeps the dialog', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /profiles': profiles, 'POST /sessions': () => err(502, 'cao_error', 'CAO said: profile not found') })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog')
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/w')
    await user.click(within(dlg).getByRole('button', { name: 'Start' }))
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('CAO said: profile not found')
  })
})

describe('stop and clean up (AC8)', () => {
  it('AC8.1 clean-up lists every session and terminal, nothing ticked, red button disabled', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /stop/plan': stalePlan(['infra-audit', 'ci-flakes']) })
    await user.click(screen.getByRole('button', { name: /Clean up stale \(9\)/ }))
    const dlg = await screen.findByRole('dialog', { name: 'Clean up stale sessions' })
    const boxes = await within(dlg).findAllByRole('checkbox')
    expect(boxes).toHaveLength(2)
    for (const c of boxes) expect(c).not.toBeChecked()
    expect(dlg).toHaveTextContent('stops 2 terminals: conductor, coder-0')
    expect(within(dlg).getByRole('button', { name: /^Stop 0 sessions/ })).toBeDisabled()
    // opening makes the plan call only, nothing else
    expect(b.writes().map((w) => w.path)).toEqual(['/stop/plan'])
    expect(b.writes()[0].body).toEqual({ scope: 'stale' })
  })

  it('AC8.1 the red button names the count and enables only once something is ticked', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': stalePlan(['infra-audit', 'ci-flakes']) })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await user.click((await within(dlg).findAllByRole('checkbox'))[0])
    const red = within(dlg).getByRole('button', { name: 'Stop 1 session (2 terminals)' })
    expect(red).toBeEnabled()
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    expect(within(dlg).getByRole('button', { name: /^Stop 0 sessions/ })).toBeDisabled()
  })

  it('Cancel and Escape send nothing beyond the plan, and focus returns to the opener', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /stop/plan': stalePlan(['infra-audit']) })
    const opener = screen.getByRole('button', { name: /Clean up stale/ })
    await user.click(opener)
    const dlg = await screen.findByRole('dialog')
    expect(within(dlg).getByRole('button', { name: 'Close' })).toHaveFocus()
    await user.click(within(dlg).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
    await user.click(opener)
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
    expect(b.writes().filter((w) => w.path === '/stop/execute')).toHaveLength(0)
  })

  it('traps Tab inside the dialog', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': stalePlan(['infra-audit']) })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await within(dlg).findAllByRole('checkbox')
    for (let i = 0; i < 8; i++) {
      await user.tab()
      expect(dlg.contains(document.activeElement)).toBe(true)
    }
    for (let i = 0; i < 8; i++) {
      await user.tab({ shift: true })
      expect(dlg.contains(document.activeElement)).toBe(true)
    }
  })

  it('AC8.2 warns in red, naming each working session', async () => {
    const user = userEvent.setup()
    const plan = stalePlan(['infra-audit', 'ci-flakes'])
    plan.sessions[1].working = true
    await boot({ 'POST /stop/plan': plan })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    expect(await within(dlg).findByText(/Heads up:/)).toBeInTheDocument()
    expect(within(dlg).getByRole('alert')).toHaveTextContent('ci-flakes is working or waiting')
    expect(within(dlg).getByRole('alert')).not.toHaveTextContent('infra-audit')
  })

  it('AC8.3 shows the truncation notice', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': stalePlan(['a-one', 'b-two'], { truncated: true }) })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    expect(await screen.findByText(/Only the first 2 stale sessions/)).toBeInTheDocument()
  })

  it('AC8.5 execute sends exactly the ticked sessions with the plan id, and no other field', async () => {
    const user = userEvent.setup()
    const plan = stalePlan(['infra-audit', 'ci-flakes', 'data-backfill'])
    const b = await boot({ 'POST /stop/plan': plan, 'POST /stop/execute': { results: [{ name: 'ci-flakes', outcome: 'stopped' }] } })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    const boxes = await within(dlg).findAllByRole('checkbox')
    await user.click(boxes[1])
    await user.click(within(dlg).getByRole('button', { name: 'Stop 1 session (2 terminals)' }))
    await waitFor(() => expect(b.writes().filter((w) => w.path === '/stop/execute')).toHaveLength(1))
    const body = b.writes().find((w) => w.path === '/stop/execute')!.body
    expect(body).toEqual({ plan_id: 'plan-1', sessions: [{ name: 'ci-flakes', last_active: plan.sessions[1].last_active }] })
    // results panel, then Done
    expect(await within(dlg).findByRole('list', { name: 'Results' })).toHaveTextContent('ci-flakes: stopped')
    await user.click(within(dlg).getByRole('button', { name: 'Done' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('AC8.5 "Select all" ticks only the listed rows and exists only in clean-up', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /stop/plan': stalePlan(['infra-audit', 'ci-flakes']), 'POST /stop/execute': { results: [] } })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await user.click(await within(dlg).findByRole('button', { name: 'Select all' }))
    expect(within(dlg).getAllByRole('checkbox').every((c) => (c as HTMLInputElement).checked)).toBe(true)
    await user.click(within(dlg).getByRole('button', { name: 'Stop 2 sessions (4 terminals)' }))
    await waitFor(() => expect(b.writes().filter((w) => w.path === '/stop/execute')).toHaveLength(1))
    expect(b.writes().at(-1)!.body.sessions.map((s: any) => s.name)).toEqual(['infra-audit', 'ci-flakes'])
    expect(JSON.stringify(b.writes().at(-1)!.body)).not.toMatch(/all|\*/)
  })

  it('per-card Stop uses scope "named", has no Select all, nothing pre-ticked, and warns for a working session', async () => {
    const user = userEvent.setup()
    const plan = {
      plan_id: 'p9',
      expires_at: '2026-11-15T12:05:00Z',
      sessions: [{ name: 'docs-refresh', tier: 'live', last_active: '2026-11-15T12:00:00.000000Z', working: true, terminals: [{ id: 'b8ff08af', label: 'writer-1', role: 'helper', status: 'processing' }] }],
    }
    const b = mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt(), 'POST /stop/plan': plan, 'POST /stop/execute': { results: [{ name: 'docs-refresh', outcome: 'stopped' }] } })
    render(<App />)
    const card = await screen.findByRole('article', { name: /docs-refresh/ })
    await user.click(within(card).getByRole('button', { name: 'Stop...' }))
    const dlg = await screen.findByRole('dialog', { name: 'Stop session docs-refresh?' })
    expect(b.writes()[0].body).toEqual({ scope: 'named', sessions: ['docs-refresh'] })
    const box = await within(dlg).findByRole('checkbox', { name: /docs-refresh/ })
    expect(box).not.toBeChecked()
    expect(within(dlg).queryByRole('button', { name: 'Select all' })).toBeNull()
    expect(dlg).toHaveTextContent('Heads up: docs-refresh is working')
    await user.click(box)
    // working: an explicit acknowledgement is needed as well
    const red = within(dlg).getByRole('button', { name: /^Stop 1 session/ })
    expect(red).toBeDisabled()
    await user.click(within(dlg).getByRole('checkbox', { name: /throws away work/ }))
    await user.click(red)
    await waitFor(() => expect(b.writes().at(-1)!.path).toBe('/stop/execute'))
    expect(b.writes().at(-1)!.body.sessions).toEqual([{ name: 'docs-refresh', last_active: '2026-11-15T12:00:00.000000Z', acknowledge_working: true }])
  })

  it('reports skipped sessions in the results panel and plan_expired as a plain message', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': stalePlan(['infra-audit', 'ci-flakes']), 'POST /stop/execute': { results: [{ name: 'infra-audit', outcome: 'skipped_changed' }, { name: 'ci-flakes', outcome: 'failed', message: 'cao_unreachable' }] } })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await user.click(await within(dlg).findByRole('button', { name: 'Select all' }))
    await user.click(within(dlg).getByRole('button', { name: /^Stop 2/ }))
    expect(await within(dlg).findByText(/skipped, it changed/)).toBeInTheDocument()
    expect(dlg).toHaveTextContent('failed (cao_unreachable)')
  })

  it('plan_expired on execute asks the user to reopen', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': stalePlan(['infra-audit']), 'POST /stop/execute': () => err(409, 'plan_expired', 'old') })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    await user.click((await within(dlg).findAllByRole('checkbox'))[0])
    await user.click(within(dlg).getByRole('button', { name: /^Stop 1/ }))
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('This list expired')
  })

  it('a plan error leaves Stop disabled and says why', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/plan': () => err(502, 'cao_unreachable', 'CAO refused the connection') })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog')
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('CAO refused the connection')
    expect(within(dlg).getByRole('button', { name: /^Stop 0/ })).toBeDisabled()
  })
})
