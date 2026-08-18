// Entry points and deep links (design §8.1), at the DashboardHome level.
//
// The load-bearing claims pinned here:
//   * a chip is an entry point IFF its subject carries a task_occurrence_id —
//     and only when a catalog answered the probe;
//   * a deployment without a conductor catalog renders byte-identical DOM to
//     before: no buttons, no actionable chips, no fetches beyond today's;
//   * the modal's open/selection state round-trips through
//     ?task_occurrence_id=&communication_id= for reload, Back, and copied links.
//
// FIXTURE DISCLOSURE — cond-0477: Every fixture in this file that carries a
// bound task_occurrence_id models a state no shipped conductor writer
// currently produces — all current writers record task_occurrence_id = NULL
// (cond-0477). The fork's contract is the published index format and a bound
// occurrence is a legal value of it. The API reports `coverage:"complete"`,
// `total:0` with no reason code for the unbound case, so the reader cannot
// distinguish "unbound" from "genuinely empty" — a known limitation that
// resolves when cond-0477 lands. The empty/unbound path is itself covered by
// the "missing catalog root" and empty-catalog tests (communicationsModal).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { DashboardHome } from '../components/DashboardHome'
import { useStore } from '../store'
import { projectedTerminal } from './projectedTerminal'
import type { Annotation, AnnotationsResponse } from '../api'

vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))

const SESSION = { id: 'sess-1', name: 'cao-fleet', status: 'active' }
const GENERATION = 'term-001-gen-1'
const TERMINAL = projectedTerminal({ id: 'term-001', generation: GENERATION, status: 'idle' })
const TASK = 'task-occ-9'
const FUTURE = '2999-01-01T00:00:00Z'

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
    details: {},
    source: 'project',
    ...overrides,
  }
}

function annotationsPayload(annotations: Annotation[]): AnnotationsResponse {
  return {
    annotation_schema: 'cao-annotations-v1',
    coverage: 'complete',
    sources_read: 1,
    sources_failed: 0,
    items_dropped: 0,
    items_omitted: 0,
    reasons: [],
    annotations,
  }
}

function commItem(id: string, overrides: Record<string, unknown> = {}) {
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
    authored_at: '2026-08-18T00:00:00Z',
    recorded_at: '2026-08-18T00:00:00Z',
    title: `Record ${id}`,
    delivery_state: 'delivered',
    visibility: 'internal',
    request_key: null,
    supersedes_communication_id: null,
    superseded_by: null,
    body: null,
    documents: [],
    ...overrides,
  }
}

function listEnvelope(items: ReturnType<typeof commItem>[], overrides: Record<string, unknown> = {}) {
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

type Handler = (url: string) => { status?: number; body?: unknown } | undefined

function stubFetch(handler: Handler) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    calls.push(url)
    const res = handler(url)
    const status = res?.status ?? (res ? 200 : 404)
    const body = res?.body ?? { detail: 'not found' }
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: `status ${status}`,
      json: async () => body,
    } as Response
  }))
  return calls
}

/** The dashboard's ordinary routes plus the given annotations/communications behavior. */
function stubDashboard(opts: {
  annotations?: AnnotationsResponse
  communications?: Handler
} = {}) {
  return stubFetch(url => {
    if (url === '/sessions/cao-fleet') return { body: { session: SESSION, terminals: [TERMINAL] } }
    if (url === '/annotations') {
      return opts.annotations ? { body: opts.annotations } : undefined
    }
    if (url.startsWith('/communications')) return opts.communications?.(url)
    if (url === `/terminals/${TERMINAL.id}`) {
      return { body: { ...TERMINAL, name: TERMINAL.id, session_name: 'cao-fleet' } }
    }
    if (url === '/agents/profiles') return { body: [] }
    return { body: {} }
  })
}

async function renderDashboard() {
  const view = render(<DashboardHome onNavigate={() => {}} />)
  await screen.findByText('cao-fleet')
  await waitFor(() => {
    expect(useStore.getState().terminalStatuses[TERMINAL.id]).toBeTruthy()
  })
  return view
}

beforeEach(() => {
  useStore.setState({ sessions: [SESSION], terminalStatuses: {} })
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useStore.setState({ sessions: [], terminalStatuses: {} })
  window.history.replaceState(null, '', '/')
})

describe('chips become entry points only with a task occurrence and a catalog', () => {
  it('a chip with a task_occurrence_id becomes a button once the probe answers', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
      communications: url =>
        url.startsWith('/communications?') ? { body: listEnvelope([commItem('c-1')]) } : { body: { communication: commItem('c-1'), content: 'x', reason: null } },
    })
    await renderDashboard()
    const chip = (await waitFor(() => {
      const found = screen.getAllByTestId('annotation-chip').find(el => el.getAttribute('data-actionable') === 'true')
      expect(found).toBeTruthy()
      return found!
    })) as HTMLElement
    expect(chip.tagName).toBe('BUTTON')
    fireEvent.click(chip)
    expect(await screen.findByTestId('communications-modal')).toBeInTheDocument()
  })

  it('a chip without a task_occurrence_id stays an inert span even with a catalog', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation()]),
      communications: url =>
        url.startsWith('/communications?') ? { body: listEnvelope([]) } : undefined,
    })
    await renderDashboard()
    await waitFor(() => expect(screen.getAllByTestId('annotation-chip').length).toBe(1))
    const chip = screen.getByTestId('annotation-chip')
    expect(chip.tagName).toBe('SPAN')
    expect(chip.getAttribute('role')).toBe('note')
    expect(chip.getAttribute('data-actionable')).toBeNull()
    expect(screen.queryByTestId('communications-button')).toBeNull()
  })

  it('a missing catalog root keeps every chip inert and no row control renders', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
      communications: url =>
        url.startsWith('/communications?')
          ? {
              body: listEnvelope([], {
                coverage: 'unavailable',
                reasons: [{ source: 'conductor-state-root', reason: 'missing' }],
              }),
            }
          : undefined,
    })
    await renderDashboard()
    await waitFor(() => expect(screen.getAllByTestId('annotation-chip').length).toBe(1))
    // Give the probe a turn to resolve.
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/communications?'), expect.anything()))
    const chip = screen.getByTestId('annotation-chip')
    expect(chip.tagName).toBe('SPAN')
    expect(chip.getAttribute('data-actionable')).toBeNull()
    expect(screen.queryByTestId('communications-button')).toBeNull()
  })

  it('a server without the route (404) behaves exactly like no catalog', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
      // communications handler omitted: every /communications call 404s.
    })
    await renderDashboard()
    await waitFor(() => expect(screen.getAllByTestId('annotation-chip').length).toBe(1))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/communications?'), expect.anything()))
    expect(screen.getByTestId('annotation-chip').tagName).toBe('SPAN')
    expect(screen.queryByTestId('communications-button')).toBeNull()
  })

  it('an unreadable catalog root still arms the entry points (the modal carries the named state)', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
      communications: url =>
        url.startsWith('/communications?')
          ? {
              body: listEnvelope([], {
                coverage: 'unavailable',
                reasons: [{ source: 'conductor-state-root', reason: 'unreadable' }],
              }),
            }
          : undefined,
    })
    await renderDashboard()
    await waitFor(() => {
      const chip = screen.getByTestId('annotation-chip')
      expect(chip.getAttribute('data-actionable')).toBe('true')
    })
    fireEvent.click(screen.getByTestId('annotation-chip'))
    expect(await screen.findByTestId('catalog-unreadable')).toBeInTheDocument()
    expect(screen.getByTestId('list-retry')).toBeInTheDocument()
  })

  it('with no task annotations at all, /communications is never fetched', async () => {
    const calls = stubDashboard({ annotations: annotationsPayload([annotation()]) })
    await renderDashboard()
    await waitFor(() => expect(screen.getAllByTestId('annotation-chip').length).toBe(1))
    expect(calls.filter(u => u.startsWith('/communications'))).toEqual([])
  })
})

describe('probe latch — a build property never changes without a restart', () => {
  async function flushMicrotasks() {
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
  }

  async function oneMorePoll() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5200)
    })
    await flushMicrotasks()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushMicrotasks()
  }

  const commCalls = (calls: string[]) => calls.filter(u => u.startsWith('/communications'))
  const annotationCalls = (calls: string[]) => calls.filter(u => u === '/annotations')

  it('latches the probe on a 404: a later poll issues no second GET', async () => {
    vi.useFakeTimers()
    try {
      const calls = stubDashboard({
        annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
        // communications handler omitted: every /communications call 404s — a
        // property of this server build, unchangeable without a restart.
      })
      render(<DashboardHome onNavigate={() => {}} />)
      for (let i = 0; i < 40; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
        if (screen.queryByText('cao-fleet') && useStore.getState().terminalStatuses[TERMINAL.id]) break
      }
      for (let i = 0; i < 40; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length >= 1) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
      }
      expect(commCalls(calls).length).toBeGreaterThanOrEqual(1)
      const commsBefore = commCalls(calls).length
      const annotationsBefore = annotationCalls(calls).length
      await oneMorePoll()
      // The annotations poll really fired again; the probe did not re-fire.
      expect(annotationCalls(calls).length).toBeGreaterThan(annotationsBefore)
      expect(commCalls(calls)).toHaveLength(commsBefore)
      expect(screen.getByTestId('annotation-chip').tagName).toBe('SPAN')
    } finally {
      vi.useRealTimers()
    }
  })

  it('latches the probe on an unreadable body for the same reason', async () => {
    vi.useFakeTimers()
    try {
      const calls = stubDashboard({
        annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
        communications: url => (url.startsWith('/communications?') ? { body: { unexpected: true } } : undefined),
      })
      render(<DashboardHome onNavigate={() => {}} />)
      for (let i = 0; i < 40; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
        if (screen.queryByText('cao-fleet') && useStore.getState().terminalStatuses[TERMINAL.id]) break
      }
      for (let i = 0; i < 40; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length >= 1) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
      }
      expect(commCalls(calls).length).toBeGreaterThanOrEqual(1)
      const commsBefore = commCalls(calls).length
      const annotationsBefore = annotationCalls(calls).length
      await oneMorePoll()
      expect(annotationCalls(calls).length).toBeGreaterThan(annotationsBefore)
      expect(commCalls(calls)).toHaveLength(commsBefore)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a transient probe error is NOT latched: the next poll re-probes', async () => {
    vi.useFakeTimers()
    try {
      let failed = false
      const calls = stubDashboard({
        annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
        communications: url => {
          if (url.startsWith('/communications?')) {
            if (!failed) {
              failed = true
              throw new TypeError('fetch failed')
            }
            return { body: listEnvelope([commItem('c-1')]) }
          }
          return { body: { communication: commItem('c-1'), content: 'x', reason: null } }
        },
      })
      render(<DashboardHome onNavigate={() => {}} />)
      for (let i = 0; i < 40; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
        if (screen.queryByText('cao-fleet') && useStore.getState().terminalStatuses[TERMINAL.id]) break
      }
      for (let i = 0; i < 40; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length >= 1) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
      }
      expect(commCalls(calls).length).toBeGreaterThanOrEqual(1)
      const commsBefore = commCalls(calls).length
      await oneMorePoll()
      // Wait for the re-probe's microtasks to settle.
      for (let i = 0; i < 20; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length > commsBefore) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
      }
      expect(commCalls(calls)).toHaveLength(commsBefore + 1)
      // The re-probe's answer armed the entry point.
      for (let i = 0; i < 20; i++) {
        await flushMicrotasks()
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        if (screen.queryByTestId('annotation-chip')?.getAttribute('data-actionable') === 'true') break
      }
      expect(screen.getByTestId('annotation-chip').getAttribute('data-actionable')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a "not installed" answer is NOT latched: a catalog appearing mid-session is picked up', async () => {
    vi.useFakeTimers()
    try {
      let installed = false
      const calls = stubDashboard({
        annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
        communications: url => {
          if (url.startsWith('/communications?')) {
            return installed
              ? { body: listEnvelope([commItem('c-1')]) }
              : {
                  body: listEnvelope([], {
                    coverage: 'unavailable',
                    reasons: [{ source: 'conductor-state-root', reason: 'missing' }],
                  }),
                }
          }
          return { body: { communication: commItem('c-1'), content: 'x', reason: null } }
        },
      })
      render(<DashboardHome onNavigate={() => {}} />)
      for (let i = 0; i < 40; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
        if (screen.queryByText('cao-fleet') && useStore.getState().terminalStatuses[TERMINAL.id]) break
      }
      for (let i = 0; i < 40; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length >= 1) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        await flushMicrotasks()
      }
      expect(commCalls(calls).length).toBeGreaterThanOrEqual(1)
      const commsBefore = commCalls(calls).length
      installed = true
      await oneMorePoll()
      for (let i = 0; i < 20; i++) {
        await flushMicrotasks()
        if (commCalls(calls).length > commsBefore) break
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
      }
      expect(commCalls(calls)).toHaveLength(commsBefore + 1)
      for (let i = 0; i < 20; i++) {
        await flushMicrotasks()
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10)
        })
        if (screen.queryByTestId('annotation-chip')?.getAttribute('data-actionable') === 'true') break
      }
      expect(screen.getByTestId('annotation-chip').getAttribute('data-actionable')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('the row control', () => {
  it('renders beside the work-state control with the producer count facet', async () => {
    stubDashboard({
      annotations: annotationsPayload([
        annotation({
          subject: { type: 'terminal', terminal_id: 'term-001', generation: GENERATION, task_occurrence_id: TASK },
          details: { communication_count: '4', latest_communication_kind: 'report' },
        }),
      ]),
      communications: url =>
        url.startsWith('/communications?') ? { body: listEnvelope([commItem('c-1')]) } : { body: { communication: commItem('c-1'), content: 'x', reason: null } },
    })
    await renderDashboard()
    const button = await screen.findByTestId('communications-button')
    expect(button.textContent).toContain('4')
    expect(button.getAttribute('title')).toContain('report')
    fireEvent.click(button)
    const modal = await screen.findByTestId('communications-modal')
    expect(modal.getAttribute('aria-label')).toContain(TASK)
  })
})

describe('deep links', () => {
  it('a link with both ids opens the modal with the record selected', async () => {
    window.history.replaceState(null, '', `/?task_occurrence_id=${TASK}&communication_id=c-1`)
    const mdBody = {
      attachment_id: 'att-body-1',
      document_id: 'doc-1',
      role: 'body',
      display_name: 'report.md',
      media_type: 'text/markdown',
      sha256: 'b'.repeat(64),
      byte_size: 14,
      blob_id: 'b'.repeat(64),
      content_state: 'present',
      capture_kind: 'inline-message',
      redaction_applied: false,
    }
    const calls = stubDashboard({
      annotations: annotationsPayload([]),
      communications: url => {
        if (url.startsWith('/communications?')) return { body: listEnvelope([commItem('c-1', { body: mdBody })]) }
        if (url === '/communications/c-1') {
          return { body: { communication: commItem('c-1', { body: mdBody }), content: '# deep linked', reason: null } }
        }
        return undefined
      },
    })
    await renderDashboard()
    expect(await screen.findByTestId('communications-modal')).toBeInTheDocument()
    expect(await screen.findByTestId('md-rendered')).toHaveTextContent('deep linked')
    expect(calls).toContain('/communications/c-1')
  })

  it('a deep link on a catalog-free deployment opens the modal to the named empty state', async () => {
    window.history.replaceState(null, '', `/?task_occurrence_id=${TASK}&communication_id=c-1`)
    stubDashboard({
      annotations: annotationsPayload([]),
      communications: url =>
        url.startsWith('/communications?')
          ? {
              body: listEnvelope([], {
                coverage: 'unavailable',
                reasons: [{ source: 'conductor-state-root', reason: 'missing' }],
              }),
            }
          : undefined,
    })
    await renderDashboard()
    expect(await screen.findByTestId('catalog-not-installed')).toBeInTheDocument()
    // Closeable, always.
    fireEvent.click(screen.getByTestId('communications-close'))
    expect(screen.queryByTestId('communications-modal')).toBeNull()
  })

  it('selection and close round-trip through the URL', async () => {
    stubDashboard({
      annotations: annotationsPayload([annotation({ subject: { type: 'task', task_occurrence_id: TASK } })]),
      communications: url => {
        if (url.startsWith('/communications?')) return { body: listEnvelope([commItem('c-1'), commItem('c-2')]) }
        const m = /^\/communications\/([^/]+)$/.exec(url)
        if (m) return { body: { communication: commItem(m[1]), content: `body of ${m[1]}`, reason: null } }
        return undefined
      },
    })
    await renderDashboard()
    const chip = await waitFor(() => {
      const found = screen.getAllByTestId('annotation-chip').find(el => el.getAttribute('data-actionable') === 'true')
      expect(found).toBeTruthy()
      return found!
    })
    fireEvent.click(chip)
    await screen.findByTestId('communications-modal')
    expect(window.location.search).toContain(`task_occurrence_id=${TASK}`)
    expect(window.location.search).not.toContain('communication_id')

    // Selecting a row pushes the opaque id into the URL.
    fireEvent.click(document.querySelector('[data-communication-id="c-2"]')!)
    await waitFor(() => expect(window.location.search).toContain('communication_id=c-2'))

    // Browser Back returns to the list-only selection.
    window.history.back()
    await waitFor(() => expect(window.location.search).not.toContain('communication_id'))
    expect(screen.getByTestId('communications-modal')).toBeInTheDocument()

    // Closing removes every trace of the modal from the URL.
    fireEvent.click(screen.getByTestId('communications-close'))
    await waitFor(() => expect(window.location.search).not.toContain('task_occurrence_id'))
    expect(screen.queryByTestId('communications-modal')).toBeNull()
  })

  it('a copied link to an unknown record opens the stable not-found state', async () => {
    window.history.replaceState(null, '', `/?task_occurrence_id=${TASK}&communication_id=c-nope`)
    stubDashboard({
      annotations: annotationsPayload([]),
      communications: url => {
        if (url.startsWith('/communications?')) return { body: listEnvelope([commItem('c-1')]) }
        if (url === '/communications/c-1') {
          return { body: { communication: commItem('c-1'), content: 'x', reason: null } }
        }
        return undefined // c-nope 404s
      },
    })
    await renderDashboard()
    expect(await screen.findByTestId('detail-not-found')).toBeInTheDocument()
    expect(screen.getByTestId('communications-modal')).toBeInTheDocument()
    // Leaveable: back to the list clears the bad id from the URL.
    fireEvent.click(screen.getByTestId('detail-back-to-list'))
    await waitFor(() => expect(window.location.search).not.toContain('c-nope'))
    expect(window.location.search).toContain(`task_occurrence_id=${TASK}`)
  })
})
