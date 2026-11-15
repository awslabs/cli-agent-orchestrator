// StepConsole: the run page's right column for one step. A header with the
// step's agent, name and state, four tabs, and Copy:
//
// - Terminal: StepTerminal in its large `console` variant, the live screen
//   while the step runs and the saved transcript after. Watch only.
// - Output: StepOutput, the step's structured output or final answer.
// - Params: the step's params from the workflow file, with its agent,
//   provider and call type. The run's own copy of the spec is not served, so
//   this is the current file, and the tab says so.
// - Events: this step's events with runView's plain labels.
//
// Copy puts the current tab's text on the clipboard. Only the selected tab is
// mounted, so the screen is polled only while Terminal shows. RunDetail keys
// the console by step: a running step opens on Terminal, a settled one on
// Output.

import { useEffect, useId, useRef, useState } from 'react'
import { Clock, Copy, CopyCheck } from 'lucide-react'
import { formatDuration, formatLocalTime, eventOutcome } from './runView'
import { flowStateKey, flowStateView } from './WorkflowFlow'
import { SPIN_CLASS } from './stateCues'
import { StepTerminal } from './StepTerminal'
import { StepOutput } from './StepOutput'
import { isReservedCall } from './workflowYaml'
import { eventsText, stepOutputText, stepParamsText, type LabelledEvent, type RailStep } from './runPage'
import { AgentAvatar, CHIP_TONES, ClockSince, copyText } from './runPageParts'

export type ConsoleTab = 'terminal' | 'output' | 'params' | 'events'

const TABS: { id: ConsoleTab; label: string }[] = [
  { id: 'terminal', label: 'Terminal' },
  { id: 'output', label: 'Output' },
  { id: 'params', label: 'Params' },
  { id: 'events', label: 'Events' },
]

/** Whether the workflow spec could be read for the Params tab. */
export type SpecStatus = 'loading' | 'ready' | 'script' | 'unavailable'

export interface StepConsoleProps {
  runId: string
  step: RailStep
  /** The terminal the Terminal tab reads (runView's stepTerminalId). */
  terminalId: string | null
  runEnded: boolean
  specStatus: SpecStatus
  /** This step's events, labelled over the whole run. */
  events: LabelledEvent[]
}

export function StepConsole({ runId, step, terminalId, runEnded, specStatus, events }: StepConsoleProps) {
  const uid = useId()
  const [tab, setTab] = useState<ConsoleTab>(() => (step.activity === 'running' ? 'terminal' : 'output'))
  const tabRefs = useRef<Partial<Record<ConsoleTab, HTMLButtonElement | null>>>({})
  const [terminalText, setTerminalText] = useState<string | null>(null)
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle')

  useEffect(() => {
    if (copied === 'idle') return
    const timer = setTimeout(() => setCopied('idle'), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const select = (next: ConsoleTab, focus: boolean) => {
    setTab(next)
    setCopied('idle')
    if (focus) tabRefs.current[next]?.focus()
  }
  const onTabKey = (e: React.KeyboardEvent) => {
    const i = TABS.findIndex(t => t.id === tab)
    let next: number | null = null
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = TABS.length - 1
    if (next == null) return
    e.preventDefault()
    select(TABS[next].id, true)
  }

  const copyValue =
    tab === 'terminal'
      ? terminalText
      : tab === 'output'
        ? stepOutputText(step.inspection)
        : tab === 'params'
          ? step.spec
            ? stepParamsText(step.spec)
            : null
          : events.length > 0
            ? eventsText(events)
            : null
  const tabLabel = TABS.find(t => t.id === tab)?.label ?? ''
  const doCopy = async () => {
    if (!copyValue) return
    setCopied((await copyText(copyValue)) ? 'copied' : 'failed')
  }

  return (
    <section aria-label={`Step ${step.id}`} className="flex min-h-0 flex-1 flex-col gap-3" data-testid="step-console">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <AgentAvatar agent={step.agent} large />
        <h2 className="truncate text-base font-semibold text-white">{step.id}</h2>
        <ConsoleStateChip step={step} runEnded={runEnded} />
        <div
          role="tablist"
          aria-label={`Step ${step.id}`}
          className="ml-1 inline-flex rounded-[9px] border border-gray-800 bg-[#0f151d] p-[3px]"
        >
          {TABS.map(t => (
            <button
              key={t.id}
              ref={el => {
                tabRefs.current[t.id] = el
              }}
              type="button"
              role="tab"
              id={`${uid}-tab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls={`${uid}-panel-${t.id}`}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => select(t.id, false)}
              onKeyDown={onTabKey}
              className={`inline-flex items-center gap-1.5 rounded-[7px] px-3 py-1 text-[12.5px] font-medium transition-colors focus:outline-hidden focus-visible:ring-2 focus-visible:ring-blue-500 ${
                tab === t.id ? 'bg-[#18212d] text-white' : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              {t.id === 'terminal' && step.activity === 'running' && (
                <span
                  aria-hidden="true"
                  data-testid="console-live-dot"
                  className="size-[7px] rounded-full bg-emerald-400 shadow-[0_0_0_3px_rgba(52,211,153,0.15)]"
                />
              )}
              {t.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={doCopy}
          disabled={!copyValue}
          title={copyValue ? `Copy the ${tabLabel} text` : `Nothing to copy on the ${tabLabel} tab`}
          className="ml-auto inline-flex h-7 items-center gap-1.5 rounded-lg border border-gray-700 bg-[#131b25] px-2.5 text-xs font-medium text-gray-200 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {copied === 'copied' ? <CopyCheck size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          {copied === 'copied' ? 'Copied' : copied === 'failed' ? 'Copy failed' : 'Copy'}
        </button>
        <span aria-live="polite" className="sr-only" data-testid="console-copy-status">
          {copied === 'copied' ? `Copied the ${tabLabel} text` : copied === 'failed' ? 'Copy failed' : ''}
        </span>
      </header>
      <div
        role="tabpanel"
        id={`${uid}-panel-${tab}`}
        aria-labelledby={`${uid}-tab-${tab}`}
        className="flex min-h-0 flex-1 flex-col"
      >
        {tab === 'terminal' && (
          <StepTerminal
            variant="console"
            runId={runId}
            stepId={step.id}
            state={step.state}
            terminalId={terminalId}
            runEnded={runEnded}
            onText={setTerminalText}
          />
        )}
        {tab === 'output' && (
          // The console has room, so the answer is not capped at StepOutput's compact height.
          <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-gray-800 bg-[#0f151d] p-4 [&_pre]:max-h-none">
            <StepOutput step={step.inspection} state={step.state} runEnded={runEnded} />
          </div>
        )}
        {tab === 'params' && <ParamsView step={step} specStatus={specStatus} />}
        {tab === 'events' && <EventsView stepId={step.id} rows={events} />}
      </div>
    </section>
  )
}

function ConsoleStateChip({ step, runEnded }: { step: RailStep; runEnded: boolean }) {
  const key = flowStateKey(step.state)
  const { cue, text } = flowStateView(step.state)
  const running = step.activity === 'running'
  let label: React.ReactNode
  if (running) {
    label = step.since != null ? (
      <>
        {text} · <ClockSince since={step.since} />
      </>
    ) : (
      text
    )
  } else if (step.activity === 'pending') {
    label = runEnded ? 'Not run' : 'Waiting'
  } else if (step.seconds != null && key === 'completed') {
    label = `${text} in ${formatDuration(step.seconds)}`
  } else if (step.seconds != null && key === 'failed') {
    label = `${text} after ${formatDuration(step.seconds)}`
  } else {
    label = text
  }
  return (
    <span
      className={`inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-xs font-semibold ${CHIP_TONES[key]}`}
      data-testid="console-state"
    >
      <cue.Icon size={13} aria-hidden="true" className={running ? SPIN_CLASS : undefined} />
      {label}
    </span>
  )
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-xl border border-gray-800 bg-[#0f151d] p-4 text-xs text-gray-400" role="status">
      <Clock size={14} className="shrink-0 text-gray-500" aria-hidden="true" />
      <span>{children}</span>
    </div>
  )
}

function ParamsView({ step, specStatus }: { step: RailStep; specStatus: SpecStatus }) {
  const spec = step.spec
  if (!spec) {
    if (specStatus === 'loading') return <Note>Reading the workflow file...</Note>
    if (specStatus === 'script') {
      return <Note>This is a Python workflow, so its steps have no params in a workflow file.</Note>
    }
    if (specStatus === 'unavailable') {
      return <Note>The workflow file could not be read, so this step&apos;s params are not shown.</Note>
    }
    return <Note>This step is not in the current workflow file.</Note>
  }
  const call = spec.call ?? 'handoff'
  const params = Object.entries(spec.params ?? {})
  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-gray-800 bg-[#0f151d] p-4" data-testid="step-params">
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-sm">
        <dt className="text-gray-500">Agent</dt>
        <dd className="break-all font-mono text-gray-200">{spec.agent}</dd>
        <dt className="text-gray-500">Provider</dt>
        <dd className="break-all font-mono text-gray-200">{spec.provider}</dd>
        <dt className="text-gray-500">Call type</dt>
        <dd className="font-mono text-gray-200">
          {call}
          {isReservedCall(call) && <span className="ml-2 font-sans text-xs text-amber-300">not built yet</span>}
        </dd>
      </dl>
      <h3 className="mt-5 text-[11px] font-semibold uppercase tracking-wider text-gray-500">Params</h3>
      {params.length === 0 ? (
        <p className="mt-2 text-sm text-gray-400">This step has no params.</p>
      ) : (
        <table className="mt-2 w-full text-sm" aria-label={`Params of step ${step.id}`}>
          <tbody>
            {params.map(([name, value]) => (
              <tr key={name} className="border-t border-gray-800/70 first:border-t-0">
                <th scope="row" className="w-40 py-1.5 pr-3 text-left align-top font-mono font-normal text-gray-400">{name}</th>
                <td className="whitespace-pre-wrap break-all py-1.5 font-mono text-gray-200">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-5 text-xs text-gray-500" data-testid="step-params-source">
        From the current workflow file. This run may have started from an earlier version of it.
      </p>
    </div>
  )
}

function EventsView({ stepId, rows }: { stepId: string; rows: LabelledEvent[] }) {
  if (rows.length === 0) return <Note>No events for this step yet.</Note>
  return (
    <ol
      aria-label={`Events of step ${stepId}`}
      className="min-h-0 flex-1 divide-y divide-gray-800/70 overflow-auto rounded-xl border border-gray-800 bg-[#0f151d]"
    >
      {rows.map(row => {
        const outcome = eventOutcome(row.event)
        const badge = outcome ? flowStateView(outcome) : null
        return (
          <li
            key={row.event.seq}
            className="flex items-center gap-3 px-4 py-2 text-xs"
            title={row.event.event_type}
            data-testid={`console-event-${row.event.seq}`}
          >
            <span className="w-24 shrink-0 font-mono tabular-nums text-gray-500">{formatLocalTime(row.event.ts)}</span>
            <span className="min-w-0 truncate text-gray-200">{row.label}</span>
            {badge && (
              <span className={`ml-auto inline-flex shrink-0 items-center gap-1 ${badge.cue.color}`}>
                <badge.cue.Icon size={12} aria-hidden="true" />
                <span>{badge.text}</span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
