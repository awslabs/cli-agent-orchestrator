// RunBar: the run page's top row. On the left, a Workflows button goes back to
// the Workflows home, then the workflow's name, the run's state (its icon spins
// while it runs, with the time since it started, or "Completed in 1 min 12 s")
// and the live-connection hint. On the right: the "Follow the running step"
// switch and Cancel while the run is live, Resume for a failed or cancelled run,
// and a More menu with Export diagnostics, Compare runs and Delete run.

import { useEffect, useRef, useState } from 'react'
import { Ban, ChevronLeft, Download, Ellipsis, GitCompare, Loader2, Radio, RotateCcw, Trash2 } from 'lucide-react'
import type { RunInspection } from '../../api'
import { Elapsed, runCue, SPIN_CLASS } from './stateCues'
import { formatDuration } from './runView'
import { useDiagnosticsExport } from './DiagnosticsExportButton'
import { DeleteRunDialog } from './DeleteRunButton'

/**
 * The back button: a bordered button with a back chevron, so it reads as a
 * button and not as part of the page title. It leaves the run page for the
 * Workflows home.
 */
export function BackToWorkflows({ onBack }: { onBack: () => void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      aria-label="Back to Workflows"
      title="Back to all workflows"
      className="inline-flex h-8 shrink-0 items-center gap-1 rounded-lg border border-gray-700 bg-gray-900 pl-1.5 pr-3 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800 hover:text-white"
    >
      <ChevronLeft size={18} strokeWidth={2.25} aria-hidden="true" />
      Workflows
    </button>
  )
}

export interface RunBarProps {
  run: RunInspection
  /** The run's state, from the polled list when it is fresher than the snapshot. */
  state: string
  runEnded: boolean
  /** The live follow stream is connected. */
  connected: boolean
  /** When the run started, in ms, for the running time. */
  startedMs: number | null
  /** How long the run ran, once it ended and both times are known. */
  seconds: number | null
  onBack: () => void
  /** The follow switch, shown while the run is live. */
  follow: { on: boolean; onChange: (on: boolean) => void } | null
  /** A Cancel or Resume request is in flight. */
  acting: boolean
  onCancel: (() => void) | null
  onResume: (() => void) | null
  onCompare: () => void
}

export function RunBar({
  run,
  state,
  runEnded,
  connected,
  startedMs,
  seconds,
  onBack,
  follow,
  acting,
  onCancel,
  onResume,
  onCompare,
}: RunBarProps) {
  const cue = runCue(state)
  const spinning = state === 'running' && !runEnded
  let suffix: React.ReactNode = null
  if (spinning && startedMs != null) {
    suffix = (
      <>
        {' · '}
        <Elapsed since={startedMs} />
      </>
    )
  } else if (state === 'completed' && seconds != null) {
    suffix = ` in ${formatDuration(seconds)}`
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3.5 gap-y-2 border-b border-gray-800 bg-[#0b1017] px-5 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <BackToWorkflows onBack={onBack} />
        <h2 className="truncate text-[17px] font-bold text-white">{run.workflow_name}</h2>
      </div>
      <span
        className={`inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full bg-gray-800/60 px-2.5 text-xs ${cue.color}`}
        data-testid="run-state"
      >
        <cue.Icon size={14} aria-hidden="true" className={spinning ? SPIN_CLASS : undefined} />
        {/* One inline run of text, so the duration sits one space after the label. */}
        <span className="font-semibold tabular-nums">
          <span>{cue.label}</span>
          {suffix}
        </span>
        <span className="sr-only">{cue.sr}</span>
      </span>
      {!runEnded && (
        <span
          className={`inline-flex shrink-0 items-center gap-1 text-[11px] ${connected ? 'text-emerald-400' : 'text-gray-500'}`}
          title={connected ? 'Live-following' : 'Reconnecting…'}
          data-testid="run-connection"
        >
          {connected ? <Radio size={12} aria-hidden="true" /> : <Loader2 size={12} className={SPIN_CLASS} aria-hidden="true" />}
          {connected ? 'Live' : 'Reconnecting'}
        </span>
      )}

      <div className="ml-auto flex shrink-0 items-center gap-2">
        {follow && (
          <button
            type="button"
            role="switch"
            aria-checked={follow.on}
            onClick={() => follow.onChange(!follow.on)}
            className={`inline-flex h-8 items-center gap-2 rounded-lg border px-3 text-[12.5px] font-semibold transition-colors ${
              follow.on
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                : 'border-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            <span aria-hidden="true" className={`relative h-4 w-7 rounded-full transition-colors ${follow.on ? 'bg-emerald-700' : 'bg-gray-700'}`}>
              <span
                className={`absolute top-0.5 size-3 rounded-full transition-all ${follow.on ? 'left-[14px] bg-emerald-100' : 'left-0.5 bg-gray-300'}`}
              />
            </span>
            Follow the running step
          </button>
        )}
        {onCancel && (
          <button
            type="button"
            disabled={acting}
            onClick={onCancel}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-amber-600/50 px-3 text-[12.5px] font-semibold text-amber-300 hover:bg-amber-900/30 disabled:opacity-50"
          >
            <Ban size={13} aria-hidden="true" /> Cancel
          </button>
        )}
        {onResume && (
          <button
            type="button"
            disabled={acting}
            onClick={onResume}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-emerald-600/50 px-3 text-[12.5px] font-semibold text-emerald-300 hover:bg-emerald-900/30 disabled:opacity-50"
          >
            <RotateCcw size={13} aria-hidden="true" /> Resume
          </button>
        )}
        <MoreMenu runId={run.run_id} workflowName={run.workflow_name} onCompare={onCompare} />
      </div>
    </div>
  )
}

interface MenuItem {
  label: string
  Icon: typeof Download
  onSelect: () => void
  disabled?: boolean
  danger?: boolean
  spin?: boolean
}

/** The More menu. The export and the delete confirm live here, outside the list, so they outlast it closing. */
function MoreMenu({ runId, workflowName, onCompare }: { runId: string; workflowName: string; onCompare: () => void }) {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const { exporting, exportDiagnostics } = useDiagnosticsExport(runId)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([])

  const close = (focusButton: boolean) => {
    setOpen(false)
    if (focusButton) buttonRef.current?.focus()
  }

  const items: MenuItem[] = [
    {
      label: 'Export diagnostics',
      Icon: exporting ? Loader2 : Download,
      spin: exporting,
      disabled: exporting,
      onSelect: () => {
        close(true)
        void exportDiagnostics()
      },
    },
    {
      label: 'Compare runs',
      Icon: GitCompare,
      onSelect: () => {
        close(false)
        onCompare()
      },
    },
    {
      label: 'Delete run',
      Icon: Trash2,
      danger: true,
      onSelect: () => {
        close(false)
        setDeleting(true)
      },
    },
  ]

  // Focus the first item that can be used when the menu opens.
  useEffect(() => {
    if (!open) return
    const first = items.findIndex(item => !item.disabled)
    itemRefs.current[first]?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // A click outside closes the menu.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || buttonRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const onMenuKey = (e: React.KeyboardEvent) => {
    const enabled = items.map((item, i) => (item.disabled ? -1 : i)).filter(i => i >= 0)
    const at = enabled.indexOf(itemRefs.current.findIndex(el => el === document.activeElement))
    let next: number | null = null
    if (e.key === 'ArrowDown') next = enabled[(at + 1) % enabled.length]
    else if (e.key === 'ArrowUp') next = enabled[(at - 1 + enabled.length) % enabled.length]
    else if (e.key === 'Home') next = enabled[0]
    else if (e.key === 'End') next = enabled[enabled.length - 1]
    else if (e.key === 'Escape') {
      e.preventDefault()
      close(true)
      return
    } else if (e.key === 'Tab') {
      setOpen(false)
      return
    }
    if (next == null) return
    e.preventDefault()
    itemRefs.current[next]?.focus()
  }

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        title="More actions"
        onClick={() => setOpen(o => !o)}
        className="inline-flex size-8 items-center justify-center rounded-lg border border-gray-700 text-gray-300 hover:bg-gray-800"
      >
        {exporting ? <Loader2 size={15} className={SPIN_CLASS} aria-hidden="true" /> : <Ellipsis size={16} aria-hidden="true" />}
      </button>
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="More actions"
          onKeyDown={onMenuKey}
          className="absolute right-0 top-full z-30 mt-1.5 w-52 rounded-lg border border-gray-700 bg-gray-900 py-1 shadow-xl shadow-black/40"
        >
          {items.map((item, i) => (
            <button
              key={item.label}
              ref={el => {
                itemRefs.current[i] = el
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={item.disabled}
              onClick={item.onSelect}
              className={`flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm focus:bg-gray-800 focus:outline-hidden disabled:opacity-50 ${
                item.danger ? 'text-red-300 hover:bg-red-950/40' : 'text-gray-200 hover:bg-gray-800'
              }`}
            >
              <item.Icon size={14} aria-hidden="true" className={item.spin ? SPIN_CLASS : undefined} />
              {item.label}
            </button>
          ))}
        </div>
      )}
      <DeleteRunDialog runId={runId} workflowName={workflowName} open={deleting} onClose={() => setDeleting(false)} />
    </div>
  )
}
