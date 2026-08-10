import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, screen } from '@testing-library/react'
import { useStore } from '../store'
import {
  SESSION,
  TERMINALS,
  TERMINALS_WITH_UNRENDERABLE,
  metadata,
  openReachabilityEditor,
  renderDashboard,
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

  it('offers a Managed Live reachability option', async () => {
    await renderDashboard()

    // The option lives in the chip's popover editor now; the container it is
    // found in is the one the appearance suite pins to exactly STATUS_ORDER.
    const options = openReachabilityEditor()
    const option = screen.getByRole('button', { name: 'Managed Live' })
    expect(options.contains(option)).toBe(true)
  })

  it('filters the terminal list to managed native workers when the option is selected', async () => {
    await renderDashboard()
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002', 'idle-001'])

    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002'])

    // Selecting it again clears the filter rather than sticking — the editor
    // stays open across toggles, so the second click lands on the same option.
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(visibleTerminalIds()).toEqual(['nfm-0001', 'nfm-0002', 'idle-001'])
  })

  it('gives the selected Managed Live option a real background, not an undefined class', async () => {
    await renderDashboard()

    // A STATUS_ORDER entry with no STATUS_ACTIVE_BG key still typechecks
    // (Record<string, string>, no noUncheckedIndexedAccess) and renders
    // `class="... undefined"` — a selected option with no selected appearance.
    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    expect(screen.getByRole('button', { name: 'Managed Live' }).className).not.toContain('undefined')
  })

  it('still filters an existing status, so the added entry did not disturb them', async () => {
    await renderDashboard()

    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Idle' }))
    expect(visibleTerminalIds()).toEqual(['idle-001'])
    expect(screen.getByRole('button', { name: 'Idle' }).className).not.toContain('undefined')
  })
})

describe('DashboardHome status summary totals (terminal lifecycle statuses)', () => {
  beforeEach(() => {
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubDashboardFetch(TERMINALS_WITH_UNRENDERABLE)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [], terminalStatuses: {} })
  })

  it('distinguishes proven dispositions from genuinely unknown liveness', async () => {
    await renderDashboard(TERMINALS_WITH_UNRENDERABLE)

    // terminal_projection reports these lifecycle states in `status` for rows
    // without a live pane. DEAD and SUPERSEDED are exact evidence and must not
    // look unknown; only UNKNOWN_LIVENESS remains in the residual bucket.
    expect(useStore.getState().terminalStatuses['dead-001']).toBe('DEAD')
    expect(useStore.getState().terminalStatuses['supr-001']).toBe('SUPERSEDED')
    expect(useStore.getState().terminalStatuses['unkn-001']).toBe('UNKNOWN-LIVENESS')
    expect(summaryCounts()).toEqual({
      'Managed Live': 2,
      Idle: 1,
      Dead: 1,
      Superseded: 1,
      Unknown: 1,
    })
  })

  it('keeps the summary total equal to the session terminal count', async () => {
    await renderDashboard(TERMINALS_WITH_UNRENDERABLE)

    expect(summaryTotal()).toBe(TERMINALS_WITH_UNRENDERABLE.length)
    // The same count the card's own header reports, from the same terminals.
    expect(metadata().textContent).toContain(`${TERMINALS_WITH_UNRENDERABLE.length} agents`)
  })
  it('lets the operator filter each exact and residual disposition', async () => {
    await renderDashboard(TERMINALS_WITH_UNRENDERABLE)

    expect(summaryCounts()['Dead']).toBe(1)
    expect(summaryCounts()['Superseded']).toBe(1)
    expect(summaryCounts()['Unknown']).toBe(1)

    const unknownOption = Array.from(openReachabilityEditor().querySelectorAll('button'))
      .find(b => (b.textContent || '').includes('Unknown'))
    expect(unknownOption).toBeTruthy()
    fireEvent.click(unknownOption!)

    // Unknown now means only the row whose liveness evidence is actually
    // unknown, not the rows already proven dead or superseded.
    expect(visibleTerminalIds(TERMINALS_WITH_UNRENDERABLE)).toEqual(['unkn-001'])
  })
})
