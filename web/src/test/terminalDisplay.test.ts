import { describe, expect, it } from 'vitest'

import type { Annotation } from '../api'
import { RECENT_RENDER_SECONDS, terminalDisplayState } from '../lib/terminalDisplay'
import { projectedTerminal } from './projectedTerminal'

function liveness(seconds: number) {
  return projectedTerminal({
    status: 'not_fifo_monitored',
    status_signals: [{ name: 'liveness', state: 'available', value: seconds }],
  })
}

function workItem(phase: string): Annotation {
  return {
    namespace: 'cao.work-state',
    kind: 'work-item',
    version: 1,
    label: phase,
    semantic_role: 'info',
    priority: 50,
    subject: { type: 'terminal', terminal_id: 'term-1', generation: 'gen-1' },
    valid_until: '2999-01-01T00:00:00Z',
    colour_key: null,
    details: { phase },
    source: 'test',
  }
}

describe('terminalDisplayState', () => {
  it('calls a managed pane active only while its rendering is recent', () => {
    expect(terminalDisplayState('not_fifo_monitored', liveness(0)).key).toBe('MANAGED_ACTIVE')
    expect(terminalDisplayState('not_fifo_monitored', liveness(RECENT_RENDER_SECONDS)).key).toBe('MANAGED_ACTIVE')
    expect(terminalDisplayState('not_fifo_monitored', liveness(RECENT_RENDER_SECONDS + 1)).key).toBe('MANAGED_LIVE')
  })

  it('lets a durable parked checkpoint outrank incidental pane redraws', () => {
    expect(
      terminalDisplayState('not_fifo_monitored', liveness(0), [workItem('parked')]).key,
    ).toBe('MANAGED_PARKED')
  })

  it('relegates a quiet provider processing claim to evidence for a managed worker', () => {
    const terminal = projectedTerminal({
      status: 'processing',
      status_signals: [
        { name: 'screen', state: 'available', value: 'processing' },
        { name: 'liveness', state: 'available', value: 90 },
      ],
    })
    expect(terminalDisplayState('processing', terminal, [workItem('in-round')]).key).toBe('MANAGED_LIVE')
  })

  it('does not relabel an unmanaged provider processing status', () => {
    const terminal = projectedTerminal({
      status: 'processing',
      status_signals: [{ name: 'liveness', state: 'available', value: 90 }],
    })
    expect(terminalDisplayState('processing', terminal).key).toBe('PROCESSING')
  })

  it('never hides an error, input wait, dead pane, or wedged observation', () => {
    for (const status of ['error', 'waiting_user_answer', 'dead', 'superseded']) {
      expect(terminalDisplayState(status, liveness(0), [workItem('parked')]).key).toBe(status.toUpperCase())
    }
    expect(
      terminalDisplayState(
        'processing',
        projectedTerminal({ status: 'processing', wedged: true }),
        [workItem('in-round')],
      ).key,
    ).toBe('MANAGED_STALLED')
  })
})
