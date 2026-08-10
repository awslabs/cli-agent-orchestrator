import { describe, expect, it } from 'vitest'

import { STATUS_ORDER } from '../lib/filters'
import { DISPLAY_STATUS_CONFIG } from '../lib/terminalDisplay'

// The old guard proved a missing style by intentionally crashing the whole
// dashboard. The managed presentation layer now owns its order and styles
// together, so the useful contract is direct: every renderable key has a
// complete, non-empty style and selected-filter treatment.
describe('worker-state order and style contract', () => {
  it('gives every ordered state a complete display style', () => {
    for (const key of STATUS_ORDER) {
      const style = DISPLAY_STATUS_CONFIG[key]
      expect(style, key).toBeDefined()
      expect(style.label, key).not.toBe('')
      expect(style.dotClass, key).not.toBe('')
      expect(style.bgClass, key).not.toBe('')
      expect(style.textClass, key).not.toBe('')
    }
  })

  it('keeps the managed headline states distinct and ordered', () => {
    expect(STATUS_ORDER.slice(0, 3)).toEqual([
      'MANAGED_ACTIVE',
      'MANAGED_PARKED',
      'MANAGED_LIVE',
    ])
    expect(DISPLAY_STATUS_CONFIG.MANAGED_ACTIVE.pulse).toBe(true)
    expect(DISPLAY_STATUS_CONFIG.MANAGED_LIVE.pulse).not.toBe(true)
    expect(DISPLAY_STATUS_CONFIG.MANAGED_PARKED.pulse).not.toBe(true)
  })
})
