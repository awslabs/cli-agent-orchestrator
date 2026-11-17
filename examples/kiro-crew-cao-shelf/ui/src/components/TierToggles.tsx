import { hours } from '../format'
import { Button } from './Button'
import type { ShelfResponse } from '../types'

export type TierKey = 'needs_you' | 'live' | 'parked' | 'stale'
export type TierVisibility = Record<TierKey, boolean>

export function TierToggles({ data, visible, onToggle }: { data: ShelfResponse; visible: TierVisibility; onToggle: (k: TierKey) => void }) {
  const t = data.counts.by_tier
  const items: Array<[TierKey, string, number]> = [
    ['needs_you', 'Needs you', t.needs_you],
    ['live', 'Live', t.live],
    ['parked', 'Parked', t.parked],
    ['stale', `Stale, over ${hours(data.thresholds.stale_hours)} h`, t.stale],
  ]
  return (
    <div className="cs-tiers" role="group" aria-label="Tiers to show">
      {items.map(([k, label, n]) => (
        <Button key={k} className="cs-tier" aria-pressed={visible[k]} onClick={() => onToggle(k)}>
          {label} <b>{n}</b>
          {k === 'stale' && !visible.stale ? ' (hidden)' : ''}
        </Button>
      ))}
    </div>
  )
}

export function HonestLine({ data }: { data: ShelfResponse }) {
  const s = data.counts.by_status
  return (
    <p className="cs-honest">
      <b>{data.counts.terminals}</b> terminals: <b>{s.processing}</b> processing, <b>{s.waiting_user_answer}</b> waiting for you,{' '}
      {s.error > 0 ? (
        <>
          <b>{s.error}</b> in error,{' '}
        </>
      ) : null}
      {s.completed > 0 ? (
        <>
          <b>{s.completed}</b> just completed,{' '}
        </>
      ) : null}
      {s.unknown > 0 ? (
        <>
          <b>{s.unknown}</b> unknown,{' '}
        </>
      ) : null}
      <b>{s.idle}</b> idle. Idle is not running.
    </p>
  )
}
