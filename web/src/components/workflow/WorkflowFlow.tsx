// WorkflowFlow: the flow of a workflow as the CAO engine runs it. An
// "Orchestrator (CAO engine)" node comes first, then one node per step, in
// order. Each edge goes from the orchestrator to its step and is labelled with
// the step number and the call type. A reserved call type (assign,
// send_message) is drawn dashed and labelled "not built yet".
//
// With `states`, each step also shows its state as an icon, a glyph and text,
// so the state never depends on colour alone (stateCues). The builder shows
// the flow without states; RunDetail passes live ones.

import { ChevronRight, Cpu } from 'lucide-react'
import type { RunInspection, WorkflowEvent, WorkflowSpecStep } from '../../api'
import { derivedStepState, type DerivedStepState } from './derivedStepState'
import { stepCue, type StateCue } from './stateCues'
import { isReservedCall, type StepCall } from './workflowYaml'

export interface FlowStep {
  id: string
  /** Leave unset when the call type is not known. The edge then shows only the step number. */
  call?: StepCall | null
  agent?: string
  provider?: string
}

export interface WorkflowFlowProps {
  steps: FlowStep[]
  /** Step id to state. When set, every step shows a state, and a step with no entry shows Pending. */
  states?: Record<string, string>
  /** Accessible name of the flow. */
  label?: string
}

interface StateView {
  cue: StateCue
  text: string
}

// Text for each step state the journal writes, plus the two the playback fold
// derives (retried, cancelled). The cue supplies the icon, glyph and colour.
const STATE_VIEWS: Record<string, { cue: DerivedStepState; text: string }> = {
  pending: { cue: 'pending', text: 'Pending' },
  running: { cue: 'running', text: 'Running' },
  completed: { cue: 'completed', text: 'Completed' },
  completed_unvalidated: { cue: 'completed', text: 'Completed, output not validated' },
  failed: { cue: 'failed', text: 'Failed' },
  skipped: { cue: 'skipped', text: 'Skipped' },
  rerun_authorized: { cue: 'retried', text: 'Rerun authorized' },
  replay_authorized: { cue: 'skipped', text: 'Replay authorized' },
  retried: { cue: 'retried', text: 'Retried' },
  cancelled: { cue: 'cancelled', text: 'Cancelled' },
}

/** How a step state reads in the flow. An unknown state shows as its raw text. */
export function flowStateView(state: string | undefined): StateView {
  const key = (state || 'pending').toLowerCase()
  const view = STATE_VIEWS[key]
  return view ? { cue: stepCue(view.cue), text: view.text } : { cue: stepCue('pending'), text: state as string }
}

const NODE_BORDER: Partial<Record<DerivedStepState, string>> = {
  running: 'border-blue-700/70',
  retried: 'border-amber-700/70',
  completed: 'border-emerald-800/70',
  failed: 'border-red-700/70',
  cancelled: 'border-orange-700/70',
}

export function WorkflowFlow({ steps, states, label = 'Workflow flow' }: WorkflowFlowProps) {
  return (
    <div role="group" aria-label={label} className="text-xs" data-testid="workflow-flow">
      <div
        className="inline-flex items-center gap-2 rounded-lg border border-violet-700/60 bg-violet-950/30 px-3 py-2 text-violet-200"
        data-testid="flow-orchestrator"
      >
        <Cpu size={14} aria-hidden="true" />
        <span className="font-medium">Orchestrator (CAO engine)</span>
      </div>
      {steps.length === 0 ? (
        <p className="mt-2 text-gray-500">No steps yet.</p>
      ) : (
        <>
          <span aria-hidden="true" className="ml-4 block h-2 border-l-2 border-gray-600" />
          <ol aria-label="Steps called by the orchestrator">
            {steps.map((step, i) => (
              <FlowRow
                key={`${i}:${step.id}`}
                step={step}
                number={i + 1}
                last={i === steps.length - 1}
                state={states ? flowStateView(states[step.id]) : null}
                stateKey={states ? (states[step.id] || 'pending').toLowerCase() : null}
              />
            ))}
          </ol>
        </>
      )}
    </div>
  )
}

interface FlowRowProps {
  step: FlowStep
  number: number
  last: boolean
  state: StateView | null
  stateKey: string | null
}

function FlowRow({ step, number, last, state, stateKey }: FlowRowProps) {
  const reserved = isReservedCall(step.call)
  const edgeColor = reserved ? 'border-amber-500/80' : 'border-gray-500'
  const derived = stateKey ? STATE_VIEWS[stateKey]?.cue : undefined
  const border = (derived && NODE_BORDER[derived]) || 'border-gray-700'
  return (
    <li className="relative flex items-center pl-4" data-testid={`flow-step-${number}`}>
      {/* The trunk down from the orchestrator. It stops at the last step's edge. */}
      <span
        aria-hidden="true"
        className={`absolute left-4 top-0 border-l-2 border-gray-600 ${last ? 'h-1/2' : 'h-full'}`}
      />
      <div
        className="relative flex w-44 shrink-0 items-center py-5"
        data-testid={`flow-edge-${number}`}
        data-call={step.call ?? ''}
        data-reserved={reserved ? 'true' : 'false'}
      >
        <span
          aria-hidden="true"
          data-testid={`flow-edge-line-${number}`}
          className={`flex-1 border-t-2 ${reserved ? 'border-dashed' : 'border-solid'} ${edgeColor}`}
        />
        <ChevronRight size={14} aria-hidden="true" className={`-ml-1.5 shrink-0 ${reserved ? 'text-amber-500/80' : 'text-gray-500'}`} />
        <span className="absolute left-3 top-0.5 whitespace-nowrap text-[11px] text-gray-300">
          <span className="sr-only">Orchestrator to step </span>
          <span>{step.call ? `${number} · ${step.call}` : `${number}`}</span>
        </span>
        {reserved && (
          <span className="absolute bottom-0.5 left-3 whitespace-nowrap text-[11px] text-amber-300">not built yet</span>
        )}
      </div>
      <div className={`my-1.5 min-w-0 flex-1 rounded-lg border ${border} bg-gray-900/60 px-3 py-2`}>
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-gray-100" title={step.id}>{step.id || '(no id)'}</span>
          {state && (
            <span className={`ml-auto inline-flex shrink-0 items-center gap-1 ${state.cue.color}`} data-testid={`flow-state-${number}`}>
              <state.cue.Icon size={12} aria-hidden="true" />
              <span aria-hidden="true">{state.cue.glyph}</span>
              <span>{state.text}</span>
            </span>
          )}
        </div>
        {(step.agent || step.provider) && (
          <p className="mt-0.5 truncate text-gray-500" title={[step.agent, step.provider].filter(Boolean).join(' on ')}>
            {step.agent || 'no agent'}
            {step.provider ? ` on ${step.provider}` : ''}
          </p>
        )}
      </div>
    </li>
  )
}

// Run view helpers, used by RunDetail.

/**
 * The steps to draw for a run: the run's own steps in journal order, with the
 * call type, agent and provider looked up in the spec by step id. Without a
 * spec there is no call label. A run that has no step rows yet falls back to
 * the spec's steps.
 */
export function runFlowSteps(run: RunInspection, specSteps: WorkflowSpecStep[] | null): FlowStep[] {
  const byId = new Map((specSteps ?? []).map(s => [s.id, s]))
  const ids = run.steps.length > 0 ? run.steps.map(s => s.id) : (specSteps ?? []).map(s => s.id)
  return ids.map(id => {
    const spec = byId.get(id)
    if (!spec) return { id }
    return { id, call: spec.call ?? 'handoff', agent: spec.agent, provider: spec.provider }
  })
}

// Journal states that only the inspection knows, or that are settled. The
// event fold cannot see them: it collapses completed_unvalidated into
// completed and has no event for a human's rerun or replay decision.
const INSPECTION_WINS = new Set([
  'completed',
  'completed_unvalidated',
  'failed',
  'skipped',
  'rerun_authorized',
  'replay_authorized',
])

/**
 * Live step states for a run, from the two sources RunDetail already follows:
 * the polled inspection (`run.steps`) and the followed event stream. While a
 * step is pending or running in the inspection, the event fold is at least as
 * fresh, so it wins. Once the inspection says a step settled, or records a
 * rerun or replay decision, the inspection wins.
 */
export function runStepStates(run: RunInspection, events: WorkflowEvent[]): Record<string, string> {
  const folded = derivedStepState(events, events.length - 1)
  const out: Record<string, string> = {}
  for (const step of run.steps) {
    const inspected = step.state
    const fold = folded[step.id]
    out[step.id] = fold && !INSPECTION_WINS.has(inspected) ? fold : inspected
  }
  for (const [id, state] of Object.entries(folded)) {
    if (!(id in out)) out[id] = state
  }
  return out
}
