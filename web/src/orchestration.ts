import { TerminalMeta } from './api'

/**
 * The non-technical mental model: a CAO session is a "run", the supervisor
 * terminal is "the Planner", every other terminal is a "Worker". Statuses
 * map to plain language so someone who has never seen tmux can follow along.
 */

export type RunPhase = 'starting' | 'working' | 'needs_you' | 'ready' | 'done' | 'problem'

export interface RunMember {
  terminalId: string
  profile: string
  provider: string
  model: string | null // profile-pinned LLM, or null = provider CLI default
  isPlanner: boolean
  status: string // normalized upper-case terminal status (or 'UNKNOWN')
}

export interface Run {
  runId: string // session name
  planner: RunMember | null
  workers: RunMember[]
  phase: RunPhase
  needsYou: RunMember[]
}

const PLANNER_PROFILES = ['supervisor'] // matched as substring, e.g. code_supervisor

export function isPlannerProfile(profile: string | null): boolean {
  const p = (profile || '').toLowerCase()
  return PLANNER_PROFILES.some(s => p.includes(s))
}

export const PLAIN_STATUS: Record<string, string> = {
  IDLE: 'Ready',
  PROCESSING: 'Working',
  COMPLETED: 'Done',
  WAITING_USER_ANSWER: 'Needs your answer',
  ERROR: 'Hit a problem',
  UNKNOWN: 'Starting',
}

export function plainStatus(status: string | null | undefined): string {
  const normalized = (status || 'UNKNOWN').toUpperCase()
  return PLAIN_STATUS[normalized] || PLAIN_STATUS.UNKNOWN
}

/** One-line narration for a run member, e.g. "Planner is working". */
export function narrate(member: RunMember): string {
  const who = member.isPlanner ? 'The planner' : `Worker ${member.profile}`
  switch (member.status) {
    case 'PROCESSING':
      return `${who} is working…`
    case 'WAITING_USER_ANSWER':
      return `${who} needs your answer`
    case 'COMPLETED':
      return `${who} finished its last task`
    case 'IDLE':
      return `${who} is ready for instructions`
    case 'ERROR':
      return `${who} hit a problem`
    default:
      return `${who} is starting up…`
  }
}

export function deriveRun(
  detail: { name: string; terminals: TerminalMeta[] },
  statuses: Record<string, string>,
): Run {
  const members: RunMember[] = detail.terminals
    .filter(t => (t.agent_profile || '') !== 'memory_manager')
    .map(t => ({
      terminalId: t.id,
      profile: t.agent_profile || t.provider,
      provider: t.provider,
      model: t.model ?? null,
      isPlanner: isPlannerProfile(t.agent_profile),
      status: (statuses[t.id] || 'UNKNOWN').toUpperCase(),
    }))

  const planner = members.find(m => m.isPlanner) || null
  const workers = members.filter(m => !m.isPlanner)
  const needsYou = members.filter(m => m.status === 'WAITING_USER_ANSWER')

  let phase: RunPhase
  const all = members.map(m => m.status)
  if (members.length === 0 || all.every(s => s === 'UNKNOWN')) phase = 'starting'
  else if (all.some(s => s === 'ERROR')) phase = 'problem'
  else if (needsYou.length > 0) phase = 'needs_you'
  else if (all.some(s => s === 'PROCESSING')) phase = 'working'
  else if (all.every(s => s === 'COMPLETED' || s === 'IDLE')) {
    // COMPLETED can be sticky while genuinely idle, so stay modest: the run
    // is "done" only when the planner itself reports COMPLETED.
    phase = planner && planner.status === 'COMPLETED' ? 'done' : 'ready'
  } else phase = 'working'

  return { runId: detail.name, planner, workers, phase, needsYou }
}

export const PHASE_COPY: Record<RunPhase, { label: string; tone: string }> = {
  starting: { label: 'Starting up…', tone: 'text-gray-400' },
  working: { label: 'Working', tone: 'text-blue-400' },
  needs_you: { label: 'Needs you', tone: 'text-amber-400' },
  ready: { label: 'Ready', tone: 'text-emerald-400' },
  done: { label: 'Done', tone: 'text-purple-400' },
  problem: { label: 'Hit a problem', tone: 'text-red-400' },
}
