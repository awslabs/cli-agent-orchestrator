import { ErrorNotice, Modal } from '@kirocrew/app-sdk/ui'
import { useEffect, useRef, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import { ageWords } from '../format'
import type { StopExecuteResponse, StopPlan } from '../types'
import { Button } from './Button'

export type StopTarget = { scope: 'stale' } | { scope: 'named'; name: string }

const OUTCOME: Record<StopExecuteResponse['results'][number]['outcome'], string> = {
  stopped: 'stopped',
  skipped_changed: 'skipped, it changed since the list was made',
  skipped_working: 'skipped, it is working or waiting',
  skipped_not_in_plan: 'skipped, it was not in the list',
  not_found: 'already gone',
  failed: 'failed',
}

/** One dialog, two entry points. Nothing is ticked at first; only ticked, listed sessions are sent.
 *  Closing it stops nothing, so Escape and a backdrop click close it, except while a stop is in flight. */
export function StopDialog({ target, onClose }: { target: StopTarget; onClose: () => void }) {
  const { api, now, announce, refresh } = useShelfCtx()
  const stale = target.scope === 'stale'
  const [plan, setPlan] = useState<StopPlan | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ticked, setTicked] = useState<Record<string, boolean>>({})
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState<StopExecuteResponse['results'] | null>(null)
  const closeBtn = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (results) closeBtn.current?.focus()
  }, [results])

  useEffect(() => {
    let dead = false
    api
      .stopPlan(target.scope === 'stale' ? { scope: 'stale' } : { scope: 'named', sessions: [target.name] })
      .then((p) => !dead && setPlan(p))
      .catch((e) => !dead && setError(toShelfError(e).message || 'The list could not be made.'))
    return () => {
      dead = true
    }
  }, [api, target])

  const sessions = plan?.sessions ?? []
  const picked = sessions.filter((s) => ticked[s.name])
  const terminals = picked.reduce((n, s) => n + s.terminals.length, 0)
  const workingListed = sessions.filter((s) => s.working)
  const workingPicked = picked.filter((s) => s.working)
  const needAck = !stale && workingPicked.length > 0
  // in flight the red button stays enabled but busy, so focus stays on it while the result comes back
  const stoppable = !!plan && picked.length > 0 && (!needAck || ack) && !results
  const canStop = stoppable && !busy

  const execute = async () => {
    if (!plan || !canStop) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.stopExecute({
        plan_id: plan.plan_id,
        sessions: picked.map((s) => (needAck && s.working ? { name: s.name, last_active: s.last_active, acknowledge_working: true } : { name: s.name, last_active: s.last_active })),
      })
      setResults(r.results)
      const n = r.results.filter((x) => x.outcome === 'stopped').length
      announce(`Stopped ${n} of ${r.results.length} sessions`)
      refresh()
    } catch (e) {
      const se = toShelfError(e)
      setError(se.code === 'plan_expired' ? 'This list expired. Close the dialog and open it again.' : se.message || 'Stopping failed.')
    } finally {
      setBusy(false)
    }
  }

  const title = stale ? 'Clean up stale sessions' : `Stop session ${target.name}?`

  const footer = results ? (
    <div className="cs-actions">
      <Button primary ref={closeBtn} onClick={onClose}>
        Done
      </Button>
    </div>
  ) : (
    <div className="cs-actions">
      {stale && sessions.length > 0 ? (
        <Button quiet className="cs-push" onClick={() => setTicked(Object.fromEntries(sessions.map((s) => [s.name, true])))}>
          Select all
        </Button>
      ) : null}
      <Button onClick={onClose} disabled={busy} title={busy ? 'Stopping is in progress; the result shows here.' : undefined}>
        Cancel
      </Button>
      <Button danger disabled={!stoppable} busy={busy} onClick={execute}>
        Stop {picked.length} session{picked.length === 1 ? '' : 's'} ({terminals} terminal{terminals === 1 ? '' : 's'})
      </Button>
    </div>
  )

  return (
    <Modal open onClose={onClose} title={<span className="cs-dlg-title">{title}</span>} maxWidth={520} dismissDisabled={busy} footer={footer}>
      {results ? (
        <ul className="cs-list" aria-label="Results">
          {results.map((r) => (
            <li key={r.name}>
              <b>{r.name}</b>: {OUTCOME[r.outcome]}
              {r.message ? <small> ({r.message})</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <div className="cs-stack">
          <small className="cs-small">Nothing is stopped until you press the red button. A stopped session does not come back.</small>
          <ErrorNotice message={error} />
          {!plan && !error ? <p role="status">Making the list...</p> : null}
          {plan && sessions.length === 0 ? <p>Nothing is stale right now.</p> : null}
          {plan?.truncated ? <p className="cs-note">Only the first {sessions.length} stale sessions (oldest idle first) are listed. Run clean-up again for the rest.</p> : null}
          {sessions.length > 0 ? (
            <ul className="cs-list">
              {sessions.map((s) => (
                <li key={s.name}>
                  <label className="cs-checkrow">
                    <input type="checkbox" className="cs-check" checked={!!ticked[s.name]} onChange={(e) => setTicked((t) => ({ ...t, [s.name]: e.target.checked }))} />
                    <span>
                      <b>{s.name}</b>, {s.last_active ? 'idle ' : ''}
                      {ageWords(s.last_active, now)}
                      {s.working ? ' (working or waiting)' : ''}
                      <small className="cs-small">
                        stops {s.terminals.length} terminal{s.terminals.length === 1 ? '' : 's'}: {s.terminals.map((t) => (t.role === 'conductor' ? 'conductor' : t.label)).join(', ')}
                      </small>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          ) : null}
          {workingListed.length > 0 ? (
            <ErrorNotice
              title="Heads up:"
              message={`${workingListed.map((s) => s.name).join(', ')} ${workingListed.length === 1 ? 'is' : 'are'} working or waiting for you right now. Stopping it throws that work away.`}
            />
          ) : null}
          {needAck ? (
            <label className="cs-checkrow">
              <input type="checkbox" className="cs-check" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              <span>I understand this throws away work in progress.</span>
            </label>
          ) : null}
        </div>
      )}
    </Modal>
  )
}
