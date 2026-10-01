/**
 * Build-time feature flags for the web UI.
 *
 * Kept in its own module so a flag can be imported (and asserted in a test)
 * without dragging in the whole component tree that `App.tsx` pulls together.
 */

/**
 * Whether the agent-plugins ("Plugins") tab is offered.
 *
 * **OFF by default.** Maintainer decision M1 has not settled the naming of the
 * agent-plugin surfaces, and Requirement 16.5 forbids shipping them to end users
 * until it does — a visible tab is exactly such a surface. The panel, its API
 * client, and the `/plugins/*` endpoints are all built and tested; once M1 lands,
 * flipping this constant is the web UI's half of the release, alongside defaulting
 * the server-side `CAO_AGENT_PLUGINS_ENABLED` gate to on.
 *
 * Typed `boolean` rather than left as the literal `false` so switching it does
 * not change the constant's type and does not make the `filter` below look like
 * dead code to the type checker.
 *
 * Note this gates only the *tab*. The `/plugins/*` endpoints and the `cao plugin`
 * CLI group carry their own execution gate — the `CAO_AGENT_PLUGINS_ENABLED`
 * environment variable, default off, which makes the routes 404 and the CLI group
 * refuse every subcommand. Hiding navigation is not a ship gate, so this constant
 * is the web UI's half of the same default-off posture rather than the whole of it.
 */
export const PLUGINS_TAB_ENABLED: boolean = false
