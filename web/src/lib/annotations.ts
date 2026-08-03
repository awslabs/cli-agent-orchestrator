// Placement, fencing and freshness for conductor annotations — pure, so the
// rules are testable without a DOM and cannot differ between the terminal row
// and the campaign surface.
//
// Three decisions live here, and each one is a specific incident:
//
//  * THE GENERATION FENCE (cond-0054). An annotation naming a terminal
//    generation is attached only to the row carrying that exact generation.
//    A different generation means the obligation that produced the annotation
//    belongs to a terminal that no longer exists at this id, so the annotation
//    is DROPPED rather than relocated — a stale claim must not outlive its
//    obligation, and re-parenting it to the live row would be the confidently
//    wrong answer.
//  * NO GENERATION IS NOT A MATCH (design §4.3, `legacy-no-generation`). An
//    annotation with a terminal id but no generation cannot be fenced, so it is
//    not attached to a live row on the strength of the id alone. It stays
//    visible on the campaign surface instead of being silently promoted.
//  * NOTHING IS DROPPED FOR WANT OF A ROW (cond-0148). A terminal id with no
//    matching row is an orphaned run; a campaign or task subject never had a
//    row; an unrecognised subject type is a future the fork does not know. All
//    three land on the same terminal-independent surface, because a chip the
//    operator cannot see is worse than one that is merely unplaced.

import type { Annotation, AnnotationsResponse } from '../api'
import { parseTimestamp } from './time'

/** The six semantic roles in design-tokens/tokens.json — nothing else exists. */
export const SEMANTIC_ROLES = ['success', 'info', 'accent', 'warning', 'danger', 'neutral'] as const
export type SemanticRole = (typeof SEMANTIC_ROLES)[number]

/**
 * A role the chip can actually draw.
 *
 * An unrecognised role resolves to `neutral` rather than being dropped or
 * defaulted to something confident: the conductor may add roles, and a
 * colourless chip carrying the conductor's own label is "I do not know how to
 * shade this", which is the honest answer. This is also why the renderer needs
 * no update when a role is added — and why design-tokens/status.json is never
 * touched, so `gen.mjs --check` cannot fire.
 */
export function resolveRole(role: string | null | undefined): SemanticRole {
  return (SEMANTIC_ROLES as readonly string[]).includes(role ?? '')
    ? (role as SemanticRole)
    : 'neutral'
}

/**
 * How much the renderer knows about whether a claim is still true.
 *
 * THREE STATES, NOT TWO. `valid_until` is producer-supplied and the fork
 * bounds every other field but not this one, so "absent" and "unparseable" are
 * real cases — and folding them into `fresh` inverts the governing principle:
 * unknown freshness is not current freshness. A conductor that died, a
 * campaign that ended, or a producer version that simply never set the field
 * would otherwise leave every chip rendering in full vivid role colour
 * forever, indistinguishable from one validated a second ago.
 */
export type Freshness = 'fresh' | 'stale' | 'unknown'

export function freshness(
  validUntil: string | null | undefined,
  now: number = Date.now(),
): Freshness {
  if (!validUntil) return 'unknown'
  const at = parseTimestamp(validUntil)
  if (at === null) return 'unknown'
  return now > at ? 'stale' : 'fresh'
}

/**
 * Past its envelope's `valid_until`, an annotation is no longer current.
 *
 * True only when the renderer can PROVE expiry. Unknown freshness is not
 * "stale" — it has its own rendering — but it is also never "current", which
 * is what `freshness()` above is for. Kept because the sort and the fence both
 * want the proven case as a boolean.
 */
export function isStale(validUntil: string | null | undefined, now: number = Date.now()): boolean {
  return freshness(validUntil, now) === 'stale'
}

/** Why an annotation could not be attached to a terminal row. */
export type UnplacedReason =
  | 'campaign'
  | 'task'
  | 'orphaned-terminal'
  | 'no-generation'
  | 'unfenceable-row'
  | 'unknown-subject'

export interface UnplacedAnnotation {
  annotation: Annotation
  reason: UnplacedReason
}

export interface PlacedAnnotations {
  /** Annotations that passed the fence, keyed by the row's terminal id. */
  byTerminal: Record<string, Annotation[]>
  /** Everything terminal-independent, in one visible place. */
  unplaced: UnplacedAnnotation[]
  /** How many the generation fence dropped. Reported, never silent. */
  fenced: number
  /**
   * Terminal-scoped annotations held back because the row set is not loaded
   * yet. Not dropped and not classified — see `rowsLoaded` below.
   */
  pending: number
}

/** The minimum a projection row must expose for the fence to run. */
export interface FenceRow {
  id: string
  generation: string | null
}

/**
 * Sort order for every capped list of chips.
 *
 * FRESHNESS IS THE PRIMARY KEY, ahead of priority. Staleness used to be
 * applied at draw time — after the cap had already chosen who gets drawn — so
 * three expired p99 chips would occupy all three row slots and push a live
 * `danger` behind a "+1". The whole argument for greying a stale chip rather
 * than keeping its colour ("the glance is the whole product") applies at least
 * as strongly to whether it is drawn at all.
 */
function byPriority(a: Annotation, b: Annotation, now: number = Date.now()): number {
  const staleA = isStale(a.valid_until, now) ? 1 : 0
  const staleB = isStale(b.valid_until, now) ? 1 : 0
  if (staleA !== staleB) return staleA - staleB
  if (a.priority !== b.priority) return b.priority - a.priority
  return a.label.localeCompare(b.label)
}

/**
 * A defensive read of the route's body.
 *
 * The route is bounded and typed, but this is a dashboard: a body that is not
 * what the type says degrades to "no annotations", which is the same rendering
 * as a machine with no conductor. Nothing here throws.
 */
export function readAnnotations(body: unknown): AnnotationsResponse {
  const empty: AnnotationsResponse = {
    annotation_schema: 'cao-annotations-v1',
    coverage: 'unavailable',
    sources_read: 0,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations: [],
  }
  if (!body || typeof body !== 'object') return empty
  const raw = body as Partial<AnnotationsResponse>
  const items = Array.isArray(raw.annotations) ? raw.annotations : []
  return {
    ...empty,
    ...raw,
    items_omitted: typeof raw.items_omitted === 'number' ? raw.items_omitted : 0,
    reasons: Array.isArray(raw.reasons) ? raw.reasons : [],
    annotations: items.filter(isRenderable),
  }
}

function isRenderable(item: unknown): item is Annotation {
  if (!item || typeof item !== 'object') return false
  const a = item as Partial<Annotation>
  return (
    typeof a.label === 'string' &&
    a.label.length > 0 &&
    typeof a.semantic_role === 'string' &&
    typeof a.priority === 'number' &&
    !!a.subject &&
    typeof a.subject === 'object' &&
    typeof (a.subject as { type?: unknown }).type === 'string'
  )
}

/**
 * @param rowsLoaded whether the fleet's row set is known.
 *
 * WHY THAT ARGUMENT EXISTS. `/annotations` and the session details are fetched
 * by independent effects, and the session pass is a sequential loop, so on
 * every page load and every list refresh the annotations arrive against an
 * EMPTY row set. Classifying then meant every terminal-scoped annotation
 * missed `byId` and was announced as an `orphaned run` — a panel asserting
 * that live workers are orphaned, flashed by the very surface whose job is to
 * report the fence. Held back and counted instead: campaign and task subjects
 * never needed a row and render immediately.
 */
export function placeAnnotations(
  annotations: Annotation[],
  rows: FenceRow[],
  rowsLoaded: boolean = true,
  now: number = Date.now(),
): PlacedAnnotations {
  const byId = new Map<string, FenceRow>()
  rows.forEach(row => byId.set(row.id, row))

  const byTerminal: Record<string, Annotation[]> = {}
  const unplaced: UnplacedAnnotation[] = []
  let fenced = 0
  let pending = 0

  for (const annotation of annotations) {
    const subject = annotation.subject
    if (subject.type !== 'terminal' || !subject.terminal_id) {
      const reason: UnplacedReason =
        subject.type === 'campaign' ? 'campaign' : subject.type === 'task' ? 'task' : 'unknown-subject'
      unplaced.push({ annotation, reason })
      continue
    }
    if (!rowsLoaded) {
      pending += 1
      continue
    }
    // Asked BEFORE the row lookup, so the reason names what is actually
    // missing. An annotation with no generation cannot be fenced whether or
    // not a row exists, and calling that "an orphaned run" would be a claim
    // about the fleet made on the strength of a field the annotation lacks.
    if (!subject.generation) {
      unplaced.push({ annotation, reason: 'no-generation' })
      continue
    }
    const row = byId.get(subject.terminal_id)
    if (!row) {
      unplaced.push({ annotation, reason: 'orphaned-terminal' })
      continue
    }
    if (row.generation === null) {
      // The ROW publishes no generation, so the fence has nothing to compare.
      // Counting that as a superseded obligation would blame the annotation
      // for the row's missing field and delete it from the surface.
      unplaced.push({ annotation, reason: 'unfenceable-row' })
      continue
    }
    if (row.generation !== subject.generation) {
      fenced += 1
      continue
    }
    ;(byTerminal[row.id] ??= []).push(annotation)
  }

  Object.values(byTerminal).forEach(list => list.sort((a, b) => byPriority(a, b, now)))
  unplaced.sort((a, b) => byPriority(a.annotation, b.annotation, now))
  return { byTerminal, unplaced, fenced, pending }
}

/**
 * `details` as `[key, value]` pairs, IN THE ORDER THE PRODUCER WROTE THEM.
 *
 * This used to be a hand-kept `FACET_ORDER` list — `task`, `role`, `round`,
 * `parked_at`, `lifecycle`, … — which is conductor vocabulary sitting on the
 * side of the seam where no guard runs. JSON object order is preserved end to
 * end (Python `dict` → pydantic → `JSON.parse`), so the producer already has a
 * way to say what should be read first, and honouring it costs the fork
 * nothing and buys it no vocabulary to keep in step.
 */
export function orderedFacets(details: Record<string, string> | undefined): Array<[string, string]> {
  return Object.entries(details ?? {})
}

/**
 * The timestamp a chip may show an age for, if the conductor sent one.
 *
 * DERIVED, NOT ALLOWLISTED. This was `details.parked_at || details.since` —
 * two conductor terms, one of which is on the Python guard's own forbidden
 * list, deciding whether the chip's headline age exists at all. Renaming one
 * facet key on the conductor side silently deleted a visible UI element, which
 * is exactly the coupling this seam exists to remove. The rule is now
 * structural: the first facet, in the producer's own order, whose value is an
 * ISO-8601 timestamp in the past.
 */
export function ageSource(annotation: Annotation, now: number = Date.now()): string | null {
  for (const [, value] of orderedFacets(annotation.details)) {
    const at = parseTimestamp(value)
    if (at !== null && at <= now) return value
  }
  return null
}

/**
 * Is this chip an IDENTITY chip rather than a SEVERITY one?
 *
 * PRESENCE, NOT TRUTHINESS. `!!a.colour_key` would be the obvious spelling and
 * it is wrong: the empty string is a real third state meaning "an identity chip
 * that has no colour", and truthiness folds it back into the severity group,
 * where it would be drawn in a role colour that means danger or warning.
 *
 * THIS IS ALSO THE WHOLE PARTITION RULE, and it deliberately reads one opaque
 * field rather than a list of names. Nothing here knows what kinds exist, which
 * is why a new identity chip needs no change on this side.
 *
 * The fragility worth writing down: the arrangement below holds only while the
 * identity chips are exactly the set carrying the field. A future COLOURED
 * SEVERITY chip would silently migrate into the identity group and into its
 * cap. This side cannot enforce that — it has nothing to enforce it with — so
 * it is the producer's invariant to keep.
 */
export function isIdentity(annotation: Annotation): boolean {
  return typeof annotation.colour_key === 'string'
}

export interface ChipPartition {
  severity: Annotation[]
  identity: Annotation[]
}

/**
 * The row's chips, split into the two groups that get INDEPENDENT caps.
 *
 * Two caps are not cosmetic. One sorted list capped at three means a row with
 * three severity chips drops its identity chips entirely, and a row carrying
 * two identity chips pushes a live `danger` behind a "+1" — which is precisely
 * the regression the freshness-before-priority sort was introduced to fix.
 * Order WITHIN each group is whatever the caller already sorted; nothing here
 * re-ranks.
 */
export function partitionChips(annotations: Annotation[]): ChipPartition {
  const severity: Annotation[] = []
  const identity: Annotation[] = []
  for (const annotation of annotations) {
    if (isIdentity(annotation)) identity.push(annotation)
    else severity.push(annotation)
  }
  return { severity, identity }
}

/**
 * A facet key's provenance class and its short name.
 *
 * Split on the FIRST dot, and only when both halves are non-empty. The
 * producer groups its facets by prefixing them; this side does not know or
 * check what the prefixes are, it only reads the shape. A key with no dot —
 * every facet published before this existed — comes back unchanged with no
 * class, so nothing about the existing rendering moves.
 */
export function splitFacetKey(key: string): { group: string | null; name: string } {
  const dot = key.indexOf('.')
  if (dot <= 0 || dot >= key.length - 1) return { group: null, name: key }
  return { group: key.slice(0, dot), name: key.slice(dot + 1) }
}

/** The facets carrying no provenance class — rendered exactly as they always were. */
export function plainFacets(annotation: Annotation): Array<[string, string]> {
  return orderedFacets(annotation.details).filter(([key]) => splitFacetKey(key).group === null)
}

export interface FacetGroup {
  group: string
  entries: Array<[string, string]>
}

/**
 * Every classed facet on the ROW, collected across all of its annotations.
 *
 * Row-scoped rather than chip-scoped on purpose: the classes describe the
 * WORKER, and which chip happened to carry a given fact is an artifact of how
 * the producer split them. Two chips both naming the same fact should say it
 * once, so entries are deduped on (class, name, value) — a genuine disagreement
 * between two chips survives as two rows, because collapsing that would hide
 * the one case worth seeing.
 *
 * Classes come out in FIRST-APPEARANCE order. The producer already decided what
 * to say first by the order it wrote its facets, and honouring that costs
 * nothing and buys no vocabulary to keep in step.
 */
export function groupedFacets(annotations: Annotation[]): FacetGroup[] {
  const order: string[] = []
  const byGroup = new Map<string, Array<[string, string]>>()
  const seen = new Set<string>()
  for (const annotation of annotations) {
    for (const [key, value] of orderedFacets(annotation.details)) {
      const { group, name } = splitFacetKey(key)
      if (group === null) continue
      const fingerprint = `${group} ${name} ${value}`
      if (seen.has(fingerprint)) continue
      seen.add(fingerprint)
      let entries = byGroup.get(group)
      if (!entries) {
        entries = []
        byGroup.set(group, entries)
        order.push(group)
      }
      entries.push([name, value])
    }
  }
  return order.map(group => ({ group, entries: byGroup.get(group) as Array<[string, string]> }))
}
