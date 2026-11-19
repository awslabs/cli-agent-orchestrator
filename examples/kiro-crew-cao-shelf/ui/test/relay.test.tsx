import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { RELAY_RISK, RELAY_RISK_DETAIL } from '../src/chat'
import type { RelaySummary, ShelfRelay, ShelfResponse } from '../src/types'
import { err, mockBackend, quiet } from './helpers'
import { resetStub, stubState, withoutChatLauncher } from './sdk-stub'

beforeEach(() => resetStub())

const card = (name: string) => screen.getByRole('article', { name: new RegExp(`^session ${name},`) })

const summary = (over: Partial<RelaySummary> = {}): RelaySummary => ({
  relay_id: 'rc-aaaaaaaaaa',
  slot_key: 'cao-shelf-rc-aaaaaaaaaa',
  title: 'Remote control: payments-fix',
  session: 'payments-fix',
  conductor_id: 'b2ce9908',
  label: 'payments-fix',
  origin: 'attached',
  state: 'active',
  end_reason: null,
  created_at: '2026-11-15T12:00:00Z',
  updated_at: '2026-11-15T12:00:00Z',
  not_relayed: 0,
  permission_pending: false,
  ended_notice_pending: false,
  ...over,
})

const shelfRelay = (over: Partial<ShelfRelay> = {}): ShelfRelay => ({
  relay_id: 'rc-aaaaaaaaaa',
  slot_key: 'cao-shelf-rc-aaaaaaaaaa',
  title: 'Remote control: phone',
  state: 'active',
  label: 'phone',
  not_relayed: 0,
  permission_pending: false,
  ended_notice_pending: false,
  ...over,
})

function withRelay(relay: ShelfRelay, name = 'payments-fix'): ShelfResponse {
  const d = quiet()
  d.sessions.find((s) => s.name === name)!.relay = relay
  return d
}

const dialog = (name = 'payments-fix') => screen.getByRole('dialog', { name: `Remote control ${name}?` })

/** Remote control, then accept the stated risk (nothing is sent before that). */
async function remoteControl(user: ReturnType<typeof userEvent.setup>, name = 'payments-fix') {
  await user.click(within(card(name)).getByRole('button', { name: 'Remote control' }))
  const dlg = dialog(name)
  expect(dlg).toHaveTextContent(RELAY_RISK)
  await user.click(within(dlg).getByRole('button', { name: 'Accept and remote control' }))
  return dlg
}

async function boot(routes: Record<string, unknown>) {
  const b = mockBackend({ 'GET /shelf': quiet(), ...routes })
  render(<App />)
  await screen.findAllByRole('article')
  return b
}

describe('remote control on a card', () => {
  it('attaches with the session name, opens the chat by its slot key and says so', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /relays': () => ({ status: 201, body: summary() }) })
    await user.click(within(card('payments-fix')).getByRole('button', { name: 'Remote control' }))
    expect(b.writes()).toHaveLength(0) // the risk is stated first; nothing is sent yet
    // the risk is in a dialog, not in the card, and the card keeps one Remote control button
    expect(card('payments-fix')).not.toHaveTextContent(RELAY_RISK)
    expect(within(card('payments-fix')).getAllByRole('button', { name: 'Remote control' })).toHaveLength(1)
    await user.click(within(dialog()).getByRole('button', { name: 'Accept and remote control' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ method: 'POST', path: '/relays', body: { session: 'payments-fix', acknowledge_risk: true } })
    expect(Object.keys(b.writes()[0].body)).toEqual(['session', 'acknowledge_risk'])
    await waitFor(() => expect(stubState.opened).toEqual([{ slotKey: 'cao-shelf-rc-aaaaaaaaaa' }]))
    expect(screen.getByTestId('live-region')).toHaveTextContent('Remote control chat for payments-fix is open')
  })

  it('without the chat launcher it names the chat to open from the sidebar', async () => {
    withoutChatLauncher()
    const user = userEvent.setup()
    await boot({ 'POST /relays': () => ({ status: 201, body: summary() }) })
    const dlg = await remoteControl(user)
    expect(await within(dlg).findByRole('status')).toHaveTextContent('Open the chat "Remote control: payments-fix" from the Kiro Crew sidebar.')
    expect(stubState.opened).toEqual([])
    await waitFor(() => expect(within(dlg).getByRole('button', { name: 'Done' })).toHaveFocus())
  })

  it('is off with a reason when the session has no conductor', async () => {
    const d = quiet()
    const s = d.sessions.find((x) => x.name === 'docs-refresh')!
    s.conductor_missing = true
    s.conductor = { ...s.conductor, role: 'helper', caller_id: 'deadbeef', orphan: true }
    const b = await boot({ 'GET /shelf': d })
    const btn = within(card('docs-refresh')).getByRole('button', { name: 'Remote control' })
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('title', 'This session has no conductor, so there is nothing to remote control.')
    expect(b.writes()).toHaveLength(0)
  })

  it('a refusal shows the error code with a fixed sentence and never the backend text', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /relays': () => err(502, 'gateway_refused', 'SECRET backend words', { gateway_status: 403, gateway_code: 'session_approval_not_granted' }) })
    const dlg = await remoteControl(user)
    const alert = await within(dlg).findByRole('alert')
    expect(alert).toHaveTextContent('gateway_refused (session_approval_not_granted): Kiro Crew refused to create the relay chat.')
    expect(alert).not.toHaveTextContent('SECRET')
    expect(stubState.opened).toEqual([])
  })

  it('relay_exists from the backend offers Open chat and Take over and creates nothing', async () => {
    const user = userEvent.setup()
    const b = await boot({
      'POST /relays': () => err(409, 'relay_exists', 'x', { relay_id: 'rc-aaaaaaaaaa', label: 'laptop' }),
      'GET /relays': { relays: [summary({ label: 'laptop', slot_key: 'cao-shelf-rc-aaaaaaaaaa' })] },
    })
    const dlg = await remoteControl(user)
    expect(await within(dlg).findByText(/This session is already remote controlled from chat/)).toHaveTextContent('This session is already remote controlled from chat laptop.')
    expect(within(dlg).getByRole('button', { name: 'Take over' })).toBeInTheDocument()
    await user.click(within(dlg).getByRole('button', { name: 'Open chat' }))
    await waitFor(() => expect(stubState.opened).toEqual([{ slotKey: 'cao-shelf-rc-aaaaaaaaaa' }]))
    expect(b.writes().map((w) => w.path)).toEqual(['/relays'])
  })
})

describe('the stated risk', () => {
  it('is the contract text word for word, short text and detail', () => {
    const doc = readFileSync(resolve(process.cwd(), '../api-contract.md'), 'utf8')
    const m = /<!-- risk:relay -->\n```text\n([\s\S]*?)\n```/.exec(doc)
    expect(m?.[1]).toBe(RELAY_RISK)
    const d = /<!-- risk:relay-detail -->\n```text\n([\s\S]*?)\n```/.exec(doc)
    expect(d?.[1].split('\n\n')).toEqual([...RELAY_RISK_DETAIL])
  })

  it('the detail names Kiro Crew\'s wait tool beside the four CAO tools', () => {
    const [kas, claude, both] = RELAY_RISK_DETAIL
    expect(kas).toContain("Kiro Crew's wait tool, which only pauses the chat")
    expect(claude).toContain('(which only pauses the chat)')
    expect(both.endsWith('other than the four CAO tools and wait.')).toBe(true)
  })

  it('the dialog is named by its title, has the short text and the detail behind a disclosure, and opens on the X', async () => {
    const user = userEvent.setup()
    await boot({})
    const opener = within(card('payments-fix')).getByRole('button', { name: 'Remote control' })
    await user.click(opener)
    const dlg = dialog()
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('dialog', { name: 'Remote control payments-fix?' })).toBe(dlg)
    expect(within(dlg).getByRole('button', { name: 'Close' })).toHaveFocus()
    const details = dlg.querySelector('details')!
    expect(details.open).toBe(false)
    expect(within(dlg).getByText('What the chat can reach').tagName).toBe('SUMMARY')
    for (const p of RELAY_RISK_DETAIL) expect(details).toHaveTextContent(p)
    await user.click(within(dlg).getByText('What the chat can reach'))
    expect(details.open).toBe(true)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })

  it('Cancel on the risk dialog sends nothing and makes no chat', async () => {
    const user = userEvent.setup()
    const b = await boot({})
    await user.click(within(card('payments-fix')).getByRole('button', { name: 'Remote control' }))
    await user.click(within(dialog()).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes()).toHaveLength(0)
    expect(stubState.opened).toEqual([])
  })

  it('Escape on the risk dialog sends nothing', async () => {
    const user = userEvent.setup()
    const b = await boot({})
    await user.click(within(card('payments-fix')).getByRole('button', { name: 'Remote control' }))
    expect(dialog()).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes()).toHaveLength(0)
  })

  it('Accept sends acknowledge_risk once, also when pressed again while the request is pending', async () => {
    const user = userEvent.setup()
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    const b = await boot({
      'POST /relays': async () => {
        await gate
        return { status: 201, body: summary() }
      },
    })
    await user.click(within(card('payments-fix')).getByRole('button', { name: 'Remote control' }))
    const dlg = dialog()
    const accept = within(dlg).getByRole('button', { name: 'Accept and remote control' })
    await user.click(accept)
    // pending: one control, refusing presses but keeping focus, and no second Remote control button anywhere
    const pending = within(dlg).getByRole('button', { name: 'Setting up...' })
    expect(pending).toHaveAttribute('aria-disabled', 'true')
    expect(pending).toHaveFocus()
    expect(within(dlg).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.click(pending)
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument() // Escape waits for the outcome
    expect(screen.getAllByRole('button', { name: 'Remote control' })).toHaveLength(screen.getAllByRole('article').length)
    expect(within(card('payments-fix')).getAllByRole('button', { name: /remote control/i })).toHaveLength(1)
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(b.writes()).toHaveLength(1)
    expect(b.writes()[0].body).toEqual({ session: 'payments-fix', acknowledge_risk: true })
  })

  it('Remote control sits in the action row with Ask, History and Stop', async () => {
    await boot({})
    const row = within(card('payments-fix')).getByRole('button', { name: 'Ask' }).parentElement!
    expect(within(row).getByRole('button', { name: 'Remote control' })).toHaveClass('cs-btn')
    expect(within(row).getByRole('button', { name: /^History/ })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Stop...' })).toBeInTheDocument()
  })
})

describe('a card with a relay', () => {
  it('shows the badge with Open chat and End relay; the label is plain text', async () => {
    const user = userEvent.setup()
    const label = '<img src=x onerror=alert(1)>'
    const b = await boot({
      'GET /shelf': withRelay(shelfRelay({ label })),
      'POST /relays/rc-aaaaaaaaaa/end': { relay: summary({ state: 'ended', end_reason: 'ended_by_owner' }), chat: 'notified' },
    })
    const c = card('payments-fix')
    expect(c).toHaveTextContent(`Remote controlled from chat ${label}`)
    expect(c.querySelector('img')).toBeNull()
    await user.click(within(c).getByRole('button', { name: 'Open chat' }))
    expect(stubState.opened).toEqual([{ slotKey: 'cao-shelf-rc-aaaaaaaaaa' }])
    await user.click(within(c).getByRole('button', { name: 'End relay' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/relays/rc-aaaaaaaaaa/end', body: {} })
    expect(screen.getByTestId('live-region')).toHaveTextContent('Relay ended. The CAO session keeps working.')
  })

  it('says the chat has not been told yet while the record says the message is owed', async () => {
    await boot({ 'GET /shelf': withRelay(shelfRelay({ state: 'ended', ended_notice_pending: true })) })
    const c = card('payments-fix')
    expect(within(c).getByText(/the chat has not been told the relay ended yet: it keeps relaying until it is/)).toBeInTheDocument()
    expect(c).not.toHaveTextContent('Remote controlled from chat')
    expect(within(c).getByRole('button', { name: 'Remote control' })).toBeEnabled()
  })

  it('shows no "not told" notice once the record no longer owes the message', async () => {
    await boot({ 'GET /shelf': withRelay(shelfRelay({ ended_notice_pending: false })) })
    expect(card('payments-fix')).not.toHaveTextContent('has not been told')
  })

  it('End relay announces when Kiro Crew could not tell the chat yet', async () => {
    const user = userEvent.setup()
    await boot({
      'GET /shelf': withRelay(shelfRelay()),
      'POST /relays/rc-aaaaaaaaaa/end': { relay: summary({ state: 'ended', end_reason: 'ended_by_owner', ended_notice_pending: true }), chat: 'unreachable' },
    })
    await user.click(within(card('payments-fix')).getByRole('button', { name: 'End relay' }))
    await waitFor(() => expect(screen.getByTestId('live-region')).toHaveTextContent('has not been told the relay ended yet'))
  })

  it('Remote control on a relayed card asks first, and Take over replaces the chat', async () => {
    const user = userEvent.setup()
    const b = await boot({
      'GET /shelf': withRelay(shelfRelay()),
      'POST /relays/rc-aaaaaaaaaa/takeover': () => ({ status: 201, body: summary({ relay_id: 'rc-aaaaaaaaaa', slot_key: 'cao-shelf-rc-aaaaaaaaaa' }) }),
    })
    const c = card('payments-fix')
    await user.click(within(c).getByRole('button', { name: 'Remote control' }))
    const dlg = dialog()
    expect(dlg).toHaveTextContent('This session is already remote controlled from chat phone. Take over ends that relay and makes a new relay chat.')
    expect(dlg).toHaveTextContent(RELAY_RISK)
    expect(within(dlg).getByRole('button', { name: 'Close' })).toHaveFocus()
    expect(b.writes()).toHaveLength(0)
    await user.click(within(dlg).getByRole('button', { name: 'Take over' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/relays/rc-aaaaaaaaaa/takeover', body: { acknowledge_risk: true } })
    await waitFor(() => expect(stubState.opened).toEqual([{ slotKey: 'cao-shelf-rc-aaaaaaaaaa' }]))
  })

  it('Cancel on the take-over prompt sends nothing', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'GET /shelf': withRelay(shelfRelay()) })
    const c = card('payments-fix')
    await user.click(within(c).getByRole('button', { name: 'Remote control' }))
    await user.click(within(dialog()).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Take over' })).toBeNull()
    expect(b.writes()).toHaveLength(0)
  })

  it('shows "N not relayed" and a pending-request notice that never says to approve', async () => {
    await boot({ 'GET /shelf': withRelay(shelfRelay({ not_relayed: 2, permission_pending: true })) })
    const c = card('payments-fix')
    expect(c).toHaveTextContent('2 not relayed')
    expect(c).toHaveTextContent('A tool request is waiting in the relay chat. Deny it unless you expect it.')
    // the full reason sits behind "Why", closed at first
    const why = within(c).getByText('Why')
    expect(why.tagName).toBe('SUMMARY')
    expect((why.parentElement as HTMLDetailsElement).open).toBe(false)
    expect(c).toHaveTextContent('never ask on the kas backend, nor on claude with the allow rule')
    expect(c).toHaveTextContent('so deny this request unless you are on claude, have not added that rule yet')
    expect(c).not.toHaveTextContent(/approve each request/i)
    // five tools never ask: the four CAO tools and Kiro Crew's wait tool
    expect(c).toHaveTextContent('wait tool')
    expect(c).toHaveTextContent('one of those five tools')
    expect(c).not.toHaveTextContent('four CAO tools never ask')
    // the short sentence matches one element only (Playwright strict mode in test_j9)
    expect(within(c).getAllByText(/^A tool request is waiting in the relay chat\. Deny it/)).toHaveLength(1)
  })

  it('a relay that needs re-attaching says why and offers Remote control again', async () => {
    const user = userEvent.setup()
    const b = await boot({
      'GET /shelf': withRelay(shelfRelay({ state: 'needs_reattach' })),
      'POST /relays': () => ({ status: 201, body: summary({ relay_id: 'rc-bbbbbbbbbb', slot_key: 'cao-shelf-rc-bbbbbbbbbb' }) }),
    })
    const c = card('payments-fix')
    expect(c).toHaveTextContent("Remote control stopped: this session's conductor changed.")
    expect(within(c).queryByRole('button', { name: 'End relay' })).toBeNull()
    await remoteControl(user)
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/relays', body: { session: 'payments-fix', acknowledge_risk: true } })
  })
})

describe('start and remote control (5.2)', () => {
  const profiles = { profiles: [{ name: 'developer', description: 'writes code' }] }

  it('goes to /relays with the start fields and no first message, and retries with the same key', async () => {
    const user = userEvent.setup()
    let attempts = 0
    const b = await boot({
      'GET /profiles': profiles,
      'POST /relays': () => {
        attempts += 1
        return attempts === 1
          ? err(503, 'gateway_unavailable', 'x')
          : { status: 201, body: summary({ session: 'fresh', origin: 'started_by_relay', slot_key: 'cao-shelf-rc-cccccccccc', title: 'Remote control: fresh' }) }
      },
    })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    await within(dlg).findByRole('option', { name: /developer/ })
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
    await user.type(within(dlg).getByLabelText('First message (optional)'), 'hello')
    await user.click(within(dlg).getByRole('checkbox', { name: 'and remote control it' }))
    expect(within(dlg).queryByLabelText('First message (optional)')).toBeNull()
    expect(dlg).toHaveTextContent('Your first message goes through the relay chat')
    await user.type(within(dlg).getByLabelText('Session name (optional)'), 'fresh')
    // the risk is stated (short text, detail behind the disclosure), and Start stays off until it is accepted
    expect(dlg).toHaveTextContent(RELAY_RISK)
    expect(within(dlg).getByText('What the chat can reach').tagName).toBe('SUMMARY')
    expect(within(dlg).getByRole('checkbox', { name: 'I accept this risk' })).toHaveAccessibleDescription(RELAY_RISK)
    expect(within(dlg).getByRole('button', { name: 'Start' })).toBeDisabled()
    await user.click(within(dlg).getByRole('checkbox', { name: 'I accept this risk' }))
    await user.click(within(dlg).getByRole('button', { name: 'Start' }))
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('gateway_unavailable')
    await user.click(within(dlg).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(b.writes()).toHaveLength(2))
    const [first, second] = b.writes()
    expect(first.path).toBe('/relays')
    expect(Object.keys(first.body)).toEqual(['start', 'acknowledge_risk'])
    expect(first.body.acknowledge_risk).toBe(true)
    expect(Object.keys(first.body.start).sort()).toEqual(['agent_profile', 'idempotency_key', 'name', 'working_directory'])
    expect(first.body.start).toMatchObject({ agent_profile: 'developer', working_directory: '/work', name: 'fresh' })
    expect(second.body).toEqual(first.body)
    expect(first.body.start.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
    await waitFor(() => expect(stubState.opened).toEqual([{ slotKey: 'cao-shelf-rc-cccccccccc' }]))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(b.writes().some((w) => w.path === '/sessions')).toBe(false)
  })

  it('unticked, Start still goes to /sessions as before', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'GET /profiles': profiles, 'POST /sessions': { status: 201, body: { session: 'x', conductor_id: 'cafe0001' } } })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog')
    await within(dlg).findByRole('option', { name: /developer/ })
    expect(within(dlg).getByRole('checkbox', { name: 'and remote control it' })).not.toBeChecked()
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/w')
    await user.click(within(dlg).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0].path).toBe('/sessions')
  })
})
