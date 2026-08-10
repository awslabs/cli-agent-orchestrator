import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, screen, within } from '@testing-library/react'
import { useStore } from '../store'
import {
  SESSION,
  openReachabilityEditor,
  renderDashboard,
  stubDashboardFetch,
  summaryChips,
} from './dashboardStatusOrderFixture'

// EDITORIAL / APPEARANCE, not requirements. Everything here pins a presentation
// decision: the exact order of the status pills and the exact Tailwind class
// strings a selected pill wears. A failure here means someone reordered or
// restyled the row — a review question, not a broken behaviour. The behaviour
// (the pill exists, filters, toggles, and has a defined selected style) is
// asserted in dashboardStatusOrder.test.tsx and must keep passing regardless.
//
// See terminalView.test.tsx for why TerminalView is stubbed under jsdom.
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

describe('DashboardHome status filter row appearance', () => {
  beforeEach(() => {
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubDashboardFetch()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [], terminalStatuses: {} })
  })

  it('renders the options in STATUS_ORDER, with Managed Live directly after Processing', async () => {
    await renderDashboard()

    // THE DELIBERATE CHANGE THIS REDESIGN WAS GRANTED. The container that
    // holds the reachability options moved: from the always-on pill row to
    // the chip's popover editor (opened here through the "+ Filter" picker,
    // the operator's path). The assertion keeps its exact-equality shape and
    // its purpose — the container must hold exactly the STATUS_ORDER entries
    // and nothing else, so a stray clear-all or overflow control inside it
    // still fails. What legitimately left the array is the 'Any status'
    // reset button: clearing a chip-bar filter is the chip's X, and
    // deselecting every option is the in-editor path, so no reset control
    // lives among the options anymore.
    const labels = [...openReachabilityEditor().querySelectorAll('button')].map(b => b.textContent)
    // 'Stopped' joined the order when STOPPED was added to STATUS_ORDER: it
    // ships in the generated STATUS_CONFIG and previously folded silently to
    // Unknown.
    expect(labels).toEqual([
      'Processing',
      'Managed Live',
      'Idle',
      'Awaiting Input',
      'Error',
      'Completed',
      'Stopped',
      'Dead',
      'Superseded',
      'Unknown',
    ])
  })

  it('orders the summary chips by STATUS_ORDER too, so Managed Live precedes Idle', async () => {
    await renderDashboard()

    expect(summaryChips()).toEqual(['2Managed Live', '1Idle'])
  })

  it('dresses the selected Managed Live option in the info (blue) palette family', async () => {
    await renderDashboard()

    // `info` semantic role in design-tokens/status.json, the same role as
    // Processing, so the same blue family — see the STATUS_ACTIVE_BG comment.
    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    const selected = screen.getByRole('button', { name: 'Managed Live' })
    expect(selected.className).toContain('bg-blue-900/40')
    expect(selected.className).toContain('border-blue-500/50')
    expect(selected.className).toContain('text-blue-300')

    // The unselected options keep the neutral row treatment.
    const idleOption = within(screen.getByTestId('global-editor-options')).getByRole('button', { name: 'Idle' })
    expect(idleOption.className).toContain('text-gray-300')
    expect(idleOption.className).not.toContain('bg-emerald-900/40')
  })

  it('dresses the selected Idle option in the success (emerald) palette family', async () => {
    await renderDashboard()

    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Idle' }))
    expect(screen.getByRole('button', { name: 'Idle' }).className).toContain('bg-emerald-900/40')
  })

  it('dresses selected lifecycle dispositions in their semantic palette families', async () => {
    await renderDashboard()

    openReachabilityEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Dead' }))
    expect(screen.getByRole('button', { name: 'Dead' }).className).toContain('bg-red-900/40')

    fireEvent.click(screen.getByRole('button', { name: 'Superseded' }))
    expect(screen.getByRole('button', { name: 'Superseded' }).className).toContain('bg-gray-800/40')
  })
})
