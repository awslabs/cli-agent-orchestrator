import { useCallback, useMemo, useState } from 'react'
import { AlertTriangle, Loader2, Pause, Play, Square } from 'lucide-react'
import {
  api,
  CohortMemberOutcome,
  CohortOperation,
  CohortProvenance,
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
 * There is deliberately no safe-Pause button: a safe Pause consumes M3-D's
 * drain receipt and per-member classification, which is not something a
 * dashboard can produce. Offering a button that quietly did a *force* pause
 * instead is exactly the mislabelling this whole surface exists to prevent.
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

type Action = 'pause-force' | 'stop-safe' | 'stop-force' | 'resume-paused' | 'resume-start'

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

  const summary = outcomeSummary(result?.provenance)

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
          {summary.lost > 0 && summary.undecided === 0 && (
            <p className="mt-2 text-xs text-yellow-300">
              The fleet is running. {summary.lost} member
              {summary.lost === 1 ? '' : 's'} did not come back; the supervisor was told.
            </p>
          )}
          {summary.undecided > 0 && (
            <p className="mt-2 text-xs text-orange-300">
              {summary.undecided} member{summary.undecided === 1 ? '' : 's'} have no decided
              outcome yet — this operation needs reconciliation.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
