import { useCallback, useMemo, useState } from 'react'
import { AlertTriangle, Loader2, Pause, Play, RotateCw, ShieldCheck, Square } from 'lucide-react'
import {
  api,
  CohortMemberOutcome,
  CohortOperation,
  CohortProvenance,
  DrainIntent,
  SessionDrain,
  SessionLifecycle,
} from '../api'

/**
 * The operator's fleet controls, and the report of what the last operation
 * actually did.
 *
 * Two rules shape this component, and both are about what a button can be
 * reached by accident:
 *
 * 1. **Force is never a checkbox.** Safe and force are separate buttons
 *    calling separate endpoints. A modifier is something a muscle-memory
 *    click carries forward; a differently-labelled, differently-coloured
 *    button is not.
 * 2. **A stopped session offers two resumes, not one.** "Resume paused"
 *    brings the panes back and sends nothing, so an operator can look first.
 *    "Resume and start" additionally wakes the supervisor. Collapsing them
 *    would mean the only way to inspect a restored fleet is to have already
 *    started it.
 *
 * 3. **Safe pause is two steps, not one.** M3-D's drain is what proves the
 *    fleet reached a boundary, and it can honestly fail. So the drain is its
 *    own button with its own visible result, and the Pause it licenses only
 *    appears once a receipt exists. Collapsing them into one "Safe pause"
 *    button would mean a drain that could not prove a boundary either becomes
 *    a silent force pause or looks like a button that did nothing — and the
 *    first of those is exactly the mislabelling this surface exists to prevent.
 */

/** Terminal outcomes. A failed member settles; it does not hold the fleet. */
const TERMINAL_OUTCOMES: ReadonlySet<CohortMemberOutcome> = new Set([
  'restored-exact', 'restored-fresh', 'failed', 'unresumable',
  'drained', 'already-idle', 'parked', 'exited', 'stopped', 'excluded-historical',
])

const OUTCOME_LABEL: Record<string, string> = {
  'restored-exact': 'restored on its own session',
  'restored-fresh': 'restored fresh',
  'failed': 'did not come back',
  'unresumable': 'no resume path',
  'reconciliation-required': 'needs reconciliation',
  'drained': 'drained to a boundary',
  'interrupted': 'turn interrupted',
  'already-idle': 'already idle',
  'parked': 'parked',
  'exited': 'exited',
  'stopped': 'collected',
  'excluded-historical': 'outside the cohort',
  'pending': 'pending',
}

export interface FleetControlsProps {
  sessionName: string
  lifecycle: SessionLifecycle | null
  /** Recorded on every operation, so it is required rather than defaulted. */
  operatorName: string
  /** Injected in tests; defaults to the browser's UUID generator. */
  mintOperationId?: () => string
  onOperation?: (operation: CohortOperation) => void
}

type Action =
  | 'drain' | 'pause-safe' | 'pause-force' | 'stop-safe' | 'stop-force'
  | 'resume-paused' | 'resume-start' | 'resume-retry'

/**
 * Whether this drain produced something a safe Pause can actually spend.
 *
 * Gating on the receipt rather than on the state string is deliberate: the
 * receipt is the thing the Pause route consumes, so a surface that offered
 * the button on any other signal would be offering a button that 409s.
 *
 * The intent is checked for the same reason. A Pause drain and a Stop drain
 * prove different things — only a Stop drain records CAO's intent to collect
 * each pane before it disappears — so a Stop receipt is not Pause evidence
 * and the server refuses it. Offering the button anyway would just teach an
 * operator a click that fails.
 */
export function drainIsSpendable(
  drain: SessionDrain | null,
  intent: DrainIntent = 'pause',
): boolean {
  return Boolean(
    drain && drain.intent === intent && drain.state === 'complete' && drain.receipt_digest,
  )
}

export function outcomeSummary(provenance: CohortProvenance | undefined): {
  total: number
  lost: number
  undecided: number
} {
  const outcomes = provenance?.member_outcomes ?? {}
  let total = 0
  let lost = 0
  let undecided = 0
  for (const [state, count] of Object.entries(outcomes)) {
    const n = count ?? 0
    total += n
    if (state === 'failed' || state === 'unresumable') lost += n
    if (!TERMINAL_OUTCOMES.has(state as CohortMemberOutcome)) undecided += n
  }
  return { total, lost, undecided }
}

/**
 * Whether this operation actually delivered a supervisor wake.
 *
 * The dashboard may only say "the supervisor was told" where that is true,
 * and there are two distinct ways for it to be false. An operation still in
 * `reconciliation-required` may have stopped *because* the wake did not land —
 * so a settled state is required, not merely a set of decided members. And a
 * Resume-paused never wakes anybody at all by design, so its success must not
 * borrow Resume-and-start's sentence.
 *
 * Reading it off the durable record rather than off which button was clicked
 * keeps it correct for an operation loaded from the projection later.
 */
export function wakeWasDelivered(provenance: CohortProvenance | undefined): boolean {
  if (!provenance) return false
  if (provenance.operation_kind !== 'resume') return false
  if (provenance.resume_target === 'paused') return false
  return provenance.state === 'settled'
}

export function FleetControls({
  sessionName,
  lifecycle,
  operatorName,
  mintOperationId,
  onOperation,
}: FleetControlsProps) {
  const [busy, setBusy] = useState<Action | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<CohortOperation | null>(null)
  const [drain, setDrain] = useState<SessionDrain | null>(null)

  const mint = useMemo(
    () => mintOperationId ?? (() => crypto.randomUUID()),
    [mintOperationId],
  )

  const stopped = lifecycle?.lifecycle === 'stopped'
  const run = useCallback(async (action: Action, call: (id: string) => Promise<CohortOperation>) => {
    setBusy(action)
    setError(null)
    try {
      const operation = await call(mint())
      setResult(operation)
      onOperation?.(operation)
    } catch (e) {
      const err = e as { detail?: string; message?: string }
      setError(err.detail ?? err.message ?? 'the operation failed')
    } finally {
      setBusy(null)
    }
  }, [mint, onOperation])

  const runDrain = useCallback(async (retry: boolean) => {
    setBusy('drain')
    setError(null)
    try {
      // A retry continues the *named* drain; a first run mints one. Reusing
      // the id is what stops a retry from steering the fleet a second time.
      const id = retry && drain ? drain.drain_id : mint()
      setDrain(await api.runSafeDrain(sessionName, id, 'pause', operatorName, retry))
    } catch (e) {
      const err = e as { detail?: string; message?: string }
      setError(err.detail ?? err.message ?? 'the drain failed')
    } finally {
      setBusy(null)
    }
  }, [drain, mint, operatorName, sessionName])

  const summary = outcomeSummary(result?.provenance)
  const settled = (result?.provenance?.state ?? result?.state) === 'settled'
  const told = wakeWasDelivered(result?.provenance)
  const needsReconciliation =
    (result?.provenance?.state ?? result?.state) === 'reconciliation-required'

  return (
    <div className="flex flex-col gap-3" data-testid="fleet-controls">
      <div className="flex flex-wrap gap-2">
        {stopped ? (
          <>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run('resume-paused', id =>
                api.cohortResumePaused(sessionName, id, operatorName))}
              className="inline-flex items-center gap-2 rounded-lg bg-gray-700 px-3 py-2 text-sm hover:bg-gray-600 disabled:opacity-50"
            >
              {busy === 'resume-paused' ? <Loader2 size={16} className="animate-spin" /> : <Pause size={16} />}
              Resume paused
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run('resume-start', id =>
                api.cohortResumeStart(sessionName, id, operatorName))}
              className="inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-3 py-2 text-sm hover:bg-emerald-600 disabled:opacity-50"
            >
              {busy === 'resume-start' ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
              Resume and start
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => runDrain(false)}
              className="inline-flex items-center gap-2 rounded-lg bg-gray-700 px-3 py-2 text-sm hover:bg-gray-600 disabled:opacity-50"
            >
              {busy === 'drain' ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
              Drain to a boundary
            </button>
            {/* Only offered once a receipt exists. A safe Pause with nothing to
                spend is a 409, and a button that 409s teaches operators to use
                the force one instead. */}
            {drainIsSpendable(drain) && (
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => run('pause-safe', id =>
                  api.cohortPauseSafeFromDrain(sessionName, id, drain!.drain_id, operatorName))}
                className="inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-3 py-2 text-sm hover:bg-emerald-600 disabled:opacity-50"
              >
                {busy === 'pause-safe' ? <Loader2 size={16} className="animate-spin" /> : <Pause size={16} />}
                Safe pause
              </button>
            )}
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run('pause-force', id =>
                api.cohortPauseForce(sessionName, id, operatorName))}
              className="inline-flex items-center gap-2 rounded-lg bg-yellow-700 px-3 py-2 text-sm hover:bg-yellow-600 disabled:opacity-50"
            >
              {busy === 'pause-force' ? <Loader2 size={16} className="animate-spin" /> : <Pause size={16} />}
              Force pause
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run('stop-force', id =>
                api.cohortStopForce(sessionName, id, operatorName))}
              className="inline-flex items-center gap-2 rounded-lg bg-red-700 px-3 py-2 text-sm hover:bg-red-600 disabled:opacity-50"
            >
              {busy === 'stop-force' ? <Loader2 size={16} className="animate-spin" /> : <Square size={16} />}
              Force stop
            </button>
          </>
        )}
      </div>

      {/* Resume paused promises zero input, so the UI has to say so where the
          decision is made rather than in documentation nobody opens. */}
      {stopped && (
        <p className="text-xs text-gray-400">
          Resume paused brings every pane back and sends nothing — no keystroke, no
          supervisor bump. Resume and start additionally wakes the supervisor once.
        </p>
      )}

      {drain && (
        <div className="rounded-lg border border-gray-700/50 p-3 text-sm" data-testid="fleet-drain-result">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium">safe drain</span>
            <span className="text-gray-300">{drain.state}</span>
            <span className="text-gray-400">attempt {drain.attempt}</span>
          </div>
          {drainIsSpendable(drain) ? (
            <p className="mt-1 text-xs text-emerald-300">
              Every worker reached a boundary and recorded it. Safe pause is available.
            </p>
          ) : (
            <div className="mt-1 space-y-2" data-testid="fleet-drain-unfinished">
              {/* Never shows a receipt digest here: an unfinished drain has
                  none, and implying otherwise hands the operator a token that
                  cannot be spent. */}
              <p className="text-xs text-orange-300">
                This drain did not prove a boundary
                {drain.reconciliation_reason ? `: ${drain.reconciliation_reason}.` : '.'}{' '}
                Nothing was paused. A drain never becomes a force pause on its own.
              </p>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => runDrain(true)}
                className="inline-flex items-center gap-2 rounded-lg bg-orange-700 px-3 py-2 text-xs hover:bg-orange-600 disabled:opacity-50"
              >
                {busy === 'drain'
                  ? <Loader2 size={14} className="animate-spin" />
                  : <RotateCw size={14} />}
                Continue this drain
              </button>
            </div>
          )}
        </div>
      )}

      {error && (
        <div role="alert" className="flex items-start gap-2 rounded-lg bg-red-900/40 p-3 text-sm text-red-200">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {result && (
        <div className="rounded-lg border border-gray-700/50 p-3 text-sm" data-testid="fleet-operation-result">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium">
              {result.provenance?.operation_kind ?? result.operation_kind}
            </span>
            <span className="text-gray-400">({result.provenance?.current_mode ?? result.current_mode})</span>
            <span className="text-gray-300">{result.state}</span>
          </div>
          {/* A promoted operation must never read as the safe one it started
              as — that is the whole reason the receipt is durable. */}
          {result.provenance?.promoted_to_force && (
            <div className="mt-1 text-xs text-yellow-300">
              promoted safe → force by {result.provenance.promoted_by}
            </div>
          )}
          {summary.total > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-gray-300">
              {Object.entries(result.provenance?.member_outcomes ?? {}).map(([state, count]) => (
                <li key={state}>
                  {count} {OUTCOME_LABEL[state] ?? state}
                </li>
              ))}
            </ul>
          )}
          {/* The success sentence is gated on the wake actually landing, not
              on the members being decided. An operation that stopped because
              its wake did not land has decided members and a supervisor that
              was told nothing. */}
          {settled && summary.lost > 0 && told && (
            <p className="mt-2 text-xs text-yellow-300">
              The fleet is running. {summary.lost} member
              {summary.lost === 1 ? '' : 's'} did not come back; the supervisor was told.
            </p>
          )}
          {settled && summary.lost > 0 && !told && (
            <p className="mt-2 text-xs text-yellow-300">
              The fleet is back and paused. {summary.lost} member
              {summary.lost === 1 ? '' : 's'} did not come back. Nothing was sent — start it
              when you are ready.
            </p>
          )}
          {needsReconciliation && (
            <div className="mt-2 space-y-2" data-testid="fleet-reconciliation">
              <p className="text-xs text-orange-300">
                This Resume did not finish
                {result.provenance?.reconciliation_reason
                  ? `: ${result.provenance.reconciliation_reason}.`
                  : '.'}{' '}
                {summary.undecided > 0
                  ? `${summary.undecided} member${summary.undecided === 1 ? '' : 's'} have no
                     decided outcome yet.`
                  : 'Its members are decided, but the operation was not completed.'}
              </p>
              {summary.lost > 0 && (
                <p className="text-xs text-gray-300">
                  {summary.lost} member{summary.lost === 1 ? '' : 's'} did not come back. That
                  is already decided and will not be retried.
                </p>
              )}
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => run('resume-retry', () =>
                  api.cohortResumeRetry(sessionName, result.operation_id, operatorName))}
                className="inline-flex items-center gap-2 rounded-lg bg-orange-700 px-3 py-2 text-xs hover:bg-orange-600 disabled:opacity-50"
              >
                {busy === 'resume-retry'
                  ? <Loader2 size={14} className="animate-spin" />
                  : <RotateCw size={14} />}
                Continue this Resume
              </button>
            </div>
          )}
          {(result.provenance?.retries ?? []).map(retry => (
            <p key={retry.transition_id} className="mt-1 text-xs text-gray-400">
              retried by {retry.actor} at {retry.created_at}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
