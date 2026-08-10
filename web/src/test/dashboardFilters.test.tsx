// DashboardHome filtering, end to end through the real component.
//
// lib/filters.test.tsx covers the predicate; this suite covers the
// COMPOSITION the predicate replaced and the states the bars must render
// honestly: the drifted-predicates defect (selecting "default" emptied a
// card), the NaN sort on zero-terminal sessions, STOPPED reaching the pills,
// the global gate vs the per-session bar, and the counter that must never be
// confused with the status summary.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { useStore } from '../store'
import { projectedTerminal } from './projectedTerminal'
import type { Annotation, AnnotationsResponse, TerminalMeta } from '../api'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

const PAST_A = '2026-07-20T10:00:00Z'
const PAST_B = '2026-07-22T10:00:00Z'
const FUTURE = '2999-01-01T00:00:00Z'
const LONG_VALUE = `agent/payment-pr04-${'x'.repeat(60)}`

// Five rows in cao-alpha, two in cao-beta, and two empty sessions: every
// layer-1 vocabulary has at least two values, and the annotation bag carries
// fleet-wide pills (phase, attention), a typeahead (13 tasks), a range
// (parked_at), a tri-state (enabled) and a long substring-only value
// (observed.branch) — one of every control shape.
const ALPHA: TerminalMeta[] = [
  projectedTerminal({ id: 'aa-0001', agent_profile: 'implementer', provider: 'kimi_cli', status: 'not_fifo_monitored', caller_id: null, last_active: '2026-07-28T12:00:00Z' }),
  projectedTerminal({ id: 'aa-0002', agent_profile: 'implementer', provider: 'kimi_cli', status: 'not_fifo_monitored', caller_id: 'aa-0001', last_active: '2026-07-28T11:00:00Z' }),
  projectedTerminal({ id: 'aa-0003', agent_profile: null, provider: 'claude_code', status: 'idle', last_active: '2026-07-28T10:00:00Z' }),
  projectedTerminal({ id: 'aa-0004', agent_profile: 'reviewer', provider: 'claude_code', status: 'stopped', last_active: '2026-07-28T09:00:00Z' }),
  projectedTerminal({ id: 'aa-0005', agent_profile: 'reviewer', provider: 'claude_code', status: 'dead', lifecycle_state: 'dead', last_active: '2026-07-28T08:00:00Z' }),
]
const BETA: TerminalMeta[] = [
  projectedTerminal({ id: 'bb-0001', tmux_session: 'cao-beta', session_name: 'cao-beta', agent_profile: 'spec-writer', provider: 'kimi_cli', status: 'processing', last_active: '2026-07-27T12:00:00Z' }),
  projectedTerminal({ id: 'bb-0002', tmux_session: 'cao-beta', session_name: 'cao-beta', agent_profile: 'reviewer', provider: 'kimi_cli', status: 'not_fifo_monitored', last_active: '2026-07-27T11:00:00Z' }),
]
const ALL_ROWS = [...ALPHA, ...BETA]

const SESSION_LIST = [
  { id: 's-1', name: 'cao-alpha', status: 'active' },
  { id: 's-2', name: 'cao-beta', status: 'active' },
  { id: 's-3', name: 'cao-empty', status: 'active' },
  { id: 's-4', name: 'cao-empty-2', status: 'active' },
]

function onRow(t: TerminalMeta): Annotation['subject'] {
  return { type: 'terminal', terminal_id: t.id, generation: t.generation }
}

function note(t: TerminalMeta, label: string, details: Record<string, string>, priority = 60): Annotation {
  return {
    namespace: 'cao-conductor',
    kind: 'display',
    version: 1,
    label,
    semantic_role: 'warning',
    priority,
    subject: onRow(t),
    valid_until: FUTURE,
    details,
  }
}

function annotations(): Annotation[] {
  const [a1, a2, a3, a4, a5] = ALPHA
  const [b1] = BETA
  return [
    note(a1, 'reported', { phase: 'reported', attention: 'needs-review', task: 't-01', parked_at: PAST_A, 'observed.branch': LONG_VALUE, enabled: 'true' }, 90),
    note(a2, 'waiting', { phase: 'waiting', attention: 'none', task: 't-02', parked_at: PAST_B, enabled: 'false' }, 80),
    // Thirteen distinct task values fleet-wide: past the pill cap, so the
    // dimension is a typeahead and belongs to the session bar.
    ...Array.from({ length: 5 }, (_, i) => note(a4, `q-${i}`, { task: `t-0${3 + i}` }, 40 - i)),
    ...Array.from({ length: 5 }, (_, i) => note(a5, `r-${i}`, { task: `t-0${8 + i}` }, 30 - i)),
    note(a3, 'plain', { task: 't-13' }, 20),
    note(b1, 'reported', { phase: 'reported', attention: 'needs-review' }, 90),
  ]
}

function payload(items: Annotation[], overrides: Partial<AnnotationsResponse> = {}): AnnotationsResponse {
  return {
    annotation_schema: 'cao-annotations-v1',
    coverage: 'complete',
    sources_read: 1,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations: items,
    ...overrides,
  }
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

function stubFetch(sessions = SESSION_LIST, body: AnnotationsResponse = payload(annotations())) {
  const bySession: Record<string, TerminalMeta[]> = {
    'cao-alpha': ALPHA,
    'cao-beta': BETA,
    'cao-empty': [],
    'cao-empty-2': [],
  }
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    const sessionMatch = /^\/sessions\/([^/]+)$/.exec(url)
    if (sessionMatch && sessionMatch[1] in bySession) {
      const name = sessionMatch[1]
      return jsonResponse({ session: sessions.find(s => s.name === name), terminals: bySession[name] })
    }
    if (url === '/annotations') return jsonResponse(body)
    const found = ALL_ROWS.find(t => url === `/terminals/${t.id}`)
    if (found) return jsonResponse(found)
    if (url === '/agents/profiles') return jsonResponse([])
    return jsonResponse({})
  }))
}

async function renderDashboard(sessions = SESSION_LIST) {
  useStore.setState({ sessions, terminalStatuses: {} })
  render(<DashboardHome onNavigate={() => {}} />)
  await screen.findAllByText('cao-alpha')
  await waitFor(() => {
    const polled = useStore.getState().terminalStatuses
    expect(ALL_ROWS.filter(t => polled[t.id]).length).toBe(ALL_ROWS.length)
  })
  // The annotation pass is a third, independent fetch; wait for the chips so
  // every assertion runs against the placed set.
  await waitFor(() => expect(screen.getAllByTestId('annotation-chip').length).toBeGreaterThan(0))
}


function cardPresent(name: string): boolean {
  return document.getElementById(`session-${name}-terminals`) !== null
}


function metadataOf(card: HTMLElement): HTMLElement {
  return card.querySelector('div.select-text') as HTMLElement
}

function sessionCard(name: string): HTMLElement {
  const region = document.getElementById(`session-${name}-terminals`)
  if (!region) throw new Error(`session card for ${name} is not rendered/expanded`)
  return region.parentElement as HTMLElement
}

function visibleIds(cardName: string): string[] {
  const region = document.getElementById(`session-${cardName}-terminals`)
  if (!region) return []
  return ALL_ROWS.map(t => t.id).filter(id => within(region).queryByText(id.slice(0, 8)) !== null)
}

// ── Chip-bar drivers ──────────────────────────────────────────────────────
//
// The bar shows only ACTIVE filters as chips; everything else is reached
// through the "+ Filter" picker (or the chip, once active) and edited in the
// chip's popover. These helpers walk that exact path: open the bar's picker,
// choose the dimension by its label, land in its editor. The popovers are
// portalled to document.body, so they are fetched by their testids rather
// than through the card.
function openGlobalEditor(dimensionLabel: string): HTMLElement {
  fireEvent.click(screen.getByTestId('global-picker-button'))
  const picker = screen.getByTestId('global-picker')
  fireEvent.click(within(picker).getByText(dimensionLabel, { exact: true }))
  return screen.getByTestId('global-editor')
}

function openSessionEditor(cardName: string, dimensionLabel: string): HTMLElement {
  const card = sessionCard(cardName)
  fireEvent.click(within(card).getByTestId(`session-${cardName}-picker-button`))
  const picker = screen.getByTestId(`session-${cardName}-picker`)
  fireEvent.click(within(picker).getByText(dimensionLabel, { exact: true }))
  return screen.getByTestId(`session-${cardName}-editor`)
}

function openSessionAdvanced(cardName: string): HTMLElement {
  const card = sessionCard(cardName)
  fireEvent.click(within(card).getByTestId(`session-${cardName}-advanced-button`))
  return screen.getByTestId(`session-${cardName}-advanced`)
}

beforeEach(() => {
  useStore.setState({ sessions: SESSION_LIST, terminalStatuses: {} })
  stubFetch()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.setState({ sessions: [], terminalStatuses: {} })
})

describe('the collapsed predicates defect: one predicate at both gates', () => {
  it('selecting "default" keeps the session AND shows its default rows', async () => {
    await renderDashboard()
    // The row gate used to compare `t.agent_profile === filter` with no
    // fallback while the session gate folded to 'default' — the card stayed
    // and rendered zero rows. Both gates now call matchesFilters.
    openGlobalEditor('Agent profile')
    fireEvent.click(screen.getByRole('button', { name: 'default' }))
    expect(cardPresent('cao-alpha')).toBe(true)
    expect(visibleIds('cao-alpha')).toEqual(['aa-0003'])
    // AND across dimensions: beta has no default row, so its card is gated out.
    expect(cardPresent('cao-beta')).toBe(false)
    // Zero-terminal sessions are always kept — the pre-existing rule.
    expect(cardPresent('cao-empty')).toBe(true)
  })
})

describe('the NaN comparator defect', () => {
  it('sorts zero-terminal sessions against each other without NaN', async () => {
    // Math.max(...[]) is -Infinity and -Infinity - -Infinity is NaN; a NaN
    // comparator is undefined sort behaviour. Two empty sessions meet in the
    // comparator on every render of this fleet.
    await renderDashboard()
    const cards = SESSION_LIST.map(s => s.name).filter(n => cardPresent(n))
    expect(cards).toEqual(['cao-alpha', 'cao-beta', 'cao-empty', 'cao-empty-2'])
  })
})

describe('STOPPED reaches the summary and the pills as itself', () => {
  it('counts a stopped row as Stopped, not Unknown', async () => {
    await renderDashboard()
    const meta = metadataOf(sessionCard('cao-alpha'))
    expect(within(meta).getByText('Stopped')).toBeTruthy()
    // …and the proven-dead row is named rather than presented as missing data.
    expect(within(meta).getByText('Dead')).toBeTruthy()
  })

  it('filters the fleet to stopped rows from the Stopped option', async () => {
    await renderDashboard()
    openGlobalEditor('Reachability')
    fireEvent.click(screen.getByRole('button', { name: 'Stopped' }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0004'])
    expect(cardPresent('cao-beta')).toBe(false)
  })

  it('filters the fleet to proven-dead rows from the Dead option', async () => {
    await renderDashboard()
    openGlobalEditor('Reachability')
    fireEvent.click(screen.getByRole('button', { name: 'Dead' }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0005'])
    expect(cardPresent('cao-beta')).toBe(false)
  })

  it('filters liveness on the row’s own word, separate from the folded chip', async () => {
    await renderDashboard()
    openGlobalEditor('Liveness')
    fireEvent.click(screen.getByRole('button', { name: 'dead' }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0005'])
    expect(cardPresent('cao-beta')).toBe(false)
  })
})

describe('reachability is multi-select: OR within, AND across', () => {
  it('selecting two reachability options shows the union', async () => {
    await renderDashboard()
    openGlobalEditor('Reachability')
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    fireEvent.click(screen.getByRole('button', { name: 'Processing' }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0001', 'aa-0002'])
    // bb-0002 is itself a managed worker: OR within the dimension means both.
    expect(visibleIds('cao-beta')).toEqual(['bb-0001', 'bb-0002'])
    // AND across dimensions: adding the spec-writer profile leaves only bb-0001.
    openGlobalEditor('Agent profile')
    fireEvent.click(screen.getByRole('button', { name: 'spec-writer' }))
    expect(cardPresent('cao-alpha')).toBe(false)
    expect(visibleIds('cao-beta')).toEqual(['bb-0001'])
  })
})

describe('the derived dimensions appear where their shape puts them', () => {
  it('offers fleet-wide pill facets in the global picker, with counts', async () => {
    await renderDashboard()
    fireEvent.click(screen.getByTestId('global-picker-button'))
    const picker = screen.getByTestId('global-picker')
    expect(picker.className).toContain('max-h-[calc(100vh-1rem)]')
    expect(picker.className).toContain('!overflow-y-auto')
    expect(within(picker).getByText('phase')).toBeTruthy()
    expect(within(picker).getByText('attention')).toBeTruthy()
    // The 13-value task facet is NOT here — an unbounded pill wall is exactly
    // what the per-session bar exists to prevent.
    expect(within(picker).queryByText('task')).toBeNull()

    // The editor shows the value counts next to each option.
    fireEvent.click(within(picker).getByText('phase', { exact: true }))
    const editor = screen.getByTestId('global-editor')
    expect(within(editor).getByRole('button', { name: /^reported/ }).textContent).toContain('2')
  })

  it('gates session visibility from a global facet selection', async () => {
    await renderDashboard()
    openGlobalEditor('phase')
    fireEvent.click(screen.getByRole('button', { name: /^reported/ }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0001'])
    expect(visibleIds('cao-beta')).toEqual(['bb-0001'])
    expect(within(sessionCard('cao-alpha')).getByTestId('session-filter-count').textContent).toBe('1 of 5 shown')
  })

  it('offers the typeahead, range, tri-state and text facets in the session Advanced sheet', async () => {
    await renderDashboard()
    // The Advanced sheet is where the old always-on rows went: every derived
    // dimension of the session, opt-in and dense.
    const modal = openSessionAdvanced('cao-alpha')
    // The typeahead is a real select, labelled by its own facet name.
    expect(within(modal).getByLabelText('task')).toBeTruthy()
    // The timestamp facet earned a range control.
    expect(within(modal).getByLabelText('parked at from')).toBeTruthy()
    expect(within(modal).getByLabelText('parked at to')).toBeTruthy()
    // The boolean facet earned a tri-state.
    expect(within(modal).getByRole('button', { name: 'true' })).toBeTruthy()
    // The long branch value is substring-only, under its provenance heading.
    expect(within(modal).getByText('observed')).toBeTruthy()
    expect(within(modal).getByLabelText('branch contains')).toBeTruthy()
  })

  it('narrows rows from a per-session facet and never removes the card', async () => {
    await renderDashboard()
    // task is a 13-value typeahead: its editor narrows by typing first.
    const editor = openSessionEditor('cao-alpha', 'task')
    fireEvent.change(within(editor).getByLabelText('Find task value'), { target: { value: 't-02' } })
    fireEvent.click(within(editor).getByRole('button', { name: /^t-02/ }))
    expect(cardPresent('cao-alpha')).toBe(true)
    expect(visibleIds('cao-alpha')).toEqual(['aa-0002'])
    // Other cards are untouched: the session bar is AND-ed inside its own card.
    expect(visibleIds('cao-beta')).toEqual(['bb-0001', 'bb-0002'])
  })

  it('filters a range facet between its bounds', async () => {
    await renderDashboard()
    const editor = openSessionEditor('cao-alpha', 'parked at')
    fireEvent.change(within(editor).getByLabelText('parked at from'), { target: { value: '2026-07-21T00:00' } })
    // Only PAST_B (aa-0002) is past the bound; PAST_A (aa-0001) is before it.
    expect(visibleIds('cao-alpha')).toEqual(['aa-0002'])
  })
})

describe('the per-session zero-result state names its cause and recovers in one click', () => {
  it('keeps the card, says 0 of N, and clears the session filters', async () => {
    await renderDashboard()
    const card = sessionCard('cao-alpha')
    fireEvent.change(within(card).getByLabelText('Session filter text'), { target: { value: 'zzz-no-match' } })
    expect(cardPresent('cao-alpha')).toBe(true)
    expect(visibleIds('cao-alpha')).toEqual([])
    expect(within(card).getByTestId('session-filter-count').textContent).toBe('0 of 5 shown')
    expect(card.textContent).toContain('the session filters hide every row')

    fireEvent.click(within(card).getAllByRole('button', { name: 'Clear session filters' })[0])
    expect(visibleIds('cao-alpha')).toEqual(ALPHA.map(t => t.id))
  })
})

describe('the summary chips keep counting ALL terminals while a filter runs', () => {
  it('diverges from the shown counter deliberately', async () => {
    await renderDashboard()
    openGlobalEditor('phase')
    fireEvent.click(screen.getByRole('button', { name: /^reported/ }))

    const card = sessionCard('cao-alpha')
    // One row survives…
    expect(within(card).getByTestId('session-filter-count').textContent).toBe('1 of 5 shown')
    // …but the summary still describes the session: Managed Live 2, Idle 1,
    // Stopped 1, Unknown 1. Pinned by dashboardStatusOrder.test.tsx — the
    // summary describes the session, the filter describes the view.
    const meta = metadataOf(card)
    expect(within(meta).getByText('Managed Live').parentElement!.textContent).toContain('2')
    expect(within(meta).getByText('Idle').parentElement!.textContent).toContain('1')
  })
})

describe('free text is case-insensitive on both sides, end to end', () => {
  it('matches a capitalised query against mixed-case values', async () => {
    await renderDashboard()
    fireEvent.change(screen.getByLabelText('Filter text'), { target: { value: '  SPEC-WRITER  ' } })
    expect(cardPresent('cao-alpha')).toBe(false)
    expect(visibleIds('cao-beta')).toEqual(['bb-0001'])
  })

  it('matches against facet values, not only identity fields', async () => {
    await renderDashboard()
    fireEvent.change(screen.getByLabelText('Filter text'), { target: { value: 'payment-pr04' } })
    expect(visibleIds('cao-alpha')).toEqual(['aa-0001'])
  })
})

describe('spawned-by is a subtree question', () => {
  it('selecting the caller shows the row it spawned', async () => {
    await renderDashboard()
    const editor = openSessionEditor('cao-alpha', 'Spawned by')
    fireEvent.click(within(editor).getByRole('button', { name: /implementer · aa-0001/ }))
    expect(visibleIds('cao-alpha')).toEqual(['aa-0002'])
  })
})

describe('the global empty state names the filters and offers the clear', () => {
  it('says the filters hid the fleet and clears them in one click', async () => {
    const twoSessions = SESSION_LIST.slice(0, 2)
    stubFetch(twoSessions)
    await renderDashboard(twoSessions)
    openGlobalEditor('Reachability')
    fireEvent.click(screen.getByRole('button', { name: 'Error' }))
    expect(cardPresent('cao-alpha')).toBe(false)
    expect(screen.getByText('No sessions match the current filter.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear all filters' }))
    expect(cardPresent('cao-alpha')).toBe(true)
  })
})

describe('degraded coverage is named beside the controls that depend on it', () => {
  it('shows the partial-data note when the envelope reports partial coverage', async () => {
    stubFetch(SESSION_LIST, payload(annotations(), { coverage: 'partial', sources_failed: 1 }))
    await renderDashboard()
    const bar = screen.getByTestId('filter-bar')
    expect(within(bar).getByTestId('filter-coverage-note').textContent).toContain('partial')
  })
})

describe('the bar itself is the summary of what is hidden', () => {
  // The pre-chip bar had a collapsed "Filters · N active" toggle; the chip bar
  // IS the summary — every active filter is a visible chip reading
  // `Dimension: selection`, and recovery is the always-visible Clear all.
  it('shows each active filter as a chip with its selection, and clears in one click', async () => {
    await renderDashboard()
    openGlobalEditor('Reachability')
    fireEvent.click(screen.getByRole('button', { name: 'Managed Live' }))
    fireEvent.click(screen.getByRole('button', { name: 'Processing' }))

    const bar = screen.getByTestId('filter-bar')
    const chip = bar.querySelector('[data-testid="global-chip"][data-dimension="reachability"]')!
    expect(chip.textContent).toContain('Managed Live')
    expect(chip.textContent).toContain('Processing')

    fireEvent.click(within(bar).getByRole('button', { name: 'Clear all' }))
    expect(bar.querySelector('[data-testid="global-chip"]')).toBeNull()
    expect(visibleIds('cao-alpha')).toEqual(ALPHA.map(t => t.id))
  })
})
