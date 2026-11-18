import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { busy, history, mockBackend } from './helpers'
import { resetStub } from './sdk-stub'

const IMG = '<img src=x onerror="window.__pwned=1">'
const JS = 'javascript:window.__pwned=1'
const SCRIPT = '<script>window.__pwned=1</script>'

beforeEach(() => {
  resetStub()
  ;(window as any).__pwned = 0
})

/** Every text slot of the shelf carries a hostile string. */
function hostile() {
  const d = busy()
  d.cao.base_url = `http://127.0.0.1:9889/${IMG}`
  d.cao.version = IMG
  d.cao.terminal_backend = JS
  for (const s of d.sessions) {
    s.name = s.name === 'payments-fix' ? 'payments-fix' : s.name
    for (const t of [s.conductor, ...s.helpers]) {
      t.label = `${t.label}${IMG}`
      t.profile = `${t.profile}${SCRIPT}`
      t.provider = JS
      t.caller_id = JS
    }
    s.asks.forEach((a) => (a.label = IMG))
    if (s.in_flight) s.in_flight.label = IMG
  }
  d.sessions[0].name = `pay${IMG}`
  d.sessions[0].conductor.profile = SCRIPT
  return d
}

describe('hostile strings render as text (AC3.4)', () => {
  it('session names, labels, profiles, providers, asks and in-flight labels are inert', async () => {
    const user = userEvent.setup()
    const d = hostile()
    const { container } = (() => {
      mockBackend({
        'GET /shelf': d,
        [`GET /terminals/b94da5c6/prompt`]: { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: JS, tail: [IMG, SCRIPT, JS], suggested: [{ label: IMG, send: 'y' }] },
      })
      return render(<App />)
    })()
    await screen.findAllByRole('article')
    await user.click(screen.getByRole('button', { name: /Stale, over 48 h/ }))
    await user.click(screen.getByRole('button', { name: new RegExp('pay<img') }))
    await screen.findByText(/Show prompt/)
    await user.click(screen.getByRole('button', { name: 'Show prompt' }))
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
    expect(container.querySelectorAll('[onerror]')).toHaveLength(0)
    expect((window as any).__pwned).toBe(0)
    // the strings are visible as text
    expect(container.textContent).toContain(IMG)
    expect(container.textContent).toContain(SCRIPT)
    expect(container.textContent).toContain(JS)
    // no href or src anywhere, so a javascript: value has nowhere to go
    expect(container.querySelectorAll('[href],[src],[srcdoc],[formaction]')).toHaveLength(0)
  })

  it('error messages from the backend and CAO are text', async () => {
    const d = busy()
    d.stale_snapshot = true
    d.cao = { ...d.cao, reachable: false, base_url: `http://127.0.0.1:9889/${IMG}`, error: { code: 'cao_error', message: IMG, retryable: true } }
    mockBackend({ 'GET /shelf': d, 'GET /terminals/b94da5c6/prompt': { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: 'x', tail: [], suggested: [] } })
    const { container } = render(<App />)
    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toContain(IMG)
    expect(container.querySelector('img')).toBeNull()
  })

  it('history rows, helper labels and external senders are text', async () => {
    const user = userEvent.setup()
    const h = history('busy', 'payments-fix')
    h.closed[0].label = IMG
    h.external_senders = [{ sender_id: SCRIPT, rows: 1 }]
    mockBackend({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: 'x', tail: [], suggested: [] },
      'GET /sessions/payments-fix/history': h,
      [`GET /sessions/payments-fix/history/${h.closed[0].terminal_id}/rows`]: { rows: [{ id: 1, created_at: '2026-11-15T10:00:00+00:00', status: IMG, text: `${IMG}${SCRIPT}` }] },
    })
    const { container } = render(<App />)
    const pay = await screen.findByRole('article', { name: /payments-fix/ })
    await user.click(within(pay).getByRole('button', { name: /History/ }))
    await user.click((await within(pay).findAllByRole('button', { name: 'inbox rows' }))[0])
    await waitFor(() => expect(container.textContent).toContain(`${IMG}${SCRIPT}`))
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect((window as any).__pwned).toBe(0)
  })

  it('stop plan sessions and profiles are text', async () => {
    const user = userEvent.setup()
    mockBackend({
      'GET /shelf': busy(),
      'GET /terminals/b94da5c6/prompt': { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: 'x', tail: [], suggested: [] },
      'POST /stop/plan': { plan_id: 'p', expires_at: '2026-11-15T12:05:00Z', sessions: [{ name: IMG, tier: 'stale', last_active: '2026-11-10T00:00:00.000000Z', working: false, terminals: [{ id: 'aaaa0001', label: SCRIPT, role: 'helper', status: 'idle' }] }] },
      'GET /profiles': { profiles: [{ name: IMG, description: SCRIPT }] },
    })
    const { container } = render(<App />)
    await screen.findAllByRole('article')
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    await screen.findByRole('dialog')
    await screen.findByText(new RegExp('stops 1 terminal: ' + SCRIPT.replace(/[()/]/g, '\\$&')))
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })
})

describe('source rules', () => {
  it('no src file uses dangerouslySetInnerHTML, innerHTML, href or markdown rendering', async () => {
    const { readdirSync, readFileSync, statSync } = await import('node:fs')
    const { join, resolve } = await import('node:path')
    const walk = (d: string): string[] => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]))
    const files = walk(resolve(process.cwd(), 'src')).filter((f) => /\.(ts|tsx)$/.test(f))
    expect(files.length).toBeGreaterThan(10)
    for (const f of files) {
      const t = readFileSync(f, 'utf8')
      expect(t, f).not.toMatch(/dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/)
      expect(t, f).not.toMatch(/\bhref=|MarkdownRenderer/)
      expect(t, f).not.toMatch(/fetch\(/)
    }
  })
})
