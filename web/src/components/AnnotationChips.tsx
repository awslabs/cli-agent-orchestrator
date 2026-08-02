// Conductor annotation chips (work-state design §9.5).
//
// THESE RENDER ALONGSIDE StatusBadge, NEVER INSTEAD OF IT. `status` on a
// projection row is the fork's own statement and stays fork-owned:
// `not_fifo_monitored` already IS a reachability claim ("Managed Live"), and
// replacing it with a conductor label would delete the only thing the fork
// knows and the conductor does not. A row therefore reads
// "<provider status> <conductor chips>", two independent sources, neither
// arbitrating the other.
//
// COLOUR COMES FROM design-tokens/tokens.json, NOT design-tokens/status.json.
// The six semantic roles already exist as the `cao-*` Tailwind family in the
// generated preset, so a chip needs no new status key, no taxonomy entry and
// no artifact regeneration — `node design-tokens/gen.mjs --check` is untouched
// by this file, which is the point: the CI drift gate must never fire because
// somebody added a chip.
//
// NOTHING HERE IS INTERACTIVE. The chips are spans with a `title` and a
// matching `aria-label`, so the hover facets are available to a pointer and to
// assistive technology without adding a control to an unauthenticated
// dashboard's tab order — and the AAA 44×44 target question (§13.8) never
// arises for an element that is not a target.
//
// `role="note"` IS NOT DECORATION. `aria-label` is PROHIBITED by ARIA in HTML
// on a `<span>` with no role, and axe says so at serious impact on every chip;
// support for it on a generic element is unreliable, so the facets the comment
// above claims are "available to assistive technology" were not reliably
// available at all. `note` is a role that takes an accessible name, which
// makes the attribute legal and the announcement dependable.
//
// AND THE HOVER IS NOT THE ONLY PATH. A `title` is unreachable at 390×844 —
// there is no hover on a touch screen — so both surfaces repeat the facets as
// visible text below `sm`. The campaign surface always has; the terminal row
// did not, which left `waiting` on a phone saying a worker is parked and
// nothing about which obligation it is parked on.
//
// NO IDENTITY BUNDLE AND NO COPY-TO-CLIPBOARD (§9.5): `conduct dashboard`
// instructs `tailscale serve` and the server is unauthenticated by default, so
// this surface publishes derived facts and nothing that identifies or actuates
// a worker. No worker-authored free text either (§7) — every string drawn here
// is either the conductor's derived label or a timestamp this file formatted.

import { Annotation } from '../api'
import {
  ageSource,
  freshness,
  orderedFacets,
  resolveRole,
  UnplacedAnnotation,
  UnplacedReason,
} from '../lib/annotations'
import { fmtAbs, fmtAge, fmtRel, parseTimestamp } from '../lib/time'

/** Most chips drawn inline on one terminal row before the overflow marker. */
export const MAX_ROW_CHIPS = 3

/** Most rows drawn on the campaign surface before its own overflow line. */
export const MAX_CAMPAIGN_ROWS = 8

// Measured contrast against the composited backdrop each chip actually sits
// on, at both viewports: success 7.1:1, info 5.5:1, accent 5.4:1,
// warning 8.1:1, danger 5.2:1, neutral 5.6:1 — all past the 4.5:1 AA floor.
// That headroom is WHY staleness is signalled by role + outline + dot rather
// than by `opacity-60`: dimming dropped the label under the floor and made the
// chip that most needs reading the hardest to read.
//
// These numbers are ENFORCED, not recorded: `chip contrast holds at both
// viewports` in e2e/workstate-annotations.spec.ts re-measures them every run.
// It has already earned its keep — deepening the `danger` tint to `/20` for
// emphasis measured 4.4:1 and the gate caught it.
const ROLE_CLASS: Record<string, { dot: string; bg: string; text: string; edge: string }> = {
  success: { dot: 'bg-cao-success', bg: 'bg-cao-success/10', text: 'text-cao-success', edge: '' },
  info: { dot: 'bg-cao-info', bg: 'bg-cao-info/10', text: 'text-cao-info', edge: '' },
  accent: { dot: 'bg-cao-accent', bg: 'bg-cao-accent/10', text: 'text-cao-accent', edge: '' },
  warning: { dot: 'bg-cao-warning', bg: 'bg-cao-warning/10', text: 'text-cao-warning', edge: '' },
  // `danger` is the only role given a second, non-colour channel: a solid ring.
  // Warning and danger are the confusion pair that actually matters and they
  // are adjacent hues; one role needing to jump out is enough, and six shape
  // variants would be worse than the colour.
  //
  // The tint stays at `/10` like every other role. Deepening it to `/20` to
  // make danger "louder" was tried and measured at 4.4:1 — under the AA floor,
  // and axe flagged `color-contrast` on `.text-cao-danger` immediately. The
  // ring adds weight without touching the backdrop the label is read against.
  danger: {
    dot: 'bg-cao-danger',
    bg: 'bg-cao-danger/10',
    text: 'text-cao-danger',
    edge: 'ring-1 ring-cao-danger/70',
  },
  neutral: { dot: 'bg-cao-neutral', bg: 'bg-cao-neutral/10', text: 'text-cao-neutral', edge: '' },
}

const REASON_TEXT: Record<UnplacedReason, string> = {
  campaign: 'campaign',
  task: 'task',
  'orphaned-terminal': 'orphaned run',
  'no-generation': 'no generation',
  'unfenceable-row': 'row publishes no generation',
  'unknown-subject': 'unrecognised subject',
}

function facetText(annotation: Annotation): string {
  const facets = orderedFacets(annotation.details)
  const parts = facets.map(([key, value]) => {
    // A timestamp facet reads as an age, which is what an operator is actually
    // asking. Decided by the VALUE's shape, not by the key's name: a
    // `_at|_utc|since` suffix allowlist is a rule about the conductor's
    // spelling, and it fails the day a facet is renamed.
    const rel = parseTimestamp(value) !== null ? fmtRel(value) : null
    if (rel) return `${key.replace(/_/g, ' ')}: ${rel}`
    return `${key.replace(/_/g, ' ')}: ${value}`
  })
  return parts.join(' · ')
}

/**
 * Who the annotation is about, in the operator's words.
 *
 * The final branch is a GENERIC identity fallback, not `return s.type`. The
 * seam's headline promise is that a subject type invented in 2031 needs no
 * fork change; it held for not-dropping the chip and failed for making it
 * useful, because a chip that says "workstream" and nothing else tells the
 * operator something is wrong somewhere. Whatever identity fields did arrive
 * are drawn, in a fixed order, with no list of type names anywhere.
 */
function subjectText(annotation: Annotation): string {
  const s = annotation.subject
  if (s.type === 'campaign') return s.campaign ? `campaign ${s.campaign}` : 'campaign'
  if (s.type === 'task') return s.task_id ? `task ${s.task_id}` : 'task'
  if (s.type === 'terminal') {
    const id = s.terminal_id ? s.terminal_id.slice(0, 8) : 'unknown'
    return s.generation ? `terminal ${id} · generation ${s.generation.slice(0, 8)}` : `terminal ${id}`
  }
  const named = new Set(['type', 'terminal_id', 'generation', 'task_id', 'campaign'])
  const identity = [
    s.campaign ? `campaign ${s.campaign}` : null,
    s.task_id ? `task ${s.task_id}` : null,
    s.terminal_id ? `terminal ${s.terminal_id.slice(0, 8)}` : null,
    // Anything the subject carries that the fork has no name for. The reader
    // bounds how many of these there can be and how long each is.
    ...Object.entries(s as Record<string, unknown>)
      .filter(([k, v]) => !named.has(k) && typeof v === 'string' && v)
      .map(([k, v]) => `${k.replace(/_/g, ' ')} ${String(v).slice(0, 40)}`),
  ].filter(Boolean)
  return identity.length ? `${s.type} · ${identity.join(' · ')}` : s.type
}

/**
 * One chip.
 *
 * A stale chip is drawn `neutral` and dimmed no matter what role it claims,
 * and says so in its own hover. Keeping the original colour and adding a
 * subtitle was rejected: a red "blocked" chip that stopped being true an hour
 * ago is read as current at a glance, and the glance is the whole product.
 */
export function AnnotationChip({ annotation, stale }: { annotation: Annotation; stale?: boolean }) {
  const state = stale === undefined ? freshness(annotation.valid_until) : stale ? 'stale' : 'fresh'
  const isOld = state !== 'fresh'
  const role = isOld ? 'neutral' : resolveRole(annotation.semantic_role)
  const cls = ROLE_CLASS[role]
  const age = fmtAge(ageSource(annotation))
  const facets = facetText(annotation)
  // Unknown freshness gets its OWN words, not the expired ones. "Stale since
  // <time>" would be a claim about a time nobody published.
  const freshnessText =
    state === 'stale'
      ? `stale since ${fmtAbs(annotation.valid_until) ?? 'an unknown time'}`
      : state === 'unknown'
        ? 'freshness not declared'
        : null
  const hover = [subjectText(annotation), facets, freshnessText].filter(Boolean).join(' · ')

  return (
    <span
      data-testid="annotation-chip"
      data-kind={annotation.kind}
      data-role={role}
      data-stale={state === 'stale' ? 'true' : 'false'}
      data-freshness={state}
      role="note"
      title={hover}
      aria-label={`${annotation.label}${age ? `, ${age}` : ''} — ${hover}`}
      // Staleness is signalled by the neutral role, a dashed outline and a
      // hollow dot — NOT by opacity. Dimming the whole chip was the first
      // attempt and axe caught it: `opacity-60` blends the label into the
      // background and drops it under the contrast floor, so the chip an
      // operator most needs to be able to read would have been the one hardest
      // to read. It also gives staleness a second, non-colour channel.
      //
      // `rounded-md` AGAINST StatusBadge's `rounded-full` IS THE POINT. The two
      // were the same object 20% smaller, separated by hue alone — and on a
      // real fleet the badge is a constant "Managed Live" carrying no
      // information while the chip is the only pill on the row that says
      // anything. The eye went to the uninformative one. A squared silhouette
      // is a pre-attentive difference that costs nothing structurally.
      //
      // `shrink-0 whitespace-nowrap` IS LOAD-BEARING AT 390px. Without them
      // flexbox shrank the sibling profile-name span to zero and then wrapped
      // the chip into a 130px-tall ellipse anyway, deleting the one thing on
      // the row that says which worker it is.
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md shrink-0 whitespace-nowrap max-w-full ${cls.bg} ${cls.edge} ${
        isOld ? 'border border-dashed border-cao-neutral' : ''
      }`}
    >
      <span
        className={`w-1.5 h-1.5 shrink-0 rounded-full ${
          isOld ? 'border border-cao-neutral' : cls.dot
        }`}
      />
      <span className={`text-[10px] font-medium truncate max-w-[16ch] ${cls.text}`}>
        {annotation.label}
      </span>
      {age && <span className="text-[10px] text-gray-400 shrink-0">{age}</span>}
      {/* Visible, because `title` does not exist on a touch screen and the
          dashed outline alone does not say WHY the chip is grey. */}
      {isOld && (
        <span data-testid="annotation-stale-note" className="text-[10px] text-gray-400 shrink-0">
          · {state === 'stale' ? 'stale' : 'age unknown'}
        </span>
      )}
    </span>
  )
}

/** The facets as a visible line — the phone's only path to them. */
function FacetLine({ annotation, className }: { annotation: Annotation; className: string }) {
  const facets = facetText(annotation)
  if (!facets) return null
  return <span className={className}>{facets}</span>
}

/**
 * The chips for one terminal row, capped with a VISIBLE overflow marker.
 *
 * Renders `null` — not an empty wrapper — when there is nothing to show, so a
 * fleet with no annotations produces byte-identical DOM to the dashboard
 * before this existed.
 */
export function TerminalAnnotations({ annotations }: { annotations: Annotation[] | undefined }) {
  if (!annotations || annotations.length === 0) return null
  const shown = annotations.slice(0, MAX_ROW_CHIPS)
  const hidden = annotations.length - shown.length
  return (
    // `basis-full` below `sm` drops the whole chip group onto its own line, so
    // the identity row keeps the agent-profile name it was previously losing
    // to a single chip at 390px. The wrapper lives INSIDE the null guard: a row
    // with no annotations still emits no element at all.
    <span
      data-testid="annotation-group"
      className="flex items-center gap-1.5 flex-wrap min-w-0 basis-full sm:basis-auto"
    >
      {shown.map((a, i) => (
        <AnnotationChip key={`${a.namespace}:${a.kind}:${a.label}:${i}`} annotation={a} />
      ))}
      {hidden > 0 && (
        <span
          data-testid="annotation-overflow"
          className="text-[10px] text-gray-400 shrink-0"
          title={`${hidden} more annotation${hidden === 1 ? '' : 's'} not shown`}
        >
          +{hidden} more
        </span>
      )}
      {/* The phone's only path to the facets. Hidden from `sm` up, where the
          hover exists and the row has the width for it. */}
      {shown.map((a, i) => (
        <FacetLine
          key={`facets:${a.namespace}:${a.kind}:${a.label}:${i}`}
          annotation={a}
          className="sm:hidden basis-full text-[10px] text-gray-400"
        />
      ))}
    </span>
  )
}

/**
 * The terminal-independent surface: unbound gates, orphaned runs, task-scoped
 * and campaign-scoped work, and any subject type this build does not know.
 *
 * A3's obligation is that none of those are dropped for want of a terminal to
 * hang on; the richer attention surface is A4's. Facets render as visible text
 * here rather than only in a hover, because the 390×844 touch viewport has no
 * hover — this is the one place the operator can read them on a phone.
 */
export function CampaignAnnotations({
  unplaced,
  fenced,
  omitted,
  degraded,
  pending = 0,
}: {
  unplaced: UnplacedAnnotation[]
  fenced: number
  omitted: number
  degraded: boolean
  pending?: number
}) {
  if (unplaced.length === 0 && omitted === 0 && fenced === 0 && !degraded) return null
  // CAPPED AND SCROLL-BOUNDED, for the same reason the terminal row is. This
  // panel is what everything unplaceable falls into and it sits ABOVE Active
  // Sessions, so uncapped it put four screens of gate rows between the
  // operator and the fleet — measured at 2936px with 60 unplaced annotations,
  // and the server's own cap is 500. The surface whose stated purpose is "a
  // chip the operator cannot see is worse than one that is merely unplaced"
  // must not make every terminal row unseeable.
  const shown = unplaced.slice(0, MAX_CAMPAIGN_ROWS)
  const hidden = unplaced.length - shown.length
  return (
    <section
      data-testid="campaign-annotations"
      aria-label="Campaign and unattached annotations"
      className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-2"
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Campaign &amp; unattached
        </h3>
        <div className="flex items-center gap-3 text-[10px] text-gray-400">
          {omitted > 0 && (
            <span data-testid="annotation-omitted">{omitted} annotation{omitted === 1 ? '' : 's'} omitted</span>
          )}
          {degraded && <span data-testid="annotation-degraded">partial data</span>}
        </div>
      </div>
      {shown.length > 0 && (
        <ul className="space-y-1.5 max-h-[40vh] overflow-y-auto">
          {shown.map((entry, i) => (
            <li
              key={`${entry.annotation.namespace}:${entry.annotation.kind}:${i}`}
              data-testid="campaign-annotation-row"
              data-reason={entry.reason}
              className="flex items-start gap-2 flex-wrap"
            >
              <AnnotationChip annotation={entry.annotation} />
              <span className="text-[10px] text-gray-300">{subjectText(entry.annotation)}</span>
              <span className="text-[10px] text-gray-400">({REASON_TEXT[entry.reason]})</span>
              <FacetLine annotation={entry.annotation} className="text-[10px] text-gray-400 basis-full" />
            </li>
          ))}
        </ul>
      )}
      {hidden > 0 && (
        <p data-testid="campaign-annotation-overflow" className="text-[10px] text-gray-400">
          +{hidden} more unattached annotation{hidden === 1 ? '' : 's'} not shown.
        </p>
      )}
      {pending > 0 && (
        <p data-testid="annotation-pending" className="text-[10px] text-gray-400">
          {pending} annotation{pending === 1 ? '' : 's'} waiting for the fleet to load.
        </p>
      )}
      {fenced > 0 && (
        <p data-testid="annotation-fenced" className="text-[10px] text-gray-400">
          {fenced} annotation{fenced === 1 ? '' : 's'} dropped: written for a terminal generation that is
          no longer running.
        </p>
      )}
    </section>
  )
}
