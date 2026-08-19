// The task-scoped communications catalog modal (design §8.2, §10).
//
// Fixtures mirror test/api/test_communications_catalog_api.py's shapes — the
// API's real envelopes, not shapes the UI wishes it had received.
//
// FIXTURE DISCLOSURE — cond-0477: Every fixture in this file that carries a
// bound task_occurrence_id models a state no shipped conductor writer
// currently produces — all current writers record task_occurrence_id = NULL
// (cond-0477). The fork's contract is the published index format and a bound
// occurrence is a legal value of it. The API reports `coverage:"complete"`,
// `total:0` with no reason code for the unbound case, so the reader cannot
// distinguish "unbound" from "genuinely empty" — a known limitation that
// resolves when cond-0477 lands. The empty/unbound path
// (`coverage:"complete"`, `total: 0`) is covered by the empty-catalog test
// below, which is the only answer production can currently give.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { useState } from 'react'
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { CommunicationsModal } from '../components/CommunicationsModal'
import type { CatalogDocumentEntry, CommunicationListItem } from '../api'

const TASK = 'task-occ-1'
const ROOT = 'conductor-state-root'

function docEntry(attachmentId: string, overrides: Partial<CatalogDocumentEntry> = {}): CatalogDocumentEntry {
  return {
    attachment_id: attachmentId,
    document_id: `doc-${attachmentId}`,
    role: 'body',
    display_name: 'note.txt',
    media_type: 'text/plain',
    sha256: 'a'.repeat(64),
    byte_size: 5,
    blob_id: 'a'.repeat(64),
    content_state: 'present',
    capture_kind: 'report-body',
    redaction_applied: false,
    ...overrides,
  }
}

function comm(id: string, recordedAt: string, overrides: Record<string, unknown> = {}): CommunicationListItem {
  return {
    communication_id: id,
    project_id: 'project',
    session_id: 'session',
    lane_id: 'lane',
    task_occurrence_id: TASK,
    goal_version: '1',
    kind: 'report',
    report_scope: null,
    authored_by_type: 'agent',
    authored_by_id: 'agent-1',
    authored_at: recordedAt,
    recorded_at: recordedAt,
    title: null,
    delivery_state: 'delivered',
    visibility: 'internal',
    request_key: null,
    supersedes_communication_id: null,
    superseded_by: null,
    body: null,
    documents: [],
    ...overrides,
  } as CommunicationListItem
}

/**
 * The five records of acceptance criterion 1, in the server's total order
 * (recorded_at DESC): an inline assignment, an intermediate checkpoint, a
 * file-backed final report, and a message-only final report, plus an ordinary
 * non-task message with attachments (criterion 2).
 */
function catalogItems(): CommunicationListItem[] {
  return [
    comm('c-final-msg', '2026-08-18T05:00:00Z', {
      kind: 'report',
      report_scope: 'final',
      title: 'Final report',
      body: docEntry('att-body-final-msg', {
        role: 'body',
        display_name: 'final-report.md',
        media_type: 'text/markdown',
        capture_kind: 'inline-message',
      }),
    }),
    comm('c-final-file', '2026-08-18T04:00:00Z', {
      kind: 'report',
      report_scope: 'final',
      title: 'Final report (file)',
      body: docEntry('att-body-final-file', {
        role: 'body',
        display_name: 'report.md',
        media_type: 'text/markdown',
        capture_kind: 'file-snapshot',
      }),
    }),
    comm('c-inter', '2026-08-18T03:00:00Z', {
      kind: 'report',
      report_scope: 'intermediate',
      title: 'Intermediate report',
      body: docEntry('att-body-inter', { role: 'body', media_type: 'text/markdown' }),
    }),
    comm('c-check', '2026-08-18T02:00:00Z', {
      kind: 'checkpoint',
      title: 'Checkpoint 1',
      body: docEntry('att-body-check', { role: 'body', media_type: 'text/markdown' }),
    }),
    comm('c-assign', '2026-08-18T01:00:00Z', {
      kind: 'assignment',
      title: 'Assignment',
      body: docEntry('att-body-assign', { role: 'body', media_type: 'text/markdown' }),
    }),
    comm('c-msg', '2026-08-18T00:30:00Z', {
      kind: 'message',
      title: 'Ordinary message',
      task_occurrence_id: null,
      body: docEntry('att-body-msg', { role: 'body', media_type: 'text/plain' }),
      documents: [
        docEntry('att-extra-1', { role: 'attachment', display_name: 'notes.md', media_type: 'text/markdown', byte_size: 1234 }),
        docEntry('att-extra-2', {
          role: 'supporting-evidence',
          display_name: 'transcript.txt',
          byte_size: 98,
          redaction_applied: true,
        }),
      ],
    }),
  ]
}

function listEnvelope(items: CommunicationListItem[], overrides: Record<string, unknown> = {}) {
  return {
    schema: 'cao-communications-index-v1',
    coverage: 'complete',
    reasons: [],
    communications: items,
    next_cursor: null,
    total: items.length,
    ...overrides,
  }
}

function detailEnvelope(item: CommunicationListItem, content: string | null, reason: string | null = null) {
  return { communication: item, content, reason }
}

type Handler = (url: string) => { status?: number; body?: unknown } | undefined

function stubFetch(handler: Handler) {
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const res = handler(String(input))
    const status = res?.status ?? (res ? 200 : 404)
    const body = res?.body ?? { detail: 'communications-catalog-not-found' }
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: `status ${status}`,
      json: async () => body,
    } as Response
  }))
  return fetch as unknown as ReturnType<typeof vi.fn>
}

/** The standard stub: full list plus per-id detail content. */
function stubCatalog(
  items: CommunicationListItem[],
  options: {
    contents?: Record<string, string | null>
    reasons?: Record<string, string>
    detailStatus?: Record<string, { status: number; detail: unknown }>
    attachments?: Record<string, { content: string | null; reason?: string | null; status?: number }>
    listOverrides?: Record<string, unknown>
  } = {},
) {
  const contents = options.contents ?? {}
  return stubFetch(url => {
    if (url.startsWith('/communications?')) return { body: listEnvelope(items, options.listOverrides) }
    const attMatch = /^\/communications\/attachments\/([^/]+)$/.exec(url)
    if (attMatch) {
      const spec = options.attachments?.[decodeURIComponent(attMatch[1])]
      if (!spec) return undefined
      if (spec.status) return { status: spec.status, body: { detail: `error ${spec.status}` } }
      return { body: { document: {}, content: spec.content, reason: spec.reason ?? null } }
    }
    const detailMatch = /^\/communications\/([^/]+)$/.exec(url)
    if (detailMatch) {
      const id = decodeURIComponent(detailMatch[1])
      const failing = options.detailStatus?.[id]
      if (failing) return { status: failing.status, body: { detail: failing.detail } }
      const item = items.find(i => i.communication_id === id)
      if (!item) return undefined
      const content = id in contents ? contents[id] : `# content of ${id}`
      return { body: detailEnvelope(item, content ?? null, options.reasons?.[id] ?? null) }
    }
    return undefined
  })
}

function renderModal(props: Partial<Parameters<typeof CommunicationsModal>[0]> = {}) {
  const onSelect = props.onSelect ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const view = render(
    <CommunicationsModal taskOccurrenceId={TASK} selectedId={null} onSelect={onSelect} onClose={onClose} {...props} />,
  )
  return { onSelect, onClose, ...view }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('list rendering', () => {
  it('renders every catalog item in the server order, with kind and scope badges', async () => {
    stubCatalog(catalogItems())
    renderModal()
    const items = await screen.findAllByTestId('communication-item')
    const ids = items.map(el => el.getAttribute('data-communication-id'))
    expect(ids).toEqual(['c-final-msg', 'c-final-file', 'c-inter', 'c-check', 'c-assign', 'c-msg'])
    // The detail pane is a second async stage (effectiveId -> getCommunication).
    // Await the detail's gated DOM before counting badges that include it.
    await screen.findByTestId('md-rendered')
    const badges = screen.getAllByTestId('scope-badge').map(b => b.textContent)
    expect(badges.filter(b => b === 'final report')).toHaveLength(2 + 1) // two list rows + auto-selected detail
    expect(badges).toContain('intermediate report')
  })

  it('auto-selects the latest communication and renders its Markdown body', async () => {
    stubCatalog(catalogItems(), { contents: { 'c-final-msg': '# Done\n\nAll **complete**.' } })
    renderModal()
    const rendered = await screen.findByTestId('md-rendered')
    expect(within(rendered).getByRole('heading', { level: 1 }).textContent).toBe('Done')
    expect(within(rendered).getByText('complete').tagName).toBe('STRONG')
  })

  it('each record is independently viewable: assignment, checkpoint, intermediate, both finals', async () => {
    stubCatalog(catalogItems())
    const onSelect = vi.fn()
    renderModal({ onSelect })
    await screen.findAllByTestId('communication-item')
    for (const id of ['c-assign', 'c-check', 'c-inter', 'c-final-file', 'c-final-msg']) {
      onSelect.mockClear()
      fireEvent.click(document.querySelector(`[data-communication-id="${id}"]`)!)
      expect(onSelect).toHaveBeenCalledWith(id)
    }
  })

  it('a deep-linked selection fetches its detail directly', async () => {
    stubCatalog(catalogItems(), { contents: { 'c-check': '# Checkpoint\n\nboundary held' } })
    renderModal({ selectedId: 'c-check' })
    const rendered = await screen.findByTestId('md-rendered')
    expect(rendered.textContent).toContain('boundary held')
  })

  it('a deep link to an unknown id is a stable not-found state, not an empty modal', async () => {
    stubCatalog(catalogItems())
    renderModal({ selectedId: 'c-never-existed' })
    const notFound = await screen.findByTestId('detail-not-found')
    expect(notFound.textContent).toContain('not in the catalog')
    expect(screen.getByTestId('communications-modal')).toBeInTheDocument()
    expect(screen.getByTestId('detail-back-to-list')).toBeInTheDocument()
  })

  it('report scope is shown as an author claim, never as a task outcome', async () => {
    stubCatalog(catalogItems())
    renderModal()
    const disclaimer = await screen.findByTestId('scope-disclaimer')
    expect(disclaimer.textContent).toContain('author')
    expect(disclaimer.textContent).toContain('does not change')
    expect(disclaimer.textContent).not.toMatch(/satisf|complete task|outcome achieved/i)
  })

  it('an empty catalog is an explicit empty state that states what was observed', async () => {
    // With cond-0477 un-fixed, every shipped conductor writer binds
    // task_occurrence_id = NULL, so the published index binds no occurrence
    // and this endpoint can only answer coverage 'complete', total 0. The
    // wording reports the binding the API actually vouched for — it must not
    // assert that no communications exist anywhere.
    stubCatalog([])
    renderModal()
    const empty = await screen.findByTestId('communications-empty')
    expect(empty).toHaveTextContent('The catalog reports no communications bound to this task occurrence')
    expect(empty.textContent).not.toContain('are recorded for this task')
  })
})

describe('attachments (criterion 2: the ordinary message path)', () => {
  it('lists every named attachment with role, media type, size, digest, redaction', async () => {
    stubCatalog(catalogItems(), { contents: { 'c-msg': 'plain message body' } })
    renderModal({ selectedId: 'c-msg' })
    const rows = await screen.findAllByTestId('attachment-row')
    expect(rows).toHaveLength(2)
    const first = rows[0].textContent!
    expect(first).toContain('notes.md')
    expect(first).toContain('attachment')
    expect(first).toContain('text/markdown')
    expect(first).toContain('1.2 KiB')
    expect(first).toContain('not redacted')
    expect(rows[0].textContent).toContain(`sha256 ${'a'.repeat(64)}`)
    expect(rows[1].textContent).toContain('redacted')
  })

  it('opens an attachment inline with its own rendered/raw/copy/download', async () => {
    stubCatalog(catalogItems(), {
      contents: { 'c-msg': 'body' },
      attachments: { 'att-extra-1': { content: '# Notes\n\nattachment **content**' } },
    })
    renderModal({ selectedId: 'c-msg' })
    const rows = await screen.findAllByTestId('attachment-row')
    fireEvent.click(within(rows[0]).getByTestId('attachment-open'))
    const rendered = await within(rows[0]).findByTestId('md-rendered')
    expect(rendered.textContent).toContain('attachment content')
    expect(within(rows[0]).getByTestId('content-copy')).toBeInTheDocument()
    expect(within(rows[0]).getByTestId('content-download')).toBeInTheDocument()
  })

  it('a failed attachment fetch degrades only that row', async () => {
    stubCatalog(catalogItems(), {
      contents: { 'c-msg': 'body still here' },
      attachments: {
        'att-extra-1': { content: null, status: 503 },
        'att-extra-2': { content: 'transcript bytes' },
      },
    })
    renderModal({ selectedId: 'c-msg' })
    const rows = await screen.findAllByTestId('attachment-row')
    fireEvent.click(within(rows[0]).getByTestId('attachment-open'))
    expect(await within(rows[0]).findByTestId('attachment-error')).toBeInTheDocument()
    // The other row and the body are untouched.
    fireEvent.click(within(rows[1]).getByTestId('attachment-open'))
    expect(await within(rows[1]).findByTestId('content-raw')).toHaveTextContent('transcript bytes')
    // Body + second attachment are the two raw views on screen.
    expect(screen.getAllByTestId('content-raw')).toHaveLength(2)
  })

  it('a quarantined attachment shows the tombstone, never the bytes', async () => {
    stubCatalog(catalogItems(), {
      contents: { 'c-msg': 'body' },
      attachments: { 'att-extra-1': { content: null, reason: 'content-quarantined' } },
    })
    renderModal({ selectedId: 'c-msg' })
    const rows = await screen.findAllByTestId('attachment-row')
    fireEvent.click(within(rows[0]).getByTestId('attachment-open'))
    const tombstone = await within(rows[0]).findByTestId('attachment-tombstone')
    expect(tombstone.textContent).toContain('quarantined')
    expect(tombstone.textContent).not.toContain('missing')
  })
})

describe('content tombstones and detail failures', () => {
  it.each([
    ['content-missing', 'Content missing'],
    ['content-quarantined', 'Content quarantined'],
    ['oversize', 'Too large to serve'],
    ['content-unreadable', 'could not be read'],
    ['content-purged', 'Content unavailable (content-purged).'],
  ])('a %s tombstone renders its own words', async (reason, expected) => {
    stubCatalog(catalogItems(), { contents: { 'c-inter': null }, reasons: { 'c-inter': reason } })
    renderModal({ selectedId: 'c-inter' })
    expect(await screen.findByTestId('content-tombstone')).toHaveTextContent(expected)
  })

  it('a digest mismatch (503) says corrupt and renders nothing', async () => {
    stubCatalog(catalogItems(), {
      detailStatus: { 'c-final-msg': { status: 503, detail: 'content-digest-mismatch' } },
    })
    renderModal({ selectedId: 'c-final-msg' })
    const corrupt = await screen.findByTestId('detail-corrupt')
    expect(corrupt.textContent).toContain('integrity check')
    expect(corrupt.textContent).toContain('corrupt')
    expect(screen.queryByTestId('md-rendered')).toBeNull()
    expect(screen.queryByTestId('content-raw')).toBeNull()
  })

  it('a transient detail failure offers a retry that recovers', async () => {
    let failures = 1
    stubFetch(url => {
      if (url.startsWith('/communications?')) return { body: listEnvelope(catalogItems()) }
      if (url === '/communications/c-final-msg') {
        if (failures-- > 0) return { status: 503, body: { detail: 'communications-catalog-unavailable' } }
        return { body: detailEnvelope(catalogItems()[0], '# recovered') }
      }
      return undefined
    })
    renderModal({ selectedId: 'c-final-msg' })
    expect(await screen.findByTestId('detail-unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('detail-retry'))
    expect(await screen.findByTestId('md-rendered')).toHaveTextContent('recovered')
  })
})

describe('list failure and coverage states', () => {
  it('a missing catalog root is "not installed", silently distinct from failure', async () => {
    stubFetch(url =>
      url.startsWith('/communications?')
        ? { body: listEnvelope([], { coverage: 'unavailable', reasons: [{ source: ROOT, reason: 'missing' }] }) }
        : undefined,
    )
    renderModal()
    expect(await screen.findByTestId('catalog-not-installed')).toHaveTextContent(
      'No communications catalog is installed',
    )
    expect(screen.queryByTestId('list-error')).toBeNull()
  })

  it('an unreadable catalog root is a named state with a retry', async () => {
    let calls = 0
    stubFetch(url => {
      if (url.startsWith('/communications?')) {
        calls += 1
        if (calls === 1) {
          return { body: listEnvelope([], { coverage: 'unavailable', reasons: [{ source: ROOT, reason: 'unreadable' }] }) }
        }
        return { body: listEnvelope(catalogItems()) }
      }
      return { body: detailEnvelope(catalogItems()[0], 'x') }
    })
    renderModal()
    expect(await screen.findByTestId('catalog-unreadable')).toHaveTextContent('could not be read')
    fireEvent.click(screen.getByTestId('list-retry'))
    expect((await screen.findAllByTestId('communication-item')).length).toBeGreaterThan(0)
  })

  it('a network failure on the list is an error with a retry', async () => {
    let attempts = 0
    stubFetch(url => {
      if (url.startsWith('/communications?')) {
        attempts += 1
        if (attempts === 1) throw new TypeError('fetch failed')
        return { body: listEnvelope(catalogItems()) }
      }
      return { body: detailEnvelope(catalogItems()[0], 'x') }
    })
    renderModal()
    expect(await screen.findByTestId('list-error')).toHaveTextContent('could not be reached')
    fireEvent.click(screen.getByTestId('list-retry'))
    await screen.findAllByTestId('communication-item')
  })

  it('a 404 on the list route is "not installed", never record-not-found, and offers no retry', async () => {
    // No handler for the list route: every call 404s, as a server build
    // without /communications answers.
    stubFetch(() => undefined)
    renderModal()
    const err = await screen.findByTestId('list-error')
    expect(err.textContent).toContain('No communications catalog is installed')
    expect(err.textContent).not.toContain('not in the catalog')
    expect(screen.queryByTestId('list-retry')).toBeNull()
  })

  it.each([400, 422])('a %i on the list route names the identifier and offers no retry', async status => {
    stubFetch(url =>
      url.startsWith('/communications?') ? { status, body: { detail: 'identifier-invalid' } } : undefined,
    )
    renderModal()
    expect(await screen.findByTestId('list-error')).toHaveTextContent('not a valid catalog identifier')
    expect(screen.queryByTestId('list-retry')).toBeNull()
  })

  it('a malformed list body is an error, never a fake empty list', async () => {
    stubFetch(url => (url.startsWith('/communications?') ? { body: { unexpected: true } } : undefined))
    renderModal()
    expect(await screen.findByTestId('list-error')).toHaveTextContent('cannot read')
    expect(screen.queryByTestId('communications-empty')).toBeNull()
  })

  it('partial coverage shows a banner naming each source and reason', async () => {
    stubCatalog(catalogItems(), {
      listOverrides: {
        coverage: 'partial',
        reasons: [
          { source: 'project-a', reason: 'malformed' },
          { source: 'project-b', reason: 'oversize' },
        ],
      },
    })
    renderModal()
    const banner = await screen.findByTestId('coverage-banner')
    expect(banner.textContent).toContain('incomplete')
    expect(banner.textContent).toContain('project-a: catalog file is malformed')
    expect(banner.textContent).toContain('project-b: catalog exceeds the read budget')
    // The list still renders beneath the banner.
    expect(screen.getAllByTestId('communication-item').length).toBeGreaterThan(0)
  })

  it('truncated coverage is named differently from partial', async () => {
    stubCatalog(catalogItems(), {
      listOverrides: { coverage: 'truncated', reasons: [{ source: ROOT, reason: 'project-limit' }] },
    })
    renderModal()
    const banner = await screen.findByTestId('coverage-banner')
    expect(banner.textContent).toContain('truncated')
    expect(banner.textContent).toContain('project limit')
  })

  it('a first page of a >50-record truncated list with no reasons renders no trailing colon', async () => {
    stubCatalog(catalogItems().slice(0, 2), {
      listOverrides: { coverage: 'truncated', reasons: [] },
    })
    renderModal()
    const banner = await screen.findByTestId('coverage-banner')
    expect(banner.textContent).toContain('truncated')
    // No colon when there is no reason line following it.
    expect(banner.textContent).not.toContain(':')
    expect(banner.textContent).toContain('not every row could be served')
  })
})

describe('paging', () => {
  it('passes the opaque cursor back verbatim and appends in server order', async () => {
    const first = catalogItems().slice(0, 2)
    const second = catalogItems().slice(2, 4)
    const fetchMock = stubFetch(url => {
      if (url === '/communications?task_occurrence_id=task-occ-1') {
        return { body: listEnvelope(first, { next_cursor: 'opaque+token/1=', total: 4 }) }
      }
      if (url === `/communications?task_occurrence_id=task-occ-1&cursor=${encodeURIComponent('opaque+token/1=')}`) {
        return { body: listEnvelope(second, { total: 4 }) }
      }
      const m = /^\/communications\/([^/]+)$/.exec(url)
      if (m) {
        const item = [...first, ...second].find(i => i.communication_id === m[1])
        if (item) return { body: detailEnvelope(item, 'x') }
      }
      return undefined
    })
    renderModal()
    await screen.findAllByTestId('communication-item')
    fireEvent.click(screen.getByTestId('load-more'))
    await waitFor(() => expect(screen.getAllByTestId('communication-item')).toHaveLength(4))
    const ids = screen.getAllByTestId('communication-item').map(el => el.getAttribute('data-communication-id'))
    expect(ids).toEqual(['c-final-msg', 'c-final-file', 'c-inter', 'c-check'])
    expect(fetchMock).toHaveBeenCalledWith(
      `/communications?task_occurrence_id=task-occ-1&cursor=${encodeURIComponent('opaque+token/1=')}`,
      expect.anything(),
    )
  })

  it('a failed next page keeps the loaded rows and says so inline', async () => {
    const first = catalogItems().slice(0, 2)
    stubFetch(url => {
      if (url === '/communications?task_occurrence_id=task-occ-1') {
        return { body: listEnvelope(first, { next_cursor: 'c2', total: 4 }) }
      }
      if (url.includes('cursor=c2')) throw new TypeError('fetch failed')
      const m = /^\/communications\/([^/]+)$/.exec(url)
      if (m) {
        const item = first.find(i => i.communication_id === m[1])
        if (item) return { body: detailEnvelope(item, 'x') }
      }
      return undefined
    })
    renderModal()
    await screen.findAllByTestId('communication-item')
    fireEvent.click(screen.getByTestId('load-more'))
    expect(await screen.findByTestId('load-more-error')).toHaveTextContent('unaffected')
    expect(screen.getAllByTestId('communication-item')).toHaveLength(2)
  })
})

describe('accessibility and window management', () => {
  it('is a labelled modal dialog with initial focus inside', async () => {
    stubCatalog(catalogItems())
    renderModal()
    const dialog = await screen.findByTestId('communications-modal')
    expect(dialog).toHaveAttribute('role', 'dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog.getAttribute('aria-label')).toContain(TASK)
    await waitFor(() => expect(document.activeElement).toBe(dialog))
  })

  it('closes on Escape and on backdrop click', async () => {
    stubCatalog(catalogItems())
    const { onClose } = renderModal()
    const dialog = await screen.findByTestId('communications-modal')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    cleanup()
    const second = renderModal()
    const backdrop = document.querySelector('.absolute.inset-0.bg-black\\/60')!
    fireEvent.click(backdrop)
    expect(second.onClose).toHaveBeenCalledTimes(1)
  })

  it('restores focus to the invoking control on close', async () => {
    stubCatalog(catalogItems())
    function Harness() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button data-testid="invoke" onClick={() => setOpen(true)}>open</button>
          {open && (
            <CommunicationsModal
              taskOccurrenceId={TASK}
              selectedId={null}
              onSelect={() => {}}
              onClose={() => setOpen(false)}
            />
          )}
        </>
      )
    }
    render(<Harness />)
    const invoke = screen.getByTestId('invoke')
    invoke.focus()
    fireEvent.click(invoke)
    const dialog = await screen.findByTestId('communications-modal')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(document.activeElement).toBe(invoke))
  })

  it('traps Tab within the dialog', async () => {
    stubCatalog(catalogItems())
    renderModal()
    const dialog = await screen.findByTestId('communications-modal')
    await screen.findAllByTestId('communication-item')
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>('a[href], button, [tabindex]')).filter(
      el => !el.hasAttribute('disabled') && el.tabIndex >= 0,
    )
    const last = focusables[focusables.length - 1]
    last.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(focusables[0])
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('navigates the list with ArrowUp/ArrowDown/Home/End', async () => {
    stubCatalog(catalogItems())
    renderModal()
    const options = await screen.findAllByTestId('communication-item')
    const listbox = screen.getByRole('listbox')
    options[0].focus()
    fireEvent.keyDown(listbox, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[1])
    fireEvent.keyDown(listbox, { key: 'End' })
    expect(document.activeElement).toBe(options[options.length - 1])
    fireEvent.keyDown(listbox, { key: 'Home' })
    expect(document.activeElement).toBe(options[0])
    fireEvent.keyDown(listbox, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(options[0])
  })

  it('mobile reader view backs out to the list and clears the selection', async () => {
    stubCatalog(catalogItems())
    const onSelect = vi.fn()
    renderModal({ onSelect })
    const options = await screen.findAllByTestId('communication-item')
    fireEvent.click(options[1])
    expect(onSelect).toHaveBeenCalledWith('c-final-file')
    fireEvent.click(await screen.findByTestId('reader-back'))
    expect(onSelect).toHaveBeenCalledWith(null)
  })
})
