import { describe, it, expect } from 'vitest'
import { PLUGINS_TAB_ENABLED } from '../featureFlags'

/**
 * The M1 gate for the web surface (Requirement 16.5).
 *
 * This file exists so the gate cannot be switched on silently. Requirement 16.5
 * forbids shipping the agent-plugin surfaces to end users before maintainers
 * settle the verb, and a visible tab is exactly such a surface, so the flag's
 * *default* is a requirement rather than a preference.
 *
 * It asserts the flag module in isolation rather than mounting `App`: the flag
 * lives in its own module precisely so this assertion needs none of the API
 * client, the store, or the WebSocket that the component tree pulls in.
 */
describe('PLUGINS_TAB_ENABLED', () => {
  it('is off, gating the Plugins tab until maintainer decision M1 lands', () => {
    expect(PLUGINS_TAB_ENABLED).toBe(false)
  })

  it('is a boolean, so flipping it does not change the constant type', () => {
    expect(typeof PLUGINS_TAB_ENABLED).toBe('boolean')
  })
})
