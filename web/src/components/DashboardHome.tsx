import { useState, useEffect, useRef, useMemo } from 'react'
import { useStore } from '../store'
import { api, Annotation, AnnotationsResponse, TerminalMeta } from '../api'
import { Bot, Zap, Package, Monitor, Terminal as TermIcon, Trash2, Mail, FileText, LogOut, Send, ChevronRight, ChevronDown, Users, Filter, ArrowDownUp } from 'lucide-react'
import { TerminalView } from './TerminalView'
import { ConfirmModal } from './ConfirmModal'
import { InboxPanel } from './InboxPanel'
import { StatusBadge, STATUS_CONFIG } from './StatusBadge'
import { OutputViewer } from './OutputViewer'
import { CampaignAnnotations, TerminalAnnotations } from './AnnotationChips'
import { WorkStateInfoButton } from './AnnotationDetails'
import { GlobalFilterBar, SessionFilterBar } from './FilterBar'
import { placeAnnotations, readAnnotations } from '../lib/annotations'
import { fmtAbs, fmtRel } from '../lib/time'
import {
  activeFilterCount,
  callerVocabulary,
  collectFacetDimensions,
  displayStatus,
  emptyFilters,
  fleetWideFacetKeys,
  groupDimensions,
  isFilterActive,
  lifecycleVocabulary,
  matchesFilters,
  profileVocabulary,
  providerVocabulary,
  STATUS_ORDER,
  FacetDimension,
  FilterState,
} from '../lib/filters'

// STATUS_ORDER / RENDERABLE_STATUSES / displayStatus live in lib/filters.ts
// now: the fold exists so that counting and filtering can never drift, and the
// filter predicate is the second consumer that forced it out of this file. The
// comments recording the two omissions that made it a defect (NOT_FIFO_MONITORED
// uncounted on a native-TUI fleet; STOPPED silently folding to UNKNOWN) moved
// with it. The render-side tables built from it stay here, because
// STATUS_META[s].dot is dereferenced unguarded below and every STATUS_ORDER
// entry MUST have a STATUS_CONFIG counterpart — see
// dashboardStatusOrderContract.test.tsx for what happens when one does not.
const STATUS_META: Record<string, { label: string; dot: string; text: string; pulse?: boolean }> = Object.fromEntries(
  Object.entries(STATUS_CONFIG).map(([k, v]) => [k, { label: v.label, dot: v.dotClass, text: v.textClass, pulse: v.pulse }])
)
STATUS_META['UNKNOWN'] = { label: 'Unknown', dot: 'bg-gray-500', text: 'text-gray-500' }

// Selected-pill backgrounds. Each entry uses the raw Tailwind palette family
// whose 400 shade IS that status's semantic-role token in tailwind.preset.cjs —
// success #34d399 = emerald-400, info #60a5fa = blue-400, accent #c084fc =
// purple-400, warning #fbbf24 = amber-400, danger #f87171 = red-400. Because
// `Record<string, string>` plus no `noUncheckedIndexedAccess` types a missing
// key as `string`, a status listed in STATUS_ORDER but absent here compiles
// cleanly and renders `class="... undefined"` — a selected pill with no
// selected appearance. NOT_FIFO_MONITORED is `info` in status.json, the same
// role as PROCESSING, so it takes the blue family.
const STATUS_ACTIVE_BG: Record<string, string> = {
  PROCESSING: 'bg-blue-900/40 border-blue-500/50 text-blue-300',
  NOT_FIFO_MONITORED: 'bg-blue-900/40 border-blue-500/50 text-blue-300',
  IDLE: 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300',
  WAITING_USER_ANSWER: 'bg-amber-900/40 border-amber-500/50 text-amber-300',
  ERROR: 'bg-red-900/40 border-red-500/50 text-red-300',
  COMPLETED: 'bg-purple-900/40 border-purple-500/50 text-purple-300',
  STOPPED: 'bg-gray-800/40 border-gray-500/50 text-gray-300',
  UNKNOWN: 'bg-gray-800/40 border-gray-500/50 text-gray-300',
}

function StatusSummary({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      {STATUS_ORDER.filter(s => counts[s] > 0).map(s => {
        const meta = STATUS_META[s]
        return (
          <span key={s} className="flex items-center gap-1 text-xs">
            <span className={`w-1.5 h-1.5 rounded-full ${meta.dot} ${meta.pulse ? 'animate-pulse' : ''}`} />
            <span className={meta.text}>{counts[s]}</span>
            <span className="text-gray-500">{meta.label}</span>
          </span>
        )
      })}
    </div>
  )
}

interface SessionWithTerminals {
  name: string
  status: string
  terminals: TerminalMeta[]
}

export function DashboardHome({ onNavigate }: { onNavigate: (tab: string) => void }) {
  const { sessions, terminalStatuses, setTerminalStatus, clearTerminalStatuses, showSnackbar, deleteSession } = useStore()
  const [profileCount, setProfileCount] = useState(0)
  const [sessionData, setSessionData] = useState<SessionWithTerminals[]>([])
  const [expandedSessions, setExpandedSessions] = useState<Set<string>>(new Set())
  const [liveTerminal, setLiveTerminal] = useState<{ id: string; provider?: string; agentProfile?: string | null } | null>(null)
  const [pendingClose, setPendingClose] = useState<TerminalMeta | null>(null)
  const [closingTerminal, setClosingTerminal] = useState<string | null>(null)
  const [inboxTerminalId, setInboxTerminalId] = useState<string | null>(null)
  const [outputTerminalId, setOutputTerminalId] = useState<string | null>(null)
  const [pendingExit, setPendingExit] = useState<TerminalMeta | null>(null)
  const [exitingTerminal, setExitingTerminal] = useState<string | null>(null)
  const [sendInputOpen, setSendInputOpen] = useState<Record<string, boolean>>({})
  const [sendInputValues, setSendInputValues] = useState<Record<string, string>>({})
  const [sendingInput, setSendingInput] = useState<string | null>(null)
  // Filter state. In-memory only, deliberately: the dashboard is served
  // unauthenticated over `tailscale serve`, so adding the first persisted or
  // URL-shared state is its own decision and not this one's. Global filters
  // gate session VISIBILITY; per-session filters narrow rows inside a
  // surviving card, keyed by session name so they survive collapse/expand and
  // the 5s refetch exactly the way expandedSessions does.
  const [globalFilters, setGlobalFilters] = useState<FilterState>(emptyFilters)
  const [sessionFilters, setSessionFilters] = useState<Record<string, FilterState>>({})
  // Collapsed by default below sm: a fully expanded bar measured over half the
  // 844px phone screen before a single session card. jsdom has no matchMedia;
  // the guarded default keeps the bar OPEN in unit tests so every control is
  // queryable without an expand click first.
  const [filtersOpen, setFiltersOpen] = useState<boolean>(() =>
    typeof window.matchMedia === 'function' ? window.matchMedia('(min-width: 640px)').matches : true,
  )
  const [sortOrder, setSortOrder] = useState<'desc' | 'asc'>('desc')
  const [pendingDeleteSession, setPendingDeleteSession] = useState<string | null>(null)
  const [deletingSession, setDeletingSession] = useState(false)
  const [annotations, setAnnotations] = useState<AnnotationsResponse | null>(null)
  /** The last /annotations poll failed; the payload on screen is unverified. */
  const [staleFetch, setStaleFetch] = useState(false)
  /** True once one full session-detail pass has landed — the fence's precondition. */
  const [rowsLoaded, setRowsLoaded] = useState(false)
  const seenSessionsRef = useRef<Set<string>>(new Set())

  const totalTerminals = sessionData.reduce((sum, s) => sum + s.terminals.length, 0)

  const fleetRows = useMemo(() => sessionData.flatMap(s => s.terminals), [sessionData])

  // Layer-1 vocabularies, scanned from the fleet. The bars decide whether a
  // scanned dimension is worth drawing (fewer than two options is not a
  // filter); these just report what is there.
  const livenessOptions = useMemo(() => lifecycleVocabulary(fleetRows), [fleetRows])
  const profileOptions = useMemo(() => profileVocabulary(fleetRows), [fleetRows])
  const providerOptions = useMemo(() => providerVocabulary(fleetRows), [fleetRows])
  const sessionOptions = useMemo(() => sessionData.map(s => s.name), [sessionData])

  // id -> caller_id, for the spawned-by subtree walk in matchesFilters. Built
  // over the WHOLE fleet: a caller in one session may have spawned a row in
  // another, and an unresolved hop is simply the top of the known tree.
  const callerOf = useMemo(() => {
    const map = new Map(fleetRows.map(t => [t.id, t.caller_id] as const))
    return (id: string) => map.get(id)
  }, [fleetRows])

  // Total-preserving by construction: StatusSummary draws only the statuses in
  // STATUS_ORDER, so a count filed under anything else is drawn by nothing and
  // silently disappears from the session's totals. That is not hypothetical —
  // terminal_projection.project_row reports the *lifecycle* vocabulary in
  // `status` ('superseded' / 'dead' / 'unknown-liveness') for every row whose
  // recorded identity no longer resolves, and the store uppercases those into
  // buckets STATUS_ORDER has never contained. Folding any unrecognised status
  // into UNKNOWN keeps the chips summing to the terminal count: an
  // unrecognised status must be visibly unknown, never invisible. It is
  // deliberately not fixed by extending STATUS_ORDER — see the note there.
  //
  // Uses the same displayStatus() fold as both filter sites, so every count in
  // the summary is reachable by clicking its pill.
  const getStatusCounts = (terminals: TerminalMeta[]) => {
    const counts: Record<string, number> = {}
    terminals.forEach(t => {
      const s = displayStatus(terminalStatuses[t.id])
      counts[s] = (counts[s] || 0) + 1
    })
    return counts
  }

  // Fetch session details with terminals
  useEffect(() => {
    const fetchAll = async () => {
      try {
        const sessionDetails = await Promise.all(
          sessions.map(async s => {
            try {
              const detail = await api.getSession(s.name)
              return { name: s.name, status: s.status, terminals: detail.terminals || [] }
            } catch {
              return { name: s.name, status: s.status, terminals: [] }
            }
          })
        )
        setSessionData(sessionDetails)
        // The annotation placement fence cannot run until the row set is
        // known — see the `placement` memo below.
        setRowsLoaded(true)
        // Auto-expand only newly seen sessions
        const newNames = sessionDetails.map(s => s.name).filter(n => !seenSessionsRef.current.has(n))
        newNames.forEach(n => seenSessionsRef.current.add(n))
        if (newNames.length > 0) {
          setExpandedSessions(prev => {
            const next = new Set(prev)
            newNames.forEach(n => next.add(n))
            return next
          })
        }
      } catch {}
    }
    fetchAll()
    const interval = setInterval(fetchAll, 5000)
    return () => clearInterval(interval)
  }, [sessions.map(s => s.id).join(',')])

  // Poll statuses
  useEffect(() => {
    const allIds = sessionData.flatMap(s => s.terminals.map(t => t.id))
    if (!allIds.length) return
    clearTerminalStatuses(allIds)
    const fetch = () => {
      allIds.forEach(id => {
        api.getTerminalStatus(id)
          .then(status => { if (status) setTerminalStatus(id, status) })
          .catch(() => {})
      })
    }
    fetch()
    const interval = setInterval(fetch, 3000)
    return () => clearInterval(interval)
  }, [sessionData.flatMap(s => s.terminals.map(t => t.id)).join(',')])

  useEffect(() => {
    api.listProfiles().then(p => setProfileCount(p.length)).catch(() => {})
  }, [])

  // Conductor annotations (§9.5). Failure-isolated in both directions: a 404
  // from a server without the route, a network error, and a body that is not
  // the documented shape all resolve to "no annotations", which renders
  // exactly as the dashboard did before this existed.
  //
  // The 5s interval is NOT chasing the producer's 30s tick — nothing new can
  // arrive in between. It re-evaluates FRESHNESS: `valid_until` passes while
  // the page sits open, and a chip must grey when it expires rather than when
  // the next document happens to land.
  //
  // A SINGLE FAILED POLL DOES NOT WIPE THE SURFACE. Discarding the payload on
  // one blip blanked every chip for 5s and then brought them back, which reads
  // as the fleet changing when nothing did. The last body is held and marked
  // unverified — the existing "partial data" marker — and only a run of
  // failures clears it, because at that point "I have not been able to check"
  // is the honest answer.
  useEffect(() => {
    let failures = 0
    const fetchAnnotations = () => {
      api.getAnnotations()
        .then(body => {
          failures = 0
          setStaleFetch(false)
          setAnnotations(readAnnotations(body))
        })
        .catch(() => {
          failures += 1
          if (failures >= 3) {
            setAnnotations(null)
            setStaleFetch(false)
          } else {
            setStaleFetch(true)
          }
        })
    }
    fetchAnnotations()
    const interval = setInterval(fetchAnnotations, 5000)
    return () => clearInterval(interval)
  }, [])

  // Placement is computed against EVERY terminal in the fleet, not per session,
  // so an annotation naming a terminal in another session is attached there
  // rather than landing on the campaign surface as "orphaned".
  //
  // `rowsLoaded` is the whole reason this is not just `sessionData`. The two
  // fetches are independent effects and the session pass is a sequential loop,
  // so `/annotations` routinely lands first, against `sessionData === []`.
  // Classifying then announced every live worker as an `orphaned run` on every
  // load and every refresh — a confidently wrong claim, made by the surface
  // whose job is to report the fence.
  const placement = useMemo(() => {
    const rows = sessionData.flatMap(s =>
      s.terminals.map(t => ({ id: t.id, generation: t.generation ?? null })),
    )
    return placeAnnotations(annotations?.annotations ?? [], rows, rowsLoaded)
  }, [annotations, sessionData, rowsLoaded])

  const annotationsFor = (terminalId: string): Annotation[] | undefined =>
    placement.byTerminal[terminalId]

  // "Available" means the payload CARRIES annotations, not merely that the
  // route answered: an empty envelope and an absent route are the same
  // no-data fleet, and the byte-identical-DOM test pins them rendering alike.
  // `degraded` folds the envelope's own coverage report, the held-stale flag
  // and the server's omission count into the one marker the bars repeat. It
  // requires a payload to exist at all — the campaign surface uses the same
  // guard, because "unverified" is a claim about data ON screen, and with no
  // payload there is nothing on screen to be unverified.
  const annotationsAvailable = annotations !== null && annotations.annotations.length > 0
  const annotationsDegraded =
    annotations !== null &&
    (staleFetch ||
      annotations.coverage === 'partial' ||
      annotations.coverage === 'truncated' ||
      annotations.items_omitted > 0)

  // Derived facet dimensions, computed against the FULL fleet — never the
  // filtered subset, for the same reason placement is: a dimension discovered
  // only on visible rows would vanish the moment it did its job.
  //
  // THE GLOBAL/PER-SESSION SPLIT IS THREE SHAPE RULES (fleetWideFacetKeys),
  // not a key list: pill-shaped for the whole fleet, emitted in at least two
  // sessions, and carrying a vocabulary the sessions SHARE rather than
  // partition. A dimension tied to one campaign — a lane, a round, a task id,
  // a PR state — stays in its session's bar, which is what keeps an unbounded
  // or operator-action-dependent vocabulary off the fleet surface. Nothing
  // here knows what any facet is CALLED.
  const sessionDimensions = useMemo(() => {
    const out: Record<string, FacetDimension[]> = {}
    for (const s of sessionData) {
      out[s.name] = collectFacetDimensions(
        s.terminals.map(t => ({ annotations: placement.byTerminal[t.id] })),
      )
    }
    return out
  }, [sessionData, placement])
  const fleetDimensions = useMemo(
    () => collectFacetDimensions(fleetRows.map(t => ({ annotations: placement.byTerminal[t.id] }))),
    [fleetRows, placement],
  )
  const globalKeys = useMemo(
    () =>
      fleetWideFacetKeys(
        fleetDimensions,
        Object.values(sessionDimensions).map(dimensions => ({ dimensions })),
      ),
    [fleetDimensions, sessionDimensions],
  )
  const globalGroups = useMemo(
    () => groupDimensions(fleetDimensions.filter(d => globalKeys.has(d.key))),
    [fleetDimensions, globalKeys],
  )
  const sessionGroups = useMemo(() => {
    const out: Record<string, ReturnType<typeof groupDimensions>> = {}
    for (const [name, dims] of Object.entries(sessionDimensions)) {
      out[name] = groupDimensions(dims.filter(d => !globalKeys.has(d.key)))
    }
    return out
  }, [sessionDimensions, globalKeys])
  const sessionCallers = useMemo(() => {
    const out: Record<string, ReturnType<typeof callerVocabulary>> = {}
    for (const s of sessionData) out[s.name] = callerVocabulary(s.terminals)
    return out
  }, [sessionData])

  // Global filters run FIRST and gate session visibility — the behaviour the
  // old two-dimension version had, now over the full FilterState. A session
  // with zero terminals is always kept (the pre-existing rule), and the sort
  // key no longer comes from Math.max(...[]): that is -Infinity for an empty
  // session, -Infinity - -Infinity is NaN, and a NaN comparator is undefined
  // Array.prototype.sort behaviour — exactly the sessions this gate always
  // keeps were comparing against each other.
  const filteredSessions = useMemo(() => {
    const filtered = sessionData.filter(s =>
      s.terminals.length === 0 ||
      s.terminals.some(t =>
        matchesFilters(t, placement.byTerminal[t.id], globalFilters, {
          status: terminalStatuses[t.id],
          callerOf,
        }),
      ),
    )
    const sentAt = (s: SessionWithTerminals) =>
      s.terminals.reduce((latest, t) => {
        const at = t.last_active ? new Date(t.last_active).getTime() : 0
        return at > latest ? at : latest
      }, 0)
    return filtered.sort((a, b) => {
      const latestA = sentAt(a)
      const latestB = sentAt(b)
      return sortOrder === 'desc' ? latestB - latestA : latestA - latestB
    })
  }, [sessionData, globalFilters, sortOrder, terminalStatuses, placement, callerOf])

  const globalFilterCount = activeFilterCount(globalFilters)

  const updateSessionFilters = (name: string, next: FilterState) =>
    setSessionFilters(prev => ({ ...prev, [name]: next }))
  const clearSessionFilters = (name: string) =>
    setSessionFilters(prev => {
      if (!(name in prev)) return prev
      const next = { ...prev }
      delete next[name]
      return next
    })

  const handleDeleteTerminal = async () => {
    if (!pendingClose) return
    setClosingTerminal(pendingClose.id)
    try {
      await api.deleteTerminal(pendingClose.id)
      if (liveTerminal?.id === pendingClose.id) setLiveTerminal(null)
      showSnackbar({ type: 'success', message: `Terminal ${pendingClose.id} closed` })
    } catch {
      showSnackbar({ type: 'error', message: `Failed to close terminal` })
    }
    setClosingTerminal(null)
    setPendingClose(null)
  }

  const handleExitTerminal = async () => {
    if (!pendingExit) return
    setExitingTerminal(pendingExit.id)
    try {
      await api.exitTerminal(pendingExit.id)
      showSnackbar({ type: 'success', message: `Graceful exit sent` })
    } catch {
      showSnackbar({ type: 'error', message: `Failed to send exit` })
    }
    setExitingTerminal(null)
    setPendingExit(null)
  }

  const handleDeleteSession = async () => {
    if (!pendingDeleteSession) return
    setDeletingSession(true)
    try {
      await deleteSession(pendingDeleteSession)
    } catch {}
    setDeletingSession(false)
    setPendingDeleteSession(null)
  }

  const handleSendInput = async (terminalId: string) => {
    const message = (sendInputValues[terminalId] || '').trim()
    if (!message) return
    setSendingInput(terminalId)
    try {
      await api.sendInput(terminalId, message)
      setSendInputValues(prev => ({ ...prev, [terminalId]: '' }))
      showSnackbar({ type: 'success', message: 'Message sent' })
    } catch {
      showSnackbar({ type: 'error', message: 'Failed to send message' })
    }
    setSendingInput(null)
  }

  const toggleSession = (name: string) => {
    setExpandedSessions(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-emerald-900/50 flex items-center justify-center">
              <Users size={20} className="text-emerald-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{sessions.length}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Sessions</div>
            </div>
          </div>
        </div>
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-900/50 flex items-center justify-center">
              <TermIcon size={20} className="text-cyan-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{totalTerminals}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Running Agents</div>
            </div>
          </div>
        </div>
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-blue-900/50 flex items-center justify-center">
              <Package size={20} className="text-blue-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{profileCount}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Profiles</div>
            </div>
          </div>
        </div>
      </div>

      {/* Quick Actions */}
      <div className="flex gap-3 flex-wrap">
        <button onClick={() => onNavigate('agents')} className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors">
          <Bot size={16} /> Spawn Agent
        </button>
        <button onClick={() => onNavigate('flows')} className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors">
          <Zap size={16} /> Manage Flows
        </button>
      </div>

      {/* Terminal-independent annotations: unbound gates, orphaned runs and
          campaign-scoped work have somewhere visible to land instead of being
          dropped for want of a terminal row. Renders nothing at all when there
          is nothing to say, so a fleet with no annotations is unchanged. */}
      <CampaignAnnotations
        unplaced={placement.unplaced}
        fenced={placement.fenced}
        pending={placement.pending}
        omitted={annotations?.items_omitted ?? 0}
        degraded={
          annotations !== null &&
          (staleFetch ||
            annotations.coverage === 'partial' ||
            annotations.coverage === 'truncated')
        }
      />

      {/* Header with sort toggle */}
      <div className="mb-1">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Active Sessions</h3>
            <p className="text-xs text-gray-500 mt-1">
              Each session is a workspace where one or more AI agents run and collaborate.
            </p>
          </div>
          {/* `last_active` is when CAO last SENT input to a pane (only
              send_input / send_special_key move it), and on a v2 managed row
              it is frozen at row creation — so the sort is labelled by what
              it actually measures, and no "recently active" control exists
              anywhere for it to feed. */}
          <button onClick={() => setSortOrder(o => o === 'desc' ? 'asc' : 'desc')} className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-200 bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-lg transition-colors">
            <ArrowDownUp size={12} />
            {sortOrder === 'desc' ? 'Newest sent first' : 'Oldest sent first'}
          </button>
        </div>
      </div>

      {/* The filter bar. Reachability renders FIRST and unconditionally —
          outside the collapsible region — because it predates the bar and
          three suites pin its contract (it must render with no session or
          terminal data, and its button container must hold exactly the pills
          below and no other control). Everything else lives behind the
          toggle so the bar does not eat half a phone screen.

          Named "Reachability", never "status" and never "working": every live
          native-TUI v2 row reports NOT_FIFO_MONITORED unconditionally, which
          is a "this pane exists and answers" claim and says nothing about
          activity. Multi-select: OR within the dimension, AND across the
          dimensions in the panel below. */}
      <div data-testid="filter-bar" className="space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={() => setFiltersOpen(o => !o)}
            aria-expanded={filtersOpen}
            aria-controls="global-filter-panel"
            className="flex items-center gap-2 min-h-[44px] px-3 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <Filter size={12} />
            Filters
            {globalFilterCount > 0 && (
              <span className="text-emerald-300">{globalFilterCount} active</span>
            )}
            {filtersOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
          {globalFilterCount > 0 && (
            <button
              type="button"
              onClick={() => setGlobalFilters(emptyFilters())}
              className="min-h-[44px] px-3 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
            >
              Clear all
            </button>
          )}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Filter size={12} className="text-gray-500" />
          <span className="text-[10px] uppercase tracking-wide text-gray-400">Reachability</span>
          <button
            onClick={() => setGlobalFilters(f => ({ ...f, reachability: [] }))}
            className={`text-xs min-h-[44px] px-3 rounded-full border transition-colors ${globalFilters.reachability.length === 0 ? 'bg-gray-700 border-gray-500/50 text-gray-200' : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}
          >
            Any status
          </button>
          {STATUS_ORDER.map(s => {
            const meta = STATUS_META[s]
            const selected = globalFilters.reachability.includes(s)
            return (
              <button
                key={s}
                aria-pressed={selected}
                onClick={() =>
                  setGlobalFilters(f => ({
                    ...f,
                    reachability: selected ? f.reachability.filter(x => x !== s) : [...f.reachability, s],
                  }))
                }
                className={`flex items-center gap-1.5 text-xs min-h-[44px] px-3 rounded-full border transition-colors ${selected ? STATUS_ACTIVE_BG[s] : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}
              >
                <span className={`w-1.5 h-1.5 rounded-full ${meta.dot}`} />
                {meta.label}
              </button>
            )
          })}
        </div>
        {filtersOpen && (
          <div id="global-filter-panel">
            <GlobalFilterBar
              filters={globalFilters}
              onChange={setGlobalFilters}
              liveness={livenessOptions}
              profiles={profileOptions}
              providers={providerOptions}
              sessions={sessionOptions}
              groups={globalGroups}
              annotationsAvailable={annotationsAvailable}
              degraded={annotationsDegraded}
            />
          </div>
        )}
      </div>

      {/* Sessions */}
      {filteredSessions.length === 0 ? (
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-8 text-center">
          <Bot size={32} className="mx-auto text-gray-600 mb-3" />
          {sessionData.length === 0 ? (
            <>
              <p className="text-gray-400 text-sm">No active sessions.</p>
              <p className="text-gray-600 text-xs mt-1">Go to the <span className="text-emerald-400 cursor-pointer" onClick={() => onNavigate('agents')}>Agents tab</span> to spawn your first agent.</p>
            </>
          ) : (
            <>
              {/* The cause is named: the fleet is not empty, the FILTERS are
                  what hid it — and the recovery is one click, not a manual
                  tour of every dimension. */}
              <p className="text-gray-400 text-sm">No sessions match the current filter.</p>
              {globalFilterCount > 0 && (
                <button
                  type="button"
                  onClick={() => setGlobalFilters(emptyFilters())}
                  className="mt-3 min-h-[44px] px-4 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  Clear all filters
                </button>
              )}
            </>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {filteredSessions.map(session => {
            // Per-session filters run SECOND, inside the surviving card, AND-ed
            // with the global result. They can never remove the card — a
            // session-scoped question is meaningless the moment it deletes the
            // session it was asked about.
            const sessionFilter = sessionFilters[session.name]
            const visibleTerminals = session.terminals.filter(t =>
              matchesFilters(t, placement.byTerminal[t.id], globalFilters, {
                status: terminalStatuses[t.id],
                callerOf,
              }) &&
              (!sessionFilter ||
                matchesFilters(t, placement.byTerminal[t.id], sessionFilter, {
                  status: terminalStatuses[t.id],
                  callerOf,
                })),
            )
            const sessionFilterActive = !!sessionFilter && isFilterActive(sessionFilter)
            // The "N of M shown" counter is a THIRD thing beside the status
            // summary (which keeps counting ALL terminals — pinned by
            // dashboardStatusOrder.test.tsx) and the session-visibility gate:
            // the summary describes the session, the gate decides the card,
            // this describes the view.
            const counterVisible = isFilterActive(globalFilters) || sessionFilterActive
            const statusCounts = getStatusCounts(session.terminals)
            const sortedTerminals = [...visibleTerminals].sort((a, b) => {
              const ta = a.last_active ? new Date(a.last_active).getTime() : 0
              const tb = b.last_active ? new Date(b.last_active).getTime() : 0
              return sortOrder === 'desc' ? tb - ta : ta - tb
            })
            const grouped: Record<string, TerminalMeta[]> = {}
            sortedTerminals.forEach(t => {
              const key = t.agent_profile || 'default'
              ;(grouped[key] ??= []).push(t)
            })
            const typeSummary = Object.entries(
              session.terminals.reduce<Record<string, number>>((acc, t) => {
                const k = t.agent_profile || 'default'
                acc[k] = (acc[k] || 0) + 1
                return acc
              }, {})
            ).sort((a, b) => b[1] - a[1])
            const sessionLastActive = session.terminals.reduce<string | null>((latest, t) => {
              if (!t.last_active) return latest
              if (!latest) return t.last_active
              return new Date(t.last_active) > new Date(latest) ? t.last_active : latest
            }, null)
            const isExpanded = expandedSessions.has(session.name)
            // Session names are validated tmux names ([A-Za-z0-9_-] only), so
            // they are already safe to embed in an HTML id.
            const terminalsRegionId = `session-${session.name}-terminals`

            return (
              <div key={session.name} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden relative">
                {/* Delete session button */}
                <button
                  onClick={(e) => { e.stopPropagation(); setPendingDeleteSession(session.name) }}
                  className="absolute top-3 right-3 p-1.5 text-gray-600 hover:text-red-400 bg-gray-800/80 hover:bg-gray-700 rounded-lg transition-colors z-10"
                  title="Delete session"
                >
                  <Trash2 size={12} />
                </button>

                {/* Session header (§7.6) — a plain container: all metadata is
                    selectable/copyable text and never toggles the card.
                    Expand/collapse belongs exclusively to the chevron button. */}
                <div className="p-4 pr-12">
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => toggleSession(session.name)}
                      aria-label={isExpanded ? `Collapse session ${session.name}` : `Expand session ${session.name}`}
                      aria-expanded={isExpanded}
                      aria-controls={terminalsRegionId}
                      className="flex items-center justify-center shrink-0 min-h-[44px] min-w-[44px] rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-700/60 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <div className="flex-1 min-w-0 select-text cursor-default">
                      <div className="flex items-center gap-3">
                        <Users size={14} className="text-emerald-400" />
                        <span className="text-sm font-mono text-gray-200">{session.name}</span>
                        <span className="text-xs text-gray-500">{session.terminals.length} agent{session.terminals.length !== 1 ? 's' : ''}</span>
                        {/* Session filters survive a collapsed card (by design,
                            keyed by session name), so the card says when it is
                            holding rows back out of view. */}
                        {sessionFilterActive && (
                          <span className="text-[10px] text-emerald-400/80">filtered</span>
                        )}
                      </div>
                      <div className="ml-8 mt-1.5 flex flex-col gap-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          {typeSummary.map(([type, count]) => (
                            <span key={type} className="text-[10px] bg-gray-700/60 text-gray-400 px-1.5 py-0.5 rounded">{type}{count > 1 ? ` ×${count}` : ''}</span>
                          ))}
                        </div>
                        <StatusSummary counts={statusCounts} />
                        <div className="flex items-center gap-3 text-[10px] text-gray-600">
                          {/* "Last sent", not "Active": on a v2 managed row this
                              timestamp is frozen at row creation (only
                              send_input moves it, and only on the v1 table),
                              so calling it activity was a false claim made on
                              every managed fleet. */}
                          {sessionLastActive && (
                            <span title={fmtAbs(sessionLastActive) ? `${fmtAbs(sessionLastActive)} — when CAO last sent input to a pane in this session` : ''}>
                              Last sent {fmtRel(sessionLastActive)}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Terminals grouped by agent type */}
                {isExpanded && (
                  <div id={terminalsRegionId} className="border-t border-gray-700/30 px-4 pb-4 space-y-3 pt-3">
                    <SessionFilterBar
                      filters={sessionFilter ?? emptyFilters()}
                      onChange={next => updateSessionFilters(session.name, next)}
                      onClear={() => clearSessionFilters(session.name)}
                      callers={sessionCallers[session.name] ?? []}
                      groups={sessionGroups[session.name] ?? []}
                      shown={visibleTerminals.length}
                      total={session.terminals.length}
                      counterVisible={counterVisible}
                      degraded={annotationsDegraded}
                    />
                    {visibleTerminals.length === 0 ? (
                      // Reachable only through the SESSION filters: the global
                      // gate keeps a card only when at least one row matches
                      // it. The card stays, the count says so, and recovery is
                      // one click — the silently-empty card the drifted
                      // predicates produced is the defect this replaces.
                      <div className="text-center py-4 space-y-2">
                        <p className="text-xs text-gray-500">
                          0 of {session.terminals.length} shown — the session filters hide every row.
                        </p>
                        <button
                          type="button"
                          onClick={() => clearSessionFilters(session.name)}
                          className="min-h-[44px] px-4 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
                        >
                          Clear session filters
                        </button>
                      </div>
                    ) : (
                    Object.entries(grouped).map(([agentType, terminals]) => (
                      <div key={agentType}>
                        <div className="flex items-center gap-2 mb-2">
                          <Bot size={11} className="text-gray-500" />
                          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{agentType}</span>
                          <span className="text-[10px] text-gray-600">({terminals.length})</span>
                        </div>
                        <div className="space-y-1.5">
                          {terminals.map(t => {
                            const relActive = fmtRel(t.last_active)
                            return (
                              <div key={t.id} className="bg-gray-900/50 border border-gray-700/30 rounded-lg px-3 py-2 space-y-1.5">
                                {/* flex-wrap keeps narrow (mobile) widths from
                                    overflowing the card: the action buttons
                                    wrap under the identity line instead of
                                    forcing a wider-than-viewport layout. */}
                                <div className="flex flex-wrap items-center justify-between gap-y-1.5">
                                  {/* `flex-wrap` so the conductor chip group can
                                      take its own line at narrow widths. At 390
                                      the identity row measured 282px of space
                                      for 459px of content: flexbox shrank the
                                      `truncate` profile name to nothing and
                                      then clipped the chips off the card
                                      anyway, so one annotation deleted the only
                                      thing saying which worker the row is. */}
                                  <div className="flex items-center gap-2 min-w-0 flex-wrap">
                                    <TermIcon size={12} className="text-gray-500 shrink-0" />
                                    <span className="text-xs font-medium text-gray-300 truncate">{t.agent_profile || 'default'}</span>
                                    <span className="text-[10px] font-mono text-gray-600">{t.id.slice(0, 8)}</span>
                                    {/* Fork-owned status first, conductor chips
                                        after it. Never a replacement: `status`
                                        is the only reachability statement the
                                        fork can make, and `not_fifo_monitored`
                                        already IS one. */}
                                    <StatusBadge status={terminalStatuses[t.id] || null} />
                                    <TerminalAnnotations annotations={annotationsFor(t.id)} />
                                    {/* Same fallback the modals use: a blank
                                        gap and the word "unknown" are the same
                                        fact, and only one of them says so. */}
                                    <span className="text-[10px] text-gray-600">{t.provider || 'unknown'}</span>
                                  </div>
                                  <div className="flex items-center gap-1 shrink-0">
                                    {/* The complete work state, and the only
                                        path to it that a keyboard or a touch
                                        screen has — the chips' hover card is a
                                        pointer-only enhancement. Renders
                                        nothing when the row has no
                                        annotations, so a fleet without the
                                        conductor keeps this cluster exactly as
                                        it was. */}
                                    <WorkStateInfoButton annotations={annotationsFor(t.id)} terminalId={t.id} agentProfile={t.agent_profile} />
                                    <button onClick={() => setInboxTerminalId(t.id)} className="p-1 text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Inbox"><Mail size={12} /></button>
                                    <button onClick={() => setOutputTerminalId(t.id)} className="p-1 text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Output"><FileText size={12} /></button>
                                    <button onClick={() => setLiveTerminal({ id: t.id, provider: t.provider ?? undefined, agentProfile: t.agent_profile })} className="flex items-center gap-1 px-2 py-1 bg-emerald-600 hover:bg-emerald-500 text-white text-[10px] font-medium rounded transition-colors"><Monitor size={12} />Terminal</button>
                                    <button onClick={() => setPendingExit(t)} disabled={exitingTerminal === t.id} className="p-1 text-gray-500 hover:text-amber-400 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Graceful Exit"><LogOut size={12} /></button>
                                    <button onClick={() => setPendingClose(t)} disabled={closingTerminal === t.id} className="p-1 text-gray-500 hover:text-red-400 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Close"><Trash2 size={12} /></button>
                                  </div>
                                </div>
                                {/* Timestamps. `last_active` is the only one
                                    the projection publishes — there is no
                                    `created_at` on a projected row, and the
                                    branch that read one could never fire.
                                    Labelled by what it measures: when CAO last
                                    SENT input to this pane (frozen at row
                                    creation on a v2 managed row), never
                                    "activity". */}
                                <div className="flex items-center gap-3 text-[10px] text-gray-600">
                                  {relActive && (
                                    <span title={fmtAbs(t.last_active) ? `${fmtAbs(t.last_active)} — when CAO last sent input to this pane` : ''}>
                                      sent {relActive}
                                    </span>
                                  )}
                                </div>
                                {/* Quick Send */}
                                {!sendInputOpen[t.id] ? (
                                  <button onClick={() => setSendInputOpen(prev => ({ ...prev, [t.id]: true }))} className="text-[10px] text-gray-600 hover:text-gray-300 transition-colors">Message agent...</button>
                                ) : (
                                  <div className="flex items-center gap-1.5">
                                    <input type="text" value={sendInputValues[t.id] || ''} onChange={e => setSendInputValues(prev => ({ ...prev, [t.id]: e.target.value }))} onKeyDown={e => { if (e.key === 'Enter') handleSendInput(t.id) }} placeholder="Type a message..." className="flex-1 bg-gray-900 border border-gray-700 text-gray-200 text-[11px] font-mono rounded px-2 py-1 focus:border-emerald-500 focus:outline-none" autoFocus />
                                    <button onClick={() => handleSendInput(t.id)} disabled={sendingInput === t.id || !(sendInputValues[t.id] || '').trim()} className="flex items-center gap-1 px-2 py-1 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-[10px] font-medium rounded transition-colors"><Send size={10} /></button>
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    ))
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Modals */}
      {inboxTerminalId && <InboxPanel terminalId={inboxTerminalId} onClose={() => setInboxTerminalId(null)} />}
      {liveTerminal && (
        <TerminalView terminalId={liveTerminal.id} provider={liveTerminal.provider} agentProfile={liveTerminal.agentProfile} onClose={() => setLiveTerminal(null)} />
      )}
      {outputTerminalId && <OutputViewer terminalId={outputTerminalId} onClose={() => setOutputTerminalId(null)} />}
      <ConfirmModal
        open={!!pendingClose}
        title="Close Terminal"
        message="This will kill the tmux window and terminate the agent process."
        details={pendingClose ? [
          { label: 'Terminal', value: `${pendingClose.agent_profile || 'default'} (${pendingClose.id})` },
          { label: 'Session', value: pendingClose.tmux_session || 'unknown' },
        ] : []}
        confirmLabel="Close Terminal"
        variant="danger"
        loading={!!closingTerminal}
        onConfirm={handleDeleteTerminal}
        onCancel={() => setPendingClose(null)}
      />
      <ConfirmModal
        open={!!pendingExit}
        title="Graceful Exit"
        message="This will send the provider-specific exit command (e.g., /exit)."
        details={pendingExit ? [
          { label: 'Terminal', value: `${pendingExit.agent_profile || 'default'} (${pendingExit.id})` },
          { label: 'Provider', value: pendingExit.provider || 'unknown' },
        ] : []}
        confirmLabel="Send Exit"
        variant="warning"
        loading={!!exitingTerminal}
        onConfirm={handleExitTerminal}
        onCancel={() => setPendingExit(null)}
      />
      <ConfirmModal
        open={!!pendingDeleteSession}
        title="Delete Session"
        message="This will terminate all agents in this session and remove it."
        details={pendingDeleteSession ? [
          { label: 'Session', value: pendingDeleteSession },
        ] : []}
        confirmLabel="Delete Session"
        variant="danger"
        loading={deletingSession}
        onConfirm={handleDeleteSession}
        onCancel={() => setPendingDeleteSession(null)}
      />
    </div>
  )
}
