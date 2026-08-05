// Conductor annotation rendering (work-state design §9.5).
//
// The suite is organised around the four things that must never regress:
// annotations render ALONGSIDE the status badge; a stale-generation annotation
// is dropped rather than re-parented; a stale annotation looks stale; and a
// fleet with no annotations renders byte-identically to the dashboard before
// this existed. The last one is asserted by DOM comparison rather than by
// inspection, because "looks the same" is exactly the claim a human review
// cannot make reliably.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import annotationsLibSource from '../lib/annotations.ts?raw'
import filtersLibSource from '../lib/filters.ts?raw'
import annotationChipsSource from '../components/AnnotationChips.tsx?raw'
import annotationDetailsSource from '../components/AnnotationDetails.tsx?raw'
import filterBarSource from '../components/FilterBar.tsx?raw'
import floatingCardSource from '../components/FloatingCard.tsx?raw'
import identityColourSource from '../lib/identityColour.ts?raw'
import { render, cleanup, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import {
  CampaignAnnotations,
  MAX_IDENTITY_CHIPS,
  MAX_ROW_CHIPS,
} from '../components/AnnotationChips'
import { detailsToText } from '../components/AnnotationDetails'
import { useStore } from '../store'
import { projectedTerminal } from './projectedTerminal'
import {
  ageSource,
  freshness,
  groupedFacets,
  isIdentity,
  isStale,
  orderedFacets,
  partitionChips,
  placeAnnotations,
  readAnnotations,
  resolveRole,
  splitFacetKey,
} from '../lib/annotations'
import { IDENTITY_PALETTE, paletteIndex, rgba } from '../lib/identityColour'
import type { Annotation, AnnotationsResponse } from '../api'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

const SESSION = { id: 'sess-1', name: 'cao-fleet', status: 'active' }
const GENERATION = 'term-001-gen-1'
const TERMINAL = projectedTerminal({ id: 'term-001', generation: GENERATION, status: 'idle' })

const FUTURE = '2999-01-01T00:00:00Z'
const PAST = '2020-01-01T00:00:00Z'

/**
 * The facet key/value pairs a card is showing.
 *
 * Structural rather than a substring match on `textContent`: the card renders
 * key and value as sibling `<dt>`/`<dd>` with no literal separator, so
 * `toContain('task: x')` would assert a format only the old `title` ever had
 * and would pass or fail for reasons unrelated to the facts being present.
 */
function facetPairs(root: HTMLElement): Record<string, string> {
  const out: Record<string, string> = {}
  for (const dt of Array.from(root.querySelectorAll('dt'))) {
    const dd = dt.nextElementSibling
    if (dd) out[(dt.textContent || '').trim()] = (dd.textContent || '').trim()
  }
  return out
}

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    namespace: 'cao-conductor',
    kind: 'work-state.display',
    version: 1,
    label: 'waiting',
    semantic_role: 'warning',
    priority: 60,
    subject: { type: 'terminal', terminal_id: 'term-001', generation: GENERATION },
    valid_until: FUTURE,
    details: { task: 'p0-09b-r1', role: 'reviewer', round: '12' },
    source: 'aegix-mobile',
    ...overrides,
  }
}

function payload(annotations: Annotation[], overrides: Partial<AnnotationsResponse> = {}): AnnotationsResponse {
  return {
    annotation_schema: 'cao-annotations-v1',
    coverage: 'complete',
    sources_read: 1,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations,
    ...overrides,
  }
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

/** `annotationsBody` of `undefined` means the route answers with nothing at all. */
function stubFetch(annotationsBody?: unknown, terminals = [TERMINAL]) {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    if (url === '/sessions/cao-fleet') return jsonResponse({ session: SESSION, terminals })
    if (url === '/annotations') {
      if (annotationsBody === undefined) throw new Error('no such route')
      return jsonResponse(annotationsBody)
    }
    const found = terminals.find(t => url === `/terminals/${t.id}`)
    if (found) return jsonResponse({ ...found, name: found.id, session_name: 'cao-fleet' })
    if (url === '/agents/profiles') return jsonResponse([])
    return jsonResponse({})
  }))
}

async function renderDashboard(terminals = [TERMINAL]) {
  const view = render(<DashboardHome onNavigate={() => {}} />)
  await screen.findByText('cao-fleet')
  await waitFor(() => {
    const polled = useStore.getState().terminalStatuses
    expect(terminals.filter(t => polled[t.id]).length).toBe(terminals.length)
  })
  return view
}

function chips(): HTMLElement[] {
  return screen.queryAllByTestId('annotation-chip')
}

beforeEach(() => {
  useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.setState({ sessions: [], terminalStatuses: {} })
})

describe('annotations render alongside StatusBadge, never instead of it', () => {
  it('draws the conductor chip next to the fork status badge on the same row', async () => {
    stubFetch(payload([annotation()]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    // The fork's own status is still there — `not_fifo_monitored`/idle is the
    // only reachability statement the fork can make, and a conductor chip
    // never replaces it.
    // Scoped to the terminal row: "Idle" also appears in the status filter row
    // and in the session summary, and neither of those is the badge in
    // question. The chip sits inside its own group wrapper (which is what lets
    // the group drop to its own line at 390px), so the identity line is one
    // level further up.
    const identityLine = chip.closest('[data-testid="annotation-group"]')!.parentElement!
    expect(within(identityLine).getByText('Idle')).toBeTruthy()
    expect(chip.textContent).toContain('waiting')
  })

  it('colours the chip from the six semantic roles, never from a status key', async () => {
    stubFetch(payload([
      annotation({ label: 'active', semantic_role: 'info', priority: 90 }),
      annotation({ label: 'blocked', semantic_role: 'danger', priority: 80 }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(2))
    const classes = chips().map(c => c.className).join(' ')
    expect(classes).toContain('bg-cao-info')
    expect(classes).toContain('bg-cao-danger')
    // The chips draw only from the token family, never from a STATUS_CONFIG key.
    expect(classes).not.toContain('bg-blue-900')
  })

  it('shows the parked age on the chip itself, from the conductor timestamp', async () => {
    const parked = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString()
    stubFetch(payload([annotation({ details: { task: 't', parked_at: parked } })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(chips()[0].textContent).toMatch(/3h/)
  })

  it('exposes the derived facets in the hover card and in the accessible name', async () => {
    stubFetch(payload([annotation()]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]

    // The accessible name is the path that does NOT depend on a pointer, and it
    // is unchanged by the move off `title`.
    expect(chip.getAttribute('aria-label')).toContain('waiting')
    expect(chip.getAttribute('aria-label')).toContain('task: p0-09b-r1')

    // `title` is gone on purpose: a native tooltip cannot be moused into or
    // selected, and rendering both would show two tooltips on one element.
    expect(chip.getAttribute('title')).toBeNull()

    fireEvent.mouseEnter(chip)
    const card = await waitFor(() => screen.getByTestId('annotation-hovercard'))
    const facets = facetPairs(card)
    expect(facets['task']).toBe('p0-09b-r1')
    expect(facets['role']).toBe('reviewer')
    expect(facets['round']).toBe('12')
  })

  it('keeps the hover card open while the pointer travels into it', async () => {
    // The whole reason for replacing `title`: an operator must be able to move
    // onto the card to read or select it. Leaving the chip must not close a
    // card the pointer has entered.
    stubFetch(payload([annotation()]))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(1))

    fireEvent.mouseEnter(chips()[0])
    const card = await waitFor(() => screen.getByTestId('annotation-hovercard'))
    fireEvent.mouseLeave(chips()[0])
    fireEvent.mouseEnter(card)

    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByTestId('annotation-hovercard')).not.toBeNull()
  })

  it('keeps the chips themselves out of the tab order', async () => {
    // Unchanged founding decision: the chips are spans, never controls, so an
    // unauthenticated dashboard does not grow one tab stop per annotation and
    // the AAA 44×44 target question never arises. The hover card is a
    // pointer-only enhancement and the row's info button is the real control.
    stubFetch(payload([annotation()]))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(1))

    for (const chip of chips()) {
      expect(chip.tagName).toBe('SPAN')
      expect(chip.querySelector('button, a, input, [role="button"], [tabindex]')).toBeNull()
      expect(chip.getAttribute('tabindex')).toBeNull()
    }
    // The copy affordance is inside the popover, so it does not exist until an
    // operator deliberately opens it.
    expect(screen.queryByTestId('workstate-copy')).toBeNull()
  })

  it('never publishes pane identity, even with the detail popover open', async () => {
    // §9.5 still binds where it bites. The popover may copy DERIVED facts the
    // page already shows; it must never surface something that identifies or
    // actuates a worker. Asserted with the popover OPEN, because asserting it
    // closed proves nothing about the surface that was actually added.
    stubFetch(payload([annotation()]))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(1))

    fireEvent.click(screen.getByTestId('workstate-info-button'))
    const details = await waitFor(() => screen.getByTestId('workstate-details'))

    expect(facetPairs(details)['task']).toBe('p0-09b-r1')
    expect(screen.queryByTestId('workstate-copy')).not.toBeNull()
    expect(document.body.textContent).not.toContain(TERMINAL.pane_id)
    expect(document.body.textContent).not.toContain(TERMINAL.server_socket_path)
  })
})

describe('generation fence (cond-0054)', () => {
  it('drops an annotation written for a different generation of the same id', async () => {
    stubFetch(payload([
      annotation({ label: 'current' }),
      annotation({
        label: 'STALE-OBLIGATION',
        subject: { type: 'terminal', terminal_id: 'term-001', generation: 'an-older-generation' },
      }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBeGreaterThan(0))
    expect(document.body.textContent).not.toContain('STALE-OBLIGATION')
    // Dropped, not relocated: re-parenting it to the live row would be the
    // confidently wrong answer this fence exists to prevent.
    expect(screen.queryByTestId('campaign-annotations')).toBeTruthy()
    expect(screen.getByTestId('annotation-fenced').textContent).toContain('1 annotation')
  })

  it('does not attach a generation-less annotation to a live row', async () => {
    stubFetch(payload([
      annotation({ label: 'legacy', subject: { type: 'terminal', terminal_id: 'term-001' } }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(screen.queryByTestId('campaign-annotations')).toBeTruthy())
    expect(chips().length).toBe(1)
    const row = screen.getByTestId('campaign-annotation-row')
    expect(row.getAttribute('data-reason')).toBe('no-generation')
  })

  it('is pure and testable without a DOM', () => {
    const rows = [{ id: 'a', generation: 'g1' }]
    const placed = placeAnnotations(
      [
        annotation({ subject: { type: 'terminal', terminal_id: 'a', generation: 'g1' } }),
        annotation({ subject: { type: 'terminal', terminal_id: 'a', generation: 'g2' } }),
      ],
      rows,
    )
    expect(placed.byTerminal['a']).toHaveLength(1)
    expect(placed.fenced).toBe(1)
    expect(placed.unplaced).toHaveLength(0)
  })
})

describe('freshness (§9.6)', () => {
  it('greys a chip past its valid_until and says so in the hover', async () => {
    stubFetch(payload([annotation({ label: 'blocked', semantic_role: 'danger', valid_until: PAST })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    expect(chip.getAttribute('data-stale')).toBe('true')
    expect(chip.getAttribute('data-role')).toBe('neutral')
    // Staleness has a second, non-colour channel: a dashed outline. It is NOT
    // signalled by opacity — dimming the label put it under the contrast floor,
    // making the chip that most needs reading the hardest to read.
    expect(chip.className).toContain('border-dashed')
    expect(chip.className).not.toContain('opacity-')
    // A stale danger chip must not keep its alarming colour.
    expect(chip.className).not.toContain('bg-cao-danger')

    fireEvent.mouseEnter(chip)
    const card = await waitFor(() => screen.getByTestId('annotation-hovercard'))
    expect(card.textContent).toContain('stale since')
  })

  it('leaves a chip inside its validity window authoritative', async () => {
    stubFetch(payload([annotation({ semantic_role: 'warning', valid_until: FUTURE })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(chips()[0].getAttribute('data-stale')).toBe('false')
    expect(chips()[0].getAttribute('data-role')).toBe('warning')
  })

  it('claims "stale" only when it can prove expiry', () => {
    expect(isStale(null)).toBe(false)
    expect(isStale(undefined)).toBe(false)
    expect(isStale('not a date')).toBe(false)
    expect(isStale(PAST)).toBe(true)
  })

  it('separates "I know it expired" from "nobody said" — three states, not two', () => {
    // Folding absent/unparseable into `fresh` inverted the governing
    // principle: unknown freshness is not current freshness. A conductor that
    // died, or a producer version that never set the field, would otherwise
    // render an amber "waiting" in full colour forever.
    expect(freshness(FUTURE)).toBe('fresh')
    expect(freshness(PAST)).toBe('stale')
    expect(freshness(null)).toBe('unknown')
    expect(freshness(undefined)).toBe('unknown')
    expect(freshness('not a date')).toBe('unknown')
  })

  it('draws undeclared freshness as neutral and says so, never as authoritative', async () => {
    stubFetch(payload([annotation({ label: 'blocked', semantic_role: 'danger', valid_until: null })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    expect(chip.getAttribute('data-freshness')).toBe('unknown')
    // Not claimed as expired either — that would be a claim about a time
    // nobody published.
    expect(chip.getAttribute('data-stale')).toBe('false')
    expect(chip.getAttribute('data-role')).toBe('neutral')
    expect(chip.className).toContain('border-dashed')

    fireEvent.mouseEnter(chip)
    const card = await waitFor(() => screen.getByTestId('annotation-hovercard'))
    expect(card.textContent).toContain('freshness not declared')
    expect(card.textContent).not.toContain('stale since')
  })

  it('puts a visible staleness token on the chip face, not only in the hover', async () => {
    // The 390×844 touch viewport has no hover, so a `title`-only explanation of
    // why a chip is grey is unreachable exactly where it is needed.
    stubFetch(payload([annotation({ valid_until: PAST })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(screen.getByTestId('annotation-stale-note').textContent).toContain('stale')
  })
})

describe('unknown kinds and roles are ignored, never errors', () => {
  it('renders a kind this build has never heard of', async () => {
    stubFetch(payload([annotation({ kind: 'quantum-lease-reconciliation-2031', label: 'novel' })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(chips()[0].getAttribute('data-kind')).toBe('quantum-lease-reconciliation-2031')
    expect(chips()[0].textContent).toContain('novel')
  })

  it('degrades an unknown semantic role to neutral rather than dropping the chip', async () => {
    stubFetch(payload([annotation({ semantic_role: 'chartreuse', label: 'unshaded' })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(chips()[0].getAttribute('data-role')).toBe('neutral')
    expect(chips()[0].textContent).toContain('unshaded')
    expect(resolveRole('chartreuse')).toBe('neutral')
  })

  it('puts an unrecognised subject type on the campaign surface rather than dropping it', async () => {
    stubFetch(payload([
      annotation({ label: 'fleet-wide', subject: { type: 'fleet-2031' } }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(screen.queryByTestId('campaign-annotations')).toBeTruthy())
    const row = screen.getByTestId('campaign-annotation-row')
    expect(row.getAttribute('data-reason')).toBe('unknown-subject')
    expect(row.textContent).toContain('fleet-wide')
  })

  it('draws whatever identity an unrecognised subject brought with it', async () => {
    // Placement was already durable; IDENTITY was not. A chip reading
    // "workstream" and nothing else says something is wrong somewhere, which
    // is not an operator action on a 3-campaign fleet.
    stubFetch(payload([
      annotation({
        label: 'workstream stalled',
        subject: {
          type: 'workstream',
          task_id: 'tk-9',
          campaign: 'aegix',
          workstream_id: 'ws-a3',
        },
      }),
    ]))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(panel.textContent).toContain('campaign aegix')
    expect(panel.textContent).toContain('task tk-9')
    // Including the identifier this build has never heard of.
    expect(panel.textContent).toContain('workstream id ws-a3')
  })
})

describe('campaign-scoped subjects render somewhere visible', () => {
  it('renders an unbound human gate that names no terminal', async () => {
    stubFetch(payload([
      annotation({
        kind: 'gate.pending',
        label: 'gate pending',
        semantic_role: 'warning',
        subject: { type: 'campaign', campaign: 'aegix-mobile-phase0-renewal' },
        details: { dependencies: 'human-gate p0-09b-pr17-merge-approval' },
      }),
    ]))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(within(panel).getByTestId('annotation-chip').textContent).toContain('gate pending')
    expect(panel.textContent).toContain('campaign aegix-mobile-phase0-renewal')
    // Facets are visible text here, not only a hover: the 390×844 touch
    // viewport has no hover.
    expect(panel.textContent).toContain('dependencies: human-gate p0-09b-pr17-merge-approval')
  })

  it('renders an orphaned run whose terminal has no row', async () => {
    stubFetch(payload([
      annotation({
        label: 'orphaned',
        subject: { type: 'terminal', terminal_id: 'gone-9999', generation: 'g-old' },
      }),
    ]))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(within(panel).getByTestId('campaign-annotation-row').getAttribute('data-reason')).toBe(
      'orphaned-terminal',
    )
  })

  it('renders a task subject with no terminal binding', async () => {
    stubFetch(payload([
      annotation({ label: 'planned', subject: { type: 'task', task_id: 'p0-10-r1' } }),
    ]))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(panel.textContent).toContain('task p0-10-r1')
  })
})

describe('degradation', () => {
  it('renders nothing when the route is absent, exactly as before it existed', async () => {
    stubFetch(undefined)
    await renderDashboard()

    expect(chips()).toHaveLength(0)
    expect(screen.queryByTestId('campaign-annotations')).toBeNull()
  })

  it.each([
    ['a null body', null],
    ['a bare array', []],
    ['a string', 'nope'],
    ['annotations as an object', { annotations: {} }],
    ['items missing required fields', { annotations: [{ kind: 'x' }, { label: 5 }] }],
  ])('degrades safely on %s', async (_name, body) => {
    stubFetch(body)
    await renderDashboard()

    expect(chips()).toHaveLength(0)
    expect(screen.queryByTestId('campaign-annotations')).toBeNull()
  })

  it('keeps the good items when a payload mixes valid and invalid ones', () => {
    const parsed = readAnnotations({
      annotations: [annotation({ label: 'good' }), { kind: 'broken' }, null, 42],
    })
    expect(parsed.annotations.map(a => a.label)).toEqual(['good'])
  })

  it('shows a partial-data marker rather than pretending coverage is complete', async () => {
    stubFetch(payload([annotation({ subject: { type: 'campaign', campaign: 'c' } })], {
      coverage: 'partial',
      sources_failed: 1,
      reasons: [{ source: 'broken-campaign', reason: 'malformed' }],
    }))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(within(panel).getByTestId('annotation-degraded')).toBeTruthy()
  })
})

describe('bounded rendering', () => {
  it('truncates a long chip row visibly, never silently', async () => {
    const many = Array.from({ length: 7 }, (_, i) =>
      annotation({ label: `chip-${i}`, priority: 90 - i }),
    )
    stubFetch(payload(many))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(3))
    // The three highest priorities survive, and the remainder is stated.
    expect(chips().map(c => c.textContent)).toEqual(['chip-0', 'chip-1', 'chip-2'])
    // Self-describing, because on a phone the `title` that explained it is
    // unreachable and "+4" alone does not say four of what.
    expect(screen.getByTestId('annotation-overflow').textContent).toBe('+4 more')
  })

  it('caps the campaign surface so it can never outrank the fleet', async () => {
    // The panel everything unplaceable falls into sits ABOVE Active Sessions.
    // Uncapped, 60 unplaced annotations measured 2936px — 4.2 phone screens of
    // gate rows before the operator sees a single worker.
    const many = Array.from({ length: 60 }, (_, i) =>
      annotation({
        label: `gate-${i}`,
        priority: 90 - i,
        subject: { type: 'campaign', campaign: `campaign-${i}` },
      }),
    )
    stubFetch(payload(many))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(within(panel).getAllByTestId('campaign-annotation-row')).toHaveLength(8)
    expect(within(panel).getByTestId('campaign-annotation-overflow').textContent).toContain('+52')
    // Highest priority first, so the cap keeps the ones worth seeing.
    expect(within(panel).getAllByTestId('campaign-annotation-row')[0].textContent).toContain('gate-0')
  })

  it('puts a fresh annotation ahead of an expired one at the row cap', async () => {
    // Staleness used to be applied at draw time — AFTER the cap had chosen who
    // gets drawn — so three expired p99 chips took all three slots and the live
    // danger was the thing behind the "+1".
    stubFetch(payload([
      annotation({ label: 'stale-A', priority: 99, valid_until: PAST }),
      annotation({ label: 'stale-B', priority: 98, valid_until: PAST }),
      annotation({ label: 'stale-C', priority: 97, valid_until: PAST }),
      annotation({ label: 'live-danger', priority: 10, semantic_role: 'danger', valid_until: FUTURE }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(3))
    const labels = chips().map(c => c.textContent)
    expect(labels.some(t => t?.includes('live-danger'))).toBe(true)
    expect(labels.some(t => t?.includes('stale-C'))).toBe(false)
    expect(screen.getByTestId('annotation-overflow').textContent).toBe('+1 more')
  })

  it('states the server-side omission count on the campaign surface', async () => {
    stubFetch(payload([], { coverage: 'truncated', items_omitted: 25 }))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    expect(within(panel).getByTestId('annotation-omitted').textContent).toContain('25')
  })
})

describe('the renderer holds no conductor vocabulary either', () => {
  // The Python service has an AST guard that forbids eight conductor terms and
  // any KNOWN_KINDS-style constant. It parses `annotations.__file__` and
  // NOTHING ELSE — so the mechanism that is supposed to keep this the last
  // fork change was enforced on the module with no vocabulary and unenforced
  // on the two that had some. `ageSource` allowlisted `parked_at`/`since` and
  // FACET_ORDER listed `parked_at`, `parked_for`, `lifecycle` and `round`;
  // every one of those would have failed the Python guard verbatim. This is
  // that guard, mirrored, over the modules most likely to acquire
  // `if (annotation.kind === 'human-gate')`.
  //
  // WIDENED, AND THE WIDENING IS THE POINT. The popover was the one module with
  // a real appetite for vocabulary and no guard over it: `if (key ===
  // 'track_id')` is the natural way to write a grouped detail card, and nothing
  // would have stopped it. The colour module is listed for the same reason —
  // it is where a palette keyed by a lane name would go. Both pass every check
  // as written, so covering them costs nothing today and is the whole cost of
  // the guard's value the day somebody reaches for the shortcut.
  //
  // AN EXPLICIT LIST, NOT `import.meta.glob('src/lib/*.ts')`. A glob sweeps in
  // unrelated modules that may legitimately contain one of these substrings and
  // turns a guard into a CI hazard; the list is four entries and the non-vacuity
  // control below is what keeps it honest.
  const MODULES: Array<[string, string]> = [
    ['src/lib/annotations.ts', annotationsLibSource],
    ['src/lib/identityColour.ts', identityColourSource],
    ['src/components/AnnotationChips.tsx', annotationChipsSource],
    ['src/components/AnnotationDetails.tsx', annotationDetailsSource],
    // The filter module and its bar: the two places a `phase` dropdown with
    // hard-coded values would naturally be written, and therefore the two the
    // guard must reach. Their derived-dimension code is where "no allowlist,
    // ever" is actually load-bearing.
    ['src/lib/filters.ts', filtersLibSource],
    ['src/components/FilterBar.tsx', filterBarSource],
    // Extracted from AnnotationDetails when the chip bar needed the same
    // anchored popover; listed because every new module joins the guard, and
    // a shared overlay primitive is exactly where a future "position the lane
    // chip's card differently" shortcut would try to live.
    ['src/components/FloatingCard.tsx', floatingCardSource],
  ]

  // The eight terms the Python guard forbids, plus the vocabulary this
  // feature's producer uses.
  //
  // ONLY UNAMBIGUOUS TOKENS. Matching is `String.includes`, so the obvious
  // additions are traps: `lane` is inside `plane`, and `base` is inside
  // `basis`, `database` and `basename` — all three of which are words this
  // renderer has legitimate reason to use. A term that false-positives must be
  // NARROWED rather than dropped.
  const CONDUCTOR_TERMS = [
    'work-state',
    'work_item',
    'human-gate',
    'route-breaker',
    'parked',
    'in-round',
    'finalized',
    'supervisor',
    'track_id',
    'lane_source',
    'lane_scope',
    'cross-lane',
    'task-prefix',
    'worktree',
    'base_branch',
    'commit_short',
    'vcs',
  ]

  /** Comments stripped — they explain the design, exactly as Python docstrings
   *  are exempt from the guard on the other side of the seam, and the design is
   *  allowed to name what it refuses to encode. */
  function stripComments(text: string): string {
    return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '')
  }

  it.each(MODULES)('%s names no conductor term outside its comments', (_name, text) => {
    const source = stripComments(text)
    const offenders = CONDUCTOR_TERMS.filter(term => source.includes(term))
    expect(offenders).toEqual([])
  })

  it.each(MODULES)('%s branches on no kind and keeps no kind allowlist', (_name, text) => {
    const source = stripComments(text)
    expect(source).not.toMatch(/kind\s*[=!]==/)
    expect(source).not.toMatch(/switch\s*\([^)]*kind[^)]*\)/)
    for (const forbidden of [
      'KNOWN_KINDS',
      'SUPPORTED_KINDS',
      'FACET_ORDER',
      'SUBJECT_TYPES',
      // A table of scope names, a table of provenance classes, or a table of
      // identity tokens are the three shapes this feature could plausibly
      // acquire. Each would move a decision the producer owns onto this side.
      'LANE_SCOPES',
      'PROVENANCE_CLASSES',
      'COLOUR_KEYS',
    ]) {
      expect(source).not.toContain(forbidden)
    }
  })

  it('the guard is not vacuous: it catches a term reintroduced in code', () => {
    // The Python guard's whole value is that it fails when somebody adds
    // `if (annotation.kind === 'human-gate')`. Proving the mirror can fail is
    // what stops it from being a green test over an empty assertion.
    const planted = stripComments("const x = 'human-gate'\nif (a.kind === 'x') {}")
    expect(CONDUCTOR_TERMS.filter(term => planted.includes(term))).toEqual(['human-gate'])
    expect(planted).toMatch(/kind\s*[=!]==/)
  })

  it('the widened guard catches this feature\'s vocabulary, in every covered module', () => {
    // The NON-VACUITY CONTROL FOR THE WIDENING. The four terms below are the
    // ones a grouped detail card and a colour module would actually reach for,
    // and every one of them is a NEW entry — a suite that only ever proved the
    // original eight could fail could be reverted to two modules and eight
    // terms without a single test going red.
    for (const planted of [
      "if (key === 'track_id') return 'Lane'",
      "const PROVENANCE_CLASSES = ['assigned', 'launch']",
      "if (facet.startsWith('lane_scope')) {}",
      "const label = a.details['commit_short']",
    ]) {
      const source = stripComments(planted)
      const offenders = CONDUCTOR_TERMS.filter(term => source.includes(term))
      const constants = ['PROVENANCE_CLASSES', 'LANE_SCOPES', 'COLOUR_KEYS'].filter(c =>
        source.includes(c),
      )
      expect(
        offenders.length + constants.length,
        `the guard would not have caught: ${planted}`,
      ).toBeGreaterThan(0)
    }
    // And the modules the widening ADDED are genuinely in the list, so planting
    // a term in either of them reaches an assertion.
    expect(MODULES.map(([name]) => name)).toContain('src/components/AnnotationDetails.tsx')
    expect(MODULES.map(([name]) => name)).toContain('src/lib/identityColour.ts')
  })
})

describe('the chip age is derived, not allowlisted', () => {
  it('shows an age for a renamed timestamp facet the fork has never seen', async () => {
    // `ageSource` was `details.parked_at || details.since`. Renaming one facet
    // key on the conductor side silently deleted the chip's headline age —
    // no marker, no fallback, no signal — which is precisely the coupling the
    // seam exists to remove.
    const at = new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString()
    stubFetch(payload([annotation({ details: { task: 't', waiting_since_utc: at } })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    expect(chips()[0].textContent).toMatch(/1d/)
  })

  it('does not mistake a round number for a date', () => {
    // `new Date('12')` is a valid Date in 2001, so "does it parse?" alone
    // would relativise a round counter into a confidently wrong age.
    expect(ageSource(annotation({ details: { round: '12', task: 'p0-09b' } }))).toBeNull()
  })

  it('ignores a future timestamp, which is not an age', () => {
    expect(ageSource(annotation({ details: { due: '2999-01-01T00:00:00Z' } }))).toBeNull()
  })

  it('reads facets in the order the producer wrote them, with no ranking table', () => {
    const ordered = orderedFacets({ zeta: '1', alpha: '2', mid: '3' })
    expect(ordered.map(([k]) => k)).toEqual(['zeta', 'alpha', 'mid'])
  })
})

describe('placement waits for the fleet before calling anything orphaned', () => {
  it('holds terminal-scoped annotations back until the row set is known', () => {
    // /annotations and the session details are independent effects and the
    // session pass is a sequential loop, so the annotations routinely land
    // first, against an empty row set. Classifying then announced every live
    // worker as an "orphaned run" on every load and every refresh.
    const held = placeAnnotations(
      [
        annotation({ subject: { type: 'terminal', terminal_id: 'term-001', generation: GENERATION } }),
        annotation({ label: 'gate', subject: { type: 'campaign', campaign: 'c' } }),
      ],
      [],
      false,
    )
    expect(held.pending).toBe(1)
    expect(held.unplaced.map(u => u.reason)).toEqual(['campaign'])
    expect(held.fenced).toBe(0)
  })

  it('classifies normally once the rows are in', () => {
    const placed = placeAnnotations(
      [annotation({ subject: { type: 'terminal', terminal_id: 'nope', generation: 'g' } })],
      [{ id: 'term-001', generation: GENERATION }],
      true,
    )
    expect(placed.pending).toBe(0)
    expect(placed.unplaced.map(u => u.reason)).toEqual(['orphaned-terminal'])
  })

  it('never flashes an orphaned-run claim while the session list is loading', async () => {
    // The DOM-level version of the same defect: gate /sessions behind a promise
    // and let /annotations land first. Nothing renders in that window — a
    // loading panel would be its own flash — and the chip attaches to the row
    // once the fleet is known.
    let releaseSessions: () => void = () => {}
    const sessionsGate = new Promise<void>(resolve => { releaseSessions = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url === '/sessions/cao-fleet') {
        await sessionsGate
        return jsonResponse({ session: SESSION, terminals: [TERMINAL] })
      }
      if (url === '/annotations') return jsonResponse(payload([annotation()]))
      if (url === `/terminals/${TERMINAL.id}`) {
        return jsonResponse({ ...TERMINAL, name: TERMINAL.id, session_name: 'cao-fleet' })
      }
      if (url === '/agents/profiles') return jsonResponse([])
      return jsonResponse({})
    }))

    render(<DashboardHome onNavigate={() => {}} />)
    // Give the annotation fetch every chance to land and render first — the
    // session list is still parked on its gate throughout this window.
    await new Promise(r => setTimeout(r, 100))
    expect(screen.queryByText('cao-fleet')).toBeNull()
    expect(document.body.textContent).not.toContain('orphaned run')
    expect(screen.queryByTestId('campaign-annotations')).toBeNull()

    releaseSessions()
    await screen.findByText('cao-fleet')
    await waitFor(() => expect(chips().length).toBe(1))
    expect(screen.queryByTestId('campaign-annotations')).toBeNull()
  })

  it('states the held-back count when the panel is already open for another reason', async () => {
    // Not a panel of its own — that would be a second flash — but if the
    // surface is up anyway, "N waiting for the fleet to load" beats a silent
    // gap where chips are about to appear.
    const held = placeAnnotations(
      [
        annotation({ subject: { type: 'terminal', terminal_id: 'term-001', generation: GENERATION } }),
        annotation({ label: 'gate', subject: { type: 'campaign', campaign: 'c' } }),
      ],
      [],
      false,
    )
    render(
      <CampaignAnnotations
        unplaced={held.unplaced}
        fenced={held.fenced}
        pending={held.pending}
        omitted={0}
        degraded={false}
      />,
    )
    expect(screen.getByTestId('annotation-pending').textContent).toContain('1 annotation')
  })
})

describe('a row that publishes no generation is unfenceable, not superseded', () => {
  it('keeps the annotation visible and says the row is the reason', () => {
    const placed = placeAnnotations(
      [annotation({ subject: { type: 'terminal', terminal_id: 'a', generation: 'g1' } })],
      [{ id: 'a', generation: null }],
    )
    // Counting it as a fence drop would blame the annotation for a field the
    // ROW is missing, and delete it from the surface entirely.
    expect(placed.fenced).toBe(0)
    expect(placed.unplaced.map(u => u.reason)).toEqual(['unfenceable-row'])
  })
})

describe('one failed poll does not blank the surface', () => {
  it('keeps the last payload and marks it unverified', async () => {
    let fail = false
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url === '/sessions/cao-fleet') return jsonResponse({ session: SESSION, terminals: [TERMINAL] })
      if (url === '/annotations') {
        if (fail) throw new Error('network blip')
        return jsonResponse(payload([annotation()]))
      }
      if (url === `/terminals/${TERMINAL.id}`) {
        return jsonResponse({ ...TERMINAL, name: TERMINAL.id, session_name: 'cao-fleet' })
      }
      if (url === '/agents/profiles') return jsonResponse([])
      return jsonResponse({})
    }))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(1))

    fail = true
    await new Promise(r => setTimeout(r, 5100))
    // Still drawn — a blip is not evidence the fleet changed — but the surface
    // now says the data is unverified rather than pretending it was re-checked.
    await waitFor(() => expect(screen.queryByTestId('annotation-degraded')).toBeTruthy())
    expect(chips().length).toBe(1)
  }, 15000)
})

describe('the no-annotations control is byte-identical to today', () => {
  it('produces the same DOM with an empty payload as with no route at all', async () => {
    stubFetch(payload([]))
    const withEmpty = await renderDashboard()
    const emptyHtml = withEmpty.container.innerHTML
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })

    stubFetch(undefined)
    const withoutRoute = await renderDashboard()
    expect(withoutRoute.container.innerHTML).toBe(emptyHtml)
  })

  it('adds no wrapper element to a terminal row that has no annotations', async () => {
    stubFetch(payload([annotation({ subject: { type: 'campaign', campaign: 'c' } })]))
    await renderDashboard()
    await screen.findByTestId('campaign-annotations')

    // The chip lives on the campaign surface; the terminal row is untouched —
    // no empty container, no stray separator.
    const region = document.getElementById('session-cao-fleet-terminals')!
    expect(within(region).queryAllByTestId('annotation-chip')).toHaveLength(0)
    // THE WRAPPER ITSELF, which is what this test is named for and did not
    // previously assert. An empty group span is invisible but not free: it is
    // a flex item, so it consumes a `gap` on every row of a fleet with no
    // conductor — the one case that must stay byte-identical.
    expect(within(region).queryAllByTestId('annotation-group')).toHaveLength(0)
  })
})

// ── Identity chips (lane / VCS design §5) ─────────────────────────────────
//
// The seam is one opaque field. Everything below asserts that the renderer
// branches on that field's PRESENCE AND EMPTINESS and on nothing else — not on
// `kind`, not on a facet name, not on a subject type — because the moment it
// does, the "this needs no fork change" promise is gone and only a test can
// tell you.

/** An identity chip: the only difference from `annotation()` is the opaque key. */
function identity(overrides: Partial<Annotation> = {}): Annotation {
  return annotation({
    kind: 'lane',
    label: 'pr04',
    semantic_role: 'neutral',
    priority: 8,
    colour_key: '9f2c41a7be03d5e8',
    details: {},
    ...overrides,
  })
}

function identityChips(): HTMLElement[] {
  return chips().filter(c => c.getAttribute('data-identity') === 'true')
}

function severityChips(): HTMLElement[] {
  return chips().filter(c => !c.hasAttribute('data-identity'))
}

describe('an identity chip never competes with a severity chip for the row cap', () => {
  it('draws three severity chips and both identity chips from a row of six', async () => {
    // ONE SHARED CAP WOULD DROP BOTH IDENTITY CHIPS HERE. The row would say
    // nothing about where the worker is, and the operator would have no signal
    // that anything had been withheld beyond a "+3" that means something else.
    stubFetch(payload([
      annotation({ label: 'sev-A', priority: 99 }),
      annotation({ label: 'sev-B', priority: 98 }),
      annotation({ label: 'sev-C', priority: 97 }),
      annotation({ label: 'sev-D', priority: 96 }),
      identity({ label: 'pr04', colour_key: 'lane-alpha' }),
      identity({ label: 'agent/payment', kind: 'vcs', colour_key: 'wt-alpha' }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(5))
    expect(severityChips().map(c => c.textContent)).toHaveLength(3)
    expect(identityChips().map(c => c.textContent?.slice(0, 20))).toHaveLength(2)
    expect(screen.getByTestId('annotation-overflow').textContent).toBe('+1 more')
    expect(screen.queryByTestId('annotation-identity-overflow')).toBeNull()

    // DOM ORDER: severity, then the severity overflow marker, then identity.
    // The marker belongs to the group it counts; putting the identity chips
    // before it would make "+1 more" read as covering them too.
    const group = screen.getByTestId('annotation-group')
    const order = Array.from(group.children)
    const markerAt = order.indexOf(screen.getByTestId('annotation-overflow'))
    for (const chip of identityChips()) {
      expect(order.indexOf(chip), 'identity chip must follow the severity marker')
        .toBeGreaterThan(markerAt)
    }
  })

  it('caps the identity group on its own and counts what it withheld', async () => {
    stubFetch(payload([
      annotation({ label: 'live-danger', semantic_role: 'danger', priority: 10 }),
      identity({ label: 'pr04', colour_key: 'lane-alpha', priority: 8 }),
      identity({ label: 'agent/x', kind: 'vcs', colour_key: 'wt-alpha', priority: 7 }),
      identity({ label: 'extra', kind: 'vcs', colour_key: 'wt-beta', priority: 6 }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(3))
    expect(severityChips()).toHaveLength(1)
    expect(identityChips()).toHaveLength(2)
    // The live danger is DRAWN, not behind a marker — the regression a single
    // shared cap reintroduces.
    expect(severityChips()[0].textContent).toContain('live-danger')
    expect(screen.queryByTestId('annotation-overflow')).toBeNull()
    expect(screen.getByTestId('annotation-identity-overflow').textContent).toBe('+1')
  })
})

describe('the identity token has three states and the renderer draws all three', () => {
  it('draws a non-empty token coloured, by the token and nothing else', async () => {
    stubFetch(payload([identity({ colour_key: 'a7f3' })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    expect(chip.getAttribute('data-identity')).toBe('true')
    expect(chip.getAttribute('data-colour')).toBe(String(paletteIndex('a7f3')))
    expect(chip.style.backgroundColor).toBe(rgba(IDENTITY_PALETTE[paletteIndex('a7f3')], 0.1))
    // THE TOKEN ITSELF IS NEVER ECHOED INTO THE DOM. It is a hash of whatever
    // identity the producer chose, on an unauthenticated page (§9.5); the
    // bucket index is all a reader needs and leaks under four bits.
    expect(document.body.innerHTML).not.toContain('a7f3')
  })

  it('draws an empty token outlined and grey, not as a severity chip', async () => {
    // THE STATE `!!colour_key` DELETES. An empty token means "an identity chip
    // with no colour"; truthiness folds it into the severity group, where it
    // would be drawn in a role colour that means severity.
    stubFetch(payload([identity({ label: 'campaign', colour_key: '' })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    expect(chip.getAttribute('data-identity')).toBe('true')
    expect(chip.getAttribute('data-colour')).toBe('none')
    expect(chip.style.backgroundColor).toBe('')
    expect(chip.className).toContain('border-gray-500')
    // Dashed already means stale. An uncoloured chip is not a stale one.
    expect(chip.className).not.toContain('border-dashed')
    expect(chip.textContent).toContain('campaign')
  })

  it('draws an absent token as the severity chip it has always been', async () => {
    stubFetch(payload([annotation({ label: 'waiting', semantic_role: 'warning' })]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    expect(chip.hasAttribute('data-identity')).toBe(false)
    expect(chip.getAttribute('data-role')).toBe('warning')
    expect(chip.style.backgroundColor).toBe('')
  })

  it('is decided by the token alone, not by kind, role or subject type', async () => {
    // The seam's headline promise, applied to this feature. A kind invented in
    // 2031, a role the palette has never heard of and a subject type with no
    // renderer — the chip is still an identity chip, because the ONE field that
    // decides is present.
    const exotic = {
      kind: 'quantum-lease-reconciliation-2031',
      semantic_role: 'chartreuse',
      subject: { type: 'fleet', campaign: 'aegix' },
      label: 'somewhere',
    }
    stubFetch(payload([identity({ ...exotic, colour_key: 'lane-beta' })]))
    await renderDashboard()

    const panel = await screen.findByTestId('campaign-annotations')
    const chip = within(panel).getByTestId('annotation-chip')
    expect(chip.getAttribute('data-identity')).toBe('true')
    expect(chip.getAttribute('data-colour')).toBe(String(paletteIndex('lane-beta')))
    expect(chip.style.backgroundColor).not.toBe('')

    // Remove ONLY the token and the very same annotation is an ordinary chip,
    // resolved to `neutral` because `chartreuse` is not a role.
    cleanup()
    vi.unstubAllGlobals()
    useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
    stubFetch(payload([annotation({ ...exotic })]))
    await renderDashboard()
    const plain = within(await screen.findByTestId('campaign-annotations')).getByTestId('annotation-chip')
    expect(plain.hasAttribute('data-identity')).toBe(false)
    expect(plain.getAttribute('data-role')).toBe('neutral')
  })
})

describe('colour is redundant: the text always carries the fact', () => {
  it('draws the label and the facets as text for coloured, uncoloured and stale chips', async () => {
    const branch = 'agent/payment-pr04-foundation-r3'
    stubFetch(payload([
      identity({ label: 'pr04', colour_key: 'lane-alpha', details: { 'assigned.lane': 'pr04' } }),
      identity({
        label: 'campaign',
        colour_key: '',
        details: { 'assigned.scope': 'cross-lane' },
      }),
      identity({
        label: branch,
        kind: 'vcs',
        colour_key: 'wt-alpha',
        valid_until: PAST,
        details: { 'observed.branch': branch },
      }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(identityChips().length).toBe(2))
    // Two are drawn, the third is behind the identity marker — so assert over
    // whatever IS drawn rather than assuming which two.
    for (const chip of identityChips()) {
      const label = chip.querySelector('span:nth-child(2)')!
      expect(label.textContent, 'the chip must draw its own label as text').toBeTruthy()
      expect(chip.getAttribute('aria-label')).toContain(label.textContent)
    }
  })

  it('carries the full untruncated value on the line the phone reads, with the label clamped', async () => {
    const branch = 'agent/payment-pr04-foundation-and-a-very-long-tail'
    stubFetch(payload([
      identity({
        label: branch,
        kind: 'vcs',
        colour_key: 'wt-alpha',
        details: { 'observed.branch': branch, 'launch.repo': 'dnd-scheduler' },
      }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    const label = chip.querySelector('span:nth-child(2)')!
    // Clamped on the chip...
    expect(label.className).toContain('truncate')
    expect(label.className).toContain('max-w-[16ch]')
    expect(label.className).toContain('sm:max-w-[30ch]')
    // ...and complete on the line that exists only where the clamp bites, and
    // in the accessible name, which is never clamped at all.
    const group = screen.getByTestId('annotation-group')
    const line = Array.from(group.children).find(
      el => el.className.includes('sm:hidden') && el.textContent?.includes(branch),
    )
    expect(line, 'the phone facet line must carry the full value').toBeTruthy()
    expect(chip.getAttribute('aria-label')).toContain(branch)
  })

  it('draws no severity word in an identity chip\'s own hover card', async () => {
    // THE `data-role` OMISSION AND THIS ARE ONE RULE. The identity chip
    // withholds `data-role` so that nothing reads severity off a chip that is
    // not making a severity claim; the hover card is that chip's own
    // statement, and printing `resolveRole(...)` in it said the quiet part out
    // loud. A severity chip still shows it, and the info popover — which
    // promises the whole envelope and labels it — still carries it for both.
    stubFetch(payload([
      identity({ label: 'pr04', colour_key: 'lane-alpha', semantic_role: 'danger' }),
      annotation({ label: 'blocked', semantic_role: 'danger', valid_until: FUTURE }),
    ]))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(2))

    const identityChip = chips().find(c => c.getAttribute('data-identity') === 'true')!
    fireEvent.mouseEnter(identityChip)
    let card = await waitFor(() => screen.getByTestId('annotation-hovercard'))
    expect(card.querySelector('[data-testid="hovercard-role"]')).toBeNull()
    expect(card.textContent).not.toContain('danger')
    fireEvent.mouseLeave(identityChip)

    const severityChip = chips().find(c => c.getAttribute('data-identity') !== 'true')!
    fireEvent.mouseEnter(severityChip)
    // Waited for by the ROLE element rather than by the card: the identity
    // card is still inside its close grace here, so a bare card query would
    // resolve against the one that correctly has no role in it.
    await waitFor(() => expect(screen.getByTestId('hovercard-role').textContent).toBe('danger'))
  })

  it('emits exactly one phone facet line per chip it drew, and no more', async () => {
    // ROW DENSITY AT 390px, PINNED. Every shown chip emits its own
    // `basis-full` facet line, so the row's height is linear in the number of
    // chips drawn — with three severity chips and two identity chips that is
    // five extra wrapped lines under one row, and nothing measured the
    // fan-out. This does not judge the shape; it makes a change to it visible
    // instead of silent, and it is the reason `MAX_FACET_LINE` and the two
    // chip caps exist.
    stubFetch(payload([
      annotation({ label: 'one', details: { a: '1' }, priority: 90, valid_until: FUTURE }),
      annotation({ label: 'two', details: { b: '2' }, priority: 80, valid_until: FUTURE }),
      annotation({ label: 'three', details: { c: '3' }, priority: 70, valid_until: FUTURE }),
      annotation({ label: 'four', details: { d: '4' }, priority: 60, valid_until: FUTURE }),
      identity({ label: 'pr04', colour_key: 'lane-alpha', details: { e: '5' } }),
      identity({ label: 'feat/x', kind: 'vcs', colour_key: 'wt-alpha', details: { f: '6' } }),
      identity({ label: 'extra', kind: 'vcs', colour_key: 'wt-beta', details: { g: '7' } }),
    ]))
    await renderDashboard()

    const group = await waitFor(() => screen.getByTestId('annotation-group'))
    const drawn = chips().length
    const lines = Array.from(group.children).filter(el =>
      el.className.includes('sm:hidden'),
    )
    expect(drawn).toBe(MAX_ROW_CHIPS + MAX_IDENTITY_CHIPS)
    expect(lines).toHaveLength(drawn)
    // Every one of them is hidden from `sm` up, where the hover exists — so
    // the density cost is the phone's alone.
    for (const line of lines) expect(line.className).toContain('basis-full')
  })

  it('caps a long facet line and says how many it withheld', async () => {
    stubFetch(payload([
      identity({
        kind: 'vcs',
        colour_key: 'wt-alpha',
        details: { a: '1', b: '2', c: '3', d: '4', e: '5', f: '6' },
      }),
    ]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const group = screen.getByTestId('annotation-group')
    const line = Array.from(group.children).find(el => el.className.includes('sm:hidden'))!
    expect(line.textContent).toContain('a: 1')
    expect(line.textContent).toContain('d: 4')
    expect(line.textContent).not.toContain('e: 5')
    expect(line.textContent).toContain('+2')
    // The accessible name still promises completeness.
    expect(chips()[0].getAttribute('aria-label')).toContain('f: 6')
  })
})

describe('the popover groups the row by the producer\'s own provenance classes', () => {
  // Priorities as the producer actually publishes them. THE ORDER OF THE
  // BLOCKS IS NOT A RULE THIS SIDE HOLDS: the chips are sorted by the existing
  // `(stale, priority desc, label)` comparator, and first-appearance ordering
  // then falls out of it. That is exactly why the producer can put the lane
  // block first by setting a priority, with no new field and no change here.
  const dossier = [
    identity({
      label: 'pr04',
      priority: 8,
      colour_key: 'lane-alpha',
      details: {
        'assigned.assigned_at': '2026-07-31T18:04:11Z',
        'assigned.role': 'implementer',
      },
    }),
    identity({
      label: 'agent/payment',
      kind: 'vcs',
      priority: 7,
      colour_key: 'wt-alpha',
      details: {
        'observed.commit': '0e63aac5f65421ff481a7186b3f2e8de5030fd52',
        'launch.repo': 'dnd-scheduler',
      },
    }),
    annotation({ label: 'waiting', details: { task: 'p0-09b-r1', role: 'implementer' } }),
  ]

  async function openPopover(items: Annotation[]) {
    stubFetch(payload(items))
    await renderDashboard()
    fireEvent.click(await screen.findByTestId('workstate-info-button'))
    return screen.getByTestId('workstate-details')
  }

  it('labels one block per class, in first-appearance order, with full-length values', async () => {
    const card = await openPopover(dossier)
    const worker = within(card).getByTestId('workstate-worker')
    const groups = within(card).getAllByTestId('workstate-worker-group')

    expect(groups.map(g => g.getAttribute('data-group'))).toEqual(['assigned', 'observed', 'launch'])
    expect(worker.textContent).toContain('assigned')
    expect(worker.textContent).toContain('observed')
    expect(worker.textContent).toContain('launch')
    // The full 40-character sha, untruncated and selectable.
    expect(worker.textContent).toContain('0e63aac5f65421ff481a7186b3f2e8de5030fd52')
    expect(card.querySelector('.select-text')).toBeTruthy()
  })

  it('draws a classed facet ONCE, in its class block and not in its annotation', async () => {
    const card = await openPopover(dossier)
    const sections = within(card).getAllByTestId('workstate-annotation')
    for (const section of sections) {
      expect(Object.keys(facetPairs(section))).not.toContain('launch repo')
      expect(Object.keys(facetPairs(section))).not.toContain('assigned role')
    }
    // The UNCLASSED facets are untouched: they render exactly where they always
    // did, on the annotation that published them.
    const plain = sections.find(s => s.textContent?.includes('waiting'))!
    expect(facetPairs(plain)).toMatchObject({ task: 'p0-09b-r1', role: 'implementer' })
  })

  it('renames its headings when the producer renames its classes', async () => {
    // THE PROOF THAT NO CLASS NAME IS HARDCODED. Same shapes, different words.
    const card = await openPopover([
      identity({ priority: 8, colour_key: 'lane-alpha', details: { 'A.one': '1' } }),
      identity({
        kind: 'vcs',
        priority: 7,
        colour_key: 'wt-alpha',
        details: { 'B.two': '2', 'C.three': '3' },
      }),
    ])
    const groups = within(card).getAllByTestId('workstate-worker-group')
    expect(groups.map(g => g.getAttribute('data-group'))).toEqual(['A', 'B', 'C'])
    expect(groups.map(g => g.querySelector('p')!.textContent)).toEqual(['A', 'B', 'C'])
  })

  it('renders no Worker block at all when nothing on the row is classed', async () => {
    const card = await openPopover([annotation({ details: { task: 't', role: 'r' } })])
    expect(within(card).queryByTestId('workstate-worker')).toBeNull()
    expect(facetPairs(card)).toMatchObject({ task: 't', role: 'r' })
  })

  it('puts the Worker block first in the copied text, and says each fact once', async () => {
    const text = detailsToText(dossier, 'Work state — x')
    expect(text.indexOf('Worker')).toBeLessThan(text.indexOf('Annotations'))
    expect(text).toContain('    role: implementer')
    expect(text).toContain('0e63aac5f65421ff481a7186b3f2e8de5030fd52')
    // `assigned.role` appears in the Worker block; the work-item chip's own
    // unclassed `role` facet is a different fact and still appears under it.
    expect(text.match(/^\s+role: implementer$/gm)).toHaveLength(2)
  })

  it('leaves the copied text byte-identical when the row carries no classes', async () => {
    // The compatibility floor: every document published before this field
    // existed must copy exactly as it did.
    const plain = [annotation({ label: 'waiting', details: { task: 't' } })]
    const text = detailsToText(plain, 'Work state — x')
    expect(text.startsWith('Work state — x\n==============\nwaiting')).toBe(true)
    expect(text).not.toContain('Worker')
  })
})
