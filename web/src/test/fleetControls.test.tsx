import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { FleetControls, outcomeSummary, wakeWasDelivered } from '../components/FleetControls'
import { api, CohortOperation, CohortProvenance, SessionLifecycle } from '../api'

const SESSION = 'cao-fleet'

function lifecycle(value: SessionLifecycle['lifecycle']): SessionLifecycle {
  return {
    session_name: SESSION,
    lifecycle: value,
    restore_to: value === 'stopped' ? 'working' : null,
    archived: false,
    kind: 'campaign',
    declared_by: 'colin',
    note: null,
    pause_deadline_at: null,
    epoch: 3,
    declared: true,
  }
}

function provenance(overrides: Partial<CohortProvenance> = {}): CohortProvenance {
  return {
    operation_id: 'op-1',
    session_name: SESSION,
    operation_kind: 'resume',
    state: 'settled',
    state_epoch: 2,
    lifecycle_epoch: 4,
    lifecycle_observation: 'stopped',
    roster_revision: 'ab'.repeat(32),
    member_snapshot_digest: 'cd'.repeat(32),
    requested_mode: 'safe',
    current_mode: 'safe',
    promoted_to_force: false,
    promotion_receipt_digest: null,
    promoted_by: null,
    initiator_kind: 'operator',
    initiated_by: 'colin',
    source_operation_id: 'stop-1',
    resume_target: 'working',
    member_outcomes: { 'restored-exact': 2, failed: 1 },
    continuity: [],
    retryable: false,
    retries: [],
    reconciliation_reason: null,
    ...overrides,
  }
}

function operation(overrides: Partial<CohortOperation> = {}): CohortOperation {
  const prov = overrides.provenance ?? provenance()
  return {
    operation_id: prov.operation_id,
    session_name: SESSION,
    operation_kind: prov.operation_kind,
    requested_mode: prov.requested_mode,
    current_mode: prov.current_mode,
    initiator_kind: 'operator',
    initiated_by: 'colin',
    state: prov.state,
    state_epoch: prov.state_epoch,
    lifecycle_epoch: prov.lifecycle_epoch,
    source_operation_id: prov.source_operation_id,
    resume_target: prov.resume_target,
    created_at: '2026-08-15T00:00:00Z',
    updated_at: '2026-08-15T00:00:01Z',
    provenance: prov,
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('outcomeSummary', () => {
  it('counts a failed member as lost but decided', () => {
    const summary = outcomeSummary(provenance())
    expect(summary).toEqual({ total: 3, lost: 1, undecided: 0 })
  })

  it('counts unresumable as lost, not undecided', () => {
    const summary = outcomeSummary(provenance({ member_outcomes: { unresumable: 2 } }))
    expect(summary).toEqual({ total: 2, lost: 2, undecided: 0 })
  })

  it('only reconciliation-required is undecided', () => {
    const summary = outcomeSummary(
      provenance({ member_outcomes: { 'restored-exact': 1, 'reconciliation-required': 1 } }),
    )
    expect(summary).toEqual({ total: 2, lost: 0, undecided: 1 })
  })

  it('is empty for an operation with no members yet', () => {
    expect(outcomeSummary(undefined)).toEqual({ total: 0, lost: 0, undecided: 0 })
  })
})

describe('the controls a session offers', () => {
  it('offers both resumes for a stopped session and neither stop', () => {
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    expect(screen.getByRole('button', { name: /resume paused/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /resume and start/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /force stop/i })).not.toBeInTheDocument()
  })

  it('offers force pause and force stop for a working session, and no resume', () => {
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    expect(screen.getByRole('button', { name: /force pause/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /force stop/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
  })

  it('never renders a force modifier — force is its own button', () => {
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })

  it('tells the operator that resume paused sends nothing', () => {
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    expect(screen.getByText(/sends nothing/i)).toBeInTheDocument()
  })
})

describe('performing an operation', () => {
  it('sends the minted operation id and the operator name', async () => {
    const spy = vi.spyOn(api, 'cohortResumePaused').mockResolvedValue(operation())
    render(
      <FleetControls
        sessionName={SESSION}
        lifecycle={lifecycle('stopped')}
        operatorName="colin"
        mintOperationId={() => 'minted-id'}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume paused/i }))

    await waitFor(() => expect(spy).toHaveBeenCalledWith(SESSION, 'minted-id', 'colin'))
  })

  it('routes resume and start to its own endpoint', async () => {
    const paused = vi.spyOn(api, 'cohortResumePaused').mockResolvedValue(operation())
    const start = vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(operation())
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    await waitFor(() => expect(start).toHaveBeenCalled())
    expect(paused).not.toHaveBeenCalled()
  })

  it('disables every control while one is in flight, so a retry is not a second click', async () => {
    let release: (value: CohortOperation) => void = () => {}
    vi.spyOn(api, 'cohortResumePaused').mockReturnValue(
      new Promise<CohortOperation>(resolve => { release = resolve }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume paused/i }))

    expect(screen.getByRole('button', { name: /resume and start/i })).toBeDisabled()
    release(operation())
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /resume and start/i })).not.toBeDisabled())
  })

  it('reports a partial restore as a running fleet that lost a member', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(operation())
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).toHaveTextContent('settled')
    expect(result).toHaveTextContent('2 restored on its own session')
    expect(result).toHaveTextContent('1 did not come back')
    expect(result).toHaveTextContent(/the fleet is running/i)
    expect(result).not.toHaveTextContent(/needs reconciliation/i)
  })

  it('reports an undecided member as needing reconciliation', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(
      operation({
        provenance: provenance({
          state: 'reconciliation-required',
          member_outcomes: { 'restored-exact': 1, 'reconciliation-required': 1 },
        }),
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).toHaveTextContent(/no decided\s+outcome yet/i)
    expect(result).not.toHaveTextContent(/the fleet is running/i)
  })

  it('never shows a promoted operation as the safe one it started as', async () => {
    vi.spyOn(api, 'cohortStopForce').mockResolvedValue(
      operation({
        provenance: provenance({
          operation_kind: 'stop',
          state: 'stopped',
          requested_mode: 'safe',
          current_mode: 'force',
          promoted_to_force: true,
          promoted_by: 'colin',
          promotion_receipt_digest: '9'.repeat(64),
          member_outcomes: { stopped: 2 },
        }),
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('working')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /force stop/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).toHaveTextContent('(force)')
    expect(result).toHaveTextContent(/promoted safe → force by colin/i)
  })

  it("surfaces the server's refusal detail instead of a generic failure", async () => {
    vi.spyOn(api, 'cohortResumeStart').mockRejectedValue(
      Object.assign(new Error('409 Conflict'), {
        detail: 'session cao-fleet was paused when it was stopped; resume it paused',
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/resume it paused/i)
  })
})

describe('never claiming the supervisor was told', () => {
  it('is false while the operation still needs reconciliation', () => {
    expect(wakeWasDelivered(provenance({ state: 'reconciliation-required' }))).toBe(false)
  })

  it('is false for a resume-paused, which wakes nobody by design', () => {
    expect(wakeWasDelivered(provenance({ resume_target: 'paused' }))).toBe(false)
  })

  it('is false for a Stop, which has no wake at all', () => {
    expect(wakeWasDelivered(provenance({ operation_kind: 'stop', resume_target: null })))
      .toBe(false)
  })

  it('is true only for a settled resume-and-start', () => {
    expect(wakeWasDelivered(provenance())).toBe(true)
  })

  it('does not say the supervisor was told when the wake never landed', async () => {
    // The regression: decided members (one failed, one restored) but the
    // operation stopped BECAUSE the wake did not land. Every member outcome
    // is terminal, so the old `lost > 0 && undecided === 0` test passed and
    // the dashboard cheerfully reported a running fleet and a told supervisor.
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(
      operation({
        provenance: provenance({
          state: 'reconciliation-required',
          retryable: true,
          reconciliation_reason:
            'the fleet was restored but its supervisor reconciliation wake did not land',
          member_outcomes: { 'restored-exact': 1, failed: 1 },
        }),
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).not.toHaveTextContent(/the supervisor was told/i)
    expect(result).not.toHaveTextContent(/the fleet is running/i)
    expect(result).toHaveTextContent(/did not finish/i)
    expect(result).toHaveTextContent(/wake did not land/i)
    // The decided failure is still reported, not hidden behind the reconcile.
    expect(result).toHaveTextContent('1 did not come back')
    expect(result).toHaveTextContent(/already decided and will not be retried/i)
  })

  it('does not borrow the wake sentence for a resume-paused that lost a member', async () => {
    vi.spyOn(api, 'cohortResumePaused').mockResolvedValue(
      operation({
        provenance: provenance({
          resume_target: 'paused',
          member_outcomes: { 'restored-exact': 1, failed: 1 },
        }),
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume paused/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).not.toHaveTextContent(/the supervisor was told/i)
    expect(result).toHaveTextContent(/back and paused/i)
    expect(result).toHaveTextContent(/nothing was sent/i)
  })

  it('still says the supervisor was told when the wake really landed', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(operation())
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    const result = await screen.findByTestId('fleet-operation-result')
    expect(result).toHaveTextContent(/the supervisor was told/i)
    expect(screen.queryByTestId('fleet-reconciliation')).not.toBeInTheDocument()
  })
})

describe('continuing a Resume that needs reconciliation', () => {
  const reconciled = () =>
    operation({
      provenance: provenance({
        state: 'reconciliation-required',
        retryable: true,
        reconciliation_reason: 'one or more cohort members have no decided restore outcome',
        member_outcomes: { 'restored-exact': 1, 'reconciliation-required': 1 },
      }),
    })

  it('offers a retry that names the same operation, not a new one', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(reconciled())
    const retry = vi.spyOn(api, 'cohortResumeRetry').mockResolvedValue(operation())
    render(
      <FleetControls
        sessionName={SESSION}
        lifecycle={lifecycle('stopped')}
        operatorName="colin"
        mintOperationId={() => 'minted-id'}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))
    fireEvent.click(await screen.findByRole('button', { name: /continue this resume/i }))

    await waitFor(() =>
      expect(retry).toHaveBeenCalledWith(SESSION, 'op-1', 'colin'))
  })

  it('replaces the reconciliation panel once the retry settles', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(reconciled())
    vi.spyOn(api, 'cohortResumeRetry').mockResolvedValue(
      operation({
        provenance: provenance({
          retries: [{
            transition_id: 't1', from_state_epoch: 2, actor: 'colin',
            reason: null, receipt_digest: 'ab'.repeat(32), created_at: '2026-08-15T01:00:00Z',
          }],
        }),
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))
    fireEvent.click(await screen.findByRole('button', { name: /continue this resume/i }))

    await waitFor(() =>
      expect(screen.queryByTestId('fleet-reconciliation')).not.toBeInTheDocument())
    const result = screen.getByTestId('fleet-operation-result')
    expect(result).toHaveTextContent(/the supervisor was told/i)
    expect(result).toHaveTextContent(/retried by colin/i)
  })

  it('offers no retry for a settled operation', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(operation())
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))

    await screen.findByTestId('fleet-operation-result')
    expect(screen.queryByRole('button', { name: /continue this resume/i })).not.toBeInTheDocument()
  })

  it('surfaces a refused retry rather than looking like it worked', async () => {
    vi.spyOn(api, 'cohortResumeStart').mockResolvedValue(reconciled())
    vi.spyOn(api, 'cohortResumeRetry').mockRejectedValue(
      Object.assign(new Error('409 Conflict'), {
        detail: 'session cao-fleet Stop barrier was reclaimed',
      }),
    )
    render(
      <FleetControls sessionName={SESSION} lifecycle={lifecycle('stopped')} operatorName="colin" />,
    )

    fireEvent.click(screen.getByRole('button', { name: /resume and start/i }))
    fireEvent.click(await screen.findByRole('button', { name: /continue this resume/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/barrier was reclaimed/i)
  })
})

describe('the typed client', () => {
  it('exposes no safe-Pause method, because a dashboard cannot produce the receipt', () => {
    expect('cohortPauseSafe' in api).toBe(false)
  })

  it('keeps safe and force stop on separate methods', () => {
    expect(typeof api.cohortStopSafe).toBe('function')
    expect(typeof api.cohortStopForce).toBe('function')
  })

  it('requires a drain receipt on safe stop only', () => {
    // Arity is the contract: safe stop takes the receipt, force stop cannot.
    expect(api.cohortStopSafe.length).toBeGreaterThan(api.cohortStopForce.length)
  })
})
