// StepTerminal: a workflow step's terminal in the run view, as plain text in a
// scrollable <pre>. The step panel's Terminal tab and the pane under the
// timeline both use it.
//
// While the step runs, it polls a read-only capture of the step's screen
// (GET /workflows/runs/{run}/steps/{step}/screen) once a second, but only
// while the page is visible, and stops when it unmounts. When the screen route
// answers 410 (the terminal has ended), or the step settles, it reads the saved
// transcript (GET .../transcript). The transcript file is written as the
// terminal closes, so a 404 right after a step settles is retried a few times.
// If there is still none, it looks at the screen once more: a step that timed
// out or crashed keeps its terminal open, and then the view shows that screen
// as "Terminal still open" until the terminal ends.
//
// It never opens a WebSocket and never attaches to the terminal, so it cannot
// resize the agent's window or send it keys. The Agents tab's xterm view is not
// used here.
//
// The `console` variant is the run page's large terminal: it fills its
// container, says "Watch only", and shows whether it is following the output
// or offers "Jump to latest" once the user has scrolled up. `onText` hands the
// text on screen to the parent, for its Copy button.

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { AlertCircle, ArrowDown, Clock, Loader2, Lock, Radio, Terminal as TermIcon } from 'lucide-react'
import { api, type ApiError, type StepScreen, type StepTranscript } from '../../api'
import { SPIN_CLASS, useNow } from './stateCues'
import { stepActivity } from './runView'

/** How often the live view reads the screen. */
export const SCREEN_POLL_MS = 1000
/** How many times a transcript 404 is retried before the view says none was saved. */
export const TRANSCRIPT_404_RETRIES = 3
/** The wait between transcript retries. */
export const TRANSCRIPT_RETRY_MS = 1000

export interface StepTerminalProps {
  runId: string
  stepId: string
  /** The step's live state, as the Flow shows it. */
  state: string
  /** The terminal the view reads (runView's stepTerminalId), or null when none is known. */
  terminalId: string | null
  /** True once the run has ended. */
  runEnded: boolean
  /** `panel` (the default) is the compact view; `console` fills its container, for the run page. */
  variant?: 'panel' | 'console'
  /** Called with the text on screen, or null when there is none. */
  onText?: (text: string | null) => void
}

/** A step's terminal. A new terminal, such as a retry's, starts the view over. */
export function StepTerminal(props: StepTerminalProps) {
  return <StepTerminalView key={JSON.stringify([props.runId, props.stepId, props.terminalId])} {...props} />
}

type Mode = 'not-started' | 'waiting' | 'live' | 'transcript' | 'no-terminal'

type TranscriptState =
  | { kind: 'loading' }
  | { kind: 'ready'; transcript: StepTranscript }
  | { kind: 'none' }
  | { kind: 'error'; message: string }

function errorText(e: unknown, fallback: string): string {
  const err = e as ApiError | undefined
  return err?.detail || err?.message || fallback
}

/** A small amber label, such as Truncated or Secrets redacted. */
export function Badge({ children }: { children: string }) {
  return (
    <span className="rounded-sm border border-amber-700/60 bg-amber-950/30 px-1.5 py-px text-[10px] text-amber-300">
      {children}
    </span>
  )
}

function StepTerminalView({ runId, stepId, state, terminalId, runEnded, variant = 'panel', onText }: StepTerminalProps) {
  const activity = stepActivity(state, runEnded)
  // The screen route answered 410: the terminal has ended, read the transcript.
  const [gone, setGone] = useState(false)
  // A settled step with no transcript whose terminal still answers. A step that
  // timed out or crashed keeps its terminal open, so its screen stays readable
  // until the terminal is deleted and the transcript is saved.
  const [stillOpen, setStillOpen] = useState(false)
  const mode: Mode =
    activity === 'pending'
      ? 'not-started'
      : (activity === 'running' || stillOpen) && !gone
        ? terminalId ? 'live' : 'waiting'
        : terminalId ? 'transcript' : 'no-terminal'

  // ── Live screen ─────────────────────────────────────────────────────────
  const [screen, setScreen] = useState<StepScreen | null>(null)
  // When the last screen arrived, by this browser's clock, so "Updated N s
  // ago" is not thrown off by a difference between the server's clock and ours.
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [liveError, setLiveError] = useState<string | null>(null)
  const now = useNow(mode === 'live')

  useEffect(() => {
    if (mode !== 'live') return
    let stopped = false
    let inFlight = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const hidden = () => document.visibilityState === 'hidden'
    const schedule = () => {
      if (stopped || inFlight || timer !== undefined || hidden()) return
      timer = setTimeout(() => {
        timer = undefined
        void poll()
      }, SCREEN_POLL_MS)
    }
    const poll = async () => {
      if (stopped || inFlight || hidden()) return
      inFlight = true
      try {
        const next = await api.getWorkflowStepScreen(runId, stepId)
        if (stopped) return
        if (next && typeof next.text === 'string') {
          setScreen(next)
          setReceivedAt(Date.now())
          setLiveError(null)
        } else {
          setLiveError('The server sent an empty screen.')
        }
      } catch (e) {
        if (stopped) return
        if ((e as ApiError)?.status === 410) {
          stopped = true
          setGone(true)
          return
        }
        // A 404 can come just after the terminal opened, before its id is saved
        // on the step: keep polling. Any other error is shown, and polling goes on.
        if ((e as ApiError)?.status !== 404) setLiveError(errorText(e, 'Failed to read the screen'))
      } finally {
        inFlight = false
      }
      schedule()
    }
    // Pause while the page is hidden; read at once when it is visible again.
    const onVisibility = () => {
      if (hidden()) {
        if (timer !== undefined) clearTimeout(timer)
        timer = undefined
      } else {
        void poll()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    void poll()
    return () => {
      stopped = true
      if (timer !== undefined) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [mode, runId, stepId])

  // ── Saved transcript ────────────────────────────────────────────────────
  const [transcript, setTranscript] = useState<TranscriptState>({ kind: 'loading' })
  useEffect(() => {
    if (mode !== 'transcript') return
    let stopped = false
    let retries = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    setTranscript({ kind: 'loading' })
    const load = async () => {
      try {
        const next = await api.getWorkflowStepTranscript(runId, stepId)
        if (stopped) return
        setTranscript(
          next && typeof next.text === 'string'
            ? { kind: 'ready', transcript: next }
            : { kind: 'error', message: 'The server sent an empty transcript.' },
        )
      } catch (e) {
        if (stopped) return
        if ((e as ApiError)?.status === 404) {
          if (retries < TRANSCRIPT_404_RETRIES) {
            retries++
            timer = setTimeout(() => void load(), TRANSCRIPT_RETRY_MS)
            return
          }
          // Still no transcript. The terminal may still be open, as it is after a
          // step times out or crashes: look at its screen once before giving up.
          if (!gone) {
            try {
              const open = await api.getWorkflowStepScreen(runId, stepId)
              if (stopped) return
              if (open && typeof open.text === 'string') {
                setScreen(open)
                setReceivedAt(Date.now())
                setStillOpen(true)
                return
              }
            } catch {
              if (stopped) return
            }
          }
          setTranscript({ kind: 'none' })
          return
        }
        setTranscript({ kind: 'error', message: errorText(e, 'Failed to read the transcript') })
      }
    }
    void load()
    return () => {
      stopped = true
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [mode, runId, stepId, gone])

  const shown =
    mode === 'live' ? screen : mode === 'transcript' && transcript.kind === 'ready' ? transcript.transcript : null
  const text = shown?.text ?? null

  // Hand the text on screen to the parent. Read through a ref, so an inline
  // callback does not re-run this on every render.
  const onTextRef = useRef(onText)
  onTextRef.current = onText
  useEffect(() => {
    onTextRef.current?.(text)
  }, [text])

  // Keep the view at the bottom, where new output lands, unless the user has
  // scrolled up to read. `stuck` mirrors the ref for the console's pill.
  const preRef = useRef<HTMLPreElement>(null)
  const atBottom = useRef(true)
  const [stuck, setStuck] = useState(true)
  useLayoutEffect(() => {
    const el = preRef.current
    if (el && atBottom.current) el.scrollTop = el.scrollHeight
  }, [text])
  const onScroll = () => {
    const el = preRef.current
    if (!el) return
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    setStuck(atBottom.current)
  }
  const jumpToLatest = () => {
    const el = preRef.current
    if (el) el.scrollTop = el.scrollHeight
    atBottom.current = true
    setStuck(true)
  }

  const isConsole = variant === 'console'

  const status = (children: React.ReactNode, Icon = Clock, spin = false) => (
    <div className="flex items-center gap-2 text-xs text-gray-400" role="status">
      <Icon size={14} className={`shrink-0 text-gray-500 ${spin ? SPIN_CLASS : ''}`} aria-hidden="true" />
      <span>{children}</span>
    </div>
  )
  const alert = (message: string) => (
    <div className="flex items-center gap-2 text-xs text-red-400" role="alert">
      <AlertCircle size={14} aria-hidden="true" />
      {message}
    </div>
  )

  let body: React.ReactNode
  let showsText = false
  if (mode === 'not-started') {
    body = status('This step has not started.')
  } else if (mode === 'waiting') {
    body = status("Waiting for the step's terminal.", Loader2, true)
  } else if (mode === 'no-terminal') {
    body = status('No transcript was saved for this step.')
  } else if (mode === 'live' && !screen) {
    body = liveError ? alert(liveError) : status("Waiting for the step's terminal.", Loader2, true)
  } else if (mode === 'transcript' && transcript.kind === 'loading') {
    body = status('Loading the transcript...', Loader2, true)
  } else if (mode === 'transcript' && transcript.kind === 'none') {
    body = status('No transcript was saved for this step.')
  } else if (mode === 'transcript' && transcript.kind === 'error') {
    body = alert(transcript.message)
  } else {
    showsText = true
    body = (
      <>
        <pre
          ref={preRef}
          onScroll={onScroll}
          tabIndex={0}
          aria-label={mode === 'live' ? `Live screen of step ${stepId}` : `Saved transcript of step ${stepId}`}
          data-testid="step-terminal-text"
          className={
            isConsole
              ? 'absolute inset-0 overflow-auto whitespace-pre px-4 py-3 font-mono text-[12.5px] leading-relaxed text-gray-300 focus:outline-hidden focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-emerald-600'
              : 'max-h-80 overflow-auto whitespace-pre font-mono text-xs text-gray-300 focus:outline-hidden focus-visible:ring-1 focus-visible:ring-emerald-600'
          }
        >
          {text || (
            <span className="italic text-gray-600">
              {mode === 'live' ? 'Nothing on the screen yet.' : 'The transcript is empty.'}
            </span>
          )}
        </pre>
        {mode === 'live' && liveError && (
          <div className={isConsole ? 'absolute left-4 top-2 rounded-md bg-[#0b1016]/95 px-2 py-1' : 'mt-2'}>{alert(liveError)}</div>
        )}
      </>
    )
  }

  const updatedSeconds = receivedAt != null ? Math.max(0, Math.floor((now - receivedAt) / 1000)) : null
  const liveLabel = activity === 'running' ? 'Live' : 'Terminal still open'

  if (isConsole) {
    return (
      <section
        aria-label={`Terminal of step ${stepId}`}
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-gray-800 bg-[#0b1016]"
        data-testid="step-terminal"
        data-mode={mode}
        data-variant="console"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-gray-800 bg-[#0d131b] px-4 py-2 text-xs text-gray-400">
          <TermIcon size={14} className="shrink-0 text-gray-500" aria-hidden="true" />
          <span className="truncate font-mono text-gray-300">{terminalId ? `terminal ${terminalId}` : 'terminal'}</span>
          <span className="ml-auto" />
          {mode === 'live' && (
            <span className="inline-flex shrink-0 items-center gap-1.5 font-semibold text-emerald-400">
              <span aria-hidden="true" className="size-1.5 rounded-full bg-emerald-400 shadow-[0_0_0_3px_rgba(52,211,153,0.15)]" />
              <span data-testid="step-terminal-live">{liveLabel}</span>
              {updatedSeconds != null && (
                <span
                  className="tabular-nums"
                  data-testid="step-terminal-updated"
                  title={screen?.captured_at ? `Captured at ${screen.captured_at}` : undefined}
                >
                  · updated {updatedSeconds} s ago
                </span>
              )}
            </span>
          )}
          {mode === 'transcript' && transcript.kind === 'ready' && <span className="shrink-0 text-gray-400">Saved transcript</span>}
          {shown?.truncated && <Badge>Truncated</Badge>}
          {shown?.redacted && <Badge>Secrets redacted</Badge>}
          <span
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-gray-700 px-2 py-0.5 text-[11px] text-gray-400"
            title="This view reads the screen. It cannot type into the agent's terminal or resize it."
            data-testid="step-terminal-watch-only"
          >
            <Lock size={11} aria-hidden="true" /> Watch only
          </span>
        </header>
        <div className={`relative min-h-0 flex-1 ${showsText ? '' : 'p-4'}`}>
          {body}
          {showsText && (stuck ? (
            mode === 'live' && (
              <span
                className="pointer-events-none absolute bottom-3 right-4 inline-flex items-center gap-1.5 rounded-full border border-blue-400/50 bg-[#10233f] px-3 py-1.5 text-xs font-semibold text-blue-200"
                data-testid="step-terminal-following"
              >
                <ArrowDown size={12} aria-hidden="true" /> Following the output
              </span>
            )
          ) : (
            <button
              type="button"
              onClick={jumpToLatest}
              className="absolute bottom-3 right-4 inline-flex items-center gap-1.5 rounded-full border border-blue-400/50 bg-[#10233f] px-3 py-1.5 text-xs font-semibold text-blue-200 hover:bg-[#15305a]"
            >
              <ArrowDown size={12} aria-hidden="true" /> Jump to latest
            </button>
          ))}
        </div>
      </section>
    )
  }

  return (
    <section
      aria-label={`Terminal of step ${stepId}`}
      className="overflow-hidden rounded-lg border border-gray-700/50 bg-[#0d1117]"
      data-testid="step-terminal"
      data-mode={mode}
    >
      <header className="flex flex-wrap items-center gap-2 border-b border-gray-700/50 bg-gray-900 px-3 py-2 text-[11px]">
        <TermIcon size={14} className="shrink-0 text-emerald-400" aria-hidden="true" />
        {terminalId && <span className="truncate font-mono text-gray-300">{terminalId}</span>}
        {mode === 'live' && (
          <span className="inline-flex items-center gap-1 text-emerald-400" data-testid="step-terminal-live">
            <Radio size={12} aria-hidden="true" /> {liveLabel}
          </span>
        )}
        {mode === 'live' && updatedSeconds != null && (
          <span
            className="text-gray-500 tabular-nums"
            data-testid="step-terminal-updated"
            title={screen?.captured_at ? `Captured at ${screen.captured_at}` : undefined}
          >
            Updated {updatedSeconds} s ago
          </span>
        )}
        {mode === 'transcript' && transcript.kind === 'ready' && <span className="text-gray-400">Saved transcript</span>}
        {shown?.truncated && <Badge>Truncated</Badge>}
        {shown?.redacted && <Badge>Secrets redacted</Badge>}
      </header>
      <div className="min-h-16 p-3">{body}</div>
    </section>
  )
}
