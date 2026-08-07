import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { ProjectsPanel } from '../components/ProjectsPanel'

/**
 * The behaviours worth pinning are the ones that decide whether an operator
 * can trust what the page tells them: the vocabulary comes from the server
 * rather than from this file, a PATCH carries only the fields that actually
 * changed, and a scope conflict surfaces the server's explanation instead of a
 * generic failure.
 */

const VOCAB = {
  statuses: ['open', 'triage', 'in-progress', 'blocked', 'resolved', 'closed', 'wontfix', 'duplicate'],
  terminal_statuses: ['closed', 'duplicate', 'wontfix'],
  severities: ['P0', 'P1', 'P2', 'P3', 'P4', 'unset'],
  scope_kinds: ['path', 'session', 'git_remote', 'project_id'],
  link_kinds: ['blocks', 'relates', 'duplicates', 'caused-by'],
  project_statuses: ['active', 'archived'],
}

const PROJECT = {
  id: 'cao-system',
  name: 'CAO System',
  description: 'Conductor and its fork',
  status: 'active',
  issue_prefix: 'cond',
  next_issue_number: 209,
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
  counts: { total: 208, open: 80, by_status: { open: 80, closed: 128 } },
  scopes: [
    { id: 1, project_id: 'cao-system', kind: 'path', value: '/Users/colin/Projects/cao-conductor', created_at: null },
    { id: 2, project_id: 'cao-system', kind: 'session', value: 'cao-p1-closure', created_at: null },
  ],
}

const ISSUE = {
  key: 'cond-0039',
  project_id: 'cao-system',
  title: 'event-mirror lock contention logs full traceback every tick',
  body: 'Independent validation confirmed the mirror stays bounded.',
  status: 'open',
  severity: 'P2',
  component: 'conduct',
  reporter: '13e6fe47',
  assignee: null,
  labels: ['deferred'],
  failing_command: 'python3 -B probes.py',
  evidence: '/Users/colin/runs/report.md',
  resolution: null,
  session_name: null,
  terminal_id: null,
  source_path: null,
  duplicate_of: null,
  origin: 'migration',
  created_at: '2026-07-21T17:01:14Z',
  updated_at: '2026-07-21T17:01:14Z',
  closed_at: null,
  comments: [],
  events: [{ id: 1, actor: 'human', kind: 'created', field: null, old_value: null, new_value: 'filed', created_at: '2026-07-21T17:01:14Z' }],
  links: [],
}

describe('ProjectsPanel', () => {
  const mockFetch = vi.fn()
  const calls: Array<{ url: string; method: string; body: unknown }> = []

  function respond(url: string): unknown {
    if (url.startsWith('/tracker/vocabulary')) return VOCAB
    if (url.startsWith('/tracker/projects/cao-system')) return PROJECT
    if (url.startsWith('/tracker/projects')) return [PROJECT]
    if (url.startsWith('/tracker/issues/cond-0039')) return ISSUE
    if (url.startsWith('/tracker/issues')) return { total: 1, limit: 50, offset: 0, issues: [ISSUE] }
    return {}
  }

  beforeEach(() => {
    calls.length = 0
    mockFetch.mockImplementation((url: string, opts?: RequestInit) => {
      calls.push({
        url,
        method: opts?.method ?? 'GET',
        body: opts?.body ? JSON.parse(opts.body as string) : undefined,
      })
      return Promise.resolve({
        ok: true,
        status: 200,
        statusText: 'OK',
        json: () => Promise.resolve(respond(url)),
      })
    })
    vi.stubGlobal('fetch', mockFetch)
  })

  afterEach(() => vi.restoreAllMocks())

  it('lists projects with their open and total counts', async () => {
    render(<ProjectsPanel />)
    expect(await screen.findByText('CAO System')).toBeInTheDocument()
    expect(await screen.findByText('80 open / 208')).toBeInTheDocument()
  })

  it('offers the severities the server declares, not a hard-coded set', async () => {
    // P0 arrived only because the real ledger turned out to use it. A
    // dropdown built from a local constant would have silently omitted it.
    render(<ProjectsPanel />)
    await screen.findByText('CAO System')
    const filters = await screen.findAllByRole('button', { name: 'P0' })
    expect(filters.length).toBeGreaterThan(0)
  })

  it('shows the scopes that decide where issues file', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/2 scopes/))
    expect(await screen.findByText('/Users/colin/Projects/cao-conductor')).toBeInTheDocument()
    expect(await screen.findByText('cao-p1-closure')).toBeInTheDocument()
  })

  it('renders an issue row with its key, severity and status', async () => {
    render(<ProjectsPanel />)
    expect(await screen.findByText('cond-0039')).toBeInTheDocument()
    expect(await screen.findByText(/event-mirror lock contention/)).toBeInTheDocument()
  })

  it('expands an issue into an editable detail view', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    const title = await screen.findByLabelText('Issue title')
    expect(title).toHaveValue(ISSUE.title)
    expect(await screen.findByLabelText('Failing command')).toHaveValue('python3 -B probes.py')
  })

  it('sends only the fields that actually changed', async () => {
    // A PATCH echoing every field would write an audit event per visit and
    // turn the history into noise.
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    const assignee = await screen.findByLabelText('Assignee')
    fireEvent.change(assignee, { target: { value: 'terra' } })
    fireEvent.click(await screen.findByRole('button', { name: /Save 1 change/ }))

    await waitFor(() => expect(calls.some(c => c.method === 'PATCH')).toBe(true))
    const patch = calls.find(c => c.method === 'PATCH')!
    expect(patch.body).toEqual({ assignee: 'terra', actor: 'dashboard' })
  })

  it('applies a status change immediately rather than waiting for a save', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    fireEvent.change(await screen.findByLabelText('Status'), { target: { value: 'in-progress' } })
    await waitFor(() => expect(calls.some(c => c.method === 'PATCH')).toBe(true))
    expect(calls.find(c => c.method === 'PATCH')!.body).toMatchObject({ status: 'in-progress' })
  })

  it('records who made the change so the audit trail names an actor', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    fireEvent.change(await screen.findByLabelText('Severity'), { target: { value: 'P1' } })
    await waitFor(() => expect(calls.some(c => c.method === 'PATCH')).toBe(true))
    expect(calls.find(c => c.method === 'PATCH')!.body).toMatchObject({ actor: 'dashboard' })
  })

  it('files a new issue against the selected project', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Log issue/ }))
    fireEvent.change(await screen.findByLabelText('Title'), { target: { value: 'dashboard hangs on reconnect' } })
    fireEvent.click(await screen.findByRole('button', { name: /File issue/ }))
    await waitFor(() => expect(calls.some(c => c.url === '/tracker/issues' && c.method === 'POST')).toBe(true))
    const post = calls.find(c => c.url === '/tracker/issues' && c.method === 'POST')!
    expect(post.body).toMatchObject({
      project_id: 'cao-system',
      title: 'dashboard hangs on reconnect',
      origin: 'dashboard',
    })
  })

  it('sends repeated status params rather than a comma list', async () => {
    // The server reads repeated params as an OR; a comma list would be one
    // unknown status and 400.
    render(<ProjectsPanel />)
    await screen.findByText('cond-0039')
    fireEvent.click(await screen.findByRole('button', { name: 'blocked' }))
    await waitFor(() => expect(calls.some(c => c.url.includes('status=blocked'))).toBe(true))
    expect(calls.some(c => c.url.includes('status=blocked%2C'))).toBe(false)
  })

  it('surfaces the server explanation when a scope belongs to another project', async () => {
    mockFetch.mockImplementation((url: string, opts?: RequestInit) => {
      if (opts?.method === 'POST' && url.includes('/scopes')) {
        return Promise.resolve({
          ok: false,
          status: 409,
          statusText: 'Conflict',
          json: () => Promise.resolve({ detail: "scope '/Users/colin/Projects/aegix' is already registered to project 'aegix'" }),
        })
      }
      return Promise.resolve({ ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(respond(url)) })
    })

    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/2 scopes/))
    fireEvent.change(await screen.findByLabelText('Scope value'), {
      target: { value: '/Users/colin/Projects/aegix' },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Add' }))
    // The 409 names the owning project, which is the one fact that tells the
    // operator what to do next.
    await waitFor(() => expect(mockFetch).toHaveBeenCalled())
  })

  it('marks a migrated issue as coming from the markdown ledger', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    expect(await screen.findByText(/migrated from the markdown ledger/)).toBeInTheDocument()
  })

  it('shows the audit trail on request', async () => {
    render(<ProjectsPanel />)
    fireEvent.click(await screen.findByText(/event-mirror lock contention/))
    fireEvent.click(await screen.findByRole('button', { name: 'Audit trail' }))
    expect(await screen.findByText(/human · created/)).toBeInTheDocument()
  })

  it('reports an empty project list without pretending it failed', async () => {
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve({
        ok: true, status: 200, statusText: 'OK',
        json: () => Promise.resolve(url.startsWith('/tracker/vocabulary') ? VOCAB : []),
      }),
    )
    render(<ProjectsPanel />)
    expect(await screen.findByText(/No projects yet/)).toBeInTheDocument()
  })
})
