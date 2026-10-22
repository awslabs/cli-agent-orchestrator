import { describe, it, expect } from 'vitest'
import { aggregateSessionStatus, normalizeActivityStatus, isFresh, StatusObservation } from '../sessionActivity'
const time = (ms = 0) => ({ wall: ms, mono: ms })
const aggregate = (states: unknown[], now = 0) => aggregateSessionStatus(
  { ...time(), ids: states.map((_, i) => String(i)) },
  new Map(states.map((status, i) => [String(i), { ...time(), status: normalizeActivityStatus(status) }])), time(now))

describe('session activity', () => {
  it.each([
    [[], 'IDLE'], [['COMPLETED'], 'COMPLETED'], [['COMPLETED', 'IDLE'], 'IDLE'],
    [['ERROR', 'IDLE'], 'ERROR'], [['ERROR', null], 'UNKNOWN'],
    [['COMPLETED', undefined], 'UNKNOWN'], [['PROCESSING', 'ERROR', null], 'PROCESSING'],
    [['WAITING_USER_ANSWER', 'PROCESSING', null], 'WAITING_USER_ANSWER'],
    [['stopped'], 'UNKNOWN'], [['processing'], 'PROCESSING'],
  ])('aggregates %j as %s', (states, expected) => expect(aggregate(states)).toBe(expected))

  it('expires at exactly 120 seconds, independently of network responses', () => {
    expect(aggregate(['WAITING_USER_ANSWER'], 119999)).toBe('WAITING_USER_ANSWER')
    expect(aggregate(['WAITING_USER_ANSWER'], 120000)).toBe('UNKNOWN')
    expect(aggregateSessionStatus(null, new Map(), time())).toBe('UNKNOWN')
  })

  it('degrades old observations before applying priorities, with independent membership age', () => {
    const observations = new Map<string, StatusObservation>([
      ['a', { ...time(), status: 'WAITING_USER_ANSWER' }],
      ['b', { ...time(110000), status: 'PROCESSING' }],
    ])
    const members = { ...time(110000), ids: ['a', 'b'] }
    expect(aggregateSessionStatus(members, observations, time(119999))).toBe('WAITING_USER_ANSWER')
    expect(aggregateSessionStatus(members, observations, time(120000))).toBe('PROCESSING')
    observations.get('b')!.status = 'COMPLETED'
    expect(aggregateSessionStatus(members, observations, time(120000))).toBe('UNKNOWN')
    observations.get('b')!.status = 'PROCESSING'
    expect(aggregateSessionStatus(members, observations, time(230000))).toBe('UNKNOWN')
  })

  it('does not extend freshness on clock rollback or browser suspension', () => {
    expect(isFresh(time(), { wall: -100000, mono: 120000 })).toBe(false)
    expect(isFresh(time(), { wall: 120000, mono: 100 })).toBe(false)
  })
})
