// runPage: pure helpers for the run page and the Workflows home list. Like
// runView, they read a run's inspection, its events and its spec, and never
// fetch, so the rules live in one tested place.
//
// Events are read in `seq` order (runView's bySeq). Their `ts` is used only for
// clocks and durations.

import type { RunInspection, StepInspection, WorkflowEvent, WorkflowSpecStep } from '../../api'
import { bySeq, eventLabels, eventTimeMs, formatLocalTime, stepActivity, type StepActivity } from './runView'
import { structuredOutputText } from './StepOutput'

/**
 * A deliberate cross-language COPY of `WORKFLOW_DEFAULT_STEP_RETRIES` in
 * constants.py: the extra attempts a step gets when its spec sets no `retries`.
 * test/services/test_web_step_retries_drift.py fails when the two differ, so
 * "Attempt 1 of 4" never counts wrong.
 */
export const DEFAULT_STEP_RETRIES = 3

/** What the run page shows in its console: one step, or the run's Result. */
export type RunPick = { kind: 'step'; id: string } | { kind: 'result' }

/** The short form of a run id shown in lists, such as `run-7f3c91a2`. */
export function shortRunId(runId: string): string {
  return runId.slice(0, 12)
}

/** Two letters for an agent's avatar: the first letters of its first two words, such as DQ for dynamodb-query-agent. */
export function agentInitials(agent: string | null | undefined): string {
  const words = (agent ?? '').split(/[^A-Za-z0-9]+/).filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return (words[0][0] + words[1][0]).toUpperCase()
}

// Avatar colours. Full class names, so Tailwind generates them.
const AGENT_TONES = [
  'bg-sky-500/15 text-sky-300',
  'bg-orange-500/15 text-orange-300',
  'bg-violet-500/15 text-violet-300',
  'bg-emerald-500/15 text-emerald-300',
  'bg-rose-500/15 text-rose-300',
  'bg-amber-500/15 text-amber-300',
  'bg-teal-500/15 text-teal-300',
  'bg-indigo-500/15 text-indigo-300',
] as const

/** The avatar colour classes for an agent, the same for the same name every time. */
export function agentTone(agent: string | null | undefined): string {
  let hash = 0
  for (const ch of agent ?? '') hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return AGENT_TONES[hash % AGENT_TONES.length]
}

/** A duration in whole seconds as a clock, such as 0:48 or 1:02:05, for a step that is running. */
export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/**
 * When a run started, as a local time, with the date in front when it was not
 * today, such as "10:35:16" or "Nov 10, 10:35:16". '' when unreadable.
 */
export function formatRunStart(ts: string | null | undefined, nowMs: number = Date.now(), seconds = true): string {
  const ms = eventTimeMs(ts)
  if (ms == null) return ''
  const at = new Date(ms)
  const time = at.toLocaleTimeString([], seconds
    ? { hour: '2-digit', minute: '2-digit', second: '2-digit' }
    : { hour: '2-digit', minute: '2-digit' })
  if (at.toDateString() === new Date(nowMs).toDateString()) return time
  return `${at.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`
}

/** Whole seconds from a run's start to its end, or null when either is unknown or out of order. */
export function runSeconds(startedAt: string | null | undefined, finishedAt: string | null | undefined): number | null {
  const start = eventTimeMs(startedAt)
  const end = eventTimeMs(finishedAt)
  if (start == null || end == null || end < start) return null
  return Math.round((end - start) / 1000)
}

export interface StepProgress {
  /** The position of the furthest step the run reached, 1-based; 0 when none has started. */
  reached: number
  total: number
}

/**
 * How far a run got, as in "2 of 3": the furthest step, in run order, that
 * started. A pending step has not started. A skipped step counts only when it
 * had an attempt, as a step that was running when the run was cancelled does;
 * a step skipped because an earlier one halted the run never started.
 */
export function stepProgress(steps: { state: string; attempts?: number | null }[]): StepProgress {
  let reached = 0
  steps.forEach((step, i) => {
    const key = (step.state || 'pending').toLowerCase()
    if (key === 'pending') return
    if (key === 'skipped' && !(step.attempts && step.attempts > 0)) return
    reached = i + 1
  })
  return { reached, total: steps.length }
}

const OUTCOMES = new Set(['step.completed', 'step.failed', 'step.skipped'])

/**
 * Each settled step's duration in whole seconds, from its latest `step.started`
 * to the outcome event after it. A step with no recorded start (a script run
 * records no events) has none.
 */
export function stepDurations(events: WorkflowEvent[]): Record<string, number> {
  const starts: Record<string, number | null> = {}
  const out: Record<string, number> = {}
  for (const ev of bySeq(events)) {
    if (!ev.step_id) continue
    if (ev.event_type === 'step.started') {
      starts[ev.step_id] = eventTimeMs(ev.ts)
      delete out[ev.step_id]
    } else if (OUTCOMES.has(ev.event_type)) {
      const start = starts[ev.step_id]
      const end = eventTimeMs(ev.ts)
      if (start != null && end != null && end >= start) out[ev.step_id] = Math.round((end - start) / 1000)
    }
  }
  return out
}

/**
 * The attempt a step is on: the latest `step.attempt.started` after its latest
 * `step.started`, else the inspection's attempt count, and at least 1.
 */
export function currentAttempt(events: WorkflowEvent[], stepId: string, inspected?: number | null): number {
  let attempt: number | null = null
  for (const ev of bySeq(events)) {
    if (ev.step_id !== stepId) continue
    if (ev.event_type === 'step.started') attempt = null
    else if (ev.event_type === 'step.attempt.started' && ev.attempt != null) attempt = ev.attempt
  }
  return Math.max(1, attempt ?? inspected ?? 1)
}

/** How many attempts a step gets in all: its `retries` plus one, with the engine's default when unset. */
export function totalAttempts(spec: WorkflowSpecStep | null | undefined): number | null {
  if (!spec) return null
  const retries = typeof spec.retries === 'number' ? spec.retries : DEFAULT_STEP_RETRIES
  return retries + 1
}

/** Why the latest `step.skipped` event of a step says it was skipped, or null. */
export function skipReason(events: WorkflowEvent[], stepId: string): string | null {
  let reason: string | null = null
  for (const ev of bySeq(events)) {
    if (ev.step_id !== stepId) continue
    if (ev.event_type === 'step.started') reason = null
    else if (ev.event_type === 'step.skipped') reason = ev.reason ?? null
  }
  return reason
}

/** The agent and provider the latest event of a step names, for a step the spec does not describe. */
export function eventAgent(events: WorkflowEvent[], stepId: string): { agent: string | null; provider: string | null } {
  let agent: string | null = null
  let provider: string | null = null
  for (const ev of bySeq(events)) {
    if (ev.step_id !== stepId) continue
    if (ev.agent_profile) agent = ev.agent_profile
    if (ev.provider) provider = ev.provider
  }
  return { agent, provider }
}

/** The first line of a text that is not blank, trimmed, or null. */
export function firstLine(text: string | null | undefined): string | null {
  for (const line of (text ?? '').split('\n')) {
    const trimmed = line.trim()
    if (trimmed) return trimmed
  }
  return null
}

/** A structured output on one line, for a preview. Not valid JSON: as stored. */
function compactJson(outputJson: string): string {
  try {
    return JSON.stringify(JSON.parse(outputJson))
  } catch {
    return outputJson
  }
}

/**
 * The first two lines of what a step answered, for its Answer box: its final
 * answer (`result.text`), or else its structured output. Blank lines are left
 * out. Null when it answered nothing.
 */
export function answerPreview(step: StepInspection | null | undefined): string | null {
  const text = step?.result?.text
  const source = typeof text === 'string' && text.trim() !== '' ? text : step?.output_json ? compactJson(step.output_json) : null
  if (!source) return null
  const lines = source.split('\n').filter(line => line.trim() !== '')
  return lines.slice(0, 2).map(line => line.trimEnd()).join('\n') || null
}

/** True when a step saved an answer: a final answer with text, or a structured output. */
export function hasAnswer(step: StepInspection | null | undefined): boolean {
  return answerPreview(step) != null
}

/**
 * The text the Output tab shows, for Copy. The same rule as StepOutput: the
 * structured output, pretty-printed, when there is one, else the final answer.
 */
export function stepOutputText(step: StepInspection | null | undefined): string | null {
  if (step?.output_json) return structuredOutputText(step.output_json)
  const text = step?.result?.text
  return typeof text === 'string' && text !== '' ? text : null
}

/** A step's params, agent, provider and call type as plain lines, for Copy. */
export function stepParamsText(spec: WorkflowSpecStep): string {
  const lines = [`agent: ${spec.agent}`, `provider: ${spec.provider}`, `call: ${spec.call ?? 'handoff'}`]
  const params = Object.entries(spec.params ?? {})
  if (params.length > 0) {
    lines.push('params:')
    for (const [key, value] of params) lines.push(`  ${key}: ${value}`)
  }
  return lines.join('\n')
}

export interface LabelledEvent {
  /** The event's index in the run's events, the timeline's selection index. */
  index: number
  event: WorkflowEvent
  label: string
}

/**
 * The run's events with runView's plain labels, in seq order. The labels are
 * worked out over the whole run, so a step's duration finds its start.
 */
export function labelledEvents(events: WorkflowEvent[], runStartedAt?: string | null): LabelledEvent[] {
  const labels = eventLabels(events, runStartedAt)
  return events.map((event, index) => ({ index, event, label: labels[index] }))
}

/** One step's events as "time  label" lines, for Copy. */
export function eventsText(rows: LabelledEvent[]): string {
  return rows.map(row => `${formatLocalTime(row.event.ts) || '--:--:--'}  ${row.label}`).join('\n')
}

/** The live state of a step as runView reads it, lower case, pending when unknown. */
function stateKey(state: string | null | undefined): string {
  return (state || 'pending').toLowerCase()
}

/**
 * The step to follow while the run is live: the one running now. Between two
 * steps, the one that started last, so the console does not jump back. A run
 * with no events (a script run) falls back to the last step that left pending.
 */
export function followTarget(
  stepIds: string[],
  running: Record<string, number | null>,
  events: WorkflowEvent[],
  states: Record<string, string>,
): string | null {
  const now = stepIds.find(id => id in running)
  if (now) return now
  let started: string | null = null
  for (const ev of bySeq(events)) {
    if (ev.event_type === 'step.started' && ev.step_id && stepIds.includes(ev.step_id)) started = ev.step_id
  }
  if (started) return started
  for (let i = stepIds.length - 1; i >= 0; i--) {
    if (stateKey(states[stepIds[i]]) !== 'pending') return stepIds[i]
  }
  return stepIds[0] ?? null
}

/**
 * The step a run that has ended opens on: the step that failed, else the last
 * step with an answer, else the last step that started, else the first.
 */
export function finishedTarget(run: RunInspection, stepIds: string[], states: Record<string, string>): string | null {
  const failed = stepIds.find(id => stateKey(states[id]) === 'failed')
  if (failed) return failed
  const byId = new Map(run.steps.map(step => [step.id, step]))
  for (let i = stepIds.length - 1; i >= 0; i--) {
    if (hasAnswer(byId.get(stepIds[i]))) return stepIds[i]
  }
  for (let i = stepIds.length - 1; i >= 0; i--) {
    if (stateKey(states[stepIds[i]]) !== 'pending') return stepIds[i]
  }
  return stepIds[0] ?? null
}

/** One step as the rail and the console show it. */
export interface RailStep {
  id: string
  /** 1-based position in run order. */
  number: number
  /** The live state, as runStepStates gives it, lower case. */
  state: string
  activity: StepActivity
  inspection: StepInspection | null
  spec: WorkflowSpecStep | null
  agent: string | null
  provider: string | null
  /** When the step's latest start was recorded (ms since the epoch), while it runs. */
  since: number | null
  /** A settled step's duration in whole seconds, when its start and outcome were recorded. */
  seconds: number | null
  attempt: number
  /** Attempts in all, from the spec; null when the spec is not known. */
  attemptsTotal: number | null
  skipReason: string | null
  /** The step before this one in run order, for "Starts when ... finishes". */
  previousId: string | null
}

export interface RailInput {
  run: RunInspection
  /** The step ids in run order (runFlowSteps). */
  stepIds: string[]
  states: Record<string, string>
  running: Record<string, number | null>
  events: WorkflowEvent[]
  specSteps: WorkflowSpecStep[] | null
  runEnded: boolean
}

/** Everything the rail and the console need about each step, in run order. */
export function railSteps({ run, stepIds, states, running, events, specSteps, runEnded }: RailInput): RailStep[] {
  const inspected = new Map(run.steps.map(step => [step.id, step]))
  const specs = new Map((specSteps ?? []).map(step => [step.id, step]))
  const durations = stepDurations(events)
  return stepIds.map((id, i) => {
    const inspection = inspected.get(id) ?? null
    const spec = specs.get(id) ?? null
    const state = stateKey(states[id] ?? inspection?.state)
    const fromEvents = spec ? null : eventAgent(events, id)
    return {
      id,
      number: i + 1,
      state,
      activity: stepActivity(state, runEnded),
      inspection,
      spec,
      agent: spec?.agent || fromEvents?.agent || null,
      provider: spec?.provider || fromEvents?.provider || null,
      since: running[id] ?? null,
      seconds: durations[id] ?? null,
      attempt: currentAttempt(events, id, inspection?.attempts),
      attemptsTotal: totalAttempts(spec),
      skipReason: skipReason(events, id),
      previousId: i > 0 ? stepIds[i - 1] : null,
    }
  })
}
