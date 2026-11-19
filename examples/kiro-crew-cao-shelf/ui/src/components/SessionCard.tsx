import { Badge, Card } from '@kirocrew/app-sdk/ui'
import { useId, useState } from 'react'
import { useShelfCtx } from '../context'
import { ageWords } from '../format'
import type { Session } from '../types'
import { AnswerBox, ErrorNeed } from './AnswerBox'
import { AskForm } from './AskForm'
import { Button } from './Button'
import { Chip, Dot, statusText } from './Chip'
import { HistoryDrawer } from './HistoryDrawer'
import { NO_CONDUCTOR, RelayControls } from './RelayControls'

export const MAX_CHIPS = 5

const POLL_NOTE: Record<Session['poll_class'], string> = { hot: ' (read every poll)', parked: ' (read about every 15 s)', stale: ' (read about every 60 s)' }

const TIER_WORD: Record<Session['tier'], string> = { needs_you: 'needs you', live: 'live', parked: 'parked', stale: 'stale' }

/** The tier Badge's variant: the host's text-safe colours, never a raw status hue as text. */
function tagVariant(s: Session): 'warn' | 'err' | 'ok' | 'muted' {
  if (s.tier === 'needs_you') return s.tier_reason === 'error' ? 'err' : 'warn'
  return s.tier === 'live' ? 'ok' : 'muted'
}

export function tagText(s: Session, now: string): string {
  const age = ageWords(s.last_active, now)
  switch (s.tier) {
    case 'needs_you':
      return s.tier_reason === 'error' ? 'NEEDS YOU, error' : 'NEEDS YOU'
    case 'live':
      return 'LIVE'
    case 'parked':
      if (s.tier_reason === 'age_unknown') return 'PARKED, age unknown'
      return s.warm ? `WARM, idle ${age}` : `PARKED, idle ${age}`
    default:
      return `STALE, idle ${age}`
  }
}

export function SessionCard({
  s,
  onStop,
  onRemote,
}: {
  s: Session
  onStop: (name: string, opener: HTMLElement | null) => void
  onRemote: (s: Session) => void
}) {
  const { now, disabledReason, reasonId } = useShelfCtx()
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const [ask, setAsk] = useState(false)
  const [hist, setHist] = useState(false)
  const c = s.conductor
  // Without a conductor the first terminal is an orphaned helper: it gets a chip like the others.
  const chipped = s.conductor_missing ? [c, ...s.helpers] : s.helpers
  const shown = chipped.slice(0, MAX_CHIPS)
  const more = chipped.length - shown.length
  const all = [c, ...s.helpers]
  const orphans = all.filter((t) => t.orphan)
  const askOff = s.conductor_missing
  const askReason = askOff ? 'This session has no conductor, so there is nobody to ask.' : disabledReason
  const hasError = s.tier === 'needs_you' && s.tier_reason === 'error'
  const remoteReason = s.conductor_missing ? NO_CONDUCTOR : disabledReason
  // a turned-off control points at the reason already on screen: the no-conductor note or the banner
  const offNote = `${id}-off`
  const askDescribed = askOff ? offNote : reasonId
  const remoteDescribed = s.conductor_missing ? offNote : reasonId

  return (
    <Card role="article" className={`cs-card cs-k-${s.tier}${hasError ? ' cs-err' : ''}`} aria-label={`session ${s.name}, ${TIER_WORD[s.tier]}`}>
      <div className="cs-head">
        <button type="button" className="cs-ttl" aria-expanded={expanded} aria-controls={`${id}-det`} onClick={() => setExpanded((e) => !e)}>
          <span className="cs-nm">{s.name}</span>
        </button>
        <Badge variant={tagVariant(s)} className="cs-tag">
          {tagText(s, now)}
        </Badge>
      </div>
      {s.conductor_missing ? (
        <div className="cs-cond">
          <span className="cs-prof">no conductor</span>
          <small className="cs-muted">it was deleted; every terminal here is a helper of it</small>
        </div>
      ) : (
        <div className="cs-cond">
          <Dot status={c.status} />
          <span className="cs-prof">conductor{c.profile ? ` (${c.profile})` : ''}</span>
          <small className="cs-muted">{statusText(c, now)}</small>
        </div>
      )}
      {chipped.length > 0 ? (
        <ul className="cs-chips" aria-label={`helpers of ${s.name}`}>
          {shown.map((h) => (
            <Chip key={h.id} t={h} />
          ))}
          {more > 0 ? (
            <li>
              <button type="button" className="cs-chip" onClick={() => setExpanded(true)} aria-label={`${more} more helpers, show details`}>
                +{more}
              </button>
            </li>
          ) : null}
        </ul>
      ) : null}
      {orphans.map((o) => (
        <p key={o.id} className="cs-note cs-note-orphan">
          {o.label}: caller no longer exists
        </p>
      ))}
      {s.conductor_missing ? (
        <p className="cs-note cs-note-orphan" id={offNote}>
          Ask is off: with no conductor there is nobody to ask. You can still answer a waiting helper or stop the session.
        </p>
      ) : null}
      {s.asks.map((a) => (a.status === 'waiting_user_answer' ? <AnswerBox key={a.terminal_id} ask={a} /> : <ErrorNeed key={a.terminal_id} ask={a} now={now} />))}
      {s.in_flight ? <div className="cs-hof">conductor working, waiting on helper {s.in_flight.label}</div> : null}
      {expanded ? (
        <div className="cs-det" id={`${id}-det`}>
          <table>
            <caption className="cs-sr">Terminals of {s.name}</caption>
            <thead>
              <tr>
                <th scope="col">terminal</th>
                <th scope="col">status</th>
                <th scope="col">last_active</th>
                <th scope="col">caller_id</th>
              </tr>
            </thead>
            <tbody>
              {all.map((t) => (
                <tr key={t.id}>
                  <td>{t.id}</td>
                  <td>{t.status}</td>
                  <td title={t.last_active}>{t.last_active ? `${ageWords(t.last_active, now)} ago` : 'age unknown'}</td>
                  <td>{t.role === 'conductor' ? 'none' : t.role === 'peer' ? 'none (peer)' : t.orphan ? `${t.caller_id} (gone)` : t.caller_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            Status is read off the screen, so treat it as a hint. last_active is the better clock. Poll class: {s.poll_class}{POLL_NOTE[s.poll_class]}.
          </p>
        </div>
      ) : null}
      <RelayControls s={s} />
      <div className="cs-acts">
        <Button aria-expanded={ask} onClick={() => setAsk((a) => !a)} disabled={!!askReason} title={askReason ?? undefined} aria-describedby={askReason ? askDescribed : undefined}>
          Ask
        </Button>
        <Button aria-expanded={hist} onClick={() => setHist((h) => !h)}>
          History{s.history_count === null ? '' : ` (${s.history_count})`}
        </Button>
        <Button aria-haspopup="dialog" onClick={() => onRemote(s)} disabled={!!remoteReason} title={remoteReason ?? undefined} aria-describedby={remoteReason ? remoteDescribed : undefined}>
          Remote control
        </Button>
        <Button danger onClick={(e) => onStop(s.name, e.currentTarget)} disabled={!!disabledReason} title={disabledReason ?? undefined} aria-describedby={reasonId}>
          Stop...
        </Button>
      </div>
      {ask && !askOff ? <AskForm session={s} onClose={() => setAsk(false)} /> : null}
      {hist ? <HistoryDrawer session={s.name} /> : null}
    </Card>
  )
}
