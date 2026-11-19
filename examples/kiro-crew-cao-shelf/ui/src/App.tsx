import { useAppInfo, useNavBadge } from '@kirocrew/app-sdk'
import { EmptyState, ErrorNotice, PageHeader, Skeleton } from '@kirocrew/app-sdk/ui'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useShelfApi } from './api'
import { AllHistory } from './components/AllHistory'
import { Button } from './components/Button'
import { RemoteControlIntro } from './components/RelayControls'
import { RelayDialog } from './components/RelayDialog'
import { Section } from './components/Section'
import { SessionCard } from './components/SessionCard'
import { Settings } from './components/Settings'
import { StartDialog } from './components/StartDialog'
import { type StopTarget, StopDialog } from './components/StopDialog'
import { HonestLine, type TierKey, type TierVisibility, TierToggles } from './components/TierToggles'
import { ShelfContext } from './context'
import { ageSince, fmtAge, hours } from './format'
import { useShelf } from './hooks/useShelf'
import { CSS } from './styles'
import type { Session, ShelfResponse } from './types'

const TIER_KEY = 'cao-shelf:tiers'
const DEFAULT_TIERS: TierVisibility = { needs_you: true, live: true, parked: true, stale: false }

function loadTiers(): TierVisibility {
  try {
    const raw = JSON.parse(localStorage.getItem(TIER_KEY) ?? 'null') as Partial<TierVisibility> | null
    if (raw && typeof raw === 'object') {
      return {
        needs_you: raw.needs_you ?? DEFAULT_TIERS.needs_you,
        live: raw.live ?? DEFAULT_TIERS.live,
        parked: raw.parked ?? DEFAULT_TIERS.parked,
        stale: raw.stale ?? DEFAULT_TIERS.stale,
      }
    }
  } catch {
    /* unreadable: use defaults */
  }
  return DEFAULT_TIERS
}

type Problem = { kind: 'unreachable' | 'auth' | 'incompatible' | 'error' | 'address'; text: string }

function caoProblem(d: ShelfResponse): Problem | null {
  if (d.cao.reachable) return null
  const url = d.cao.base_url
  switch (d.cao.error?.code) {
    case 'cao_auth_required':
      return { kind: 'auth', text: 'CAO answered 401 or 403: it has authentication on. This version of the CAO Shelf cannot sign in to an auth-enabled CAO.' }
    case 'cao_incompatible':
      return { kind: 'incompatible', text: `CAO at ${url} answered in a shape this version does not understand: ${d.cao.error.message}` }
    case 'cao_error':
      return { kind: 'error', text: `CAO at ${url} returned an error: ${d.cao.error.message}` }
    case 'forbidden_host':
      return { kind: 'address', text: `The CAO address ${url} is not allowed. Only http on 127.0.0.1, localhost or ::1 works. Change it in Settings.` }
    default:
      return { kind: 'unreachable', text: `CAO is not reachable at ${url}.` }
  }
}

/** A state that holds `Date.now()` and refreshes every `ms`, so ages on screen keep moving. */
function useClock(ms: number): number {
  const [t, setT] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setT(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return t
}

const CLOCK_MS = 15000
const REASON_ID = 'cs-reason'

export default function App() {
  const api = useShelfApi()
  const { data, receivedAt, error, loading, refresh } = useShelf()
  const tickMs = useClock(CLOCK_MS)
  const { active } = useAppInfo()
  const setBadge = useNavBadge()
  const [tiers, setTiers] = useState<TierVisibility>(loadTiers)
  const [message, setMessage] = useState('')
  const [stop, setStop] = useState<StopTarget | null>(null)
  const [start, setStart] = useState(false)
  const [remote, setRemote] = useState<Session | null>(null)
  const [allHistory, setAllHistory] = useState(false)
  const [settings, setSettings] = useState(false)
  const prevNeeds = useRef<Set<string> | null>(null)

  const toggle = (k: TierKey) =>
    setTiers((t) => {
      const next = { ...t, [k]: !t[k] }
      try {
        localStorage.setItem(TIER_KEY, JSON.stringify(next))
      } catch {
        /* storage unavailable: the choice lasts for this page view */
      }
      return next
    })

  const announce = useCallback((m: string) => setMessage(m), [])

  const needs = data?.counts.by_tier.needs_you
  useEffect(() => {
    if (needs !== undefined) setBadge(needs)
  }, [needs, setBadge])

  useEffect(() => {
    if (!data) return
    const now = new Set(data.sessions.filter((s) => s.tier === 'needs_you').map((s) => s.name))
    if (prevNeeds.current) {
      const fresh = [...now].filter((n) => !prevNeeds.current!.has(n))
      if (fresh.length > 0) setMessage(`${fresh.join(', ')} needs you`)
    }
    prevNeeds.current = now
  }, [data])

  const problem = data ? caoProblem(data) : null
  const noSnapshot = !!data && !data.cao.reachable && !data.stale_snapshot
  const backendDown = !!error
  const disabledReason = backendDown ? 'The CAO Shelf backend is not reachable.' : problem ? 'CAO is not reachable.' : null
  // one banner says why actions are off; the controls it turns off point at it
  const problemBanner = !!problem && !!data && !noSnapshot
  const backendBanner = backendDown && !!data
  const reasonId = disabledReason && (problemBanner || backendBanner) ? REASON_ID : undefined

  // The page's "now" is the backend's clock as of the last full answer, moved on by this page's own
  // elapsed time, so ages follow `last_active` between polls and a skewed browser clock does not show.
  // The age of a stale snapshot is taken from `last_good_at` against this clock too: the backend
  // answers "unchanged" while CAO stays down, so `snapshot_age_seconds` would be frozen.
  const generatedAt = data?.generated_at
  const now = useMemo(() => {
    if (!generatedAt) return new Date(tickMs).toISOString()
    const base = Date.parse(generatedAt)
    return new Date(Math.max(base, tickMs - (receivedAt - base))).toISOString()
  }, [generatedAt, receivedAt, tickMs])
  const ctx = useMemo(
    () => ({ api, disabledReason, reasonId, announce, refresh, now }),
    [api, disabledReason, reasonId, announce, refresh, now],
  )

  const grouped = useMemo(() => {
    const g: Record<TierKey, Session[]> = { needs_you: [], live: [], parked: [], stale: [] }
    for (const s of data?.sessions ?? []) g[s.tier].push(s)
    return g
  }, [data])

  const subtitle =
    data && !noSnapshot
      ? `${data.counts.sessions} CAO session${data.counts.sessions === 1 ? '' : 's'} at a glance: what needs you, what is working, what is parked.`
      : 'Your CAO sessions at a glance: what needs you, what is working, what is parked.'
  const hidden = data?.counts.hidden_sessions ?? 0
  const hiddenNote =
    hidden > 0 ? (
      <p className="cs-note" role="status">
        {hidden} session{hidden === 1 ? '' : 's'} hidden (unsupported name). The shelf can only address names of letters, digits, - and _ (up to 59 characters), so it cannot show, answer or stop {hidden === 1 ? 'it' : 'them'}. Use CAO for {hidden === 1 ? 'it' : 'them'}.
      </p>
    ) : null
  const stopFromCard = (name: string) => setStop({ scope: 'named', name })
  // the dialog follows the live session while it is open, and keeps the last copy if it goes away
  const remoteSession = remote ? (data?.sessions.find((x) => x.name === remote.name) ?? remote) : null

  const zeroSessions = !!data && !noSnapshot && data.sessions.length === 0
  const startButton = (
    <Button primary onClick={() => setStart(true)} disabled={!!disabledReason} title={disabledReason ?? undefined} aria-describedby={reasonId}>
      Start session
    </Button>
  )
  // with zero sessions the empty state holds the one Start session
  const header = <PageHeader title="CAO Shelf" subtitle={subtitle} actions={zeroSessions ? undefined : startButton} />

  let body
  if (!data && loading) {
    body = (
      <div className="cs-grid" aria-busy="true" aria-label="Loading sessions">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="cs-skel" />
        ))}
      </div>
    )
  } else if (!data) {
    body = (
      <EmptyState
        icon={null}
        title="Cannot reach the CAO Shelf backend."
        subtitle={`${error?.message ?? 'No answer yet.'} It starts with Kiro Crew; check that the app is enabled and trusted.`}
        action={
          <Button primary onClick={refresh}>
            Try again
          </Button>
        }
      />
    )
  } else if (noSnapshot) {
    body = (
      <EmptyState
        icon={null}
        title={problem?.kind === 'auth' ? 'This CAO needs a sign-in.' : 'Cannot reach CAO.'}
        subtitle={problem?.kind === 'unreachable' ? `Cannot reach CAO at ${data.cao.base_url}. Start it with cao-server, or change the address in Settings.` : problem?.text}
        action={
          <Button primary onClick={() => setSettings(true)}>
            Open Settings
          </Button>
        }
      />
    )
  } else if (data.sessions.length === 0) {
    body = (
      <>
        <EmptyState icon={null} title="No CAO sessions yet." subtitle="Start one and it shows up here as a card." action={startButton} />
        {hiddenNote}
      </>
    )
  } else {
    const dim = !!problem || backendDown
    body = (
      <div className={dim ? 'cs-dim' : undefined}>
        <TierToggles data={data} visible={tiers} onToggle={toggle} />
        <HonestLine data={data} />
        {hiddenNote}
        {(
          [
            ['needs_you', 'Needs you', 'Nothing needs you.'],
            ['live', 'Live now', 'Nothing is working.'],
            ['parked', `Parked, idle under ${hours(data.thresholds.stale_hours)} h`, undefined],
            ['stale', `Stale, over ${hours(data.thresholds.stale_hours)} h, dimmed`, undefined],
          ] as Array<[TierKey, string, string | undefined]>
        ).map(([k, title, empty]) =>
          tiers[k] ? (
            <Section key={k} title={title} count={grouped[k].length} empty={empty}>
              {grouped[k].map((s) => (
                <SessionCard key={s.name} s={s} onStop={stopFromCard} onRemote={setRemote} />
              ))}
            </Section>
          ) : null,
        )}
      </div>
    )
  }

  const staleCount = data?.counts.by_tier.stale ?? 0

  return (
    <ShelfContext.Provider value={ctx}>
      <style data-cao-shelf="">{CSS}</style>
      {header}
      <div className="cs-root cs-page px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0" data-active={active === false ? 'false' : 'true'}>
        {problemBanner && problem && data ? (
          // a problem banner implies a stale snapshot: without one the page shows the empty state instead
          <ErrorNotice
            id={backendBanner ? undefined : REASON_ID}
            className="cs-gap"
            title={problem.text}
            message={`Showing the last snapshot, ${fmtAge(ageSince(data.last_good_at ?? data.generated_at, now))} old. Actions are off until CAO answers.`}
          />
        ) : null}
        {backendBanner && data ? (
          <ErrorNotice
            id={REASON_ID}
            className="cs-gap"
            title="Lost contact with the CAO Shelf backend."
            message={`Showing the last snapshot, ${fmtAge(ageSince(data.last_good_at ?? data.generated_at, now))} old. Actions are off until it answers.`}
          />
        ) : null}
        <RemoteControlIntro />
        {settings ? <Settings onClose={() => setSettings(false)} /> : null}
        {body}
        {data && !noSnapshot && data.sessions.length > 0 ? (
          <div className="cs-foot">
            <Button danger onClick={() => setStop({ scope: 'stale' })} disabled={!!disabledReason || staleCount === 0} title={disabledReason ?? undefined} aria-describedby={reasonId}>
              Clean up stale ({staleCount})...
            </Button>
            <Button aria-expanded={allHistory} onClick={() => setAllHistory((a) => !a)}>
              All history{data.history_total === null ? '' : ` (${data.history_total} closed helpers)`}
            </Button>
            <Button aria-expanded={settings} onClick={() => setSettings((s) => !s)}>
              Settings
            </Button>
            <span>Cleanup always asks first and starts with nothing ticked.</span>
          </div>
        ) : null}
        {allHistory && data ? <AllHistory /> : null}
        {data && !noSnapshot && data.sessions.length === 0 && !settings ? (
          <div className="cs-foot">
            <Button onClick={() => setSettings(true)}>Settings</Button>
          </div>
        ) : null}
        <div className="cs-live-region" role="status" aria-live="polite" data-testid="live-region">
          {message}
        </div>
        {stop ? <StopDialog target={stop} onClose={() => setStop(null)} /> : null}
        {start ? <StartDialog onClose={() => setStart(false)} /> : null}
        {remoteSession ? <RelayDialog s={remoteSession} onClose={() => setRemote(null)} /> : null}
      </div>
    </ShelfContext.Provider>
  )
}
