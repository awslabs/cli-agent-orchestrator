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
import annotationChipsSource from '../components/AnnotationChips.tsx?raw'
import { render, cleanup, screen, waitFor, within } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { CampaignAnnotations } from '../components/AnnotationChips'
import { useStore } from '../store'
import { projectedTerminal } from './projectedTerminal'
import {
  ageSource,
  freshness,
  isStale,
  orderedFacets,
  placeAnnotations,
  readAnnotations,
  resolveRole,
} from '../lib/annotations'
import type { Annotation, AnnotationsResponse } from '../api'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

const SESSION = { id: 'sess-1', name: 'cao-fleet', status: 'active' }
const GENERATION = 'term-001-gen-1'
const TERMINAL = projectedTerminal({ id: 'term-001', generation: GENERATION, status: 'idle' })

const FUTURE = '2999-01-01T00:00:00Z'
const PAST = '2020-01-01T00:00:00Z'

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

  it('exposes the derived facets in the hover and in the accessible name', async () => {
    stubFetch(payload([annotation()]))
    await renderDashboard()

    await waitFor(() => expect(chips().length).toBe(1))
    const chip = chips()[0]
    const hover = chip.getAttribute('title')!
    expect(hover).toContain('task: p0-09b-r1')
    expect(hover).toContain('role: reviewer')
    expect(hover).toContain('round: 12')
    expect(chip.getAttribute('aria-label')).toContain('waiting')
    expect(chip.getAttribute('aria-label')).toContain('task: p0-09b-r1')
  })

  it('adds no interactive element and no copy affordance to the dashboard', async () => {
    stubFetch(payload([annotation()]))
    await renderDashboard()
    await waitFor(() => expect(chips().length).toBe(1))

    for (const chip of chips()) {
      expect(chip.tagName).toBe('SPAN')
      expect(chip.querySelector('button, a, input, [role="button"], [tabindex]')).toBeNull()
      expect(chip.getAttribute('tabindex')).toBeNull()
    }
    // §9.5: no identity bundle and no copy-to-clipboard on an unauthenticated
    // dashboard. The chip publishes derived facts only.
    expect(screen.queryByText(/copy/i)).toBeNull()
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
    expect(chip.getAttribute('title')).toContain('stale since')
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
    expect(chip.getAttribute('title')).toContain('freshness not declared')
    expect(chip.getAttribute('title')).not.toContain('stale since')
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
  const MODULES: Array<[string, string]> = [
    ['src/lib/annotations.ts', annotationsLibSource],
    ['src/components/AnnotationChips.tsx', annotationChipsSource],
  ]

  // The same eight terms the Python guard forbids.
  const CONDUCTOR_TERMS = [
    'work-state',
    'work_item',
    'human-gate',
    'route-breaker',
    'parked',
    'in-round',
    'finalized',
    'supervisor',
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
    for (const forbidden of ['KNOWN_KINDS', 'SUPPORTED_KINDS', 'FACET_ORDER', 'SUBJECT_TYPES']) {
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
  })
})
