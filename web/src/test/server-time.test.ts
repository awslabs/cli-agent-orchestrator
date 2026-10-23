;(globalThis as any).process.env.TZ = 'Asia/Taipei'

import { describe, expect, it } from 'vitest'
import { parseServerTime } from '../time'

describe('parseServerTime', () => {
  it('treats offset-less server timestamps as UTC', () => {
    expect(new Date('2026-10-01T13:37:12').getTimezoneOffset()).toBe(-480)
    expect(parseServerTime('2026-10-01T13:37:12').toISOString()).toBe('2026-10-01T13:37:12.000Z')
  })

  it('preserves timestamps that already have an offset', () => {
    expect(parseServerTime('2026-10-01T21:37:12+08:00').toISOString()).toBe('2026-10-01T13:37:12.000Z')
    expect(parseServerTime('2026-10-01T13:37:12Z').toISOString()).toBe('2026-10-01T13:37:12.000Z')
  })
})
