import { describe, it, expect } from 'vitest'
import {
  deriveRun,
  isPlannerProfile,
  plainStatus,
  narrate,
  type RunMember,
} from '../orchestration'
import type { TerminalMeta } from '../api'

function term(id: string, agent_profile: string | null, provider = 'claude_code'): TerminalMeta {
  return {
    id,
    tmux_session: 'cao-run',
    tmux_window: id,
    provider,
    agent_profile,
    model: null,
    created_at: null,
    last_active: null,
  }
}

const run = (terminals: TerminalMeta[], statuses: Record<string, string>) =>
  deriveRun({ name: 'cao-run', terminals }, statuses)

describe('isPlannerProfile', () => {
  it('matches supervisor as a substring, case-insensitively', () => {
    expect(isPlannerProfile('code_supervisor')).toBe(true)
    expect(isPlannerProfile('SUPERVISOR')).toBe(true)
  })
  it('is false for workers and null', () => {
    expect(isPlannerProfile('developer')).toBe(false)
    expect(isPlannerProfile(null)).toBe(false)
  })
})

describe('plainStatus', () => {
  it('maps known statuses to plain language', () => {
    expect(plainStatus('IDLE')).toBe('Ready')
    expect(plainStatus('processing')).toBe('Working')
    expect(plainStatus('WAITING_USER_ANSWER')).toBe('Needs your answer')
    expect(plainStatus('ERROR')).toBe('Hit a problem')
  })
  it('falls back to Starting for null/unknown', () => {
    expect(plainStatus(null)).toBe('Starting')
    expect(plainStatus('SOMETHING_NEW')).toBe('Starting')
  })
})

describe('narrate', () => {
  const m = (over: Partial<RunMember>): RunMember => ({
    terminalId: 't',
    profile: 'developer',
    provider: 'claude_code',
    model: null,
    isPlanner: false,
    status: 'IDLE',
    ...over,
  })
  it('names the planner and workers distinctly', () => {
    expect(narrate(m({ isPlanner: true, status: 'PROCESSING' }))).toBe('The planner is working…')
    expect(narrate(m({ profile: 'developer', status: 'WAITING_USER_ANSWER' }))).toBe(
      'Worker developer needs your answer'
    )
  })
})

describe('deriveRun phase mapping', () => {
  const planner = term('p1', 'code_supervisor')
  const worker = term('w1', 'developer')

  it('is "starting" with no terminals or all-UNKNOWN', () => {
    expect(run([], {}).phase).toBe('starting')
    expect(run([planner, worker], {}).phase).toBe('starting') // no statuses -> UNKNOWN
  })

  it('is "problem" if any member errored (over other signals)', () => {
    expect(run([planner, worker], { p1: 'PROCESSING', w1: 'ERROR' }).phase).toBe('problem')
  })

  it('is "needs_you" when a member waits on the user, and lists them', () => {
    const r = run([planner, worker], { p1: 'PROCESSING', w1: 'WAITING_USER_ANSWER' })
    expect(r.phase).toBe('needs_you')
    expect(r.needsYou.map(m => m.terminalId)).toEqual(['w1'])
  })

  it('is "working" when someone is processing (and nobody waits/errored)', () => {
    expect(run([planner, worker], { p1: 'IDLE', w1: 'PROCESSING' }).phase).toBe('working')
  })

  it('is "done" only when the planner itself completed', () => {
    expect(run([planner, worker], { p1: 'COMPLETED', w1: 'COMPLETED' }).phase).toBe('done')
  })

  it('is "ready" (not done) when a worker completed but the planner is only idle', () => {
    expect(run([planner, worker], { p1: 'IDLE', w1: 'COMPLETED' }).phase).toBe('ready')
  })

  it('splits planner vs workers and hides memory_manager terminals', () => {
    const mem = term('m1', 'memory_manager')
    const r = run([planner, worker, mem], { p1: 'IDLE', w1: 'IDLE', m1: 'PROCESSING' })
    expect(r.planner?.terminalId).toBe('p1')
    expect(r.workers.map(w => w.terminalId)).toEqual(['w1'])
    // memory_manager is filtered, so its PROCESSING does not force "working"
    expect(r.phase).toBe('ready')
  })
})
