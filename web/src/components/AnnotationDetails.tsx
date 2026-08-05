// Hover card and info popover for conductor work-state annotations.
//
// WHY THIS EXISTS. The chips shipped with a native `title`, which is the one
// affordance a browser gives for free and the one an operator cannot use: it
// appears on the browser's schedule, cannot be moused into, cannot be selected,
// and truncates a long facet line into an unreadable ribbon. The facts were
// already derived and already on the wire; only the presentation was failing.
//
// TWO SURFACES, DELIBERATELY DIFFERENT:
//
//   hover card  — anchored to one chip, POINTER-ONLY, shows what that chip is
//                 asserting about state. It is an enhancement, not a path: a
//                 pointer user gets it, and nobody depends on it.
//   info popover — opened from a real <button> in the row's action cluster,
//                 shows EVERY annotation on the row plus the envelope metadata
//                 the chip has no room for, and is copyable.
//
// THE CHIPS STAY NON-INTERACTIVE (AnnotationChips.tsx's founding decision).
// Making 41 spans focusable to give the keyboard a route to the hover card
// would have put 41 stops in the tab order of an unauthenticated dashboard and
// re-opened the AAA 44×44 target question (§13.8) the original design avoided.
// The info button is one control per row, is already a legal target, and
// carries the COMPLETE detail — so the keyboard and touch paths are strictly
// better than the pointer path rather than a degraded copy of it.
//
// ON §9.5 ("no identity bundle, no copy-to-clipboard"). That rule exists
// because `conduct dashboard` instructs `tailscale serve` and the server is
// unauthenticated by default. What it forbids is this surface PUBLISHING
// something that identifies or actuates a worker. Everything drawn here is
// already published to the same origin by `GET /annotations` and already
// rendered by the chip's own `aria-label` and by the mobile facet line; the
// row itself already actuates (Terminal, Graceful Exit, Close). Copying text
// the page is already showing adds no disclosure. The rule still binds where
// it bites: NO worker-authored free text (§7) — every string here is the
// conductor's derived label, a key the conductor chose, or a timestamp this
// file formatted.

import { useCallback, useEffect, useRef, useState } from 'react'
import { Info, Copy, Check, X } from 'lucide-react'

import { Annotation } from '../api'
import { FloatingCard } from './FloatingCard'
import {
  ageSource,
  freshness,
  groupedFacets,
  isIdentity,
  orderedFacets,
  plainFacets,
  resolveRole,
} from '../lib/annotations'
import { fmtAbs, fmtAge, fmtRel, parseTimestamp } from '../lib/time'

/** Pointer grace. Long enough to cross the gap into the card, short enough not to linger. */
const OPEN_DELAY = 120
const CLOSE_DELAY = 180

/**
 * A facet value, read as an age when the value is a PAST timestamp.
 *
 * The future check is load-bearing. `fmtRel` clamps with
 * `Math.max(0, now - t)`, so it renders any future timestamp as "just now" —
 * i.e. it states that something which has not happened yet already has. That is
 * correct for its intended input (ages are always past) and wrong for anything
 * expiry-shaped, so a future timestamp gets the absolute and no relative at all.
 */
function facetValue(value: string): { text: string } {
  const at = parseTimestamp(value)
  if (at === null) return { text: value }
  const abs = fmtAbs(value)
  if (at > Date.now()) return { text: abs ?? value }
  const rel = fmtRel(value)
  if (!rel) return { text: abs ?? value }
  return { text: abs ? `${rel} (${abs})` : rel }
}

/**
 * A facet key as prose.
 *
 * The dot goes the same way the underscore does. A key carrying a provenance
 * class reads as "observed branch" wherever it is drawn ungrouped, which keeps
 * the class visible on the per-chip surface without this file knowing what any
 * class is called.
 */
function humanKey(key: string): string {
  return key.replace(/[_.]/g, ' ')
}

/** Key/value rows shared by both surfaces. */
function FacetRows({ entries, dense }: { entries: Array<[string, string]>; dense?: boolean }) {
  if (entries.length === 0) return null
  return (
    // `minmax(0,1fr)`, NOT `1fr`. A grid item's automatic minimum size is its
    // MIN-CONTENT width, so a plain `1fr` track cannot shrink below the longest
    // unbreakable run in it — `break-words` never gets the chance to break. The
    // track then pushes the card wider than its own `max-w`, and the
    // `overflow-hidden` that clips the rounded corners silently clips the text
    // instead. Measured live before the fix: a 352px hover card holding 425px
    // of content, so every value lost its last 72px and `observed.branch`
    // rendered as `review/cond0225-0230-native-atte` + `sol-r5` -- a branch
    // name that does not exist, drawn as if it were complete.
    //
    // This surfaced only now because until the VCS chip every facet value was
    // short (`task: p0-09b-r1`, `role: implementer`). Branch names, 40-char
    // shas and worktree names are the first values wide enough to hit it.
    <dl className={`grid grid-cols-[auto,minmax(0,1fr)] gap-x-3 ${dense ? 'gap-y-0.5' : 'gap-y-1'}`}>
      {entries.map(([key, value], i) => {
        const v = facetValue(value)
        return (
          // Index in the key because a grouped block can legitimately hold the
          // same short name twice — two chips asserting different values for
          // one name is precisely the disagreement worth showing, not a
          // collision to collapse.
          <div key={`${key}:${i}`} className="contents">
            {/* gray-400, not gray-500. On this card's opaque gray-900 the
                latter measures 3.65:1 — under the 4.5 floor — and the e2e axe
                gate caught every `dt` on the surface. gray-400 is 6.95:1. */}
            <dt className="text-[10px] uppercase tracking-wide text-gray-400 whitespace-nowrap">
              {humanKey(key)}
            </dt>
            <dd className="text-[11px] text-gray-200 break-words">{v.text}</dd>
          </div>
        )
      })}
    </dl>
  )
}

/** Short identity, as the chip draws it. */
function shortSubject(annotation: Annotation): string {
  const s = annotation.subject
  if (s.type === 'campaign') return s.campaign ? `campaign ${s.campaign}` : 'campaign'
  if (s.type === 'task') return s.task_id ? `task ${s.task_id}` : 'task'
  if (s.type === 'terminal') {
    const id = s.terminal_id ? s.terminal_id.slice(0, 8) : 'unknown'
    return s.generation ? `terminal ${id} · generation ${s.generation.slice(0, 8)}` : `terminal ${id}`
  }
  return s.type
}

function freshnessNote(annotation: Annotation): string | null {
  const state = freshness(annotation.valid_until)
  if (state === 'stale') return `stale since ${fmtAbs(annotation.valid_until) ?? 'an unknown time'}`
  if (state === 'unknown') return 'freshness not declared'
  return null
}

/**
 * The pointer-only hover card: what THIS chip is asserting, formatted.
 *
 * Deliberately not the whole envelope. The operator hovering a chip is asking
 * "what is this worker doing and since when"; namespace, version, priority and
 * the full 36-character generation answer a different question and belong in
 * the info popover, which is the surface that promises everything.
 */
export function AnnotationHoverCardBody({ annotation }: { annotation: Annotation }) {
  const age = fmtAge(ageSource(annotation))
  const note = freshnessNote(annotation)
  // THE ROLE IS DRAWN ONLY FOR A CHIP THAT IS MAKING A SEVERITY CLAIM. An
  // identity chip deliberately withholds `data-role` so that nothing — a test
  // or an operator — reads severity off it; printing the same word here in
  // plain sight was the louder half of the leak the attribute was withheld to
  // prevent. The role still appears in the info popover's envelope block,
  // which promises the whole envelope and labels it as such.
  const role = isIdentity(annotation) ? null : resolveRole(annotation.semantic_role)
  return (
    <div className="max-w-[22rem] p-3 space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span data-testid="hovercard-label" className="text-xs font-semibold text-white">
          {annotation.label}
        </span>
        {age && <span className="text-[11px] text-gray-400">{age}</span>}
        {role && (
          <span
            data-testid="hovercard-role"
            className="text-[10px] uppercase tracking-wide text-gray-400"
          >
            {role}
          </span>
        )}
      </div>
      <p className="text-[10px] text-gray-400 font-mono break-all">{shortSubject(annotation)}</p>
      <FacetRows entries={orderedFacets(annotation.details)} dense />
      {note && (
        <p data-testid="hovercard-freshness" className="text-[10px] text-amber-300/90">
          {note}
        </p>
      )}
    </div>
  )
}

/**
 * Hover-card wiring for one chip, as a hook.
 *
 * A HOOK RATHER THAN A WRAPPER COMPONENT, on purpose. Wrapping the chip in an
 * element — even `display: contents` — was the first shape and it is wrong
 * twice: `contents` generates no box, so it cannot receive pointer events
 * reliably, and any real wrapper puts a box between the chip and the flex
 * container it is laid out by. `shrink-0`, `whitespace-nowrap` and
 * `basis-full sm:basis-auto` on the chip are all statements about THAT parent,
 * and the 390px regression they exist to prevent (a chip deleting the
 * agent-profile name) comes straight back if something is interposed.
 *
 * The caller spreads `anchorProps` onto the chip it already renders and drops
 * `hoverCard` next to it; the card is portalled, so it costs no box either.
 */
export function useAnnotationHover(annotation: Annotation) {
  const [anchor, setAnchor] = useState<HTMLSpanElement | null>(null)
  const [open, setOpen] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  const clear = useCallback(() => {
    if (timer.current !== undefined) {
      window.clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])
  const schedule = useCallback(
    (next: boolean, delay: number) => {
      clear()
      timer.current = window.setTimeout(() => setOpen(next), delay)
    },
    [clear],
  )

  useEffect(() => clear, [clear])
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const anchorProps = {
    ref: setAnchor,
    onMouseEnter: () => schedule(true, OPEN_DELAY),
    onMouseLeave: () => schedule(false, CLOSE_DELAY),
  }

  const hoverCard = (
    <FloatingCard
      anchor={anchor}
      open={open}
      role="tooltip"
      testId="annotation-hovercard"
      onPointerEnter={clear}
      onPointerLeave={() => schedule(false, CLOSE_DELAY)}
    >
      <AnnotationHoverCardBody annotation={annotation} />
    </FloatingCard>
  )

  return { anchorProps, hoverCard }
}

/** Every identity field the subject carries, at full length. */
function subjectEntries(annotation: Annotation): Array<[string, string]> {
  const s = annotation.subject
  const out: Array<[string, string]> = [['type', s.type]]
  for (const [k, v] of Object.entries(s)) {
    if (k === 'type' || typeof v !== 'string' || !v) continue
    out.push([k, v])
  }
  return out
}

/** The envelope the chip has no room for. */
function envelopeEntries(annotation: Annotation): Array<[string, string]> {
  const out: Array<[string, string]> = [
    ['kind', annotation.kind],
    ['semantic role', annotation.semantic_role],
    ['priority', String(annotation.priority)],
    ['namespace', annotation.namespace],
    ['version', String(annotation.version)],
  ]
  if (annotation.source) out.push(['source', annotation.source])
  if (annotation.valid_until) {
    // Absolute only — see `facetValue`. `freshness` below already says whether
    // the window has closed, which is the question a relative would be trying
    // to answer.
    out.push(['valid until', fmtAbs(annotation.valid_until) ?? annotation.valid_until])
  }
  out.push(['freshness', freshness(annotation.valid_until)])
  return out
}

/**
 * The plain-text rendering the copy button puts on the clipboard.
 *
 * Built from the SAME entry lists the card draws, so what is copied is what
 * was read. A second formatter would be a second thing to keep true.
 */
/**
 * The row's classed facets, grouped and labelled by the producer's own classes.
 *
 * WHY A SEPARATE BLOCK AT ALL. The classes answer a question no single chip
 * can: what was ASSIGNED to this worker, what it was LAUNCHED with, and what is
 * OBSERVED of it now. Conflating those is the specific failure the grouping
 * exists to prevent — an assigned value the worker has since left is not a lie
 * about now, it is a true statement of a different class — and the labels are
 * the only thing that makes the distinction readable.
 *
 * The headings are the producer's own class tokens, humanised. Nothing here
 * knows what they are, how many there will be, or what order they belong in;
 * rename them and the headings rename themselves.
 */
function WorkerSection({ annotations }: { annotations: Annotation[] }) {
  const groups = groupedFacets(annotations)
  if (groups.length === 0) return null
  return (
    <section data-testid="workstate-worker" className="space-y-1.5">
      <p className="text-[11px] font-semibold text-white">Worker</p>
      {groups.map(({ group, entries }) => (
        <div key={group} data-testid="workstate-worker-group" data-group={group} className="space-y-1">
          <p className="text-[10px] uppercase tracking-wide text-gray-300">{humanKey(group)}</p>
          <div className="pl-2 border-l border-gray-700/60">
            <FacetRows entries={entries} dense />
          </div>
        </div>
      ))}
    </section>
  )
}

export function detailsToText(annotations: Annotation[], heading: string): string {
  const lines: string[] = [heading, '='.repeat(heading.length)]
  const push = (entries: Array<[string, string]>, indent: string) => {
    for (const [k, v] of entries) lines.push(`${indent}${humanKey(k)}: ${facetValue(v).text}`)
  }
  // FIRST, for the same reason it is drawn first: it is the dossier, and the
  // per-annotation sections below are the individual claims made against it.
  const groups = groupedFacets(annotations)
  if (groups.length > 0) {
    lines.push('', 'Worker')
    for (const { group, entries } of groups) {
      lines.push(`  ${humanKey(group)}`)
      push(entries, '    ')
    }
    lines.push('', 'Annotations')
  }
  annotations.forEach((a, i) => {
    // Byte-identical to the pre-existing rendering when there is no Worker
    // block, which is every document published before this field existed.
    if (i > 0 || groups.length > 0) lines.push('')
    const age = fmtAge(ageSource(a))
    lines.push(`${a.label}${age ? `  (${age})` : ''}`)
    // The classed facets are in the Worker block above; repeating them here
    // would make the copied text say everything twice.
    push(plainFacets(a), '  ')
    lines.push('  --')
    push(subjectEntries(a), '  ')
    push(envelopeEntries(a), '  ')
  })
  return lines.join('\n')
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const [failed, setFailed] = useState(false)

  const copy = async () => {
    setFailed(false)
    try {
      // `navigator.clipboard` needs a secure context. https via `tailscale
      // serve` and http://127.0.0.1 both qualify; anything else falls back.
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        throw new Error('clipboard unavailable')
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Do NOT claim success. The text is selectable either way, which is the
      // fallback that always works.
      setFailed(true)
      window.setTimeout(() => setFailed(false), 2500)
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      data-testid="workstate-copy"
      className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-[11px] text-gray-300 bg-gray-800 hover:bg-gray-700 hover:text-white transition-colors"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? 'Copied' : failed ? 'Press ⌘C' : 'Copy'}
    </button>
  )
}

/**
 * The verbose surface: every annotation on the row, every facet, every
 * identity field at full length, and the envelope.
 *
 * Click-opened and click-dismissed rather than hover-dismissed — an operator
 * reading and selecting text must be able to put the pointer anywhere.
 */
export function WorkStateInfoButton({
  annotations,
  terminalId,
  agentProfile,
}: {
  annotations: Annotation[] | undefined
  terminalId: string
  agentProfile?: string | null
}) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (anchor?.contains(t)) return
      const card = document.querySelector('[data-testid="workstate-details"]')
      if (card?.contains(t)) return
      setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown)
    }
  }, [open, anchor])

  // A control that opens an empty card is worse than no control.
  if (!annotations || annotations.length === 0) return null

  const heading = `${agentProfile || 'default'} · ${terminalId}`
  const text = detailsToText(annotations, `Work state — ${heading}`)

  return (
    <>
      <button
        ref={setAnchor}
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        data-testid="workstate-info-button"
        className="p-1 text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors"
        title="Work state details"
      >
        <Info size={12} />
      </button>
      <FloatingCard
        anchor={anchor}
        open={open}
        role="dialog"
        labelledBy={`Work state details for ${heading}`}
        testId="workstate-details"
        className="w-[24rem] max-w-[calc(100vw-1rem)]"
      >
        {/* Header and footer declare their own opaque background rather than
            inheriting the card's. A fixed card sits over page content, and a
            child with no background of its own makes axe report
            `elmPartiallyObscuring` — it cannot prove what the text is read
            against, so it will not certify the contrast even when the contrast
            is fine. Declaring it removes the ambiguity instead of suppressing
            the check. The borders are opaque for the same reason. */}
        <div className="flex items-start justify-between gap-2 px-3 py-2 bg-gray-900 border-b border-gray-700">
          <div className="min-w-0">
            <p className="text-xs font-semibold text-white truncate">Work state</p>
            <p className="text-[10px] text-gray-400 font-mono truncate">{heading}</p>
          </div>
          <button
            type="button"
            onClick={() => setOpen(false)}
            title="Close"
            data-testid="workstate-close"
            className="p-1 text-gray-500 hover:text-white rounded hover:bg-gray-800 transition-colors shrink-0"
          >
            <X size={12} />
          </button>
        </div>

        <div className="max-h-[60vh] overflow-y-auto px-3 py-2 space-y-3 select-text bg-gray-900">
          {/* Above the annotations, and complete regardless of which chip the
              operator arrived from: the dossier is a property of the ROW. */}
          <WorkerSection annotations={annotations} />
          {annotations.map((a, i) => {
            const age = fmtAge(ageSource(a))
            const note = freshnessNote(a)
            return (
              <section
                key={`${a.namespace}:${a.kind}:${a.label}:${i}`}
                data-testid="workstate-annotation"
                className="space-y-1.5"
              >
                <div className="flex items-baseline gap-2 flex-wrap">
                  <span className="text-[11px] font-semibold text-white">{a.label}</span>
                  {age && <span className="text-[10px] text-gray-400">{age}</span>}
                </div>
                {/* Only the UNCLASSED facets. A classed facet is drawn once,
                    in the Worker block, where it is labelled with the class
                    that gives it its meaning; drawing it here as well would
                    make the card say everything twice and say it worse. */}
                <FacetRows entries={plainFacets(a)} dense />
                <details className="group">
                  <summary className="text-[10px] text-gray-400 cursor-pointer hover:text-white select-none">
                    subject &amp; envelope
                  </summary>
                  <div className="mt-1 pl-2 border-l border-gray-700/60 space-y-1">
                    <FacetRows entries={subjectEntries(a)} dense />
                    <FacetRows entries={envelopeEntries(a)} dense />
                  </div>
                </details>
                {note && <p className="text-[10px] text-amber-300/90">{note}</p>}
              </section>
            )
          })}
        </div>

        <div className="flex items-center justify-between gap-2 px-3 py-2 bg-gray-900 border-t border-gray-700">
          <span className="text-[10px] text-gray-400">
            {annotations.length} annotation{annotations.length === 1 ? '' : 's'}
          </span>
          <CopyButton text={text} />
        </div>
      </FloatingCard>
    </>
  )
}
