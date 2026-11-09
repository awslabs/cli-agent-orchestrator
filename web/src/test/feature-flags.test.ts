import { describe, it, expect } from 'vitest'
import { PLUGINS_TAB_ENABLED } from '../featureFlags'

/**
 * The graduation gate for the web surface (#1244).
 *
 * This file exists so the gate cannot be switched on silently. The agent-plugin
 * surfaces do not ship to end users until they graduate (#1244), and a visible
 * tab is exactly such a surface, so the flag's *default* is a requirement rather
 * than a preference. The verb is decision M1, recorded by the issue author in #573.
 *
 * It asserts the flag module in isolation rather than mounting `App`: the flag
 * lives in its own module precisely so this assertion needs none of the API
 * client, the store, or the WebSocket that the component tree pulls in.
 */
describe('PLUGINS_TAB_ENABLED', () => {
  it('is off, gating the Plugins tab until the surface graduates (#1244)', () => {
    expect(PLUGINS_TAB_ENABLED).toBe(false)
  })

  it('is a boolean, so flipping it does not change the constant type', () => {
    expect(typeof PLUGINS_TAB_ENABLED).toBe('boolean')
  })
})
