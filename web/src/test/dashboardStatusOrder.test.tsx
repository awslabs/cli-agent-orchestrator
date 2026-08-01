import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, screen } from '@testing-library/react'
import { useStore } from '../store'
import {
  SESSION,
  TERMINALS,
  TERMINALS_WITH_UNRENDERABLE,
  metadata,
  renderDashboard,
  statusFilterRow,
  stubDashboardFetch,
  summaryCounts,
  summaryTotal,
  visibleTerminalIds,
} from './dashboardStatusOrderFixture'

// Requirements, not appearance. STATUS_ORDER gates BOTH the per-session status
// summary and the status filter pills:
//
//  1. It omitted NOT_FIFO_MONITORED — the status every lifecycle-live
//     native-TUI worker reports — so on a native-TUI fleet nearly every agent
//     was uncounted in the summary and unreachable from the filter row, even
//     though STATUS_CONFIG has carried the label since the design-token SSOT
//     landed.
//  2. Anything STATUS_ORDER cannot draw used to be counted into a bucket
//     nothing renders and disappear from the totals. The counting site now
//     folds unrenderable statuses into UNKNOWN.
//
// The exact pill sequence and the literal Tailwind class strings live in
// dashboardStatusOrderAppearance.test.tsx: reordering the row or restyling a
// pill is an editorial decision, and should not read here as a broken
// requirement.
//
// DashboardHome never renders TerminalView unless a terminal is opened (never
// happens here), so stub it out — the real module pulls in @xterm/xterm, which
// is not load-safe under jsdom (see terminalView.test.tsx).
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

describe('DashboardHome status order (NOT_FIFO_MONITORED)', () => {
  beforeEach(() => {
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubDashboardFetch()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [], terminalStatuses: {} })
  })

  it('counts managed native workers in the per-session status summary', async () => {
    await renderDashboard()

    // Both managed workers are counted under the generated "Managed Live"
    // label, and the FIFO-monitored one is still counted separately.
    expect(summaryCounts()).toEqual({ 'Managed Live': 2, Idle: 1 })
    expect(summaryTotal()).toBe(TERMINALS.length)
  })

  it('offers a Managed Live status filter pill', async () => {
    await renderDashboard()

    const pill = screen.getByRole('button', { name: 'Managed Live' })
    expect(statusFilterRow().contains(pill)).toBe(true)
  })

  it('filters the terminal list to managed native workers when the pill is selected', async () => {
    await renderDashboard()
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002', 'idle-001'])

    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002'])

    // Selecting it again clears the filter rather than sticking.
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002', 'idle-001'])
  })

  it('gives the selected Managed Live pill a real background, not an undefined class', async () => {
    await renderDashboard()

    // A STATUS_ORDER entry with no STATUS_ACTIVE_BG key still typechecks
    // (Record<string, string>, no noUncheckedIndexedAccess) and renders
    // `class="... undefined"` — a selected pill with no selected appearance.
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(screen.getByRole('button', { name: 'Managed Live' }).className).not.toContain('undefined')
  })

  it('still filters an existing status, so the added entry did not disturb them', async () => {
    await renderDashboard()

    fireEvent.click(screen.getByRole('button', { name: 'Idle' }))
    expect(visibleTerminalIds()).toEqual(['idle-001'])
    expect(screen.getByRole('button', { name: 'Idle' }).className).not.toContain('undefined')
  })
})

describe('DashboardHome status summary totals (unrenderable statuses)', () => {
  beforeEach(() => {
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubDashboardFetch(TERMINALS_WITH_UNRENDERABLE)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [], terminalStatuses: {} })
  })

  it('counts rows whose status the summary cannot draw, instead of dropping them', async () => {
    await renderDashboard(TERMINALS_WITH_UNRENDERABLE)

    // 'dead' and 'superseded' are lifecycle states that terminal_projection
    // reports in `status`; the store uppercases them and STATUS_ORDER has no
    // entry for either. Before the fold they were counted into buckets nothing
    // renders, so the session showed 3 agents in its summary while its header
    // said 5 — the read failure produced a more confident state than the truth
    // warranted. They are now visibly Unknown.
    expect(useStore.getState().terminalStatuses['dead-001']).toBe('DEAD')
    expect(useStore.getState().terminalStatuses['supr-001']).toBe('SUPERSEDED')
    expect(summaryCounts()).toEqual({ 'Managed Live': 2, Idle: 1, Unknown: 2 })
  })

  it('keeps the summary total equal to the session terminal count', async () => {
    await renderDashboard(TERMINALS_WITH_UNRENDERABLE)

    expect(summaryTotal()).toBe(TERMINALS_WITH_UNRENDERABLE.length)
    // The same count the card's own header reports, from the same terminals.
    expect(metadata().textContent).toContain(`${TERMINALS_WITH_UNRENDERABLE.length} agents`)
  })
})
