// StepRail: the run page's left column. A vertical stepper of the run's steps
// in run order. Its line is green below a finished step, blue below the
// running one and grey after. Each step shows a state dot, its name, a
// duration chip and its agent, and one line about where it stands: the start
// of its answer, its attempt, what it waits for, or why it failed.
//
// The running step's dot spins and pulses, both only under motion-safe
// (stateCues). The selected step has a blue border and glow. Once the run has
// ended, a last node, Result, opens the run-level panels in the console. Under
// the steps are the run's facts: when it started, how long it ran, its id and
// its tier.
//
// Every state has its own icon and a chip with text, and each step's button
// names its state for a screen reader, so colour is never the only cue.

import { Fragment } from 'react'
import { Ban, Check, Flag, Loader2, SkipForward, X } from 'lucide-react'
import { Elapsed, PULSE_CLASS, SPIN_CLASS } from './stateCues'
import type { DerivedStepState } from './derivedStepState'
import { flowStateKey, flowStateView } from './WorkflowFlow'
import { formatDuration, eventTimeMs } from './runView'
import { answerPreview, firstLine, formatRunStart, type RailStep, type RunPick, type StepProgress } from './runPage'
import { AgentAvatar, CHIP_TONES, ClockSince } from './runPageParts'

const LINE: Record<DerivedStepState, string> = {
  completed: 'bg-emerald-500/70',
  running: 'bg-blue-500/80',
  retried: 'bg-amber-500/70',
  failed: 'bg-red-500/60',
  skipped: 'bg-gray-700',
  cancelled: 'bg-gray-700',
  pending: 'bg-gray-700',
}

const SKIP_REASONS: Record<string, string> = {
  cancelled: 'Stopped when the run was cancelled.',
  upstream_halt: 'Skipped because an earlier step stopped the run.',
}

export interface StepRailProps {
  steps: RailStep[]
  /** What the console shows now. */
  selected: RunPick | null
  onSelectStep: (stepId: string) => void
  onSelectResult: () => void
  runEnded: boolean
  /** The run's state, from the polled list when it is fresher than the snapshot. */
  runState: string
  /** How long the run ran, once it ended and both times are known. */
  runSeconds: number | null
  progress: StepProgress
  startedAt: string
  runId: string
  tier: string
}

export function StepRail({
  steps,
  selected,
  onSelectStep,
  onSelectResult,
  runEnded,
  runState,
  runSeconds,
  progress,
  startedAt,
  runId,
  tier,
}: StepRailProps) {
  const startMs = eventTimeMs(startedAt)
  return (
    <aside aria-label="Steps" className="flex min-h-0 flex-col border-r border-gray-800 bg-[#0c1118]">
      <h3
        className="shrink-0 px-5 pb-2 pt-3.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500"
        data-testid="rail-progress"
      >
        Steps · {progress.reached} of {progress.total}
      </h3>
      {steps.length === 0 ? (
        <p className="px-5 text-xs text-gray-500">No steps recorded for this run.</p>
      ) : (
        <ol aria-label="Steps of this run" className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 pb-3 pt-1">
          {steps.map((step, i) => (
            <RailItem
              key={step.id}
              step={step}
              selected={selected?.kind === 'step' && selected.id === step.id}
              onSelect={onSelectStep}
              runEnded={runEnded}
              connector={i < steps.length - 1 || runEnded}
            />
          ))}
          {runEnded && (
            <ResultItem
              state={runState}
              seconds={runSeconds}
              selected={selected?.kind === 'result'}
              onSelect={onSelectResult}
            />
          )}
        </ol>
      )}
      <dl
        aria-label="Run facts"
        className="grid shrink-0 grid-cols-[5.5rem_minmax(0,1fr)] gap-x-2 gap-y-1.5 border-t border-gray-800 px-5 py-3 text-xs"
        data-testid="run-facts"
      >
        <dt className="text-gray-500">Started</dt>
        <dd className="tabular-nums text-gray-200">{formatRunStart(startedAt) || 'Unknown'}</dd>
        <dt className="text-gray-500">Duration</dt>
        <dd className="tabular-nums text-gray-200">
          {!runEnded && startMs != null ? <Elapsed since={startMs} /> : runSeconds != null ? formatDuration(runSeconds) : 'Unknown'}
        </dd>
        <dt className="text-gray-500">Run id</dt>
        <dd className="break-all font-mono text-gray-200">{runId}</dd>
        <dt className="text-gray-500">Tier</dt>
        <dd className="text-gray-200">{tier}</dd>
      </dl>
    </aside>
  )
}

interface RailItemProps {
  step: RailStep
  selected: boolean
  onSelect: (stepId: string) => void
  runEnded: boolean
  /** Draw the line down to the next node. */
  connector: boolean
}

function RailItem({ step, selected, onSelect, runEnded, connector }: RailItemProps) {
  const key = flowStateKey(step.state)
  const pending = step.activity === 'pending'
  const view = flowStateView(step.state)
  const srState = pending ? (runEnded ? 'did not run' : 'waiting') : view.cue.sr
  return (
    <li className="relative" data-testid={`rail-step-${step.number}`} data-state={step.state}>
      {connector && (
        <span
          aria-hidden="true"
          data-testid={`rail-line-${step.number}`}
          className={`absolute -bottom-9 left-[27px] top-7 w-0.5 ${LINE[key]}`}
        />
      )}
      <button
        type="button"
        onClick={() => onSelect(step.id)}
        aria-current={selected ? 'step' : undefined}
        className={`relative flex w-full gap-3 rounded-xl p-2.5 pr-3 text-left transition-colors focus:outline-hidden focus-visible:ring-2 focus-visible:ring-blue-500 ${
          selected
            ? 'bg-[#0f1828] shadow-[inset_0_0_0_1px_rgba(59,130,246,0.6),0_0_16px_rgba(59,130,246,0.2)]'
            : 'hover:bg-gray-800/40'
        }`}
      >
        <StepDot step={step} />
        <span className="block min-w-0 flex-1 pt-0.5">
          <span className="flex items-center gap-2">
            <span className={`truncate text-[14.5px] font-semibold ${pending ? 'text-gray-400' : 'text-gray-100'}`}>{step.id}</span>
            <DurationChip step={step} runEnded={runEnded} />
          </span>
          {step.agent && (
            <span className="mt-1.5 flex min-w-0 items-center gap-2 text-xs text-gray-400">
              <AgentAvatar agent={step.agent} />
              <span className="truncate">{step.agent}</span>
              {step.provider && <span className="shrink-0 text-gray-500">· {step.provider}</span>}
            </span>
          )}
          <StepDetail step={step} runEnded={runEnded} />
          <span className="sr-only">, {srState}</span>
        </span>
      </button>
    </li>
  )
}

const DOT = 'relative z-10 grid size-9 shrink-0 place-items-center rounded-full border-2'

function StepDot({ step }: { step: RailStep }) {
  const key = flowStateKey(step.state)
  const moving = step.activity === 'running'
  switch (key) {
    case 'completed':
      return (
        <span className={`${DOT} border-emerald-500 bg-[#07140d] text-emerald-400`} data-testid={`rail-dot-${step.number}`}>
          <Check size={16} strokeWidth={2.6} aria-hidden="true" />
        </span>
      )
    case 'running':
    case 'retried': {
      const blue = key === 'running'
      return (
        <span
          className={`${DOT} ${blue ? 'border-blue-500 bg-[#0a1322] text-blue-400' : 'border-amber-500 bg-[#1a1306] text-amber-400'}`}
          data-testid={`rail-dot-${step.number}`}
        >
          {/* The halo pulses; the dot itself keeps its full contrast. */}
          {moving && (
            <span
              aria-hidden="true"
              data-testid={`rail-pulse-${step.number}`}
              className={`pointer-events-none absolute -inset-1 rounded-full border-2 ${blue ? 'border-blue-500/50' : 'border-amber-500/50'} ${PULSE_CLASS}`}
            />
          )}
          <Loader2 size={16} aria-hidden="true" className={moving ? SPIN_CLASS : undefined} />
        </span>
      )
    }
    case 'failed':
      return (
        <span className={`${DOT} border-red-500 bg-red-950/50 text-red-400`} data-testid={`rail-dot-${step.number}`}>
          <X size={16} strokeWidth={2.6} aria-hidden="true" />
        </span>
      )
    case 'skipped':
      return (
        <span className={`${DOT} border-gray-600 bg-[#0a0d12] text-gray-500`} data-testid={`rail-dot-${step.number}`}>
          <SkipForward size={14} aria-hidden="true" />
        </span>
      )
    case 'cancelled':
      return (
        <span className={`${DOT} border-orange-500 bg-[#170e06] text-orange-400`} data-testid={`rail-dot-${step.number}`}>
          <Ban size={15} aria-hidden="true" />
        </span>
      )
    default:
      return (
        <span
          className={`${DOT} border-dashed border-gray-600 bg-[#0a0d12] text-[11px] font-semibold text-gray-500`}
          data-testid={`rail-dot-${step.number}`}
        >
          <span aria-hidden="true">{step.number}</span>
        </span>
      )
  }
}

function DurationChip({ step, runEnded }: { step: RailStep; runEnded: boolean }) {
  const key = flowStateKey(step.state)
  const text = flowStateView(step.state).text
  let content: React.ReactNode
  if (step.activity === 'running') content = step.since != null ? <ClockSince since={step.since} /> : text
  else if (step.activity === 'pending') content = runEnded ? 'Not run' : 'Waiting'
  else if (step.seconds != null && (key === 'completed' || key === 'failed')) content = formatDuration(step.seconds)
  else content = text
  return (
    <span
      className={`ml-auto inline-flex h-[21px] shrink-0 items-center rounded-full px-2 text-[11.5px] font-semibold ${CHIP_TONES[key]}`}
      data-testid={`rail-chip-${step.number}`}
    >
      {content}
    </span>
  )
}

function StepDetail({ step, runEnded }: { step: RailStep; runEnded: boolean }) {
  const key = flowStateKey(step.state)
  const line = (children: React.ReactNode, tone = 'text-gray-500') => (
    <span className={`mt-1.5 block text-xs ${tone}`} data-testid={`rail-detail-${step.number}`}>
      {children}
    </span>
  )

  if (step.activity === 'running') {
    const params = Object.keys(step.spec?.params ?? {})
    return line(
      <>
        {step.attemptsTotal ? `Attempt ${step.attempt} of ${step.attemptsTotal}` : `Attempt ${step.attempt}`}
        {params.length > 0 && (
          <>
            {' · params: '}
            {params.map((name, i) => (
              <Fragment key={name}>
                {i > 0 && ', '}
                <span className="font-mono">{name}</span>
              </Fragment>
            ))}
          </>
        )}
      </>,
    )
  }
  if (step.activity === 'pending') {
    if (runEnded) return line('The run ended before this step started.')
    return line(step.previousId ? `Starts when ${step.previousId} finishes.` : 'Waiting to start.')
  }
  if (key === 'failed') {
    const kind = step.inspection?.error_kind
    const first = firstLine(step.inspection?.error)
    return line(
      kind || first ? (
        <>
          {kind && <span className="font-mono">{kind}</span>}
          {kind && first && ' · '}
          {first}
        </>
      ) : (
        'The step failed.'
      ),
      'text-red-300/90',
    )
  }
  if (key === 'skipped') {
    if (!step.skipReason) return null
    return line(SKIP_REASONS[step.skipReason] ?? `Skipped: ${step.skipReason}`)
  }
  if (key === 'cancelled') return line('Stopped when the run was cancelled.')
  if (key === 'completed') {
    const preview = answerPreview(step.inspection)
    if (!preview) return line('No answer was recorded')
    return (
      <span
        className="mt-2 block rounded-lg border border-gray-800 bg-[#0f151d] px-2.5 py-1.5 text-xs leading-normal text-gray-300"
        data-testid={`rail-answer-${step.number}`}
      >
        <span className="mb-0.5 block text-[10.5px] font-semibold uppercase tracking-wider text-gray-500">Answer</span>
        <span className="line-clamp-2 whitespace-pre-line wrap-break-word">{preview}</span>
      </span>
    )
  }
  return null
}

function ResultItem({
  state,
  seconds,
  selected,
  onSelect,
}: {
  state: string
  seconds: number | null
  selected: boolean
  onSelect: () => void
}) {
  const key = flowStateKey(state)
  const tone =
    key === 'completed'
      ? 'border-emerald-500 bg-[#07140d] text-emerald-400'
      : key === 'failed'
        ? 'border-red-500 bg-red-950/50 text-red-400'
        : key === 'cancelled'
          ? 'border-orange-500 bg-[#170e06] text-orange-400'
          : 'border-gray-600 bg-[#0a0d12] text-gray-400'
  const chip = key === 'completed' && seconds != null ? `Completed in ${formatDuration(seconds)}` : flowStateView(state).text
  return (
    <li className="relative" data-testid="rail-result">
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? 'step' : undefined}
        className={`relative flex w-full gap-3 rounded-xl p-2.5 pr-3 text-left transition-colors focus:outline-hidden focus-visible:ring-2 focus-visible:ring-blue-500 ${
          selected
            ? 'bg-[#0f1828] shadow-[inset_0_0_0_1px_rgba(59,130,246,0.6),0_0_16px_rgba(59,130,246,0.2)]'
            : 'hover:bg-gray-800/40'
        }`}
      >
        <span className={`${DOT} ${tone}`}>
          <Flag size={15} aria-hidden="true" />
        </span>
        <span className="block min-w-0 flex-1 pt-0.5">
          <span className="flex items-center gap-2">
            <span className="text-[14.5px] font-semibold text-gray-100">Result</span>
            <span
              className={`ml-auto inline-flex h-[21px] shrink-0 items-center rounded-full px-2 text-[11.5px] font-semibold ${CHIP_TONES[key]}`}
              data-testid="rail-result-chip"
            >
              {chip}
            </span>
          </span>
          <span className="mt-1.5 block text-xs text-gray-500">Report, artifacts and run comparison.</span>
        </span>
      </button>
    </li>
  )
}
