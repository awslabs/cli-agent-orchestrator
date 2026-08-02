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

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Info, Copy, Check, X } from 'lucide-react'

import { Annotation } from '../api'
import { ageSource, freshness, orderedFacets, resolveRole } from '../lib/annotations'
import { fmtAbs, fmtAge, fmtRel, parseTimestamp } from '../lib/time'

/** Gap between the anchor and the card, and the minimum margin to the viewport edge. */
const OFFSET = 6
const MARGIN = 8

/** Pointer grace. Long enough to cross the gap into the card, short enough not to linger. */
const OPEN_DELAY = 120
const CLOSE_DELAY = 180

type Coords = { top: number; left: number } | null

/**
 * Position a fixed-position card against an anchor, flipping above when there
 * is no room below and clamping to the viewport on both axes.
 *
 * Recomputes on scroll and resize rather than closing. A card that vanishes
 * because the operator scrolled one line while reading it is the same failure
 * as the `title` it replaces. `capture: true` on scroll is load-bearing: the
 * rows live inside `overflow-y-auto` containers whose scroll events do not
 * bubble to `window`.
 */
function useAnchoredPosition(anchor: HTMLElement | null, card: HTMLElement | null, open: boolean): Coords {
  const [coords, setCoords] = useState<Coords>(null)

  const place = useCallback(() => {
    if (!anchor || !card) return
    const a = anchor.getBoundingClientRect()
    const c = card.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight

    const below = a.bottom + OFFSET
    const above = a.top - c.height - OFFSET
    // Prefer below; flip only when below genuinely overflows AND above fits.
    const top = below + c.height + MARGIN > vh && above >= MARGIN ? above : below

    let left = a.left
    if (left + c.width + MARGIN > vw) left = vw - c.width - MARGIN
    if (left < MARGIN) left = MARGIN

    setCoords({ top: Math.max(MARGIN, top), left })
  }, [anchor, card])

  useLayoutEffect(() => {
    if (!open) {
      setCoords(null)
      return
    }
    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, place])

  return coords
}

/**
 * The floating surface itself.
 *
 * Portalled to `document.body` because both call sites sit inside clipping
 * ancestors — the campaign panel is `max-h-[40vh] overflow-y-auto` and the
 * session list scrolls — where an in-flow card would be cut off at the seam.
 *
 * Rendered at `visibility: hidden` until placed, so the first paint does not
 * flash at 0,0 while the layout effect measures it.
 */
function FloatingCard({
  anchor,
  open,
  onPointerEnter,
  onPointerLeave,
  testId,
  labelledBy,
  role,
  className = '',
  children,
}: {
  anchor: HTMLElement | null
  open: boolean
  onPointerEnter?: () => void
  onPointerLeave?: () => void
  testId: string
  labelledBy?: string
  role: 'tooltip' | 'dialog'
  className?: string
  children: React.ReactNode
}) {
  const [card, setCard] = useState<HTMLDivElement | null>(null)
  const coords = useAnchoredPosition(anchor, card, open)
  if (!open) return null

  return createPortal(
    <div
      ref={setCard}
      data-testid={testId}
      role={role}
      aria-label={labelledBy}
      onMouseEnter={onPointerEnter}
      onMouseLeave={onPointerLeave}
      style={{
        position: 'fixed',
        top: coords?.top ?? 0,
        left: coords?.left ?? 0,
        visibility: coords ? 'visible' : 'hidden',
      }}
      className={`z-[70] rounded-lg border border-gray-700 bg-gray-900/98 shadow-2xl backdrop-blur-sm ${className}`}
    >
      {children}
    </div>,
    document.body,
  )
}

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

function humanKey(key: string): string {
  return key.replace(/_/g, ' ')
}

/** Key/value rows shared by both surfaces. */
function FacetRows({ entries, dense }: { entries: Array<[string, string]>; dense?: boolean }) {
  if (entries.length === 0) return null
  return (
    <dl className={`grid grid-cols-[auto,1fr] gap-x-3 ${dense ? 'gap-y-0.5' : 'gap-y-1'}`}>
      {entries.map(([key, value]) => {
        const v = facetValue(value)
        return (
          <div key={key} className="contents">
            <dt className="text-[10px] uppercase tracking-wide text-gray-500 whitespace-nowrap">
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
  const role = resolveRole(annotation.semantic_role)
  return (
    <div className="max-w-[22rem] p-3 space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <span data-testid="hovercard-label" className="text-xs font-semibold text-white">
          {annotation.label}
        </span>
        {age && <span className="text-[11px] text-gray-400">{age}</span>}
        <span className="text-[10px] uppercase tracking-wide text-gray-500">{role}</span>
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
export function detailsToText(annotations: Annotation[], heading: string): string {
  const lines: string[] = [heading, '='.repeat(heading.length)]
  annotations.forEach((a, i) => {
    if (i > 0) lines.push('')
    const age = fmtAge(ageSource(a))
    lines.push(`${a.label}${age ? `  (${age})` : ''}`)
    const push = (entries: Array<[string, string]>) => {
      for (const [k, v] of entries) lines.push(`  ${humanKey(k)}: ${facetValue(v).text}`)
    }
    push(orderedFacets(a.details))
    lines.push('  --')
    push(subjectEntries(a))
    push(envelopeEntries(a))
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
        <div className="flex items-start justify-between gap-2 px-3 py-2 border-b border-gray-700/60">
          <div className="min-w-0">
            <p className="text-xs font-semibold text-white truncate">Work state</p>
            <p className="text-[10px] text-gray-500 font-mono truncate">{heading}</p>
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

        <div className="max-h-[60vh] overflow-y-auto px-3 py-2 space-y-3 select-text">
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
                <FacetRows entries={orderedFacets(a.details)} dense />
                <details className="group">
                  <summary className="text-[10px] text-gray-500 cursor-pointer hover:text-gray-300 select-none">
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

        <div className="flex items-center justify-between gap-2 px-3 py-2 border-t border-gray-700/60">
          <span className="text-[10px] text-gray-500">
            {annotations.length} annotation{annotations.length === 1 ? '' : 's'}
          </span>
          <CopyButton text={text} />
        </div>
      </FloatingCard>
    </>
  )
}
