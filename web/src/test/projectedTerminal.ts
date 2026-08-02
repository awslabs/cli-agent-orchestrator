// ONE definition of a projected terminal row, for every fixture that needs one.
//
// Three separate hand-written copies of this shape existed — src/api.ts's
// `TerminalMeta`, e2e/stub.ts's terminals array and sessionCard.test.tsx's
// TERMINAL — and all three encoded a seven-key row against
// `terminal_projection.project_row`'s twenty-five. Widening one without the
// others produces green tests over a shape the server never sends, which is
// exactly how `created_at` survived: no `TerminalModel` column and no
// projection key by that name exists, yet all three fixtures supplied one and
// the dashboard branch that read it looked covered while being permanently
// dead in production.
//
// So: this file is the single fixture source, derived key-for-key from
// `project_row`, and it is typed as `TerminalMeta` so a drift between the
// interface and the fixtures is a compile error rather than a green test.

import type { TerminalMeta } from '../api'

export interface ProjectedTerminalOverrides extends Partial<TerminalMeta> {
  id?: string
}

/**
 * A projected row with every key `project_row` returns.
 *
 * Defaults describe the common live managed worker: a v2 native-TUI terminal
 * whose pane resolves, so `lifecycle_state` is `live`, `fifo_monitored` is
 * false and `status` is `not_fifo_monitored` — the combination
 * `project_row` produces for the majority of Colin's fleet.
 */
export function projectedTerminal(overrides: ProjectedTerminalOverrides = {}): TerminalMeta {
  const id = overrides.id ?? 'term-001'
  const session = overrides.tmux_session ?? 'cao-fleet'
  const window = overrides.tmux_window ?? 'reviewer-a1b2'
  return {
    terminal_id: id,
    name: window,
    session_name: session,
    id,
    tmux_session: session,
    tmux_window: window,
    provider: 'kimi_cli',
    agent_profile: 'reviewer',
    caller_id: null,
    generation: `${id}-gen-1`,
    callback_target_generation: `${id}-gen-1`,
    protocol_vintage: 'v2',
    server_socket_path: '/private/tmp/tmux-501/default',
    session_id: '$3',
    window_id: '@9',
    pane_id: '%7',
    pane_pid: 4242,
    native_session_id: 'native-session-1',
    lifecycle_state: 'live',
    lifecycle_reason: null,
    superseded_by_terminal_id: null,
    superseded_by_generation: null,
    fifo_monitored: false,
    status: 'not_fifo_monitored',
    last_active: '2026-07-28T12:00:00Z',
    ...overrides,
  }
}
