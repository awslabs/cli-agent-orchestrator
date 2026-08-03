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
// THE CHIPS STAY OUT OF THE TAB ORDER. They are spans, never controls, so the
// AAA 44×44 target question (§13.8) never arises for an element that is not a
// target and an unauthenticated dashboard does not grow 41 tab stops.
//
// They now carry a POINTER-ONLY hover card (AnnotationDetails.tsx) where the
// native `title` used to be. `title` was the one affordance the browser gives
// for free and the one an operator cannot use: it appears on the browser's
// schedule, cannot be moused into, and cannot be selected or copied. It is
// gone rather than kept alongside, because two tooltips on one element is a
// bug, not a fallback.
//
// NOTHING DEPENDS ON THE HOVER. The complete detail lives behind the row's
// info button, which IS a real control and therefore the keyboard and touch
// path; the facets stay in `aria-label` for assistive technology and stay
// visible as text below `sm`, where there is no hover at all.
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
// NO IDENTITY BUNDLE AND NO COPY-TO-CLIPBOARD *ON THE CHIP* (§9.5): `conduct
// dashboard` instructs `tailscale serve` and the server is unauthenticated by
// default, so this surface publishes derived facts and nothing that identifies
// or actuates a worker. No worker-authored free text either (§7) — every string
// drawn here is either the conductor's derived label or a timestamp this file
// formatted. The info popover does offer copy, of text this page already
// renders and this origin already serves; AnnotationDetails.tsx argues that
// case at its head.

import { Annotation } from '../api'
import { useAnnotationHover } from './AnnotationDetails'
import {
  ageSource,
  freshness,
  isIdentity,
  orderedFacets,
  partitionChips,
  resolveRole,
  splitFacetKey,
  UnplacedAnnotation,
  UnplacedReason,
} from '../lib/annotations'
import { identityStyle } from '../lib/identityColour'
import { fmtAbs, fmtAge, fmtRel, parseTimestamp } from '../lib/time'

/** Most SEVERITY chips drawn inline on one terminal row before the overflow marker. */
export const MAX_ROW_CHIPS = 3

/**
 * Most IDENTITY chips drawn on one terminal row, capped SEPARATELY.
 *
 * A single shared cap is the obvious shape and it is wrong in both directions:
 * a row with three severity chips would drop every identity chip, and a row
 * with two identity chips would push a live `danger` behind a "+1" — the exact
 * regression the freshness-before-priority sort exists to prevent. The two
 * groups answer different questions ("is anything wrong?" and "where is this
 * worker?") and neither may starve the other.
 */
export const MAX_IDENTITY_CHIPS = 2

/**
 * Most facets drawn on one visible facet line before a `+k` count.
 *
 * The line is the phone's only path to the facets, and an identity chip can
 * carry ten of them. Uncapped, that measured roughly five wrapped lines per row
 * across a 44-row fleet at 390px — the surface stops being scannable long
 * before it stops being complete. The popover is the complete rendering.
 */
export const MAX_FACET_LINE = 4

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

function facetParts(annotation: Annotation): string[] {
  return orderedFacets(annotation.details).map(([key, value]) => {
    // The provenance class is dropped HERE and only here. On a 390px line the
    // class is repeated noise — every facet on a chip shares one or two of them
    // — and the popover's grouped block is where the classes are actually
    // labelled and read. Nothing knows what the classes ARE; the shape is read,
    // the value is not inspected.
    const { name } = splitFacetKey(key)
    // A timestamp facet reads as an age, which is what an operator is actually
    // asking. Decided by the VALUE's shape, not by the key's name: a
    // `_at|_utc|since` suffix allowlist is a rule about the conductor's
    // spelling, and it fails the day a facet is renamed.
    const rel = parseTimestamp(value) !== null ? fmtRel(value) : null
    if (rel) return `${name.replace(/_/g, ' ')}: ${rel}`
    return `${name.replace(/_/g, ' ')}: ${value}`
  })
}

/** Every facet, uncapped — the accessible name promises completeness. */
function facetText(annotation: Annotation): string {
  return facetParts(annotation).join(' · ')
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
 * One chip, of whichever of the two shapes this annotation asked for.
 *
 * THE DISPATCH IS HERE, NOT IN `TerminalAnnotations`. The campaign surface
 * renders `AnnotationChip` too, so a partition applied only on the terminal row
 * would leave the campaign panel drawing identity chips in role colours — and
 * the campaign panel is exactly where an identity chip that could not be fenced
 * onto a row ends up.
 *
 * Neither branch calls a hook, so the two sub-components own their own hook
 * order and an annotation changing shape between renders cannot desynchronise
 * it.
 */
export function AnnotationChip({ annotation, stale }: { annotation: Annotation; stale?: boolean }) {
  return isIdentity(annotation) ? (
    <IdentityChip annotation={annotation} stale={stale} />
  ) : (
    <SeverityChip annotation={annotation} stale={stale} />
  )
}

/**
 * A chip whose colour means SEVERITY.
 *
 * A stale chip is drawn `neutral` and dimmed no matter what role it claims,
 * and says so in its own hover. Keeping the original colour and adding a
 * subtitle was rejected: a red "blocked" chip that stopped being true an hour
 * ago is read as current at a glance, and the glance is the whole product.
 */
function SeverityChip({ annotation, stale }: { annotation: Annotation; stale?: boolean }) {
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
  // The card is portalled, so `hoverCard` adds no box here and `anchorProps`
  // go straight onto the chip — nothing is interposed between it and the flex
  // container whose rules the 390px layout depends on.
  const { anchorProps, hoverCard } = useAnnotationHover(annotation)

  return (
    <>
    <span
      {...anchorProps}
      data-testid="annotation-chip"
      data-kind={annotation.kind}
      data-role={role}
      data-stale={state === 'stale' ? 'true' : 'false'}
      data-freshness={state}
      role="note"
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
    {hoverCard}
    </>
  )
}

/**
 * A chip whose colour means IDENTITY.
 *
 * THREE PRE-ATTENTIVE CHANNELS SEPARATE IT FROM A SEVERITY CHIP, and hue is
 * none of them: `rounded-sm` against the severity chip's `rounded-md` and
 * StatusBadge's `rounded-full`, and a monospaced face against both. Hue must
 * never have to carry "is this an alarm or a location", because the whole
 * reason for a separate palette is that the operator reads colour as severity.
 *
 * COLOUR SURVIVES STALENESS HERE, unlike on a severity chip. Greying is right
 * there because an expired `danger` misread as current is the failure; an
 * identity does not stop being that identity, and blanking its colour would
 * make a stale chip indistinguishable from an uncoloured one — a different
 * claim entirely. Staleness keeps its two NON-colour channels: the dashed
 * outline and the hollow dot, plus the visible note.
 *
 * NO `data-role`. The annotation has a `semantic_role` like every other, but
 * this chip does not draw it, and echoing it into an attribute would invite a
 * test — or an operator — to read severity off a chip that is not making a
 * severity claim.
 */
function IdentityChip({ annotation, stale }: { annotation: Annotation; stale?: boolean }) {
  const state = stale === undefined ? freshness(annotation.valid_until) : stale ? 'stale' : 'fresh'
  const isOld = state !== 'fresh'
  const { index, colour, tint } = identityStyle(annotation.colour_key ?? '')
  const age = fmtAge(ageSource(annotation))
  const facets = facetText(annotation)
  const freshnessText =
    state === 'stale'
      ? `stale since ${fmtAbs(annotation.valid_until) ?? 'an unknown time'}`
      : state === 'unknown'
        ? 'freshness not declared'
        : null
  const hover = [subjectText(annotation), facets, freshnessText].filter(Boolean).join(' · ')
  const { anchorProps, hoverCard } = useAnnotationHover(annotation)

  // Dashed-and-hollow already means stale, so the UNCOLOURED variant is given
  // the opposite of both — a solid outline and a filled dot — rather than a
  // second dashed treatment nobody could tell apart from an expired chip.
  const edge = isOld
    ? 'border border-dashed border-cao-neutral'
    : colour
      ? ''
      : 'border border-gray-500'

  return (
    <>
      <span
        {...anchorProps}
        data-testid="annotation-chip"
        data-kind={annotation.kind}
        data-identity="true"
        // THE BUCKET, NEVER THE TOKEN. Echoing `colour_key` itself would put a
        // hash of whatever identity the producer chose into the DOM of an
        // unauthenticated page (§9.5); the slot index leaks under four bits and
        // is all a test needs to prove two chips resolved together.
        data-colour={index === null ? 'none' : String(index)}
        data-stale={state === 'stale' ? 'true' : 'false'}
        data-freshness={state}
        role="note"
        aria-label={`${annotation.label}${age ? `, ${age}` : ''} — ${hover}`}
        // `shrink-0 whitespace-nowrap` for the same reason the severity chip
        // carries them: without them flexbox shrinks the sibling profile-name
        // span to zero at 390px. These chips are DIRECT SIBLINGS in the group's
        // flex container — no inner wrapper — because every one of those class
        // names is a statement about that specific parent.
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm font-mono shrink-0 whitespace-nowrap max-w-full ${edge}`}
        style={colour ? { backgroundColor: tint ?? undefined } : undefined}
      >
        <span
          className={`w-1.5 h-1.5 shrink-0 rounded-full ${
            isOld ? 'border border-cao-neutral' : colour ? '' : 'bg-gray-400'
          }`}
          style={!isOld && colour ? { backgroundColor: colour } : undefined}
        />
        {/* Clamped. WHERE THE FULL VALUE STILL IS, measured rather than
            assumed — an earlier comment here claimed the facet line below
            covers the clamp at every viewport, and it does not:

              * a VCS chip's branch is `observed.branch`, the FIRST facet
                `_vcs_details` writes, so it is always inside `MAX_FACET_LINE`
                and the line does carry it in full below `sm`;
              * a LANE chip's id is the label and is NOT a facet, so below `sm`
                the line carries nothing that repairs a clamp at 16ch;
              * at and above `sm` the line is hidden entirely, so on desktop
                NEITHER chip is repaired by it.

            The full value is reached instead by the two surfaces built for it:
            the pointer hover card (desktop) and the row's info popover (both,
            and the only path on a phone). Colour is a WEAK second channel: it
            keys on the worktree (and on the full lane id), never on the drawn
            text, so two chips clamped to the same characters usually differ in
            colour — usually, not always, because the ramp is twelve slots.
            Widening the clamp is a live layout question — of 172 branches on
            the fleet, 116 exceed 30ch — and it is left to visual review on real
            data rather than settled here. */}
        <span
          className={`text-[10px] font-medium truncate max-w-[16ch] sm:max-w-[30ch] ${
            colour ? '' : 'text-gray-400'
          }`}
          style={colour ? { color: colour } : undefined}
        >
          {annotation.label}
        </span>
        {age && <span className="text-[10px] text-gray-400 shrink-0">{age}</span>}
        {isOld && (
          <span data-testid="annotation-stale-note" className="text-[10px] text-gray-400 shrink-0">
            · {state === 'stale' ? 'stale' : 'age unknown'}
          </span>
        )}
      </span>
      {hoverCard}
    </>
  )
}

/**
 * The facets as a visible line — the phone's only path to them.
 *
 * Capped, with the count stated. An uncapped line is complete and unreadable;
 * a silently cut one is neither.
 */
function FacetLine({ annotation, className }: { annotation: Annotation; className: string }) {
  const parts = facetParts(annotation)
  if (parts.length === 0) return null
  const shown = parts.slice(0, MAX_FACET_LINE)
  const hidden = parts.length - shown.length
  return (
    <span className={className}>
      {shown.join(' · ')}
      {hidden > 0 && ` · +${hidden}`}
    </span>
  )
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
  // TWO GROUPS, TWO CAPS, TWO MARKERS. See `MAX_IDENTITY_CHIPS`: one shared cap
  // lets either group delete the other, and both deletions are the failure the
  // cap exists to prevent.
  const { severity, identity } = partitionChips(annotations)
  const shownSeverity = severity.slice(0, MAX_ROW_CHIPS)
  const hiddenSeverity = severity.length - shownSeverity.length
  const shownIdentity = identity.slice(0, MAX_IDENTITY_CHIPS)
  const hiddenIdentity = identity.length - shownIdentity.length
  const shown = [...shownSeverity, ...shownIdentity]
  return (
    // `basis-full` below `sm` drops the whole chip group onto its own line, so
    // the identity row keeps the agent-profile name it was previously losing
    // to a single chip at 390px. The wrapper lives INSIDE the null guard: a row
    // with no annotations still emits no element at all.
    //
    // EVERY CHIP BELOW IS A DIRECT CHILD OF THIS SPAN. Grouping the identity
    // chips in an inner wrapper is the obvious tidier shape and it reproduces
    // the 390px regression exactly: `shrink-0`, `whitespace-nowrap` and
    // `basis-full` are statements about THIS flex container, and an interposed
    // box makes all three apply to the wrapper instead of the chips.
    <span
      data-testid="annotation-group"
      className="flex items-center gap-1.5 flex-wrap min-w-0 basis-full sm:basis-auto"
    >
      {shownSeverity.map((a, i) => (
        <AnnotationChip key={`${a.namespace}:${a.kind}:${a.label}:${i}`} annotation={a} />
      ))}
      {hiddenSeverity > 0 && (
        <span
          data-testid="annotation-overflow"
          className="text-[10px] text-gray-400 shrink-0"
          title={`${hiddenSeverity} more annotation${hiddenSeverity === 1 ? '' : 's'} not shown`}
        >
          +{hiddenSeverity} more
        </span>
      )}
      {shownIdentity.map((a, i) => (
        <AnnotationChip key={`identity:${a.namespace}:${a.kind}:${a.label}:${i}`} annotation={a} />
      ))}
      {hiddenIdentity > 0 && (
        <span
          data-testid="annotation-identity-overflow"
          className="text-[10px] text-gray-400 shrink-0"
          title={`${hiddenIdentity} more not shown`}
        >
          +{hiddenIdentity}
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
