// The anchored floating surface, extracted from AnnotationDetails.tsx so the
// filter chip editors and pickers can float over the same clipping ancestors
// (the campaign panel is `max-h-[40vh] overflow-y-auto`, the session cards are
// `overflow-hidden`, and the session list scrolls) without a second
// positioning implementation to keep true.
//
// Everything here is a measured decision, moved verbatim from the annotation
// surfaces: the flip-and-clamp placement, the scroll listener with
// `capture: true`, the opaque background, and the `overflow-hidden` clipping.
// The comments are the original incident reports; the filter bars inherit
// them.

import { useCallback, useLayoutEffect, useState } from 'react'
import { createPortal } from 'react-dom'

/** Gap between the anchor and the card, and the minimum margin to the viewport edge. */
const OFFSET = 6
const MARGIN = 8

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
    let top = below + c.height + MARGIN > vh && above >= MARGIN ? above : below

    // CLAMP BOTH EDGES, not just the top. Clamping only the top let a card
    // taller than the space beneath its anchor hang off the bottom of the
    // viewport — which at 390×844 put the footer, and with it the copy button,
    // out of reach entirely. The mobile axe run caught it as an unresolvable
    // contrast on the footer, because a node outside the viewport cannot be
    // composited. Bottom clamp first, then top, so a card taller than the
    // viewport pins to the top and scrolls internally rather than pinning to
    // the bottom and losing its header.
    if (top + c.height + MARGIN > vh) top = vh - c.height - MARGIN
    if (top < MARGIN) top = MARGIN

    let left = a.left
    if (left + c.width + MARGIN > vw) left = vw - c.width - MARGIN
    if (left < MARGIN) left = MARGIN

    setCoords({ top, left })
  }, [anchor, card])

  useLayoutEffect(() => {
    if (!open) {
      setCoords(null)
      return
    }
    place()
    // Portalled cards can change size after their first measured paint (for
    // example when asynchronously-fetched filter dimensions arrive). Keep the
    // fixed coordinates truthful instead of leaving newly-added controls past
    // the viewport edge until the next scroll or window resize.
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    if (observer && anchor && card) {
      observer.observe(anchor)
      observer.observe(card)
    }
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      observer?.disconnect()
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
export function FloatingCard({
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
      // FULLY OPAQUE, deliberately. A translucent surface cannot be composited
      // by axe, so every text node on it reports `color-contrast` as
      // *incomplete* at serious impact — and this repo's gate asserts on
      // `scan.incomplete` as well as `scan.violations`, precisely because an
      // unverifiable contrast claim is not a passing one. `bg-gray-900/98`
      // bought nothing visually and cost the whole card its verifiability.
      // `overflow-hidden` is not cosmetic. Without it the square-cornered
      // header and footer paint OUTSIDE the card's rounded boundary, so they
      // overlap the page behind the card directly — visible as corners poking
      // out of the radius, and reported by axe as
      // `color-contrast[elmPartiallyObscuring]` on the footer, because a node
      // that partially covers other elements has no determinable backdrop.
      // Clipping to the radius fixes the artefact and the finding together.
      className={`z-[70] overflow-hidden rounded-lg border border-gray-700 bg-gray-900 shadow-2xl ${className}`}
    >
      {children}
    </div>,
    document.body,
  )
}
