import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, screen } from '@testing-library/react'
import { useStore } from '../store'
import {
  SESSION,
  renderDashboard,
  statusFilterRow,
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

  it('renders the pills in STATUS_ORDER, with Managed Live directly after Processing', async () => {
    await renderDashboard()

    const labels = [...statusFilterRow().querySelectorAll('button')].map(b => b.textContent)
    expect(labels).toEqual([
      'Any status',
      'Processing',
      'Managed Live',
      'Idle',
      'Awaiting Input',
      'Error',
      'Completed',
      'Unknown',
    ])
  })

  it('orders the summary chips by STATUS_ORDER too, so Managed Live precedes Idle', async () => {
    await renderDashboard()

    expect(summaryChips()).toEqual(['2Managed Live', '1Idle'])
  })

  it('dresses the selected Managed Live pill in the info (blue) palette family', async () => {
    await renderDashboard()

    // `info` semantic role in design-tokens/status.json, the same role as
    // Processing, so the same blue family — see the STATUS_ACTIVE_BG comment.
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    const selected = screen.getByRole('button', { name: 'Managed Live' })
    expect(selected.className).toContain('bg-blue-900/40')
    expect(selected.className).toContain('border-blue-500/50')
    expect(selected.className).toContain('text-blue-300')

    // The unselected pills keep the neutral outline treatment.
    const idlePill = screen.getByRole('button', { name: 'Idle' })
    expect(idlePill.className).toContain('border-gray-700')
    expect(idlePill.className).not.toContain('bg-emerald-900/40')
  })

  it('dresses the selected Idle pill in the success (emerald) palette family', async () => {
    await renderDashboard()

    fireEvent.click(screen.getByRole('button', { name: 'Idle' }))
    expect(screen.getByRole('button', { name: 'Idle' }).className).toContain('bg-emerald-900/40')
  })
})
