// StatusBadge — terminal status pill for the web dashboard.
//
// STATUS_CONFIG / UNKNOWN_CONFIG are generated from the shared design-token SSOT
// (design-tokens/status.json + tokens.json) via `node design-tokens/gen.mjs`.
// Do not hand-edit the status taxonomy here — edit the JSON and regenerate.
import { useEffect, useId, useRef, useState } from 'react'
import type { MutableRefObject } from 'react'

import type { Annotation, TerminalMeta } from '../api'
import { DISPLAY_STATUS_CONFIG, terminalDisplayState } from '../lib/terminalDisplay'
import { STATUS_CONFIG } from '../status.generated'
import { FloatingCard } from './FloatingCard'
import { MetadataRows, terminalMetadataSections } from './TerminalMetadata'

export { STATUS_CONFIG }

type TerminalStatus = 'IDLE' | 'PROCESSING' | 'COMPLETED' | 'WAITING_USER_ANSWER' | 'ERROR' | string | null

const OPEN_DELAY = 120
const CLOSE_DELAY = 180

export function StatusBadge({
  status,
  terminal,
  annotations,
}: {
  status: TerminalStatus
  /** Optional so standalone badges and existing callers remain useful. */
  terminal?: TerminalMeta
  /** Current, generation-fenced conductor observations for this terminal. */
  annotations?: Annotation[]
}) {
  const display = terminalDisplayState(status, terminal, annotations)
  const config = DISPLAY_STATUS_CONFIG[display.key] || DISPLAY_STATUS_CONFIG.UNKNOWN
  const explanation = config.explanation
  // The explanation doubles as the badge's accessible description; useId keeps
  // the id unique when the dashboard renders several badges.
  const descId = useId()
  const [anchor, setAnchor] = useState<HTMLSpanElement | null>(null)
  const [open, setOpen] = useState(false)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  const clear = (timer: MutableRefObject<number | null>) => {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
  }
  const enter = () => {
    if (!terminal) return
    clear(closeTimer)
    clear(openTimer)
    openTimer.current = window.setTimeout(() => setOpen(true), OPEN_DELAY)
  }
  const leave = () => {
    clear(openTimer)
    clear(closeTimer)
    closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY)
  }

  useEffect(() => () => {
    clear(openTimer)
    clear(closeTimer)
  }, [])

  const evidence = terminal ? terminalMetadataSections(terminal, status)[0] : null
  const headlineEntries = terminal
    ? [
        ...(config.explanation ? [{ label: 'Headline meaning', value: config.explanation }] : []),
        { label: 'Headline basis', value: display.reason },
      ]
    : []

  return (
    <>
      <span
        ref={setAnchor}
        role="note"
        aria-label={terminal ? `${config.label}; pointer hover shows status evidence` : config.label}
        aria-describedby={explanation ? descId : undefined}
        // Native title only for a standalone badge: when terminal metadata is
        // supplied the evidence card is the single hover surface.
        title={!terminal ? explanation : undefined}
        onMouseEnter={enter}
        onMouseLeave={leave}
        data-status={display.key.toLowerCase()}
        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full ${config.bgClass}`}
      >
        <span className={`w-2 h-2 rounded-full ${config.dotClass} ${config.pulse ? 'animate-pulse motion-reduce:animate-none' : ''}`} />
        <span className={`text-xs font-medium ${config.textClass}`}>{config.label}</span>
        {explanation && (
          <span id={descId} className="sr-only">{explanation}</span>
        )}
      </span>
      <FloatingCard
        anchor={anchor}
        open={open && evidence !== null}
        onPointerEnter={enter}
        onPointerLeave={leave}
        role="tooltip"
        labelledBy={`${config.label} status evidence`}
        testId="status-hovercard"
        className="w-[24rem] max-w-[calc(100vw-1rem)]"
      >
        {evidence && (
          <div className="max-h-[60vh] overflow-y-auto p-3 space-y-2 select-text bg-gray-900">
            <p className="text-xs font-semibold text-white">{config.label}</p>
            {headlineEntries.length > 0 && <MetadataRows entries={headlineEntries} dense />}
            <p className="pt-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
              Provider and lifecycle evidence
            </p>
            <MetadataRows entries={evidence.entries} dense />
          </div>
        )}
      </FloatingCard>
    </>
  )
}
