// The fleet-filter module: one predicate, derived dimensions, no vocabulary.
//
// The suite is organised around the three ways this feature can lie:
// counting and filtering disagreeing (the drifted-predicates defect), a
// control claiming a shape the values do not have (equality against a
// truncated value), and a facet key hard-coded anywhere (the seam the
// MODULES guard in annotations.test.tsx now covers mechanically).

import { describe, it, expect } from 'vitest'
import {
  activeFilterCount,
  callerVocabulary,
  collectFacetDimensions,
  collectLabelDimensions,
  dimensionMerit,
  displayStatus,
  emptyFacetSelection,
  emptyFilters,
  facetStats,
  fleetWideFacetKeys,
  groupDimensions,
  isFilterActive,
  isFleetWide,
  LABEL_DIMENSION_PREFIX,
  lifecycleVocabulary,
  matchesFilters,
  profileVocabulary,
  providerVocabulary,
  rowSearchText,
  STATUS_ORDER,
} from '../lib/filters'
import { projectedTerminal } from './projectedTerminal'
import type { Annotation } from '../api'

const GENERATION = 'term-001-gen-1'
const PAST = '2020-01-01T00:00:00Z'
const FUTURE = '2999-01-01T00:00:00Z'

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    namespace: 'cao-conductor',
    kind: 'display',
    version: 1,
    label: 'waiting',
    semantic_role: 'warning',
    priority: 60,
    subject: { type: 'terminal', terminal_id: 'term-001', generation: GENERATION },
    valid_until: FUTURE,
    details: {},
    ...overrides,
  }
}

describe('displayStatus is one fold for counting and filtering', () => {
  it('case-normalises: the projection row carries the lowercase spelling', () => {
    // The store uppercases the polled status; the row itself reports
    // 'not_fifo_monitored'. A fold accepting only uppercase filed every row
    // read straight from /sessions/{name} under UNKNOWN.
    expect(displayStatus('not_fifo_monitored')).toBe('NOT_FIFO_MONITORED')
    expect(displayStatus('NOT_FIFO_MONITORED')).toBe('NOT_FIFO_MONITORED')
  })

  it('renders proven lifecycle dispositions and folds only unknown evidence', () => {
    expect(displayStatus('dead')).toBe('DEAD')
    expect(displayStatus('SUPERSEDED')).toBe('SUPERSEDED')
    expect(displayStatus('unknown-liveness')).toBe('UNKNOWN')
    expect(displayStatus(undefined)).toBe('UNKNOWN')
    expect(displayStatus(null)).toBe('UNKNOWN')
  })

  it('renders STOPPED as itself, in a defensible position', () => {
    // STOPPED ships in the generated STATUS_CONFIG; absent from STATUS_ORDER
    // it folded to UNKNOWN — the same defect shape as NOT_FIFO_MONITORED.
    expect(displayStatus('stopped')).toBe('STOPPED')
    expect(STATUS_ORDER.indexOf('STOPPED')).toBeGreaterThan(STATUS_ORDER.indexOf('COMPLETED'))
    expect(STATUS_ORDER.indexOf('STOPPED')).toBeLessThan(STATUS_ORDER.indexOf('UNKNOWN'))
    expect(STATUS_ORDER.indexOf('DEAD')).toBeLessThan(STATUS_ORDER.indexOf('UNKNOWN'))
    expect(STATUS_ORDER.indexOf('SUPERSEDED')).toBeLessThan(STATUS_ORDER.indexOf('UNKNOWN'))
  })
})

describe('matchesFilters: OR within a dimension, AND across dimensions', () => {
  const row = projectedTerminal({ id: 'term-001', status: 'not_fifo_monitored' })

  it('matches everything when no dimension is constrained', () => {
    expect(matchesFilters(row, undefined, emptyFilters())).toBe(true)
  })

  it('ORs within reachability and routes every comparison through the fold', () => {
    const f = { ...emptyFilters(), reachability: ['IDLE', 'ERROR'] }
    expect(matchesFilters(row, undefined, f)).toBe(false)
    expect(matchesFilters(projectedTerminal({ id: 'x', status: 'idle' }), undefined, f)).toBe(true)
    expect(matchesFilters(projectedTerminal({ id: 'y', status: 'error' }), undefined, f)).toBe(true)
    // Proven lifecycle words are first-class reachability values.
    const dead = projectedTerminal({ id: 'z', status: 'dead', lifecycle_state: 'dead' })
    expect(matchesFilters(dead, undefined, { ...emptyFilters(), reachability: ['DEAD'] })).toBe(true)
    const unknown = projectedTerminal({
      id: 'u', status: 'unknown-liveness', lifecycle_state: 'unknown-liveness',
    })
    expect(matchesFilters(unknown, undefined, {
      ...emptyFilters(), reachability: ['UNKNOWN'],
    })).toBe(true)
  })

  it('prefers the polled status from the context over the row’s stored one', () => {
    const f = { ...emptyFilters(), reachability: ['IDLE'] }
    expect(matchesFilters(row, undefined, f, { status: 'IDLE' })).toBe(true)
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })

  it('filters liveness on the row’s own vocabulary, not the folded chip', () => {
    // The whole point of the dimension: 'dead' / 'superseded' /
    // 'unknown-liveness' stop collapsing into one UNKNOWN chip.
    const dead = projectedTerminal({ id: 'd', status: 'dead', lifecycle_state: 'dead' })
    const f = { ...emptyFilters(), liveness: ['dead'] }
    expect(matchesFilters(dead, undefined, f)).toBe(true)
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })

  it('folds a null profile to default in the ONE place it now exists', () => {
    // The defect this pins: the session gate folded (agent_profile ||
    // 'default') while the row gate did not, so selecting "default" kept the
    // card and emptied it. There is now one predicate to get right.
    const noProfile = projectedTerminal({ id: 'np', agent_profile: null })
    const f = { ...emptyFilters(), profiles: ['default'] }
    expect(matchesFilters(noProfile, undefined, f)).toBe(true)
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })

  it('filters provider and session on the fork’s own keys', () => {
    const f = { ...emptyFilters(), providers: ['kimi_cli'], sessions: ['cao-fleet'] }
    expect(matchesFilters(row, undefined, f)).toBe(true)
    expect(matchesFilters(row, undefined, { ...f, providers: ['claude_code'] })).toBe(false)
    expect(matchesFilters(row, undefined, { ...f, sessions: ['elsewhere'] })).toBe(false)
  })

  it('ANDs across dimensions', () => {
    const f = { ...emptyFilters(), providers: ['kimi_cli'], liveness: ['dead'] }
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })

  it('treats spawned-by as a subtree, grandchildren included', () => {
    const grandparent = projectedTerminal({ id: 'gp', caller_id: null })
    const parent = projectedTerminal({ id: 'pa', caller_id: 'gp' })
    const child = projectedTerminal({ id: 'ch', caller_id: 'pa' })
    const callerOf = (id: string) => [grandparent, parent, child].find(t => t.id === id)?.caller_id
    const f = { ...emptyFilters(), callers: ['gp'] }
    expect(matchesFilters(parent, undefined, f, { callerOf })).toBe(true)
    expect(matchesFilters(child, undefined, f, { callerOf })).toBe(true)
    expect(matchesFilters(grandparent, undefined, f, { callerOf })).toBe(false)
  })

  it('never spins on a cyclic caller graph', () => {
    const a = projectedTerminal({ id: 'a', caller_id: 'b' })
    const callerOf = (id: string) => (id === 'a' ? 'b' : id === 'b' ? 'a' : null)
    const f = { ...emptyFilters(), callers: ['unrelated'] }
    expect(matchesFilters(a, undefined, f, { callerOf })).toBe(false)
  })

  it('filters freshness across the row’s annotations, three states honestly', () => {
    const fresh = [annotation({ valid_until: FUTURE })]
    const stale = [annotation({ valid_until: PAST })]
    const undeclared = [annotation({ valid_until: null })]
    const f = { ...emptyFilters(), freshness: 'fresh' as const }
    expect(matchesFilters(row, fresh, f)).toBe(true)
    expect(matchesFilters(row, stale, f)).toBe(false)
    // Unknown freshness is not current freshness — undeclared matches neither.
    expect(matchesFilters(row, undeclared, f)).toBe(false)
    expect(matchesFilters(row, undeclared, { ...f, freshness: 'stale' })).toBe(false)
    expect(matchesFilters(row, stale, { ...f, freshness: 'stale' })).toBe(true)
    // A row carrying no annotations makes no freshness claim at all.
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })

  it('filters chip colour through resolveRole, so an unknown role is neutral', () => {
    const f = { ...emptyFilters(), roles: ['neutral'] }
    expect(matchesFilters(row, [annotation({ semantic_role: 'chartreuse' })], f)).toBe(true)
    expect(matchesFilters(row, [annotation({ semantic_role: 'danger' })], f)).toBe(false)
  })
})

describe('free text matches case-insensitively on BOTH sides', () => {
  const row = projectedTerminal({ id: 'term-001', agent_profile: 'Reviewer-Opus' })
  const annotations = [annotation({ label: 'Waiting', details: { branch: 'Feat/Payment-PR04' } })]

  it('lowercases the haystack as well as the needle', () => {
    // MemoryPanel lowercased only the needle; a capitalised query then
    // silently matched nothing against a capitalised value.
    const f = { ...emptyFilters(), text: '  REVIEWER-opus  ' }
    expect(matchesFilters(row, annotations, f)).toBe(true)
    expect(matchesFilters(row, annotations, { ...f, text: 'payment-pr04' })).toBe(true)
    expect(matchesFilters(row, annotations, { ...f, text: 'WAITING' })).toBe(true)
    expect(matchesFilters(row, annotations, { ...f, text: 'absent' })).toBe(false)
  })

  it('searches ids, names and profile even with no annotations', () => {
    expect(rowSearchText(row, undefined)).toContain('reviewer-opus')
    expect(rowSearchText(row, undefined)).toContain('term-001')
  })
})

describe('derived facets: shape decides the match, never the key name', () => {
  const row = projectedTerminal({ id: 'term-001' })

  it('ORs within a facet dimension and ANDs across facet keys', () => {
    const annotations = [annotation({ details: { phase: 'alpha', queue: 'q1' } })]
    const f = emptyFilters()
    f.facets = {
      phase: { ...emptyFacetSelection(), values: ['alpha', 'beta'] },
      queue: { ...emptyFacetSelection(), values: ['q2'] },
    }
    expect(matchesFilters(row, annotations, f)).toBe(false)
    f.facets.queue = { ...emptyFacetSelection(), values: ['q1'] }
    expect(matchesFilters(row, annotations, f)).toBe(true)
  })

  it('matches a facet carried only by a chip the row cap would hide', () => {
    // MAX_ROW_CHIPS slices the DRAWN chips to three; filtering runs upstream
    // of every cap, so the fourth chip's facets still match.
    const annotations = [
      annotation({ label: 'one', priority: 90 }),
      annotation({ label: 'two', priority: 80 }),
      annotation({ label: 'three', priority: 70 }),
      annotation({ label: 'four', priority: 60, details: { signal: 'hidden-but-matchable' } }),
    ]
    const f = emptyFilters()
    f.facets = { signal: { ...emptyFacetSelection(), values: ['hidden-but-matchable'] } }
    expect(matchesFilters(row, annotations, f)).toBe(true)
  })

  it('prefix-matches a server-truncated observed value, and only then', () => {
    const truncated = [annotation({ details: { branch: 'agent/payment-pr04-foundation-and-a-very-long-ta…' } })]
    const f = emptyFilters()
    f.facets = { branch: { ...emptyFacetSelection(), values: ['agent/payment-pr04-foundation-and-a-very-long-tail'] } }
    expect(matchesFilters(row, truncated, f)).toBe(true)

    // Two COMPLETE values still compare exactly: selecting r1 must not match r11.
    const rounds = [annotation({ details: { round: 'r11' } })]
    f.facets = { round: { ...emptyFacetSelection(), values: ['r1'] } }
    expect(matchesFilters(row, rounds, f)).toBe(false)
  })

  it('matches tri-state, range and substring selections by shape', () => {
    const annotations = [annotation({
      details: { enabled: 'true', since: '2026-07-01T10:00:00Z', note: 'needs a human to look' },
    })]
    const f = emptyFilters()
    f.facets = {
      enabled: { ...emptyFacetSelection(), tri: 'true' },
      since: { ...emptyFacetSelection(), from: '2026-06-01T00:00', to: '2026-08-01T00:00' },
      note: { ...emptyFacetSelection(), text: 'HUMAN' },
    }
    expect(matchesFilters(row, annotations, f)).toBe(true)
    f.facets.enabled = { ...emptyFacetSelection(), tri: 'false' }
    expect(matchesFilters(row, annotations, f)).toBe(false)
    f.facets.enabled = { ...emptyFacetSelection(), tri: 'true' }
    f.facets.since = { ...emptyFacetSelection(), from: '2027-01-01T00:00' }
    expect(matchesFilters(row, annotations, f)).toBe(false)
  })

  it('ignores a facet entry whose selection is empty', () => {
    const f = emptyFilters()
    f.facets = { phase: emptyFacetSelection() }
    expect(matchesFilters(row, undefined, f)).toBe(true)
  })
})

describe('collectFacetDimensions chooses the control by value shape', () => {
  const rowsOf = (...bags: Array<Record<string, string>>) =>
    bags.map(details => ({ annotations: [annotation({ details })] }))

  it('keeps producer insertion order for the key set', () => {
    const dims = collectFacetDimensions(rowsOf({ zeta: '1', alpha: '2' }, { mid: '3', zeta: '1' }))
    expect(dims.map(d => d.key)).toEqual(['zeta', 'alpha', 'mid'])
  })

  it('counts rows carrying a value, not annotation occurrences', () => {
    const twoClaimsOneRow = [{
      annotations: [
        annotation({ details: { phase: 'alpha' } }),
        annotation({ details: { phase: 'alpha', other: 'x' } }),
      ],
    }]
    const dims = collectFacetDimensions(twoClaimsOneRow)
    expect(dims.find(d => d.key === 'phase')?.values).toEqual([{ value: 'alpha', rows: 1 }])
    expect(dims.find(d => d.key === 'other')?.values).toEqual([{ value: 'x', rows: 1 }])
  })

  it('makes a past-ISO facet a range control — and only a past one', () => {
    expect(collectFacetDimensions(rowsOf({ since: PAST }))[0].control).toBe('range')
    // A FUTURE timestamp is not an age; the shape test stands first precisely
    // so a date cannot fall through to the count rule either.
    expect(collectFacetDimensions(rowsOf({ due: FUTURE }))[0].control).toBe('pills')
    // Mixed shapes are not a range.
    expect(collectFacetDimensions(rowsOf({ when: PAST }, { when: 'soon' }))[0].control).toBe('pills')
  })

  it('makes an exactly-true/false facet a tri-state', () => {
    expect(collectFacetDimensions(rowsOf({ enabled: 'true' }, { enabled: 'false' }))[0].control).toBe('tri-state')
    expect(collectFacetDimensions(rowsOf({ enabled: 'yes' }))[0].control).toBe('pills')
  })

  it('makes long or ellipsised values substring-only', () => {
    expect(collectFacetDimensions(rowsOf({ note: 'x'.repeat(65) }))[0].control).toBe('text')
    expect(collectFacetDimensions(rowsOf({ branch: 'agent/payment…' }))[0].control).toBe('text')
  })

  it('makes twelve distinct values pills and thirteen a typeahead', () => {
    const asRows = (bags: Array<Record<string, string>>) =>
      bags.map(details => ({ annotations: [annotation({ details })] }))
    const twelve = asRows(Array.from({ length: 12 }, (_, i) => ({ task: `t-${i}` })))
    const thirteen = asRows(Array.from({ length: 13 }, (_, i) => ({ task: `t-${i}` })))
    expect(collectFacetDimensions(twelve)[0].control).toBe('pills')
    expect(collectFacetDimensions(thirteen)[0].control).toBe('typeahead')
  })

  it('splits the dotted prefix into group and name, humanising both apart', () => {
    const [dim] = collectFacetDimensions(rowsOf({ 'assigned.assigned_at': 'x' }))
    expect(dim.group).toBe('assigned')
    expect(dim.name).toBe('assigned_at')
    expect(dim.label).toBe('assigned at')
  })

  it('ranks the most-carried value first, alphabetically on a tie', () => {
    const dims = collectFacetDimensions([
      { annotations: [annotation({ details: { phase: 'b' } })] },
      { annotations: [annotation({ details: { phase: 'a' } })] },
      { annotations: [annotation({ details: { phase: 'a' } })] },
    ])
    expect(dims[0].values.map(v => v.value)).toEqual(['a', 'b'])
  })

  it('offers only a pill-row dimension to the global bar', () => {
    const dims = collectFacetDimensions([
      { annotations: [annotation({ details: { phase: 'alpha', lane: 'l-1', since: PAST } })] },
    ])
    const byKey = Object.fromEntries(dims.map(d => [d.key, d]))
    expect(isFleetWide(byKey.phase)).toBe(true)
    expect(isFleetWide(byKey.since)).toBe(false)
    const many = collectFacetDimensions(
      Array.from({ length: 13 }, (_, i) => ({ annotations: [annotation({ details: { lane: `l-${i}` } })] })),
    )
    expect(isFleetWide(many[0])).toBe(false)
  })

  it('takes a dimension global only when two sessions share one vocabulary', () => {
    // "Stable vocabulary across sessions" is a shape fact, not a key name:
    // a facet tied to one campaign — a lane, a round, a PR state — stays in
    // its session's bar even when its values would fit one pill row, and two
    // campaigns emitting one key with DISJOINT values are two session-local
    // vocabularies, not one stable dimension.
    const alpha = { dimensions: collectFacetDimensions([
      { annotations: [annotation({ details: { phase: 'reported', lane: 'l-1', 'publication.pr': 'pr17 open' } })] },
      { annotations: [annotation({ details: { phase: 'waiting' } })] },
    ]) }
    const beta = { dimensions: collectFacetDimensions([
      { annotations: [annotation({ details: { phase: 'reported', lane: 'l-2' } })] },
    ]) }
    const fleet = collectFacetDimensions([
      { annotations: [annotation({ details: { phase: 'reported', lane: 'l-1', 'publication.pr': 'pr17 open' } })] },
      { annotations: [annotation({ details: { phase: 'waiting' } })] },
      { annotations: [annotation({ details: { phase: 'reported', lane: 'l-2' } })] },
    ])
    const global = fleetWideFacetKeys(fleet, [alpha, beta])
    // phase: emitted in both sessions, fleet vocabulary {reported, waiting}
    // equals alpha's — shared, not partitioned.
    expect(global.has('phase')).toBe(true)
    // lane: disjoint per campaign ({l-1} vs {l-2}): partitioned, stays local.
    expect(global.has('lane')).toBe(false)
    // The §5.4 case: PR state is offered per-session and never presented as
    // a fleet-wide authoritative filter.
    expect(global.has('publication.pr')).toBe(false)
  })

  it('keeps a cross-session facet out of the global bar when its fleet vocabulary overflows the pill cap', () => {
    const perSession = Array.from({ length: 2 }, (_, s) => ({
      dimensions: collectFacetDimensions(
        Array.from({ length: 7 }, (_, i) => ({
          annotations: [annotation({ details: { branch: `s${s}-b${i}` } })],
        })),
      ),
    }))
    const fleet = collectFacetDimensions(
      perSession.flatMap(s =>
        Array.from({ length: 7 }, (_, i) => i).map(i => ({
          annotations: [annotation({ details: { branch: `s${perSession.indexOf(s)}-b${i}` } })],
        })),
      ),
    )
    // Pill-shaped in each session (7), typeahead for the fleet (14): the wall
    // argument is evaluated on the fleet's full vocabulary.
    expect(fleet[0].control).toBe('typeahead')
    expect(fleetWideFacetKeys(fleet, perSession).has('branch')).toBe(false)
  })
})

describe('groupDimensions collects the dotted classes in first-appearance order', () => {
  it('groups classed facets under their headings and lets plain keys stand alone', () => {
    const dims = collectFacetDimensions([
      {
        annotations: [annotation({
          details: { task: 't', 'B.two': '2', 'A.one': '1', 'B.three': '3' },
        })],
      },
    ])
    const groups = groupDimensions(dims)
    expect(groups.map(g => g.heading)).toEqual([null, 'B', 'A'])
    expect(groups[0].dimensions[0].label).toBe('task')
    expect(groups[1].dimensions.map(d => d.label)).toEqual(['two', 'three'])
  })

  it('renames its headings when the producer renames its classes', () => {
    const dims = collectFacetDimensions([
      { annotations: [annotation({ details: { 'Xray.one': '1' } })] },
    ])
    expect(groupDimensions(dims)[0].heading).toBe('Xray')
  })
})

describe('Layer-1 vocabularies scan the fleet, folded the way the predicate folds', () => {
  const fleet = [
    projectedTerminal({ id: 'a', agent_profile: null, provider: null, lifecycle_state: 'live', caller_id: null }),
    projectedTerminal({ id: 'b', agent_profile: 'reviewer', provider: 'kimi_cli', lifecycle_state: 'dead', caller_id: 'a' }),
  ]

  it('scans liveness, profiles and providers', () => {
    expect(lifecycleVocabulary(fleet)).toEqual(['dead', 'live'])
    expect(profileVocabulary(fleet)).toEqual(['default', 'reviewer'])
    expect(providerVocabulary(fleet)).toEqual(['kimi_cli', 'unknown'])
  })

  it('labels a caller with its profile when the fleet can resolve it', () => {
    expect(callerVocabulary(fleet)).toEqual([{ id: 'a', label: 'default · a' }])
    expect(callerVocabulary([projectedTerminal({ id: 'x', caller_id: 'not-in-fleet-1234' })])).toEqual([
      { id: 'not-in-fleet-1234', label: 'not-in-f' },
    ])
  })
})

describe('filter activity accounting', () => {
  it('counts each constrained dimension once', () => {
    expect(isFilterActive(emptyFilters())).toBe(false)
    expect(activeFilterCount(emptyFilters())).toBe(0)
    const f = emptyFilters()
    f.reachability = ['IDLE', 'ERROR']
    f.text = 'x'
    f.facets = { phase: { ...emptyFacetSelection(), values: ['alpha'] } }
    expect(isFilterActive(f)).toBe(true)
    // Two selected pills are ONE constrained dimension.
    expect(activeFilterCount(f)).toBe(3)
  })
})

describe('label dimensions: one per kind, values are the annotations’ labels', () => {
  it('collects a label:-prefixed dimension per kind, in first-appearance order', () => {
    const rows = [
      {
        annotations: [
          annotation({ kind: 'display', label: 'reported' }),
          annotation({ kind: 'badge', label: 'cond-01' }),
        ],
      },
      {
        annotations: [
          annotation({ kind: 'display', label: 'waiting' }),
          annotation({ kind: 'badge', label: 'cond-01' }),
        ],
      },
    ]
    const dims = collectLabelDimensions(rows)
    expect(dims.map(d => d.key)).toEqual([`${LABEL_DIMENSION_PREFIX}display`, `${LABEL_DIMENSION_PREFIX}badge`])
    const badge = dims[1]
    // Row-counted, like facet values: two rows carry cond-01.
    expect(badge.values).toEqual([{ value: 'cond-01', rows: 2 }])
    expect(badge.carriers).toBe(2)
    expect(badge.control).toBe('pills')
  })

  it('counts a repeated label on one row once', () => {
    const rows = [{
      annotations: [
        annotation({ kind: 'display', label: 'waiting' }),
        annotation({ kind: 'display', label: 'waiting', priority: 10 }),
      ],
    }]
    const [dim] = collectLabelDimensions(rows)
    expect(dim.values).toEqual([{ value: 'waiting', rows: 1 }])
    expect(dim.carriers).toBe(1)
  })

  it('humanises the kind for the label without parsing it', () => {
    const [dim] = collectLabelDimensions([
      { annotations: [annotation({ kind: 'some-producer.kind_2031', label: 'x' })] },
    ])
    expect(dim.label).toBe('some producer kind 2031')
  })

  it('treats round-suffixed labels as identity-shaped by shape, never by parsing the suffix', () => {
    // "parked · r1" … "parked · r13": thirteen distinct labels over thirteen
    // rows. Nothing here reads the ' · rN' format; the cardinality does the
    // talking and the merit ranking demotes it like any identity column.
    const rows = Array.from({ length: 13 }, (_, i) => ({
      annotations: [annotation({ label: `waiting · r${i + 1}` })],
    }))
    const [dim] = collectLabelDimensions(rows)
    expect(dim.control).toBe('typeahead')
    expect(dimensionMerit(facetStats(dim), 13).tier).toBe('niche')
  })

  it('matches a label selection within its own kind only', () => {
    const row = projectedTerminal({ id: 'term-001' })
    const annotations = [
      annotation({ kind: 'display', label: 'waiting' }),
      annotation({ kind: 'badge', label: 'cond-01' }),
    ]
    const f = emptyFilters()
    f.facets = { [`${LABEL_DIMENSION_PREFIX}badge`]: { ...emptyFacetSelection(), values: ['cond-01'] } }
    expect(matchesFilters(row, annotations, f)).toBe(true)
    // The same WORD under another kind is a different fact: 'waiting' belongs
    // to 'display', so selecting it under 'badge' must not match.
    f.facets = { [`${LABEL_DIMENSION_PREFIX}badge`]: { ...emptyFacetSelection(), values: ['waiting'] } }
    expect(matchesFilters(row, annotations, f)).toBe(false)
    f.facets = { [`${LABEL_DIMENSION_PREFIX}display`]: { ...emptyFacetSelection(), values: ['waiting'] } }
    expect(matchesFilters(row, annotations, f)).toBe(true)
  })

  it('ANDs a label dimension with a detail facet and supports the substring needle', () => {
    const row = projectedTerminal({ id: 'term-001' })
    const annotations = [annotation({ kind: 'badge', label: 'cond-01', details: { task: 't-1' } })]
    const f = emptyFilters()
    f.facets = {
      [`${LABEL_DIMENSION_PREFIX}badge`]: { ...emptyFacetSelection(), values: ['cond-01'] },
      task: { ...emptyFacetSelection(), values: ['t-2'] },
    }
    expect(matchesFilters(row, annotations, f)).toBe(false)
    f.facets.task = { ...emptyFacetSelection(), values: ['t-1'] }
    expect(matchesFilters(row, annotations, f)).toBe(true)
    f.facets[`${LABEL_DIMENSION_PREFIX}badge`] = { ...emptyFacetSelection(), text: 'COND' }
    expect(matchesFilters(row, annotations, f)).toBe(true)
    // A row carrying no annotations matches no label selection.
    expect(matchesFilters(row, undefined, f)).toBe(false)
  })
})

describe('opaque values are text-matched, never pill-picked', () => {
  const rowsOf = (...bags: Array<Record<string, string>>) =>
    bags.map(details => ({ annotations: [annotation({ details })] }))

  it('forces a long-hex facet to the text control by value shape', () => {
    // A 40-char sha is under the 64-char length rule, so without the opaque
    // rule this would have been a pill row of hashes.
    const [dim] = collectFacetDimensions(rowsOf({ ref: 'a'.repeat(40) }))
    expect(dim.control).toBe('text')
    expect(dim.opaque).toBe(true)
  })

  it('leaves short ids alone — a human can pick those from a list', () => {
    const [dim] = collectFacetDimensions(rowsOf({ ref: 'a1b2c3d4' }, { ref: 'e5f60718' }))
    expect(dim.control).toBe('pills')
    expect(dim.opaque).toBe(false)
  })

  it('does not mistake an ISO timestamp for a hash — the range test still stands first', () => {
    const [dim] = collectFacetDimensions(rowsOf({ when: PAST }))
    expect(dim.control).toBe('range')
    expect(dim.opaque).toBe(false)
  })
})

describe('dimensionMerit ranks by shape, never by key name', () => {
  const stats = (overrides: Partial<Parameters<typeof dimensionMerit>[0]>) => ({
    control: 'pills' as const,
    distinct: 3,
    carriers: 43,
    opaque: false,
    ...overrides,
  })

  it('omits a single-value dimension the whole scope carries', () => {
    // The measured fleet: one provenance value on 43 of 43 rows — selecting
    // it could only ever pass, so the picker does not offer it at all.
    const merit = dimensionMerit(stats({ distinct: 1, carriers: 43 }), 43)
    expect(merit.tier).toBeNull()
    expect(merit.note).toContain('one value')
  })

  it('keeps a single-value dimension some rows carry — a presence question', () => {
    const merit = dimensionMerit(stats({ distinct: 1, carriers: 5 }), 43)
    expect(merit.tier).toBe('secondary')
  })

  it('ranks few-shared-values dimensions primary', () => {
    expect(dimensionMerit(stats({ distinct: 4, carriers: 43 }), 43).tier).toBe('primary')
    expect(dimensionMerit(stats({ control: 'tri-state', distinct: 2 }), 43).tier).toBe('primary')
  })

  it('never demotes a timestamp as identity-shaped, though distinct ≈ carriers', () => {
    expect(dimensionMerit(stats({ control: 'range', distinct: 10, carriers: 10 }), 43).tier).toBe('primary')
  })

  it('demotes opaque values to niche however many rows carry them', () => {
    expect(dimensionMerit(stats({ control: 'text', distinct: 3, opaque: true }), 43).tier).toBe('niche')
  })

  it('demotes identity-shaped dimensions once the scope is big enough to mean it', () => {
    // 48 distinct commits over 48 rows was the measurement.
    expect(dimensionMerit(stats({ control: 'typeahead', distinct: 48, carriers: 48 }), 48).tier).toBe('niche')
    expect(dimensionMerit(stats({ distinct: 10, carriers: 10 }), 10).tier).toBe('niche')
  })

  it('does not demote small scopes or real vocabularies on the ratio alone', () => {
    // 4 distinct over 4 carriers is a small fleet, not an identity column.
    expect(dimensionMerit(stats({ distinct: 4, carriers: 4 }), 4).tier).toBe('primary')
    // 30 distinct over 43 carriers is a real, large vocabulary: secondary.
    expect(dimensionMerit(stats({ control: 'typeahead', distinct: 30, carriers: 43 }), 43).tier).toBe('secondary')
  })
})
