// The three dialogs on the host Modal (design sections 3 and 4, Gandalf rulings 1, 2, 3 and 6):
// focus starts on the host X ("Close") and never on a primary or danger button, Tab wraps, Escape
// and a backdrop click close a dialog unless a request is in flight or the Start form holds input,
// end states say "Done", and Start waits for a profile.
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import type { RelaySummary } from '../src/types'
import { mockBackend, quiet } from './helpers'
import { resetStub, withoutChatLauncher } from './sdk-stub'

beforeEach(() => resetStub())

const card = (name: string) => screen.getByRole('article', { name: new RegExp(`^session ${name},`) })
const backdrop = () => document.querySelector('[data-modal-backdrop]') as HTMLElement
const profiles = { profiles: [{ name: 'developer', description: 'writes code' }, { name: 'supervisor' }] }

const plan = (names: string[]) => ({
  plan_id: 'plan-1',
  expires_at: '2026-11-15T12:05:00Z',
  sessions: names.map((n, i) => ({
    name: n,
    tier: 'stale',
    last_active: `2026-11-1${i}T08:00:00.000000Z`,
    terminals: [{ id: 'aaaa000' + i, label: 'conductor', role: 'conductor', status: 'idle' }],
    working: false,
  })),
})

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

async function boot(routes: Record<string, unknown> = {}) {
  const b = mockBackend({ 'GET /shelf': quiet(), 'GET /profiles': profiles, 'POST /stop/plan': plan(['infra-audit', 'ci-flakes']), ...routes })
  render(<App />)
  await screen.findAllByRole('article')
  return b
}

type Opened = { opener: HTMLElement; dlg: HTMLElement }
const OPEN: Record<string, (u: ReturnType<typeof userEvent.setup>) => Promise<Opened>> = {
  'Remote control': async (user) => {
    const opener = within(card('payments-fix')).getByRole('button', { name: 'Remote control' })
    await user.click(opener)
    return { opener, dlg: screen.getByRole('dialog', { name: 'Remote control payments-fix?' }) }
  },
  'Stop session': async (user) => {
    const opener = within(card('payments-fix')).getByRole('button', { name: 'Stop...' })
    await user.click(opener)
    const dlg = await screen.findByRole('dialog', { name: /^Stop session/ })
    return { opener, dlg }
  },
  'Clean up': async (user) => {
    const opener = screen.getByRole('button', { name: /Clean up stale/ })
    await user.click(opener)
    const dlg = await screen.findByRole('dialog', { name: 'Clean up stale sessions' })
    await within(dlg).findAllByRole('checkbox')
    return { opener, dlg }
  },
  Start: async (user) => {
    const opener = screen.getByRole('button', { name: 'Start session' })
    await user.click(opener)
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    await within(dlg).findByRole('option', { name: /developer/ })
    return { opener, dlg }
  },
}
const DIALOGS = Object.keys(OPEN)

/** Accessible names of the buttons in an element: aria-label, else the text. */
const buttonNames = (el: HTMLElement) => within(el).getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? (b.textContent ?? '').trim())

describe.each(DIALOGS)('the %s dialog', (which) => {
  it('opens with focus on the host X, never on a primary or danger button', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN[which](user)
    const focused = document.activeElement as HTMLElement
    expect(focused).toBe(within(dlg).getByRole('button', { name: 'Close' }))
    expect(focused).not.toHaveClass('busy-cue-primary')
    expect(focused).not.toHaveClass('busy-cue-danger')
  })

  it('has unique button names, with "Close" once (the X)', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN[which](user)
    const names = buttonNames(dlg)
    expect(names.filter((n) => n === 'Close')).toHaveLength(1)
    expect(new Set(names).size).toBe(names.length)
  })

  it('Tab from the last control wraps to the X, and Shift+Tab from the X wraps to the last', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN[which](user)
    const x = within(dlg).getByRole('button', { name: 'Close' })
    expect(x).toHaveFocus()
    await user.tab({ shift: true })
    const last = document.activeElement as HTMLElement
    expect(dlg.contains(last)).toBe(true)
    expect(last).not.toBe(x)
    await user.tab()
    expect(x).toHaveFocus()
  })

  it('Escape on a clean dialog closes it, sends nothing more, and focus returns to the opener', async () => {
    const user = userEvent.setup()
    const b = await boot()
    const { opener } = await OPEN[which](user)
    const before = b.writes().length
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
    expect(b.writes()).toHaveLength(before)
  })

  it('a backdrop click on a clean dialog closes it and sends nothing more (ruling 2)', async () => {
    const user = userEvent.setup()
    const b = await boot()
    await OPEN[which](user)
    const before = b.writes().length
    await user.click(backdrop())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes()).toHaveLength(before)
  })

  it('the X closes it', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg, opener } = await OPEN[which](user)
    await user.click(within(dlg).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })
})

describe('dialog titles', () => {
  it.each(['Remote control', 'Stop session', 'Clean up'])('the %s title carries cs-dlg-title, so a long session name wraps', async (which) => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN[which](user)
    const titleId = dlg.getAttribute('aria-labelledby')!
    expect(document.getElementById(titleId)!.querySelector('.cs-dlg-title')).not.toBeNull()
  })
})

describe('Stop and clean up', () => {
  it('the red Stop button is the last control in Tab order', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN['Clean up'](user)
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    const red = within(dlg).getByRole('button', { name: /^Stop 1 session/ })
    within(dlg).getByRole('button', { name: 'Close' }).focus()
    await user.tab({ shift: true })
    expect(red).toHaveFocus()
  })

  it('with rows ticked, Escape and a backdrop click still close and stop nothing (ruling 2)', async () => {
    const user = userEvent.setup()
    const b = await boot()
    let { dlg } = await OPEN['Clean up'](user)
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    ;({ dlg } = await OPEN['Clean up'](user))
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    await user.click(backdrop())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes().filter((w) => w.path === '/stop/execute')).toHaveLength(0)
  })

  it('the results end state says "Done" (primary), which closes, and has one "Close"', async () => {
    const user = userEvent.setup()
    await boot({ 'POST /stop/execute': { results: [{ name: 'infra-audit', outcome: 'stopped' }] } })
    const { dlg } = await OPEN['Clean up'](user)
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    await user.click(within(dlg).getByRole('button', { name: /^Stop 1 session/ }))
    expect(await within(dlg).findByRole('list', { name: 'Results' })).toHaveTextContent('infra-audit: stopped')
    const done = within(dlg).getByRole('button', { name: 'Done' })
    expect(done).toHaveClass('busy-cue-primary')
    expect(within(dlg).getAllByRole('button', { name: 'Close' })).toHaveLength(1)
    await user.click(done)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('while the stop request is in flight, the X, Escape and backdrop are refused', async () => {
    const user = userEvent.setup()
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    await boot({
      'POST /stop/execute': async () => {
        await gate
        return { body: { results: [{ name: 'infra-audit', outcome: 'stopped' }] } }
      },
    })
    const { dlg } = await OPEN['Clean up'](user)
    await user.click(within(dlg).getAllByRole('checkbox')[0])
    await user.click(within(dlg).getByRole('button', { name: /^Stop 1 session/ }))
    expect(within(dlg).getByRole('button', { name: 'Close' })).toBeDisabled()
    await user.keyboard('{Escape}')
    await user.click(backdrop())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    release()
    expect(await within(dlg).findByRole('list', { name: 'Results' })).toBeInTheDocument()
  })
})

describe('Remote control while the request is in flight', () => {
  it('the pressed button stays focusable (aria-disabled), and Escape, backdrop and X do nothing', async () => {
    const user = userEvent.setup()
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    const b = await boot({
      'POST /relays': async () => {
        await gate
        return { status: 201, body: summary() }
      },
    })
    const { dlg } = await OPEN['Remote control'](user)
    await user.click(within(dlg).getByRole('button', { name: 'Accept and remote control' }))
    const pending = within(dlg).getByRole('button', { name: 'Setting up...' })
    expect(pending).toHaveFocus()
    expect(pending).toBeEnabled()
    expect(pending).toHaveAttribute('aria-disabled', 'true')
    await user.click(pending)
    await user.keyboard('{Escape}')
    await user.click(backdrop())
    expect(within(dlg).getByRole('button', { name: 'Close' })).toBeDisabled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(b.writes()).toHaveLength(1)
  })

  it('without the chat launcher the end state says "Done", which takes focus', async () => {
    withoutChatLauncher()
    const user = userEvent.setup()
    await boot({ 'POST /relays': () => ({ status: 201, body: summary() }) })
    const { dlg } = await OPEN['Remote control'](user)
    await user.click(within(dlg).getByRole('button', { name: 'Accept and remote control' }))
    expect(await within(dlg).findByRole('status')).toHaveTextContent('from the Kiro Crew sidebar')
    await waitFor(() => expect(within(dlg).getByRole('button', { name: 'Done' })).toHaveFocus())
    expect(within(dlg).getAllByRole('button', { name: 'Close' })).toHaveLength(1)
  })
})

describe('Start', () => {
  it('a dirty form ignores Escape and a backdrop click; Cancel still closes it', async () => {
    const user = userEvent.setup()
    const b = await boot()
    const { dlg } = await OPEN.Start(user)
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/w')
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: 'Start a session' })).toBeInTheDocument()
    await user.click(backdrop())
    expect(screen.getByRole('dialog', { name: 'Start a session' })).toBeInTheDocument()
    expect(within(dlg).getByLabelText('Working directory (required)')).toHaveValue('/w')
    await user.click(within(dlg).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(b.writes()).toHaveLength(0)
  })

  it('a dirty form still closes from the X', async () => {
    const user = userEvent.setup()
    await boot()
    const { dlg } = await OPEN.Start(user)
    await user.type(within(dlg).getByLabelText('Session name (optional)'), 'x')
    await user.click(within(dlg).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Start stays off while the profiles load, even with a working directory', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /profiles': () => new Promise(() => {}) })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    const profile = within(dlg).getByRole('combobox', { name: 'Profile' })
    expect(profile).toBeDisabled()
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
    expect(within(dlg).getByRole('button', { name: 'Start' })).toBeDisabled()
  })

  it('Start stays off when no profile is found', async () => {
    const user = userEvent.setup()
    await boot({ 'GET /profiles': { profiles: [] } })
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    await waitFor(() => expect(within(dlg).getByRole('combobox', { name: 'Profile' })).toHaveTextContent('No profiles found'))
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
    expect(within(dlg).getByRole('button', { name: 'Start' })).toBeDisabled()
  })

  it('Start turns on once a profile is picked and sends that profile', async () => {
    const user = userEvent.setup()
    const b = await boot({ 'POST /sessions': { status: 201, body: { session: 'x', conductor_id: 'cafe0001' } } })
    const { dlg } = await OPEN.Start(user)
    const profile = within(dlg).getByRole('combobox', { name: 'Profile' })
    await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
    await user.selectOptions(profile, 'supervisor')
    const start = within(dlg).getByRole('button', { name: 'Start' })
    expect(start).toBeEnabled()
    await user.click(start)
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ path: '/sessions', body: { agent_profile: 'supervisor', working_directory: '/work' } })
  })
})
