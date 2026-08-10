// Shared fixture for the DashboardHome status-order tests.
//
// Not a test file itself (vitest collects only *.test.* / *.spec.*): it is the
// single fleet definition behind the requirement suite
// (dashboardStatusOrder.test.tsx) and the appearance suite
// (dashboardStatusOrderAppearance.test.tsx), so a fixture change cannot make
// the two suites disagree about what the dashboard was shown.
import { vi, expect } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { useStore } from '../store'
import { projectedTerminal } from './projectedTerminal'
import type { TerminalMeta } from '../api'

export const SESSION = { id: 'sess-1', name: 'cao-fleet', status: 'active' }

/** The session card's terminals region — the component's own stable handle. */
export const REGION_ID = 'session-cao-fleet-terminals'

// The full projected row, not a hand-written subset: this fixture drives two
// suites, and a shape the server never sends would make both of them agree
// about something untrue. `status` is the server's lowercase value; the store
// uppercases it.
export type TerminalFixture = TerminalMeta

// Ids are exactly 8 characters: the terminal row renders `t.id.slice(0, 8)`,
// so a longer id would not be findable by its own name.
function terminal(id: string, status: string, profile: string, lastActive: string): TerminalFixture {
  return projectedTerminal({
    id,
    tmux_window: `${profile}-${id}`,
    provider: profile === 'reviewer' ? 'claude_code' : 'kimi_cli',
    agent_profile: profile,
    last_active: lastActive,
    status,
  })
}

// Two managed native workers and one FIFO-monitored worker, so the summary has
// to distinguish a count of 2 from a count of 1 and the filter has something to
// exclude.
export const TERMINALS: TerminalFixture[] = [
  terminal('nfm-0001', 'not_fifo_monitored', 'implementer', '2026-07-28T12:00:00Z'),
  terminal('nfm-0002', 'not_fifo_monitored', 'implementer', '2026-07-28T11:00:00Z'),
  terminal('idle-001', 'idle', 'reviewer', '2026-07-28T10:30:00Z'),
]

// The same fleet plus three rows whose recorded identity no longer resolves.
// terminal_projection.project_row reports the lifecycle vocabulary in `status`
// for those. DEAD and SUPERSEDED are exact dispositions; UNKNOWN_LIVENESS is
// deliberately residual because the evidence cannot prove either one.
export const TERMINALS_WITH_UNRENDERABLE: TerminalFixture[] = [
  ...TERMINALS,
  terminal('dead-001', 'dead', 'implementer', '2026-07-28T09:00:00Z'),
  terminal('supr-001', 'superseded', 'implementer', '2026-07-28T08:00:00Z'),
  terminal('unkn-001', 'unknown-liveness', 'implementer', '2026-07-28T07:00:00Z'),
]

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

export function stubDashboardFetch(terminals: TerminalFixture[] = TERMINALS) {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    if (url === '/sessions/cao-fleet') {
      return jsonResponse({ session: SESSION, terminals })
    }
    const found = terminals.find(t => url === `/terminals/${t.id}`)
    if (found) {
      return jsonResponse({ ...found, name: found.id, session_name: 'cao-fleet' })
    }
    if (url === '/agents/profiles') return jsonResponse([])
    return jsonResponse({})
  }))
}

export async function renderDashboard(terminals: TerminalFixture[] = TERMINALS) {
  render(<DashboardHome onNavigate={() => {}} />)
  await screen.findByText('cao-fleet')
  // The per-terminal status poll is a second, independent async pass, and no
  // rendered label identifies it as finished: a terminal that has not reported
  // yet and a terminal reporting an unrenderable lifecycle status both read as
  // "Unknown". The store is the exact settled condition — every terminal has
  // reported at least once.
  await waitFor(() => {
    const polled = useStore.getState().terminalStatuses
    expect(terminals.filter(t => polled[t.id]).length).toBe(terminals.length)
  })
}

/**
 * The session card's metadata block, which holds the status summary.
 *
 * Anchored on the terminals region id rather than on the session name: the
 * name string is also every fixture terminal's `tmux_session`, so a text query
 * for it is one rendering change away from matching something else. The region
 * id is a contract the component already publishes (see the aria-controls
 * assertion in e2e/dashboard.spec.ts) and its parent is the session card.
 */
export function metadata(): HTMLElement {
  const region = document.getElementById(REGION_ID)
  if (!region) throw new Error(`session card region #${REGION_ID} is not rendered`)
  const card = region.parentElement as HTMLElement
  return card.querySelector('div.select-text') as HTMLElement
}

/**
 * The StatusSummary chip row: one chip per status with a nonzero count.
 *
 * Located via its count spans, whose text is bare digits — the only such text
 * in the metadata block (the agent-type chips read "implementer ×2" and the
 * header reads "3 agents").
 */
export function summaryChips(): string[] {
  const counts = within(metadata()).queryAllByText(/^\d+$/)
  if (counts.length === 0) return []
  const row = counts[0].parentElement!.parentElement!
  return [...row.children].map(c => c.textContent ?? '')
}

/** Chip label -> count, e.g. { 'Managed Live': 2, Idle: 1 }. */
export function summaryCounts(): Record<string, number> {
  const out: Record<string, number> = {}
  for (const chip of summaryChips()) {
    const m = /^(\d+)(.*)$/.exec(chip)
    if (m) out[m[2]] = Number(m[1])
  }
  return out
}

/** Every terminal the summary accounts for, across all chips. */
export function summaryTotal(): number {
  return Object.values(summaryCounts()).reduce((a, b) => a + b, 0)
}

/**
 * The reachability editor's options container — the chip-bar successor of
 * the old always-on pill row, and the container the appearance suite pins.
 *
 * THE CONTAINER MOVED, THE CONTRACT DID NOT. Reachability is a chip now: the
 * options live in the chip's popover editor, reached from the "+ Filter"
 * picker (or by clicking the chip once it is active). The container still
 * holds exactly the STATUS_ORDER entries and nothing else — a stray
 * clear-all or overflow control inside it fails the appearance suite exactly
 * as it did when the container was the always-on row.
 *
 * Opening it here, in the fixture, keeps every caller honest about the same
 * path the operator takes.
 */
export function openReachabilityEditor(): HTMLElement {
  const existing = screen.queryByTestId('global-editor-options')
  if (existing) return existing
  const chip = document.querySelector('[data-testid="global-chip"][data-dimension="reachability"] button')
  if (chip) {
    fireEvent.click(chip)
  } else {
    fireEvent.click(screen.getByTestId('global-picker-button'))
    fireEvent.click(within(screen.getByTestId('global-picker')).getByText('Reachability'))
  }
  return screen.getByTestId('global-editor-options')
}

export function visibleTerminalIds(terminals: TerminalFixture[] = TERMINALS): string[] {
  const region = document.getElementById(REGION_ID)!
  return terminals.map(t => t.id).filter(id => within(region).queryByText(id) !== null)
}
