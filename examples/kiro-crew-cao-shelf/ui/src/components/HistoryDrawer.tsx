import { ErrorNotice } from '@kirocrew/app-sdk/ui'
import { useEffect, useId, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import { ageSince, fmtAge, helperName } from '../format'
import type { HistoryResponse, HistoryRows } from '../types'
import { Button } from './Button'

function Rows({ session, terminalId }: { session: string; terminalId: string }) {
  const { api, now } = useShelfCtx()
  const [rows, setRows] = useState<HistoryRows['rows'] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let dead = false
    api
      .historyRows(session, terminalId)
      .then((r) => !dead && setRows(r.rows))
      .catch((e) => !dead && setError(toShelfError(e).message))
    return () => {
      dead = true
    }
  }, [api, session, terminalId])
  if (error) return <ErrorNotice message={error} />
  if (!rows) return <div className="cs-rows">Loading rows...</div>
  return (
    <div className="cs-rows">
      {rows.length === 0 ? 'No rows.' : null}
      {rows.map((r) => (
        <div key={String(r.id)}>
          {fmtAge(ageSince(r.created_at, now))} ago [{r.status}] {r.text}
        </div>
      ))}
    </div>
  )
}

export function HistoryDrawer({ session }: { session: string }) {
  const { api, now } = useShelfCtx()
  const id = useId()
  const [h, setH] = useState<HistoryResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Record<string, boolean>>({})

  useEffect(() => {
    let dead = false
    api
      .history(session)
      .then((r) => !dead && setH(r))
      .catch((e) => !dead && setError(toShelfError(e).message || 'History could not be read.'))
    return () => {
      dead = true
    }
  }, [api, session])

  return (
    <div className="cs-hist" id={id}>
      <b>Closed helpers</b> <small>rebuilt from inbox rows; their terminals no longer exist</small>
      <ErrorNotice className="cs-gap" message={error} />
      {!h && !error ? <small>Loading...</small> : null}
      {h ? (
        <>
          {h.closed.length === 0 ? <small>none recorded</small> : null}
          <ul>
            {h.closed.map((c) => (
              <li key={c.terminal_id}>
                <b>{helperName(c.label, c.terminal_id)}</b>, {c.rows} {c.rows === 1 ? 'row' : 'rows'}, last message {fmtAge(ageSince(c.last_at, now))} ago{' '}
                <Button quiet aria-expanded={!!open[c.terminal_id]} onClick={() => setOpen((o) => ({ ...o, [c.terminal_id]: !o[c.terminal_id] }))}>
                  inbox rows
                </Button>
                <small>last status: {c.last_status}</small>
                {open[c.terminal_id] ? <Rows session={session} terminalId={c.terminal_id} /> : null}
              </li>
            ))}
          </ul>
          {h.external_senders.length > 0 ? (
            <small>
              Other senders (still sending, not helpers): {h.external_senders.map((s) => `${s.sender_id} (${s.rows} ${s.rows === 1 ? 'row' : 'rows'})`).join(', ')}
            </small>
          ) : null}
          {!h.complete ? <small>Only the newest {h.window_rows} inbox rows were read, so older closed helpers may be missing.</small> : null}
        </>
      ) : null}
    </div>
  )
}
