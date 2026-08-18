// Communications catalog client + coverage/reason mapping (design §7, §10).
//
// The mapping tests pin the §2.1 contract: every reason code gets its own
// words, absent and unreadable never collapse into one another, and unknown
// future codes degrade to a neutral message that still shows the raw code.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { api, type CatalogReason, type CommunicationsListResponse } from '../api'
import {
  catalogAvailability,
  contentReasonText,
  coverageReasonText,
  detailFailure,
  kindLabel,
  listFailure,
  readCommunicationsList,
  reportScopeBadge,
  REASON,
  ROOT_SOURCE_LABEL,
} from '../lib/communications'

afterEach(() => {
  vi.restoreAllMocks()
})

function listBody(overrides: Partial<CommunicationsListResponse> = {}): CommunicationsListResponse {
  return {
    schema: 'cao-communications-index-v1',
    coverage: 'complete',
    reasons: [],
    communications: [],
    next_cursor: null,
    total: 0,
    ...overrides,
  }
}

describe('api client URL shapes', () => {
  function stubFetchOk(body: unknown) {
    return vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => body,
    } as Response)
  }

  it('lists by task occurrence, passing an opaque cursor back verbatim', async () => {
    const fetchMock = stubFetchOk(listBody())
    await api.listCommunications('task/occ 1', 'cursor+token/with=chars')
    expect(fetchMock).toHaveBeenCalledWith(
      `/communications?task_occurrence_id=${encodeURIComponent('task/occ 1')}` +
        `&cursor=${encodeURIComponent('cursor+token/with=chars')}`,
      expect.anything(),
    )
  })

  it('omits the cursor parameter on the first page', async () => {
    const fetchMock = stubFetchOk(listBody())
    await api.listCommunications('t1')
    expect(fetchMock).toHaveBeenCalledWith('/communications?task_occurrence_id=t1', expect.anything())
  })

  it('fetches detail and attachment by their own opaque ids', async () => {
    const fetchMock = stubFetchOk({})
    await api.getCommunication('c1')
    await api.getCommunicationAttachment('att/9')
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/communications/c1', expect.anything())
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/communications/attachments/${encodeURIComponent('att/9')}`,
      expect.anything(),
    )
  })
})

describe('readCommunicationsList', () => {
  it('passes a documented body through', () => {
    const body = listBody({
      coverage: 'partial',
      reasons: [{ source: 'proj-a', reason: 'malformed' }],
      next_cursor: 'opaque-token',
      total: 3,
    })
    expect(readCommunicationsList(body)).toEqual(body)
  })

  it.each([
    ['not an object', 42],
    ['missing coverage', { communications: [] }],
    ['communications not a list', { coverage: 'complete', communications: {} }],
    ['null', null],
  ])('returns null for a malformed body (%s)', (_label, body) => {
    expect(readCommunicationsList(body)).toBeNull()
  })

  it('drops malformed reason pairs but keeps the valid ones', () => {
    const body = listBody({
      coverage: 'partial',
      reasons: [{ source: 'a', reason: 'missing' }, { reason: 'x' } as unknown as CatalogReason, null as unknown as CatalogReason],
    })
    expect(readCommunicationsList(body)!.reasons).toEqual([{ source: 'a', reason: 'missing' }])
  })
})

describe('catalogAvailability — absent and unreadable are different answers', () => {
  it('a missing root means not installed', () => {
    expect(
      catalogAvailability('unavailable', [{ source: ROOT_SOURCE_LABEL, reason: REASON.MISSING }]),
    ).toBe('not-installed')
  })

  it('an unreadable root is a different, named state', () => {
    expect(
      catalogAvailability('unavailable', [{ source: ROOT_SOURCE_LABEL, reason: REASON.UNREADABLE }]),
    ).toBe('unreadable')
  })

  it('unavailable without a root reason is still not "absent"', () => {
    expect(catalogAvailability('unavailable', [])).toBe('unreadable')
  })

  it.each(['complete', 'partial', 'truncated'])('%s coverage is available', (coverage) => {
    expect(catalogAvailability(coverage, [])).toBe('available')
  })
})

describe('coverageReasonText', () => {
  it.each([
    [REASON.MISSING, 'proj-a: no catalog published'],
    [REASON.UNREADABLE, 'proj-a: could not be read'],
    [REASON.MALFORMED, 'proj-a: catalog file is malformed'],
    [REASON.OVERSIZE, 'proj-a: catalog exceeds the read budget'],
    [REASON.NOT_REGULAR, 'proj-a: catalog is not a regular file'],
    [REASON.SYMLINK_REFUSED, 'proj-a: catalog is a symlink (refused)'],
    [REASON.OUTSIDE_ROOT, 'proj-a: catalog resolves outside the state root (refused)'],
    [REASON.PROJECT_LIMIT, 'proj-a: project limit reached; not every project was read'],
  ])('maps %s to its own words', (reason, expected) => {
    expect(coverageReasonText({ source: 'proj-a', reason })).toBe(expected)
  })

  it('an unknown future reason still shows the raw code', () => {
    expect(coverageReasonText({ source: 'proj-a', reason: 'future-code' })).toBe(
      'proj-a: unavailable (future-code)',
    )
  })
})

describe('contentReasonText — tombstones never impersonate each other', () => {
  it('quarantined says deliberate removal, never missing or corrupt', () => {
    const text = contentReasonText(REASON.CONTENT_QUARANTINED)
    expect(text).toContain('quarantined')
    expect(text).not.toContain('missing')
    expect(text).not.toContain('corrupt')
  })

  it('missing, oversize, and unreadable are distinct', () => {
    const missing = contentReasonText(REASON.CONTENT_MISSING)
    const oversize = contentReasonText(REASON.OVERSIZE)
    const unreadable = contentReasonText(REASON.CONTENT_UNREADABLE)
    expect(missing).toContain('Content missing')
    expect(oversize).toContain('Too large to serve')
    expect(unreadable).toContain('could not be read')
    expect(new Set([missing, oversize, unreadable]).size).toBe(3)
  })

  it('an unknown reason falls back to neutral wording with the raw code', () => {
    expect(contentReasonText('content-purged')).toBe('Content unavailable (content-purged).')
  })
})

describe('detailFailure', () => {
  it('404 is a stable not-found, not an error with a retry', () => {
    const f = detailFailure({ status: 404, message: '404 Not Found' })
    expect(f.kind).toBe('not-found')
    expect(f.message).toContain('not in the catalog')
  })

  it('400 means the link identifier is invalid', () => {
    expect(detailFailure({ status: 400 }).kind).toBe('invalid')
  })

  it('a digest mismatch says corrupt and is never rendered', () => {
    const f = detailFailure({ status: 503, detail: 'content-digest-mismatch' })
    expect(f.kind).toBe('corrupt')
    expect(f.message).toContain('integrity check')
    expect(f.message).toContain('corrupt')
    expect(f.message).not.toContain('missing')
  })

  it('a 503 carrying content-unreadable is an ordinary unavailable state', () => {
    const f = detailFailure({ status: 503, detail: 'content-unreadable' })
    expect(f.kind).toBe('unavailable')
    expect(f.message).toContain('could not be read')
  })

  it('401/403 are ordinary unavailable states, not auth flows', () => {
    expect(detailFailure({ status: 401 }).kind).toBe('unavailable')
    expect(detailFailure({ status: 403 }).kind).toBe('unavailable')
  })

  it('a network failure is unavailable and says so', () => {
    const f = detailFailure(new TypeError('fetch failed'))
    expect(f.kind).toBe('unavailable')
    expect(f.message).toContain('could not be reached')
  })
})

describe('listFailure — the list route reads statuses its own way', () => {
  it('a list 404 means this build has no catalog route, never record-not-found', () => {
    // The real list route cannot 404: root problems come back 200 +
    // coverage 'unavailable'. A 404 here is a build without the route.
    const f = listFailure({ status: 404, detail: 'communications-catalog-not-found' })
    expect(f.kind).toBe('not-found')
    expect(f.message).toContain('No communications catalog is installed')
    expect(f.message).not.toContain('not in the catalog')
  })

  it.each([400, 422])('a list %i is a deterministic invalid identifier, not retryable', status => {
    const f = listFailure({ status, detail: 'identifier-invalid' })
    expect(f.kind).toBe('invalid')
    expect(f.message).toContain('not a valid catalog identifier')
  })

  it('network and 5xx stay retryable unavailable states', () => {
    expect(listFailure(new TypeError('fetch failed')).kind).toBe('unavailable')
    expect(listFailure({ status: 503 }).kind).toBe('unavailable')
    expect(listFailure({ status: 401 }).kind).toBe('unavailable')
  })
})

describe('open vocabulary rendering', () => {
  it('kind labels pass the conductor vocabulary through', () => {
    expect(kindLabel('report')).toBe('report')
    expect(kindLabel('checkpoint_boundary')).toBe('checkpoint boundary')
    expect(kindLabel(null)).toBe('communication')
    expect(kindLabel('invented-in-2031')).toBe('invented-in-2031')
  })

  it('final and intermediate scope are distinct badges, never an outcome', () => {
    expect(reportScopeBadge('final')).toBe('final report')
    expect(reportScopeBadge('intermediate')).toBe('intermediate report')
    expect(reportScopeBadge('final')).not.toBe(reportScopeBadge('intermediate'))
    expect(reportScopeBadge(null)).toBeNull()
    expect(reportScopeBadge('quarterly')).toBe('quarterly')
  })
})
