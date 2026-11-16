// runPageParts: small pieces the run page shares: the agent avatar, a ticking
// clock, the state chip colours, Copy, the slide-over panel, and the hook that
// makes the page fill the window below the tab row.

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import type { DerivedStepState } from './derivedStepState'
import { useNow } from './stateCues'
import { agentInitials, agentTone, formatClock } from './runPage'

/** Chip colours for each derived state. Every chip also carries text, so colour is never the only cue. */
export const CHIP_TONES: Record<DerivedStepState, string> = {
  pending: 'bg-gray-500/15 text-gray-300',
  running: 'bg-blue-500/15 text-blue-300',
  retried: 'bg-amber-500/15 text-amber-300',
  completed: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-red-500/15 text-red-300',
  skipped: 'bg-gray-500/15 text-gray-400',
  cancelled: 'bg-orange-500/15 text-orange-300',
}

/** An agent's initials on its colour. Decorative: the agent's name is always next to it. */
export function AgentAvatar({ agent, large = false }: { agent: string | null; large?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-grid shrink-0 place-items-center rounded-md font-bold ${
        large ? 'size-7 text-[11px]' : 'size-[22px] text-[10px]'
      } ${agentTone(agent)}`}
    >
      {agentInitials(agent)}
    </span>
  )
}

/** The time since `since` (ms since the epoch) as a clock, such as 0:48, updated every second. */
export function ClockSince({ since }: { since: number }) {
  const now = useNow()
  return <span className="tabular-nums">{formatClock((now - since) / 1000)}</span>
}

/**
 * Put text on the clipboard. The Clipboard API needs a secure context, which a
 * CAO server reached over plain http on another host is not, so a hidden
 * textarea and execCommand are tried next. Resolves to whether it worked.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Fall through to the textarea.
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = typeof document.execCommand === 'function' && document.execCommand('copy')
    area.remove()
    return ok
  } catch {
    return false
  }
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export interface SlideOverProps {
  title: string
  onClose: () => void
  children: ReactNode
  testId?: string
}

/**
 * A panel that slides over the right of the page, such as the full event
 * timeline. A modal dialog: focus moves to its close button and stays inside
 * until it closes, Escape or a click outside closes it, and focus then goes
 * back to what opened it.
 */
export function SlideOver({ title, onClose, children, testId }: SlideOverProps) {
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    closeRef.current?.focus()
    return () => opener?.focus()
  }, [])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !panelRef.current) return
    const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE))
    if (items.length === 0) return
    const first = items[0]
    const last = items[items.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <div aria-hidden="true" className="absolute inset-0 bg-black/50" onClick={onClose} data-testid="slide-over-backdrop" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        data-testid={testId}
        className="relative flex h-full w-[min(46rem,94vw)] flex-col border-l border-gray-800 bg-[#0c1118] shadow-2xl shadow-black/50"
      >
        <header className="flex shrink-0 items-center gap-2 border-b border-gray-800 px-5 py-3">
          <h2 id={titleId} className="text-sm font-semibold text-white">{title}</h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label={`Close ${title}`}
            className="ml-auto rounded-md p-1.5 text-gray-400 hover:bg-gray-800 hover:text-white"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/**
 * A height, in px, that makes the element reach the bottom of the window,
 * less the bottom padding of the <main> it sits in, so the page itself does
 * not scroll. Never less than `min`; re-measured when the window resizes.
 */
export function useFillHeight(ref: RefObject<HTMLElement | null>, min = 480): number | undefined {
  const [height, setHeight] = useState<number>()
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const top = el.getBoundingClientRect().top + window.scrollY
      const main = el.closest('main')
      const pad = main ? Number.parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0
      setHeight(Math.max(min, Math.floor(window.innerHeight - top - pad)))
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [ref, min])
  return height
}
