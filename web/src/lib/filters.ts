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
//  * STOPPED, DEAD and SUPERSEDED — each is a truthful terminal/lifecycle
//    disposition the projection can publish. Folding the last two into
//    UNKNOWN made exact evidence look missing and hid the distinction between
//    an absent pane, a replaced incarnation and genuinely unknown liveness.
//    They sit after COMPLETED and ahead of UNKNOWN, which remains the residual
//    bucket and always closes the row.
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
  'DEAD',
  'SUPERSEDED',
  'UNKNOWN',
]

// The statuses the summary and the filter row can actually draw. Held as a
// set so the counting site can ask "is this renderable?" against the same list
// that governs rendering, rather than against a second hand-kept copy.
const RENDERABLE_STATUSES = new Set(STATUS_ORDER)

export { STATUS_ORDER }

/**
 * The single status accessor for counting AND filtering. The projection's
 * proven DEAD and SUPERSEDED lifecycle values are first-class display states.
 * A row whose reported status still has no chip (including
 * `unknown-liveness`, which is explicitly not proof of death) folds to UNKNOWN
 * so it stays visible in the totals.
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

/**
 * The label dimension prefix. A facet entry keyed `label:<kind>` matches
 * against the annotations' LABELS carrying that kind, not against `details`.
 *
 * Why this dimension exists: the lane identity is the annotation's `label`
 * (the chip's visible text), and `collectFacetDimensions` reads only
 * `details` — so "show me the rows on this one lane" was answerable only by
 * free text, never by a dimension. The kind is carried through verbatim as
 * an opaque grouping key, exactly the way section headings already use it:
 * nothing here branches on its value, no kind name appears as a literal, and
 * the grouping below is a Map lookup rather than a comparison, because the
 * MODULES guard forbids `kind ===` outright.
 *
 * The prefix namespaces label dimensions away from detail keys, which are
 * producer data and could in principle collide with a bare kind name.
 */
export const LABEL_DIMENSION_PREFIX = 'label:'

/** Labels grouped by their annotation's kind — the match side of `label:<kind>`. */
function indexLabelsByKind(annotations: Annotation[] | undefined): Map<string, string[]> {
  const byKind = new Map<string, string[]>()
  for (const a of annotations ?? []) {
    const list = byKind.get(a.kind)
    if (list) list.push(a.label)
    else byKind.set(a.kind, [a.label])
  }
  return byKind
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
  //
  // `label:<kind>` keys match the annotations' labels (grouped by kind through
  // a Map — never a `kind ===` branch); every other key matches `details`.
  // Both run through the same facetValueMatches, so a label dimension inherits
  // the exact equality, the ellipsis prefix rule, the range bounds and the
  // substring needle unchanged.
  let labelsByKind: Map<string, string[]> | null = null
  for (const [key, sel] of Object.entries(filters.facets)) {
    if (!facetSelectionActive(sel)) continue
    if (key.startsWith(LABEL_DIMENSION_PREFIX)) {
      labelsByKind ??= indexLabelsByKind(annotations)
      const labels = labelsByKind.get(key.slice(LABEL_DIMENSION_PREFIX.length)) ?? []
      if (!labels.some(label => facetValueMatches(label, sel))) return false
      continue
    }
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
  /** Rows in scope carrying this dimension at all — the coverage half of the
   *  usefulness ranking. Distinct from `values[].rows` (per-value counts). */
  carriers: number
  /** Every value is a long hex/opaque token — see isOpaqueValue. */
  opaque: boolean
  /** Value vocabulary with row counts, for the pills/typeahead controls. */
  values: FacetValue[]
}

/** The minimum a row must expose for dimension discovery. */
export interface DimensionRow {
  annotations?: Annotation[]
}

/**
 * The opaque-value shape: a long run of hex (and dashes, for the uuid shape).
 *
 * Length plus character class, never the key's name. A 64-char sha256, a
 * 40-char sha1, a 36-char dashed uuid and a 16-char identity token are all
 * values a human cannot pick out of a list — they are read by pasting, not by
 * scanning — so the dimension they belong to is text-matched, never a pill
 * wall of hashes. The 16-character floor is what keeps short shas and short
 * ids OUT of this rule: those a human can pick, and the identity-ratio rule
 * (dimensionMerit) already demotes them on their own measured shape.
 */
const OPAQUE_VALUE = /^[0-9a-f][0-9a-f-]{14,}$/i

function isOpaqueValue(value: string): boolean {
  return OPAQUE_VALUE.test(value)
}

/**
 * The control a dimension's values earn, by SHAPE ALONE. The order of these
 * tests is load-bearing: an ISO timestamp is short and few in number, so the
 * count rule would happily make it a pill row of dates; only the shape test
 * standing first keeps it a range. The opaque test stands ahead of the length
 * test because a 64-char hash is NOT over 64 characters — without it the
 * fleet's one opaque facet would have been a single-pill "filter".
 */
function controlForValues(values: string[], now: number): { control: FacetControl; opaque: boolean } {
  if (values.length > 0 && values.every(v => {
    const at = parseTimestamp(v)
    return at !== null && at <= now
  })) {
    return { control: 'range', opaque: false }
  }
  if (values.length > 0 && values.every(v => v === 'true' || v === 'false')) {
    return { control: 'tri-state', opaque: false }
  }
  if (values.length > 0 && values.every(isOpaqueValue)) {
    return { control: 'text', opaque: true }
  }
  if (values.some(v => v.length > MAX_EQUALITY_VALUE_LENGTH || v.endsWith('…'))) {
    return { control: 'text', opaque: false }
  }
  if (values.length <= MAX_PILL_VALUES) {
    return { control: 'pills', opaque: false }
  }
  return { control: 'typeahead', opaque: false }
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
 * THE CONTROL IS CHOSEN BY VALUE SHAPE, NEVER BY KEY NAME (controlForValues):
 *
 *   every value parses as a past ISO instant → range control
 *   every value is exactly "true"/"false"      → tri-state toggle
 *   every value is a long hex token            → substring text only
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
  const carriers = new Map<string, number>()
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
    for (const key of perRow.keys()) carriers.set(key, (carriers.get(key) ?? 0) + 1)
  }
  return order.map(key => {
    const observed = byKey.get(key) as Map<string, number>
    const values = [...observed.keys()]
    const { control, opaque } = controlForValues(values, now)
    const { group, name } = splitFacetKey(key)
    // Producer order governs the KEYS. Within a dimension the most-carried
    // value comes first — frequency, not vocabulary, and a rename changes
    // nothing about it.
    const ranked = [...observed.entries()]
      .map(([value, rowsCount]) => ({ value, rows: rowsCount }))
      .sort((a, b) => b.rows - a.rows || a.value.localeCompare(b.value))
    return {
      key,
      group,
      name,
      label: name.replace(/_/g, ' '),
      control,
      carriers: carriers.get(key) ?? 0,
      opaque,
      values: ranked,
    }
  })
}

/**
 * The label dimensions present in `rows`: one per annotation kind, whose
 * values are the LABELS observed for that kind (row-counted, exactly the way
 * facet values are).
 *
 * Same engine, same shape rules, same producer order. The kind is used as an
 * opaque grouping key and nothing more — it is indexed, never compared — and
 * no kind name is written here, so a kind invented next year gets its
 * dimension for free. Labels carrying a round suffix ("… · r1") are not
 * parsed: the suffix makes the dimension identity-SHAPED, and the
 * identity-ratio demotion in dimensionMerit handles it the same way it
 * handles a 48-distinct-commit dimension — by shape, not by reading the
 * producer's formatting.
 */
export function collectLabelDimensions(rows: DimensionRow[], now: number = Date.now()): FacetDimension[] {
  const order: string[] = []
  const byKind = new Map<string, Map<string, number>>()
  const carriers = new Map<string, number>()
  for (const row of rows) {
    const perRow = new Map<string, Set<string>>()
    for (const a of row.annotations ?? []) {
      let values = byKind.get(a.kind)
      if (!values) {
        values = new Map()
        byKind.set(a.kind, values)
        order.push(a.kind)
      }
      let seen = perRow.get(a.kind)
      if (!seen) {
        seen = new Set()
        perRow.set(a.kind, seen)
      }
      if (!seen.has(a.label)) {
        seen.add(a.label)
        values.set(a.label, (values.get(a.label) ?? 0) + 1)
      }
    }
    for (const kind of perRow.keys()) carriers.set(kind, (carriers.get(kind) ?? 0) + 1)
  }
  return order.map(kind => {
    const observed = byKind.get(kind) as Map<string, number>
    const values = [...observed.keys()]
    const { control, opaque } = controlForValues(values, now)
    const ranked = [...observed.entries()]
      .map(([value, rowsCount]) => ({ value, rows: rowsCount }))
      .sort((a, b) => b.rows - a.rows || a.value.localeCompare(b.value))
    return {
      key: LABEL_DIMENSION_PREFIX + kind,
      group: null,
      name: kind,
      // Dashes go the way of underscores here: a kind is a display token, not
      // a facet key, and its humanised form is what the chip and picker show.
      label: kind.replace(/[_.-]/g, ' '),
      control,
      carriers: carriers.get(kind) ?? 0,
      opaque,
      values: ranked,
    }
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

// ── Picker ranking: usefulness derived from value shape ───────────────────
//
// The "+ Filter" picker is the curated surface, and curation here means
// MEASURED usefulness, never key names. Every rule below reads the same three
// numbers — how many distinct values a dimension has, how many rows carry it,
// what control its values earned — because the day a rule reads a key name, a
// rename on the producer side silently re-ranks the picker. The measurements
// behind each rule come from the live 43-row fleet that motivated the chip
// bar, and are quoted in each rule's comment.

/** The shape facts the ranking reads. Layer-1 (fork-owned) dimensions are
 *  reduced to this same shape so one function ranks everything. */
export interface DimensionStats {
  control: FacetControl
  /** Distinct values observed. */
  distinct: number
  /** Rows in scope carrying the dimension at all. */
  carriers: number
  /** Every value is a long hex token (isOpaqueValue). */
  opaque: boolean
}

/** Pick a facet dimension's stats straight off the collected dimension. */
export function facetStats(dim: FacetDimension): DimensionStats {
  return { control: dim.control, distinct: dim.values.length, carriers: dim.carriers, opaque: dim.opaque }
}

/**
 * distinct/carriers at or above this means the dimension IDENTIFIES rows
 * rather than grouping them. Measured: 48 distinct commits over 48 carrying
 * rows (1.0) and 67 distinct checkout paths over 70 (0.957) were offered as
 * filters and could only ever single out one row apiece. 0.9 sits below both
 * and far above a real grouping vocabulary (a 10-value lane set over 43 rows
 * measures 0.23).
 */
export const IDENTITY_DISTINCT_RATIO = 0.9

/** The ratio above only means something once the scope is bigger than a pill
 *  row: on a four-row session a two-value dimension measures 2/2 = 1.0 and
 *  is not an identity column, it is just a small fleet. */
export const IDENTITY_MIN_CARRIERS = 8

export type PickerTier = 'primary' | 'secondary' | 'niche'

export interface DimensionMerit {
  /** null: do not offer this dimension in the picker at all. */
  tier: PickerTier | null
  /** The one-line, shape-derived explanation the picker shows under the label. */
  note: string
}

/**
 * How useful is this dimension as a filter, derived from its shape alone:
 *
 *  * ONE VALUE, CARRIED BY EVERY ROW → omitted from the picker. Selecting it
 *    can only ever pass, so the control is noise with a label — the measured
 *    cases were two provenance facets that read `task-prefix` on 43 of 43
 *    rows and `lane` on 43 of 43 rows respectively. A single value carried
 *    by SOME rows is a different shape entirely: selecting it is a presence
 *    question that does discriminate, so it stays, as secondary.
 *  * TRI-STATE and RANGE → primary, and exempt from the identity rule by
 *    construction: a boolean has two values by definition, and a timestamp
 *    has distinct ≈ carriers almost always. Neither is an identity column.
 *  * OPAQUE VALUES → niche. A wall of hashes cannot be picked from by a
 *    human; the dimension is text-matched (controlForValues already forced
 *    that) and demoted, never deleted — the Advanced sheet still lists it.
 *  * IDENTITY-SHAPED (distinct ≈ carriers, on a scope of at least
 *    IDENTITY_MIN_CARRIERS rows) → niche. The dimension names rows one by
 *    one; that is what the free-text box is for.
 *  * FEW SHARED VALUES (pills that passed the identity rule) → primary. This
 *    is the shape the operator's "can I filter on actively working?"
 *    question has: 3–4 values across the whole fleet.
 *  * Everything else → secondary: real vocabularies too big for pills.
 */
export function dimensionMerit(stats: DimensionStats, totalRows: number): DimensionMerit {
  const { control, distinct, carriers, opaque } = stats
  const coversAll = totalRows > 0 && carriers >= totalRows
  const coverage =
    totalRows === 0 ? 'no rows in scope' : coversAll ? 'on every row' : `on ${carriers} of ${totalRows} rows`
  if (distinct <= 1) {
    if (coversAll) {
      return { tier: null, note: `one value, ${coverage} — selecting it filters nothing out` }
    }
    return { tier: 'secondary', note: `one value · present on ${carriers} of ${totalRows} rows` }
  }
  // Range and tri-state stand ahead of the identity rule by construction: a
  // timestamp has distinct ≈ carriers almost always, and a boolean has two
  // values by definition — neither is an identity column.
  if (control === 'tri-state') {
    return { tier: 'primary', note: `${distinct} values · ${coverage}` }
  }
  if (control === 'range') {
    return { tier: 'primary', note: `a timestamp range · ${coverage}` }
  }
  if (opaque) {
    return { tier: 'niche', note: `opaque values · ${coverage} — match by text` }
  }
  if (carriers >= IDENTITY_MIN_CARRIERS && distinct / carriers >= IDENTITY_DISTINCT_RATIO) {
    return { tier: 'niche', note: `a different value on nearly every row · ${coverage}` }
  }
  if (control === 'pills') {
    return { tier: 'primary', note: `${distinct} values · ${coverage}` }
  }
  return { tier: 'secondary', note: `${distinct} values · ${coverage}` }
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
