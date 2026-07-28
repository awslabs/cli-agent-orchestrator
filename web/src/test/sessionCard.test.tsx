import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { useStore } from '../store'

// §7.6 session-card header: metadata must be selectable text that never
// toggles the card; expand/collapse belongs exclusively to the chevron button.
//
// DashboardHome never renders TerminalView unless a terminal is opened (never
// happens here), so stub it out — the real module pulls in @xterm/xterm, which
// is not load-safe under jsdom (see terminalView.test.tsx for the elaborate
// mock it needs).
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

const SESSION = { id: 'sess-1', name: 'cao-fleet', status: 'active' }
const TERMINAL = {
  id: 'term-001',
  tmux_session: 'cao-fleet',
  tmux_window: 'reviewer-a1b2',
  provider: 'kimi_cli',
  agent_profile: 'reviewer',
  created_at: '2026-07-28T10:00:00Z',
  last_active: '2026-07-28T12:00:00Z',
}
const REGION_ID = 'session-cao-fleet-terminals'

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

function stubDashboardFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    if (url === '/sessions/cao-fleet') {
      return jsonResponse({ session: SESSION, terminals: [TERMINAL] })
    }
    if (url === '/terminals/term-001') {
      return jsonResponse({ ...TERMINAL, name: 'term-001', session_name: 'cao-fleet', status: 'idle' })
    }
    if (url === '/agents/profiles') return jsonResponse([])
    return jsonResponse({})
  }))
}

async function renderDashboard() {
  render(<DashboardHome onNavigate={() => {}} />)
  // The first fetch also auto-expands the newly seen session.
  await screen.findByText('cao-fleet')
}

function chevron(): HTMLElement {
  return screen.getByRole('button', { name: /session cao-fleet/ })
}

function collapseCard() {
  fireEvent.click(screen.getByRole('button', { name: /Collapse session cao-fleet/ }))
  expect(chevron()).toHaveAttribute('aria-expanded', 'false')
  expect(document.getElementById(REGION_ID)).toBeNull()
}

function expectCollapsed() {
  expect(chevron()).toHaveAttribute('aria-expanded', 'false')
  expect(screen.getByRole('button', { name: /Expand session cao-fleet/ })).toBeTruthy()
  expect(document.getElementById(REGION_ID)).toBeNull()
}

describe('DashboardHome session-card header (§7.6)', () => {
  beforeEach(() => {
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubDashboardFetch()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    window.getSelection()?.removeAllRanges()
    useStore.setState({ sessions: [], terminalStatuses: {} })
  })

  it('clicking metadata (name, count, badge, timestamps) does not toggle the card', async () => {
    await renderDashboard()
    collapseCard()

    const name = screen.getByText('cao-fleet')
    const count = screen.getByText('1 agent')
    // 'reviewer' also appears in the agent-type filter row; the card's type
    // badge is the one inside the selectable metadata container.
    const badge = screen.getAllByText('reviewer').find(el => el.closest('div.select-text'))!
    const started = screen.getByText(/^Started \d/)
    const active = screen.getByText(/^Active \d/)

    for (const el of [name, count, badge, started, active]) {
      fireEvent.click(el)
      expectCollapsed()
    }

    // State must also survive metadata clicks while expanded ("unchanged").
    fireEvent.click(chevron())
    expect(chevron()).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByText('cao-fleet'))
    fireEvent.click(screen.getByText('1 agent'))
    expect(chevron()).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(REGION_ID)).not.toBeNull()
  })

  it('double-click and drag-select on metadata leave card state unchanged', async () => {
    await renderDashboard()
    collapseCard()

    const name = screen.getByText('cao-fleet')
    const started = screen.getByText(/^Started \d/)

    fireEvent.doubleClick(name)
    fireEvent.doubleClick(started)
    expectCollapsed()

    // Simulated drag-select from the session name across the timestamp,
    // including a real Selection range as the selection intent.
    fireEvent.mouseDown(name)
    const range = document.createRange()
    range.setStartBefore(name)
    range.setEndAfter(started)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    fireEvent.mouseMove(started)
    fireEvent.mouseUp(started)

    expect(selection?.toString()).toContain('cao-fleet')
    expectCollapsed()
  })

  it('metadata is selectable text with default cursor and no button role or click handler', async () => {
    await renderDashboard()

    const name = screen.getByText('cao-fleet')
    // The name (and by construction the rest of the metadata) lives inside a
    // plain container — not inside any <button>.
    expect(name.closest('button')).toBeNull()

    const metadata = name.closest('div.select-text')
    expect(metadata).not.toBeNull()
    expect(metadata).toHaveClass('select-text', 'cursor-default')
    // Behavioral proof of "no click handler": clicks change nothing (covered
    // above); structurally the container is not a toggle control.
    expect(metadata).not.toHaveAttribute('role', 'button')
    expect(metadata).not.toHaveAttribute('aria-expanded')
  })

  it('chevron toggles exactly once per click and updates accessible name and aria-expanded', async () => {
    await renderDashboard()

    // Auto-expanded on first load.
    const expanded = screen.getByRole('button', { name: /Collapse session cao-fleet/ })
    expect(expanded).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(REGION_ID)).not.toBeNull()

    // One click → exactly one state transition.
    fireEvent.click(expanded)
    const collapsed = screen.getByRole('button', { name: /Expand session cao-fleet/ })
    expect(collapsed).toHaveAttribute('aria-expanded', 'false')
    expect(document.getElementById(REGION_ID)).toBeNull()

    // And back again — still exactly one transition per click.
    fireEvent.click(collapsed)
    expect(screen.getByRole('button', { name: /Collapse session cao-fleet/ })).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(REGION_ID)).not.toBeNull()
  })

  it('chevron is a native type="button" element, so Enter/Space activation is structural', async () => {
    await renderDashboard()

    // @testing-library/user-event is not a dependency here and jsdom's
    // fireEvent.keyDown does not synthesize clicks, so keyboard activation is
    // asserted structurally: a native button gets Enter/Space click semantics
    // from the browser with no custom keymap.
    const button = chevron()
    expect(button.tagName).toBe('BUTTON')
    expect(button).toHaveAttribute('type', 'button')
    // No nested interactive elements inside the chevron button.
    expect(button.querySelector('button, a, input, [role="button"]')).toBeNull()
  })

  it('aria-controls resolves to the terminals region when expanded', async () => {
    await renderDashboard()
    collapseCard()

    // Constant while collapsed (region is rendered only when expanded).
    expect(chevron()).toHaveAttribute('aria-controls', REGION_ID)
    expect(document.getElementById(REGION_ID)).toBeNull()

    fireEvent.click(chevron())
    const region = document.getElementById(REGION_ID)
    expect(region).not.toBeNull()
    expect(chevron().getAttribute('aria-controls')).toBe(region!.id)
  })

  it('chevron precedes the terminals region in DOM order and has a focus ring and 44px target', async () => {
    await renderDashboard()

    const button = chevron()
    const region = document.getElementById(REGION_ID)!
    expect(region).not.toBeNull()
    expect(button.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    expect(button.className).toContain('focus:outline-none')
    expect(button.className).toContain('focus:ring-2')
    expect(button.className).toContain('focus:ring-emerald-500')
    expect(button).toHaveClass('min-h-[44px]', 'min-w-[44px]')
  })
})
