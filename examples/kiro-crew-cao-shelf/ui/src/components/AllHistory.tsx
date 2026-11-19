import { Card, CardTitle, ErrorNotice } from '@kirocrew/app-sdk/ui'
import { useEffect, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import { ageSince, fmtAge, helperName } from '../format'
import type { AllHistory as AllHistoryData } from '../types'

export function AllHistory() {
  const { api, now } = useShelfCtx()
  const [d, setD] = useState<AllHistoryData | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let dead = false
    api
      .allHistory()
      .then((r) => !dead && setD(r))
      .catch((e) => !dead && setError(toShelfError(e).message || 'History could not be read.'))
    return () => {
      dead = true
    }
  }, [api])
  const sorted = d ? [...d.closed].sort((a, b) => Date.parse(b.last_at) - Date.parse(a.last_at)) : []
  return (
    <Card className="cs-box" role="region" aria-label="All history">
      <CardTitle className="cs-box-title">
        Closed helpers, newest first <small className="cs-muted">(from inbox rows; CAO deleted the terminals)</small>
      </CardTitle>
      <ErrorNotice className="cs-gap" message={error} />
      {!d && !error ? <p className="cs-muted">Loading...</p> : null}
      {d ? (
        <>
          <ul>
            {sorted.slice(0, 8).map((c) => (
              <li key={`${c.session}/${c.terminal_id}`}>
                <b>{helperName(null, c.terminal_id)}</b> in {c.session}, last message {fmtAge(ageSince(c.last_at, now))} ago ({c.rows} {c.rows === 1 ? 'row' : 'rows'})
              </li>
            ))}
          </ul>
          {sorted.length > 8 ? <small className="cs-muted">and {sorted.length - 8} older</small> : null}
          {sorted.length === 0 ? <p className="cs-muted">none recorded</p> : null}
          {!d.complete ? <p className="cs-muted">Some sessions were not read yet or their window was full, so older closed helpers may be missing.</p> : null}
        </>
      ) : null}
    </Card>
  )
}
