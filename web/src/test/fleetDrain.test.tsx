import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { FleetControls, drainIsSpendable } from '../components/FleetControls'
import { api, SessionDrain, SessionLifecycle } from '../api'

/**
 * The dashboard half of M3-D's safe drain.
 *
 * What these pin is the honesty of the surface rather than its layout: a
 * receipt is never shown or spendable unless it exists, a drain that could not
 * prove a boundary says so and offers a *continue*, and nothing here quietly
 * turns into a force pause.
 */

const SESSION = 'cao-fleet'

function lifecycle(value: SessionLifecycle['lifecycle']): SessionLifecycle {
  return {
    session_name: SESSION,
    lifecycle: value,
    restore_to: null,
    archived: false,
    kind: 'campaign',
    declared_by: 'colin',
    note: null,
    pause_deadline_at: null,
    epoch: 3,
    declared: true,
  }
}

function drain(overrides: Partial<SessionDrain> = {}): SessionDrain {
  return {
    drain_id: 'drain-1',
    session_name: SESSION,
    intent: 'pause',
    state: 'complete',
    attempt: 0,
    lifecycle_epoch: 3,
    roster_revision: 'ab'.repeat(32),
    receipt_digest: 'cd'.repeat(32),
    reconciliation_reason: null,
    initiated_by: 'colin',
    created_at: '2026-08-15T00:00:00Z',
    updated_at: '2026-08-15T00:00:01Z',
    ...overrides,
  }
}

const unfinished = drain({
  state: 'reconciliation-required',
  receipt_digest: null,
  reconciliation_reason: '1 member(s) reached no proven boundary: a1',
})

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('drainIsSpendable', () => {
  it('requires the receipt, not just the state', () => {
    expect(drainIsSpendable(drain())).toBe(true)
    expect(drainIsSpendable(drain({ receipt_digest: null }))).toBe(false)
    expect(drainIsSpendable(unfinished)).toBe(false)
    expect(drainIsSpendable(null)).toBe(false)
  })
})

describe('the drain button', () => {
  it('is offered for a working session and not for a stopped one', () => {
    const { unmount } = render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )
    expect(screen.getByRole('button', { name: /drain to a boundary/i })).toBeInTheDocument()
    unmount()

    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )
    expect(screen.queryByRole('button', { name: /drain to a boundary/i })).not.toBeInTheDocument()
  })

  it('mints a drain id and asks for a pause drain', async () => {
    const spy = vi.spyOn(api, 'runSafeDrain').mockResolvedValue(drain())
    render(
      <FleetControls
        sessionName={SESSION}
        lifecycle={lifecycle('working')}
        operatorName="colin"
        mintOperationId={() => 'minted-1'}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    expect(spy).toHaveBeenCalledWith(SESSION, 'minted-1', 'pause', 'colin', false)
  })
})

describe('safe pause is only offered against a spendable receipt', () => {
  it('appears once the drain completes', async () => {
    vi.spyOn(api, 'runSafeDrain').mockResolvedValue(drain())
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )
    expect(screen.queryByRole('button', { name: /safe pause/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    await screen.findByRole('button', { name: /safe pause/i })
  })

  it('stays hidden when the drain could not prove a boundary', async () => {
    vi.spyOn(api, 'runSafeDrain').mockResolvedValue(unfinished)
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    await screen.findByTestId('fleet-drain-unfinished')
    expect(screen.queryByRole('button', { name: /safe pause/i })).not.toBeInTheDocument()
    // ...and force pause is still its own separate button, not a fallback.
    expect(screen.getByRole('button', { name: /force pause/i })).toBeInTheDocument()
  })

  it('spends the named drain rather than a digest', async () => {
    vi.spyOn(api, 'runSafeDrain').mockResolvedValue(drain())
    const pause = vi.spyOn(api, 'cohortPauseSafeFromDrain').mockResolvedValue({
      operation_id: 'op-1',
      session_name: SESSION,
      operation_kind: 'pause',
      requested_mode: 'safe',
      current_mode: 'safe',
      initiator_kind: 'operator',
      initiated_by: 'colin',
      state: 'paused',
      state_epoch: 2,
      lifecycle_epoch: 3,
      source_operation_id: null,
      resume_target: null,
      created_at: '2026-08-15T00:00:00Z',
      updated_at: '2026-08-15T00:00:01Z',
    })
    render(
      <FleetControls
        sessionName={SESSION}
        lifecycle={lifecycle('working')}
        operatorName="colin"
        mintOperationId={() => 'minted-1'}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))
    fireEvent.click(await screen.findByRole('button', { name: /safe pause/i }))

    await waitFor(() => expect(pause).toHaveBeenCalledTimes(1))
    expect(pause).toHaveBeenCalledWith(SESSION, 'minted-1', 'drain-1', 'colin')
  })
})

describe('an unfinished drain reports itself honestly', () => {
  it('never shows a receipt digest it does not have', async () => {
    vi.spyOn(api, 'runSafeDrain').mockResolvedValue(unfinished)
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    const panel = await screen.findByTestId('fleet-drain-result')
    expect(panel.textContent).not.toContain('cd'.repeat(32))
    expect(panel.textContent).toContain('did not prove a boundary')
    expect(panel.textContent).toContain('Nothing was paused')
    expect(panel.textContent).toContain('never becomes a force pause on its own')
  })

  it('continues the same drain rather than starting a second one', async () => {
    const spy = vi.spyOn(api, 'runSafeDrain').mockResolvedValue(unfinished)
    render(
      <FleetControls
        sessionName={SESSION}
        lifecycle={lifecycle('working')}
        operatorName="colin"
        mintOperationId={() => 'minted-1'}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))
    fireEvent.click(await screen.findByRole('button', { name: /continue this drain/i }))

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    // Same drain id, retry flag set: the workers already steered are not
    // steered again.
    expect(spy).toHaveBeenLastCalledWith(SESSION, 'drain-1', 'pause', 'colin', true)
  })

  it('surfaces a refused drain as an error rather than a silent no-op', async () => {
    vi.spyOn(api, 'runSafeDrain').mockRejectedValue({ detail: 'the fleet moved since it was observed' })
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('the fleet moved since it was observed')
    expect(screen.queryByTestId('fleet-drain-result')).not.toBeInTheDocument()
  })
})

describe('a drain is evidence for the intent it was earned under', () => {
  it('a stop drain is not pause evidence', () => {
    const stop = drain({ intent: 'stop' })
    expect(drainIsSpendable(stop)).toBe(false)
    expect(drainIsSpendable(stop, 'stop')).toBe(true)
    expect(drainIsSpendable(drain(), 'stop')).toBe(false)
  })

  it('the pause button stays hidden for a stop drain', async () => {
    vi.spyOn(api, 'runSafeDrain').mockResolvedValue(drain({ intent: 'stop' }))
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /drain to a boundary/i }))

    await screen.findByTestId('fleet-drain-result')
    expect(screen.queryByRole('button', { name: /safe pause/i })).not.toBeInTheDocument()
  })
})
