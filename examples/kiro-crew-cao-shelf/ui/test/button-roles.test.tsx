// Button roles per surface and the host components each surface is built from (design sections 1
// and 3, with Gandalf ruling 4: every suggested answer is secondary). Roles are read from the host
// Btn's classes, which the stub copies: busy-cue-primary, busy-cue-danger, and the page's own
// cs-quiet. `data-kit` is the stub's name for the host component that rendered an element.
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import type { ShelfRelay, ShelfResponse } from '../src/types'
import { busy, err, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

beforeEach(() => resetStub())

type Role = 'primary' | 'danger' | 'quiet' | 'secondary'

function roleOf(el: HTMLElement): Role {
  if (el.classList.contains('busy-cue-primary')) return 'primary'
  if (el.classList.contains('busy-cue-danger')) return 'danger'
  if (el.classList.contains('cs-quiet')) return 'quiet'
  return 'secondary'
}

/** The element is a host Btn in this role; danger buttons also carry the guide's caution marker. */
function expectRole(el: HTMLElement, role: Role) {
  expect(el, el.textContent ?? '').toHaveAttribute('data-kit', 'Btn')
  expect(roleOf(el), el.textContent ?? '').toBe(role)
  if (role === 'danger') expect(el).toHaveAttribute('data-guide-caution', '')
  else expect(el).not.toHaveAttribute('data-guide-caution')
}

const primaries = (el: HTMLElement) => el.querySelectorAll('.busy-cue-primary')
const card = (name: string) => screen.getByRole('article', { name: new RegExp(`^session ${name},`) })
const btn = (scope: HTMLElement, name: string | RegExp) => within(scope).getByRole('button', { name })

const prompt = (suggested: Array<{ label: string; send: string }> = [{ label: 'Approve push', send: 'y' }, { label: 'Deny', send: 'n' }]) => ({
  terminal_id: 'b94da5c6',
  status: 'waiting_user_answer',
  provider: 'mock_cli',
  tail: ['APPROVAL_REQUIRED: push to origin? [y/n]'],
  suggested,
})

const relay = (over: Partial<ShelfRelay> = {}): ShelfRelay => ({
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

function withRelay(r: ShelfRelay, name = 'payments-fix'): ShelfResponse {
  const d = quiet()
  d.sessions.find((s) => s.name === name)!.relay = r
  return d
}

async function boot(routes: Record<string, unknown>) {
  const b = mockBackend({ 'GET /shelf': quiet(), ...routes })
  const r = render(<App />)
  await screen.findAllByRole('article')
  return { ...b, ...r }
}

/** No element anywhere carries the page's old hand-made button classes. */
function expectNoOldButtonClasses() {
  expect(document.body.querySelectorAll('.cs-b, .cs-g, .cs-rd, .cs-rd-solid')).toHaveLength(0)
}

describe('page header and empty states', () => {
  it('the header holds the one primary, Start session', async () => {
    await boot({})
    const header = screen.getByTestId('page-header')
    expectRole(btn(header, 'Start session'), 'primary')
    expect(primaries(header)).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Start session' })).toHaveLength(1)
    expectNoOldButtonClasses()
  })

  it('with zero sessions, Start session is the empty state\'s one primary and the header has none', async () => {
    mockBackend({ 'GET /shelf': { ...quiet(), sessions: [] } })
    render(<App />)
    const empty = await screen.findByTestId('empty-state')
    expect(empty).toHaveAttribute('data-kit', 'EmptyState')
    expect(screen.getAllByRole('button', { name: 'Start session' })).toHaveLength(1)
    expectRole(btn(screen.getByTestId('empty-state-action'), 'Start session'), 'primary')
    expect(within(screen.getByTestId('page-header')).queryByRole('button', { name: 'Start session' })).toBeNull()
  })

  it('"Cannot reach CAO" is the host empty state with Open Settings as its primary', async () => {
    const q = quiet()
    mockBackend({ 'GET /shelf': { ...q, sessions: [], cao: { ...q.cao, reachable: false, error: { code: 'cao_unreachable', message: 'x', retryable: true } } } })
    render(<App />)
    const empty = await screen.findByTestId('empty-state')
    expect(empty).toHaveAttribute('data-kit', 'EmptyState')
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('Cannot reach CAO.')
    expectRole(btn(screen.getByTestId('empty-state-action'), 'Open Settings'), 'primary')
  })

  it('"Cannot reach the backend" is the host empty state with Try again as its primary', async () => {
    mockBackend({ 'GET /shelf': () => err(502, 'backend_unreachable', 'no answer') })
    render(<App />)
    const action = await screen.findByTestId('empty-state-action')
    expectRole(btn(action, 'Try again'), 'primary')
  })

  it('loading shows host Skeletons that keep the cs-skel class', async () => {
    mockBackend({ 'GET /shelf': () => new Promise(() => {}) })
    const { container } = render(<App />)
    await waitFor(() => expect(container.querySelectorAll('.cs-skel').length).toBeGreaterThan(0))
    for (const s of container.querySelectorAll('.cs-skel')) expect(s).toHaveAttribute('data-slot', 'skeleton')
  })

  it('an unreachable CAO with a snapshot is a host ErrorNotice', async () => {
    const q = quiet()
    mockBackend({ 'GET /shelf': { ...q, stale_snapshot: true, cao: { ...q.cao, reachable: false, error: { code: 'cao_unreachable', message: 'x', retryable: true } } } })
    render(<App />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveAttribute('data-kit', 'ErrorNotice')
    expect(alert).toHaveTextContent('CAO is not reachable')
  })
})

describe('tier row and page footer', () => {
  it('tier toggles are secondary host buttons with aria-pressed in the labelled group', async () => {
    await boot({})
    const group = screen.getByRole('group', { name: 'Tiers to show' })
    const tiers = within(group).getAllByRole('button')
    expect(tiers).toHaveLength(4)
    for (const t of tiers) {
      expectRole(t, 'secondary')
      expect(t).toHaveAttribute('aria-pressed')
    }
  })

  it('Clean up stale is danger; All history and Settings are secondary', async () => {
    await boot({})
    expectRole(screen.getByRole('button', { name: /^Clean up stale \(\d+\)/ }), 'danger')
    expectRole(screen.getByRole('button', { name: /^All history/ }), 'secondary')
    expectRole(screen.getByRole('button', { name: 'Settings' }), 'secondary')
  })
})

describe('a session card', () => {
  it('is a host Card with Ask, History and Remote control secondary and Stop... danger, and no primary', async () => {
    await boot({})
    const c = card('payments-fix')
    expect(c).toHaveAttribute('data-kit', 'Card')
    expect(c).toHaveClass('cs-card', 'cs-k-parked')
    expectRole(btn(c, 'Ask'), 'secondary')
    expectRole(btn(c, /^History/), 'secondary')
    expectRole(btn(c, 'Remote control'), 'secondary')
    expect(btn(c, 'Remote control')).toHaveAttribute('aria-haspopup', 'dialog')
    expectRole(btn(c, 'Stop...'), 'danger')
    expect(primaries(c)).toHaveLength(0)
  })

  it('the four card actions are the items of one .cs-acts grid, Stop... included', async () => {
    await boot({})
    const c = card('payments-fix')
    const acts = btn(c, 'Ask').parentElement!
    expect(acts).toHaveClass('cs-acts')
    for (const name of [/^History/, 'Remote control', 'Stop...']) expect(btn(c, name).parentElement).toBe(acts)
  })

  it('the header row holds the title toggle and the tier Badge, outside the toggle', async () => {
    await boot({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt() })
    const c = card('payments-fix')
    const badge = within(c).getByText('NEEDS YOU')
    expect(badge).toHaveAttribute('data-kit', 'Badge')
    const head = badge.parentElement!
    expect(head).toHaveClass('cs-head')
    const title = head.querySelector('button.cs-ttl')!
    expect(title).not.toBeNull()
    expect(title.contains(badge)).toBe(false)
    expect(title).toHaveAttribute('aria-expanded')
  })

  it.each([
    ['payments-fix', 'NEEDS YOU', ['bg-warn-subtle', 'text-warn-text']],
    ['docs-refresh', 'LIVE', ['bg-ok-subtle', 'text-ok-text']],
    ['migrate-db', 'PARKED', ['bg-[var(--bg-hover)]', 'text-[var(--muted)]']],
  ])('the %s tier Badge uses the text-safe variant', async (name, text, classes) => {
    await boot({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt() })
    const badge = within(card(name)).getByText(new RegExp(`^${text}`))
    expect(badge).toHaveAttribute('data-kit', 'Badge')
    expect(badge).toHaveClass(...classes)
  })

  it('a Live card has no primary button', async () => {
    await boot({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt() })
    expect(primaries(card('docs-refresh'))).toHaveLength(0)
  })

  it('helper chips sit in one wrapping .cs-chips row, and ORPHAN is an err Badge', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt() })
    await user.click(screen.getByRole('button', { name: /Stale, over 48 h/ }))
    const c = card('search-tuning')
    const chip = c.querySelector('.cs-chip-orphan')!
    expect(chip.parentElement).toHaveClass('cs-chips')
    const orphan = within(c).getByText('ORPHAN')
    expect(orphan).toHaveAttribute('data-kit', 'Badge')
    expect(orphan).toHaveClass('bg-danger-subtle', 'text-danger-text')
  })
})

describe('the answer box', () => {
  it('every suggested answer is secondary, and Show prompt is quiet (ruling 4)', async () => {
    mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt() })
    render(<App />)
    const c = await screen.findByRole('article', { name: /^session payments-fix,/ })
    const approve = await within(c).findByRole('button', { name: 'Approve push' })
    expectRole(approve, 'secondary')
    expectRole(btn(c, 'Deny'), 'secondary')
    expectRole(btn(c, 'Show prompt'), 'quiet')
    expect(primaries(c)).toHaveLength(0)
    expect(c.querySelectorAll('.busy-cue-danger')).toHaveLength(1) // Stop... only
  })

  it('with no suggestions, Send answer is the card\'s one primary and the box is a host Input', async () => {
    mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt([]) })
    render(<App />)
    const c = await screen.findByRole('article', { name: /^session payments-fix,/ })
    expect(await within(c).findByLabelText('Answer for tester')).toHaveAttribute('data-kit', 'Input')
    expectRole(btn(c, 'Send answer'), 'primary')
    expect(primaries(c)).toHaveLength(1)
  })

  it('a failed answer is a host ErrorNotice inside the card', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt(), 'POST /terminals/b94da5c6/answer': () => err(409, 'not_waiting', 'no', { status: 'idle' }) })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Approve push' }))
    const alert = await within(card('payments-fix')).findByRole('alert')
    expect(alert).toHaveAttribute('data-kit', 'ErrorNotice')
  })
})

describe('ask form and settings', () => {
  it('Ask opens a host Input and Send is the one primary on the card', async () => {
    const user = userEvent.setup()
    await boot({})
    const c = card('payments-fix')
    const ask = btn(c, 'Ask')
    await user.click(ask)
    expect(ask).toHaveAttribute('aria-expanded', 'true')
    expect(within(c).getByLabelText('Message to the conductor of payments-fix')).toHaveAttribute('data-kit', 'Input')
    expectRole(btn(c, 'Send'), 'primary')
    expect(primaries(c)).toHaveLength(1)
  })

  it('Settings is a host Card with host Inputs, a Toggle, Save primary and Close secondary', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet(), 'GET /config': { cao_url: 'http://127.0.0.1:9889', poll_seconds: 5, warm_hours: 1, stale_hours: 48, persist_history: true } })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Settings' }))
    const form = await screen.findByRole('form', { name: 'Settings' })
    const panel = form.closest('[data-kit="Card"]') as HTMLElement
    expect(panel).not.toBeNull()
    expect(await within(form).findByLabelText('CAO address')).toHaveAttribute('data-kit', 'Input')
    expect(within(form).getByRole('switch')).toHaveAttribute('data-kit', 'Toggle')
    expectRole(btn(form, 'Save'), 'primary')
    expectRole(btn(panel, 'Close'), 'secondary')
    expect(primaries(panel)).toHaveLength(1)
  })
})

describe('a card with a relay', () => {
  it('Open chat is secondary and End relay is danger', async () => {
    await boot({ 'GET /shelf': withRelay(relay()) })
    const c = card('payments-fix')
    expectRole(btn(c, 'Open chat'), 'secondary')
    expectRole(btn(c, 'End relay'), 'danger')
    expect(primaries(c)).toHaveLength(0)
  })

  it('Dismiss on a relay that needs re-attaching is secondary', async () => {
    await boot({ 'GET /shelf': withRelay(relay({ state: 'needs_reattach' })) })
    expectRole(btn(card('payments-fix'), 'Dismiss'), 'secondary')
  })

  it('the pending-request notice is an amber cs-notice with a native disclosure', async () => {
    await boot({ 'GET /shelf': withRelay(relay({ permission_pending: true })) })
    const c = card('payments-fix')
    const notice = c.querySelector('.cs-notice[data-tone="warn"]')!
    expect(notice).not.toBeNull()
    expect(notice).toHaveTextContent('A tool request is waiting in the relay chat. Deny it unless you expect it.')
    expect(notice.querySelector('details.cs-disc > summary')).toHaveTextContent('Why')
  })

  it.each([
    ['no relay', null],
    ['an active relay', relay()],
    ['a relay that needs re-attaching', relay({ state: 'needs_reattach' })],
    ['an ended relay whose chat is not told yet', relay({ state: 'ended', ended_notice_pending: true })],
  ])('with %s the card has exactly one Remote control button', async (_n, r) => {
    await boot({ 'GET /shelf': r ? withRelay(r) : quiet() })
    expect(within(card('payments-fix')).getAllByRole('button', { name: /remote control/i })).toHaveLength(1)
  })
})

describe('dialogs', () => {
  it('Remote control: Accept is the one primary, Cancel secondary, in a .cs-actions footer', async () => {
    const user = userEvent.setup()
    await boot({})
    await user.click(btn(card('payments-fix'), 'Remote control'))
    const dlg = screen.getByRole('dialog', { name: 'Remote control payments-fix?' })
    expect(dlg).toHaveAttribute('data-kit', 'Modal')
    expectRole(btn(dlg, 'Accept and remote control'), 'primary')
    expectRole(btn(dlg, 'Cancel'), 'secondary')
    expect(primaries(dlg)).toHaveLength(1)
    expect(btn(dlg, 'Cancel').parentElement).toHaveClass('cs-actions')
    expect(within(dlg).queryAllByRole('button', { name: 'Remote control' })).toHaveLength(0)
    expect(dlg.querySelector('details.cs-disc > summary')).toHaveTextContent('What the chat can reach')
  })

  it('Take over is the one primary in the take-over dialog', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /shelf': withRelay(relay()) })
    await user.click(btn(card('payments-fix'), 'Remote control'))
    const dlg = screen.getByRole('dialog', { name: 'Remote control payments-fix?' })
    expectRole(btn(dlg, 'Take over'), 'primary')
    expectRole(btn(dlg, 'Cancel'), 'secondary')
    expect(primaries(dlg)).toHaveLength(1)
  })

  it('Start: Start is the one primary; Profile is the host Select, the text fields host Inputs', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /profiles': { profiles: [{ name: 'developer', description: 'writes code' }] } })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    expect(within(dlg).getByRole('combobox', { name: 'Profile' })).toHaveAttribute('data-kit', 'Select')
    for (const l of ['Provider (optional)', 'Working directory (required)', 'Session name (optional)']) expect(within(dlg).getByLabelText(l)).toHaveAttribute('data-kit', 'Input')
    expect(within(dlg).getByLabelText('First message (optional)').tagName).toBe('TEXTAREA')
    expectRole(btn(dlg, 'Start'), 'primary')
    expectRole(btn(dlg, 'Cancel'), 'secondary')
    expect(primaries(dlg)).toHaveLength(1)
    for (const box of within(dlg).getAllByRole('checkbox')) expect(box).toHaveClass('cs-check')
  })

  it('Clean up: Select all quiet, Cancel secondary, Stop N danger, and no primary', async () => {
    const user = userEvent.setup()
    const plan = {
      plan_id: 'plan-1',
      expires_at: '2026-11-15T12:05:00Z',
      sessions: [{ name: 'infra-audit', tier: 'stale', last_active: '2026-11-10T08:00:00.000000Z', working: true, terminals: [{ id: 'aaaa0000', label: 'conductor', role: 'conductor', status: 'idle' }] }],
    }
    await boot({ 'POST /stop/plan': plan })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    const dlg = await screen.findByRole('dialog', { name: 'Clean up stale sessions' })
    await within(dlg).findAllByRole('checkbox')
    expectRole(btn(dlg, 'Select all'), 'quiet')
    expectRole(btn(dlg, 'Cancel'), 'secondary')
    expectRole(btn(dlg, /^Stop \d+ session/), 'danger')
    expect(primaries(dlg)).toHaveLength(0)
    // "Heads up" is a host ErrorNotice
    const heads = within(dlg).getByRole('alert')
    expect(heads).toHaveAttribute('data-kit', 'ErrorNotice')
    expect(heads).toHaveTextContent('Heads up:')
    expectNoOldButtonClasses()
  })
})
