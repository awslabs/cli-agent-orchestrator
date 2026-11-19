import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { axe } from 'jest-axe'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { busy, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

beforeEach(() => resetStub())
const prompt = { terminal_id: 'b94da5c6', status: 'waiting_user_answer', provider: 'mock_cli', tail: ['push to origin? [y/n]'], suggested: [{ label: 'Approve push', send: 'y' }, { label: 'Deny', send: 'n' }] }
const plan = { plan_id: 'p', expires_at: '2026-11-15T12:05:00Z', sessions: [{ name: 'infra-audit', tier: 'stale', last_active: '2026-11-01T00:00:00.000000Z', working: false, terminals: [{ id: 'aaaa0001', label: 'conductor', role: 'conductor', status: 'idle' }] }] }

// color-contrast needs real layout and computed CSS; it is covered by the Chromium screenshots instead.
const run = (el: Element) => axe(el, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } })

describe('accessibility (B9)', () => {
  it('quiet fixture has no axe violations, with the stale tier open and cards expanded', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet() })
    const { container } = render(<App />)
    await user.click(await screen.findByRole('button', { name: /Stale, over 48 h/ }))
    for (const b of screen.getAllByRole('button', { name: /^(payments-fix|search-tuning)/ })) await user.click(b)
    expect(await run(container)).toHaveNoViolations()
  })

  it('busy fixture has no axe violations (needs-you card with answer buttons)', async () => {
    mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt })
    const { container } = render(<App />)
    await screen.findByRole('button', { name: 'Approve push' })
    expect(await run(container)).toHaveNoViolations()
  })

  it('each dialog has no axe violations and is labelled', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': busy(), 'GET /terminals/b94da5c6/prompt': prompt, 'POST /stop/plan': plan, 'GET /profiles': { profiles: [{ name: 'developer' }] } })
    const { container } = render(<App />)
    await screen.findByRole('button', { name: 'Approve push' })
    await user.click(screen.getByRole('button', { name: /Clean up stale/ }))
    let dlg = await screen.findByRole('dialog', { name: 'Clean up stale sessions' })
    await within(dlg).findAllByRole('checkbox')
    expect(await run(container)).toHaveNoViolations()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Start session' }))
    dlg = await screen.findByRole('dialog', { name: 'Start a session' })
    await within(dlg).findByRole('option', { name: 'developer' })
    expect(await run(container)).toHaveNoViolations()
  })

  it('keyboard walk-through: toggles, then cards in reading order, are all reachable with Tab', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet() })
    render(<App />)
    await screen.findAllByRole('article')
    const seen: string[] = []
    for (let i = 0; i < 40; i++) {
      await user.tab()
      const el = document.activeElement as HTMLElement
      seen.push(el.getAttribute('aria-label') ?? el.textContent ?? '')
    }
    const idx = (re: RegExp) => seen.findIndex((s) => re.test(s))
    expect(idx(/Start session/)).toBeGreaterThanOrEqual(0)
    expect(idx(/Needs you/)).toBeGreaterThan(idx(/Start session/))
    expect(idx(/^Parked/)).toBeGreaterThan(idx(/Needs you/))
    expect(idx(/payments-fix/)).toBeGreaterThan(idx(/^Stale/))
    expect(idx(/^Ask$/)).toBeGreaterThan(idx(/payments-fix/))
    expect(idx(/Clean up stale/)).toBeGreaterThan(idx(/^Ask$/))
    // no positive tabindex anywhere
    expect(document.querySelectorAll('[tabindex]:not([tabindex="0"]):not([tabindex="-1"])')).toHaveLength(0)
  })

  it('tier toggles are a labelled group of aria-pressed buttons', async () => {
    mockBackend({ 'GET /shelf': quiet() })
    render(<App />)
    const group = await screen.findByRole('group', { name: 'Tiers to show' })
    const buttons = within(group).getAllByRole('button')
    expect(buttons).toHaveLength(4)
    for (const b of buttons) expect(b).toHaveAttribute('aria-pressed')
  })
})
