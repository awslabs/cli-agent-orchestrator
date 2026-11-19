import { Badge } from '@kirocrew/app-sdk/ui'
import { useShelfCtx } from '../context'
import { ageWords } from '../format'
import type { Terminal } from '../types'

/** Ages are computed from `last_active` against `now` (the page's ticking clock), not read from the
 *  shelf, so they keep moving between polls while the backend answers "unchanged". */
export function statusText(t: Terminal, now: string): string {
  const age = ageWords(t.last_active, now)
  const known = age !== 'age unknown'
  switch (t.status) {
    case 'idle':
      return known ? `idle ${age}` : 'idle, age unknown'
    case 'waiting_user_answer':
      return known ? `waiting about ${age}` : 'waiting, age unknown'
    case 'unknown':
      return known ? `status unknown, idle ${age}` : 'status unknown, age unknown'

    default:
      return t.status
  }
}

export function Dot({ status }: { status: Terminal['status'] }) {
  // Idle (and unknown) is a hollow dot; the status word is always printed next to it.
  return <i className={`cs-dot cs-dot-${status}`} aria-hidden="true" />
}

export function Chip({ t }: { t: Terminal }) {
  const { now } = useShelfCtx()
  return (
    <li className={`cs-chip${t.orphan ? ' cs-chip-orphan' : ''}`}>
      <Dot status={t.status} />
      <span>{t.label}</span>
      <small>{statusText(t, now)}</small>
      {t.orphan ? <Badge variant="err">ORPHAN</Badge> : null}
      {t.role === 'peer' ? <small>(peer)</small> : null}
    </li>
  )
}
