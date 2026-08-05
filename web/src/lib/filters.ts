// Fleet filtering — one predicate, two layers of vocabulary.
//
// LAYER 1 IS THE FORK'S OWN SCHEMA. Reachability (`status` folded through
// displayStatus), liveness (`lifecycle_state`), agent profile, provider,
// session, caller, freshness (`valid_until`) and chip colour (`semantic_role`)
// are fields the fork publishes and may legitimately filter on.
//
// LAYER 2 IS DERIVED, AND MUST STAY DERIVED. The facet dimensions are the union
// of `Object.keys(details)` over the current payload, in the producer's own
// insertion order, with the control type chosen by the SHAPE of the values and
// never by the key's name. The day an allowlist of facet keys appears here, a
// rename on the conductor side silently deletes a filter dimension — the exact
// failure `ageSource`'s docstring (lib/annotations.ts) records for the chip's
// headline age. The MODULES guard in test/annotations.test.tsx scans this file
// for conductor vocabulary; it passing is the mechanical proof that no key name
// leaked in.
//
// TWO THINGS THIS MODULE REFUSES, on measured grounds:
//
//  * A "working"/"active" dimension backed by `status`. Every live native-TUI
//    v2 row reports NOT_FIFO_MONITORED unconditionally
//    (terminal_projection.project_row): it is a REACHABILITY claim — "this pane
//    exists and answers" — not an activity claim. On the fleet that motivated
//    this work, 34 of 44 rows in one session read "Managed Live" while every
//    one of them had been idle for over twelve hours. A filter that says
//    "working" and means "alive" converts uncertainty into false confidence,
//    which is worse than no filter. Reachability and work phase are therefore
//    two separately-named dimensions, and the operator can see that the answer
//    to their real question is an intersection.
//  * Any control built on `last_active`. Only send_input / send_special_key
//    move it, and on a v2 managed row the value is frozen at row creation
//    forever (update_last_active touches only the v1 table). It is labelled
//    "last sent" at the call sites and given no range, sort-amplification or
//    "recently active" control here.

import type { Annotation, TerminalMeta } from '../api'
import { freshness, orderedFacets, resolveRole, splitFacetKey } from './annotations'
import { parseTimestamp } from './time'

// Render/filter order for the per-session status summary and the reachability
// filter pills. NOT_FIFO_MONITORED sits second, immediately after PROCESSING,
// because the two share the `info` semantic role in design-tokens/status.json:
// both are "this agent is alive" statements, and keeping them adjacent keeps
// that read at the head of the row. It is not first, because PROCESSING is the
// stronger claim (a turn is running) while NOT_FIFO_MONITORED is only
// reachability.
//
// Omitting entries was twice a real defect, not a style choice:
//
//  * NOT_FIFO_MONITORED — every managed native-TUI worker reports it
//    (terminal_projection.project_row assigns it to any lifecycle-live
//    native-TUI row), so on a native-TUI fleet nearly every agent was
//    uncounted by StatusSummary and unreachable from the filter pills.
//  * STOPPED — it has shipped in the generated STATUS_CONFIG since the
//    design-token SSOT landed, but was absent here, so a stopped row folded
//    silently to UNKNOWN. It sits after COMPLETED, the other terminal state,
//    and ahead of UNKNOWN, which is the residual bucket and always closes
//    the row.
//
// Every entry here MUST have a counterpart in the generated STATUS_CONFIG (or
// be the hand-added 'UNKNOWN' below): STATUS_META is built only from
// STATUS_CONFIG, and `STATUS_META[s].dot` is dereferenced unguarded in both
// StatusSummary and the reachability pill row, so an entry with no counterpart
// is a TypeError at render, not a missing dot.
const STATUS_ORDER = [
  'PROCESSING',
  'NOT_FIFO_MONITORED',
  'IDLE',
  'WAITING_USER_ANSWER',
  'ERROR',
  'COMPLETED',
  'STOPPED',
  'UNKNOWN',
]

// The statuses the summary and the filter row can actually draw. Held as a
// set so the counting site can ask "is this renderable?" against the same list
// that governs rendering, rather than against a second hand-kept copy.
const RENDERABLE_STATUSES = new Set(STATUS_ORDER)

export { STATUS_ORDER }

/**
 * The single status accessor for counting AND filtering. A row whose reported
 * status has no chip (the lifecycle vocabulary 'dead' / 'superseded' /
 * 'unknown-liveness' that terminal_projection assigns to non-live rows) folds
 * to UNKNOWN so it stays visible in the totals.
 *
 * Counting and filtering MUST use this same fold. Folding only at the counting
 * site produces an Unknown chip reading "2" whose pill then matches nothing and
 * empties the card — a count the operator cannot click through to.
 *
 * Case-normalised on the way in. The status poll reaches the store already
 * uppercased, but the projection row carries the server's lowercase spelling,
 * and a fold that accepted only one case filed every row read straight from
 * `/sessions/{name}` under UNKNOWN.
 */
export function displayStatus(raw: string | null | undefined): string {
  const reported = (raw || 'UNKNOWN').toUpperCase()
  return RENDERABLE_STATUSES.has(reported) ? reported : 'UNKNOWN'
}

/** A tri-state dimension: unconstrained, or only rows carrying a true/false claim. */
export type TriState = 'any' | 'true' | 'false'

/** What the operator asked of ONE derived facet dimension. */
export interface FacetSelection {
  /** pill/typeahead picks — OR within the dimension. */
  values: string[]
  tri: TriState
  /** datetime-local bounds for a timestamp-shaped facet; '' means open. */
  from: string
  to: string
  /** substring needle for a free-text facet. */
  text: string
}

/**
 * The complete filter state for one bar. One shape serves both bars: the
 * global bar never sets `callers` (spawn trees are a session-scoped question)
 * and the per-session bar never sets the fleet-stable dimensions.
 */
export interface FilterState {
  /** displayStatus() vocabulary — reachability, never activity. */
  reachability: string[]
  /** lifecycle_state vocabulary, scanned from the fleet. */
  liveness: string[]
  /** agent_profile vocabulary; the row's null folds to 'default'. */
  profiles: string[]
  providers: string[]
  sessions: string[]
  /** semantic_role vocabulary — the six fork-owned tokens. */
  roles: string[]
  /** valid_until freshness across the row's annotations. */
  freshness: 'any' | 'fresh' | 'stale'
  /** caller_id selections — subtree semantics, see matchesFilters. */
  callers: string[]
  /** free text over ids, names, profile and every facet value. */
  text: string
  /** derived facet dimensions, keyed by the producer's own facet key. */
  facets: Record<string, FacetSelection>
}

export function emptyFacetSelection(): FacetSelection {
  return { values: [], tri: 'any', from: '', to: '', text: '' }
}

export function emptyFilters(): FilterState {
  return {
    reachability: [],
    liveness: [],
    profiles: [],
    providers: [],
    sessions: [],
    roles: [],
    freshness: 'any',
    callers: [],
    text: '',
    facets: {},
  }
}

export function facetSelectionActive(sel: FacetSelection | undefined): boolean {
  if (!sel) return false
  return (
    sel.values.length > 0 ||
    sel.tri !== 'any' ||
    sel.from !== '' ||
    sel.to !== '' ||
    sel.text.trim() !== ''
  )
}

export function isFilterActive(f: FilterState): boolean {
  return (
    f.reachability.length > 0 ||
    f.liveness.length > 0 ||
    f.profiles.length > 0 ||
    f.providers.length > 0 ||
    f.sessions.length > 0 ||
    f.roles.length > 0 ||
    f.freshness !== 'any' ||
    f.callers.length > 0 ||
    f.text.trim() !== '' ||
    Object.values(f.facets).some(facetSelectionActive)
  )
}

/** How many dimensions carry a constraint — the collapsed bar's summary. */
export function activeFilterCount(f: FilterState): number {
  let n = 0
  if (f.reachability.length > 0) n += 1
  if (f.liveness.length > 0) n += 1
  if (f.profiles.length > 0) n += 1
  if (f.providers.length > 0) n += 1
  if (f.sessions.length > 0) n += 1
  if (f.roles.length > 0) n += 1
  if (f.freshness !== 'any') n += 1
  if (f.callers.length > 0) n += 1
  if (f.text.trim() !== '') n += 1
  n += Object.values(f.facets).filter(facetSelectionActive).length
  return n
}

/** Everything the predicate needs that is not on the row itself. */
export interface MatchContext {
  /** The polled status, which wins over the row's stored `status` when present. */
  status?: string
  /** id -> caller_id, for the spawned-by subtree walk. */
  callerOf?: (id: string) => string | null | undefined
}

/**
 * The haystack for free-text matching: identity, provenance, and every facet
 * key and value the row carries.
 *
 * BOTH SIDES ARE LOWERCASED. MemoryPanel's search lowercased only the needle
 * (`m.key.includes(search.toLowerCase())`), so a capitalised query silently
 * matched nothing against a capitalised key — a filter that lies is worse than
 * none. The needle is lowered at the match site; this side is lowered here.
 */
export function rowSearchText(terminal: TerminalMeta, annotations: Annotation[] | undefined): string {
  const parts: (string | null | undefined)[] = [
    terminal.id,
    terminal.terminal_id,
    terminal.name,
    terminal.tmux_window,
    terminal.agent_profile,
    terminal.provider,
    terminal.caller_id,
    terminal.tmux_session,
    terminal.session_name,
  ]
  for (const a of annotations ?? []) {
    parts.push(a.label)
    for (const [k, v] of orderedFacets(a.details)) parts.push(k, v)
  }
  return parts.filter(Boolean).join('\n').toLowerCase()
}

/**
 * Equality for a possibly-truncated facet value.
 *
 * The server ellipsises detail values past MAX_DETAIL_VALUE
 * (services/annotations.py), so a strict `===` against an observed value that
 * was cut would silently never match — the filter would claim zero rows while
 * the row is on screen carrying the prefix of the selected value. Prefix
 * comparison applies ONLY to the side carrying the ellipsis: two complete
 * values are still compared exactly, so selecting `r1` never matches `r11`.
 */
function facetValueEqual(observed: string, selected: string): boolean {
  if (observed === selected) return true
  if (observed.endsWith('…')) return selected.startsWith(observed.slice(0, -1))
  if (selected.endsWith('…')) return observed.startsWith(selected.slice(0, -1))
  return false
}

function facetValueMatches(observed: string, sel: FacetSelection): boolean {
  if (sel.values.length > 0 && sel.values.some(v => facetValueEqual(observed, v))) return true
  if (sel.tri !== 'any' && observed === sel.tri) return true
  if (sel.from !== '' || sel.to !== '') {
    const at = parseTimestamp(observed)
    if (at === null) return false
    if (sel.from !== '') {
      const from = parseTimestamp(sel.from)
      if (from !== null && at < from) return false
    }
    if (sel.to !== '') {
      const to = parseTimestamp(sel.to)
      if (to !== null && at > to) return false
    }
    return true
  }
  const needle = sel.text.trim().toLowerCase()
  if (needle !== '' && observed.toLowerCase().includes(needle)) return true
  return false
}

function rowMatchesFacet(
  annotations: Annotation[] | undefined,
  key: string,
  sel: FacetSelection,
): boolean {
  for (const a of annotations ?? []) {
    for (const [k, v] of orderedFacets(a.details)) {
      // Keys are compared exactly — only VALUES are ellipsised by the server.
      if (k !== key) continue
      if (facetValueMatches(v, sel)) return true
    }
  }
  return false
}

/**
 * THE ONE ROW PREDICATE. The session gate, the row gate and every counter call
 * this and nothing else.
 *
 * The two call sites it replaces had already drifted once: the session gate
 * read `(t.agent_profile || 'default') === agentTypeFilter` while the row gate
 * read `t.agent_profile === agentTypeFilter` with no fallback, so selecting
 * "default" kept the session card and rendered zero rows in it — a silently
 * empty card that read as a broken fleet. Collapsing them here means the
 * 'default' fold, the displayStatus fold and every future dimension exist
 * exactly once.
 *
 * Semantics: OR within a dimension, AND across dimensions. No negation, no
 * boolean expression syntax — deliberately.
 */
export function matchesFilters(
  terminal: TerminalMeta,
  annotations: Annotation[] | undefined,
  filters: FilterState,
  ctx: MatchContext = {},
): boolean {
  // Reachability routes through displayStatus — the same fold the summary
  // counts with, so a count is always click-through-able to its rows.
  if (filters.reachability.length > 0) {
    if (!filters.reachability.includes(displayStatus(ctx.status ?? terminal.status))) return false
  }
  if (filters.liveness.length > 0 && !filters.liveness.includes(terminal.lifecycle_state)) {
    return false
  }
  // The 'default' fold lives HERE and nowhere else — see the docstring above.
  if (filters.profiles.length > 0 && !filters.profiles.includes(terminal.agent_profile || 'default')) {
    return false
  }
  if (filters.providers.length > 0 && !filters.providers.includes(terminal.provider || 'unknown')) {
    return false
  }
  const sessionName = terminal.tmux_session ?? terminal.session_name ?? 'unknown'
  if (filters.sessions.length > 0 && !filters.sessions.includes(sessionName)) return false

  // Spawned-by is a SUBTREE question: "which rows did this run launch" includes
  // the grandchildren. The walk is hop-bounded because the caller graph is
  // producer data and a cycle in it must not spin the renderer forever.
  if (filters.callers.length > 0) {
    const wanted = new Set(filters.callers)
    let cursor: string | null | undefined = terminal.caller_id
    let matched = false
    for (let hops = 0; cursor && hops < 64; hops += 1) {
      if (wanted.has(cursor)) {
        matched = true
        break
      }
      cursor = ctx.callerOf?.(cursor)
    }
    if (!matched) return false
  }

  // Freshness and chip colour are claims the ROW's annotations make; a row
  // carrying none makes no claim and matches neither 'fresh' nor 'stale'.
  if (filters.freshness !== 'any') {
    const states = (annotations ?? []).map(a => freshness(a.valid_until))
    if (!states.includes(filters.freshness)) return false
  }
  if (filters.roles.length > 0) {
    const carries = (annotations ?? []).some(a => filters.roles.includes(resolveRole(a.semantic_role)))
    if (!carries) return false
  }

  const needle = filters.text.trim().toLowerCase()
  if (needle !== '' && !rowSearchText(terminal, annotations).includes(needle)) return false

  // Derived facets. Matching runs against the row's FULL annotation set,
  // upstream of every chip cap — a row whose fourth chip is behind a "+1 more"
  // marker is still matchable on that chip's facets.
  for (const [key, sel] of Object.entries(filters.facets)) {
    if (!facetSelectionActive(sel)) continue
    if (!rowMatchesFacet(annotations, key, sel)) return false
  }
  return true
}

// ── Derived facet dimensions (Layer 2) ────────────────────────────────────

/** Most distinct values a dimension may have and still be a pill row. Past
 *  this it is a typeahead — the 21-profile pill wall the global bar would
 *  otherwise become. */
export const MAX_PILL_VALUES = 12

/** Longest value eligible for equality-style controls; longer values, and any
 *  value the server ellipsised, get substring matching only. */
export const MAX_EQUALITY_VALUE_LENGTH = 64

export type FacetControl = 'pills' | 'typeahead' | 'tri-state' | 'range' | 'text'

export interface FacetValue {
  value: string
  /** Rows in scope carrying the value — the operator's "how many would this show". */
  rows: number
}

export interface FacetDimension {
  /** The producer's key, verbatim — matching keys compare exactly against it. */
  key: string
  /** The dotted provenance class, when the key carries one. */
  group: string | null
  /** The facet's short name (key with the class stripped). */
  name: string
  /** Humanised label — underscores and dots to spaces, nothing else known. */
  label: string
  control: FacetControl
  /** Value vocabulary with row counts, for the pills/typeahead controls. */
  values: FacetValue[]
}

/** The minimum a row must expose for dimension discovery. */
export interface DimensionRow {
  annotations?: Annotation[]
}

/**
 * The facet dimensions present in `rows`, in PRODUCER INSERTION ORDER.
 *
 * `Object.entries` order is preserved end to end (Python dict → pydantic →
 * JSON.parse), so the producer already has a way to say what should be read
 * first. Keys are counted once per ROW — an annotation set asserting the same
 * fact twice is one row carrying it, and the count answers "how many rows
 * would selecting this show".
 *
 * THE CONTROL IS CHOSEN BY VALUE SHAPE, NEVER BY KEY NAME:
 *
 *   every value parses as a past ISO instant → range control
 *   every value is exactly "true"/"false"      → tri-state toggle
 *   any value > 64 chars or ellipsised         → substring text only
 *   ≤ MAX_PILL_VALUES distinct values          → multi-select pills
 *   more                                       → typeahead
 *
 * The order of those tests is load-bearing: an ISO timestamp is short and few
 * in number, so the count rule would happily make it a pill row of dates;
 * only the shape test standing first keeps it a range.
 */
export function collectFacetDimensions(rows: DimensionRow[], now: number = Date.now()): FacetDimension[] {
  const order: string[] = []
  const byKey = new Map<string, Map<string, number>>()
  for (const row of rows) {
    const perRow = new Map<string, Set<string>>()
    for (const a of row.annotations ?? []) {
      for (const [k, v] of orderedFacets(a.details)) {
        let values = byKey.get(k)
        if (!values) {
          values = new Map()
          byKey.set(k, values)
          order.push(k)
        }
        let seen = perRow.get(k)
        if (!seen) {
          seen = new Set()
          perRow.set(k, seen)
        }
        if (!seen.has(v)) {
          seen.add(v)
          values.set(v, (values.get(v) ?? 0) + 1)
        }
      }
    }
  }
  return order.map(key => {
    const observed = byKey.get(key) as Map<string, number>
    const values = [...observed.keys()]
    let control: FacetControl
    if (values.length > 0 && values.every(v => {
      const at = parseTimestamp(v)
      return at !== null && at <= now
    })) {
      control = 'range'
    } else if (values.length > 0 && values.every(v => v === 'true' || v === 'false')) {
      control = 'tri-state'
    } else if (values.some(v => v.length > MAX_EQUALITY_VALUE_LENGTH || v.endsWith('…'))) {
      control = 'text'
    } else if (values.length <= MAX_PILL_VALUES) {
      control = 'pills'
    } else {
      control = 'typeahead'
    }
    const { group, name } = splitFacetKey(key)
    // Producer order governs the KEYS. Within a dimension the most-carried
    // value comes first — frequency, not vocabulary, and a rename changes
    // nothing about it.
    const ranked = [...observed.entries()]
      .map(([value, rowsCount]) => ({ value, rows: rowsCount }))
      .sort((a, b) => b.rows - a.rows || a.value.localeCompare(b.value))
    return { key, group, name, label: name.replace(/_/g, ' '), control, values: ranked }
  })
}

/**
 * A dimension belongs in the GLOBAL bar only when its ENTIRE fleet vocabulary
 * fits one pill row. Unbounded vocabularies (typeahead), timestamps (range),
 * booleans and long text are session-scoped questions: hoisting them global is
 * the unbounded pill wall the per-session bar exists to prevent. This is a
 * shape rule, not a vocabulary rule — a facet moves bars when the producer's
 * values change shape, with no edit here.
 */
export function isFleetWide(dim: FacetDimension): boolean {
  return dim.control === 'pills'
}

/**
 * The global derived set — three shape conditions, no vocabulary.
 *
 *  1. The dimension is pill-shaped for the WHOLE fleet (isFleetWide): the
 *     wall argument, applied to the fleet's full value list, not to one
 *     session's slice of it.
 *  2. The producer emits it against rows in AT LEAST TWO sessions. A facet
 *     tied to one campaign stays in its session's bar. The PR case is
 *     load-bearing: publication facets arrive only when somebody ran the
 *     PR-state collection for that campaign, so a fleet-wide "has PR"
 *     control would read as "no PRs exist" on every fleet that never ran it.
 *  3. The vocabulary is SHARED, not partitioned: no session contributes a
 *     distinct value the others do not also carry (fleet distinct == the
 *     largest per-session distinct). That is the only reading of "stable
 *     vocabulary across sessions" that does not require knowing what any
 *     facet means. Two campaigns emitting `lane` with disjoint lane names
 *     are not one stable dimension — they are two session-local
 *     vocabularies sharing a key, which is §4d's per-session case exactly.
 *
 * On a single-session fleet every facet lands in the session bar; the global
 * gate is nearly vacuous there anyway, so nothing the operator can do moves
 * out of reach — it moves down one card.
 */
export function fleetWideFacetKeys(
  fleetDimensions: FacetDimension[],
  perSession: Array<{ dimensions: FacetDimension[] }>,
): Set<string> {
  const pillFleetWide = new Set(fleetDimensions.filter(isFleetWide).map(d => d.key))
  const fleetDistinct = new Map(fleetDimensions.map(d => [d.key, d.values.length] as const))
  const emitters = new Map<string, number>()
  const maxDistinct = new Map<string, number>()
  for (const { dimensions } of perSession) {
    for (const dim of dimensions) {
      emitters.set(dim.key, (emitters.get(dim.key) ?? 0) + 1)
      maxDistinct.set(dim.key, Math.max(maxDistinct.get(dim.key) ?? 0, dim.values.length))
    }
  }
  return new Set(
    [...pillFleetWide].filter(
      key => (emitters.get(key) ?? 0) >= 2 && fleetDistinct.get(key) === maxDistinct.get(key),
    ),
  )
}

export interface DimensionGroup {
  /** The provenance class, humanised — rendered verbatim as the section heading. */
  group: string | null
  heading: string | null
  dimensions: FacetDimension[]
}

/**
 * Dimensions collected under their dotted-prefix classes, in first-appearance
 * order. A key with no class stands alone — no heading, and its label keeps
 * the full key so the two surfaces (filter bar and detail popover) call the
 * same facet by the same words.
 */
export function groupDimensions(dimensions: FacetDimension[]): DimensionGroup[] {
  const out: DimensionGroup[] = []
  const byGroup = new Map<string, DimensionGroup>()
  for (const dim of dimensions) {
    if (dim.group === null) {
      out.push({ group: null, heading: null, dimensions: [{ ...dim, label: dim.key.replace(/[_.]/g, ' ') }] })
      continue
    }
    let g = byGroup.get(dim.group)
    if (!g) {
      g = { group: dim.group, heading: dim.group.replace(/_/g, ' '), dimensions: [] }
      byGroup.set(dim.group, g)
      out.push(g)
    }
    g.dimensions.push(dim)
  }
  return out
}

// ── Layer-1 vocabularies, scanned from the fleet ──────────────────────────

/** Distinct lifecycle_state values present, alphabetical. */
export function lifecycleVocabulary(terminals: TerminalMeta[]): string[] {
  return [...new Set(terminals.map(t => t.lifecycle_state))].sort()
}

/** Distinct profiles present, with the same 'default' fold the predicate uses. */
export function profileVocabulary(terminals: TerminalMeta[]): string[] {
  return [...new Set(terminals.map(t => t.agent_profile || 'default'))].sort()
}

export function providerVocabulary(terminals: TerminalMeta[]): string[] {
  return [...new Set(terminals.map(t => t.provider || 'unknown'))].sort()
}

export interface CallerOption {
  id: string
  /** The caller's profile when the fleet can resolve it, plus its short id. */
  label: string
}

/** Distinct non-null caller_ids present, labelled with whatever the fleet knows. */
export function callerVocabulary(terminals: TerminalMeta[]): CallerOption[] {
  const profileOf = new Map(terminals.map(t => [t.id, t.agent_profile || 'default']))
  const ids = [...new Set(terminals.map(t => t.caller_id).filter((c): c is string => !!c))].sort()
  return ids.map(id => {
    const profile = profileOf.get(id)
    return { id, label: `${profile ? `${profile} · ` : ''}${id.slice(0, 8)}` }
  })
}
