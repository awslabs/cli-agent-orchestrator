import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { ProfilesPanel } from '../components/ProfilesPanel'
import { useStore } from '../store'
import { ProfileEditorModal } from '../components/ProfileEditorModal'

const okJson = (data: unknown) => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  json: () => Promise.resolve(data),
  text: () => Promise.resolve(JSON.stringify(data)),
})

const noContent = () => ({
  ok: true,
  status: 204,
  statusText: 'No Content',
  json: () => Promise.reject(new Error('no body')),
  text: () => Promise.resolve(''),
})

const errJson = (status: number, detail: unknown) => ({
  ok: false,
  status,
  statusText: 'Error',
  json: () => Promise.resolve({ detail }),
  text: () => Promise.resolve(JSON.stringify({ detail })),
})

const CATALOG = [
  { name: 'local-agent', description: 'Mine', source: 'local' },
  { name: 'builtin-agent', description: 'Shipped', source: 'built-in' },
]

// Placeholder deliberately present: the editor must round-trip it verbatim.
const SOURCE = `---\nname: local-agent\ndescription: Mine\napi_key: \${MY_KEY}\n---\n\n# Do local things.\n`

const DETAIL = { name: 'local-agent', description: 'Mine' }

function routedFetch(overrides: Record<string, (url: string, opts?: any) => any> = {}) {
  return vi.fn(async (url: string, opts?: any) => {
    for (const [needle, handler] of Object.entries(overrides)) {
      if (url.includes(needle)) return handler(url, opts)
    }
    if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
    if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
    if (opts?.method === 'DELETE') return noContent()
    if (opts?.method === 'PUT') return okJson({ name: 'local-agent', warnings: [] })
    if (url.endsWith('/agents/profiles') && opts?.method === 'POST') return okJson({ name: JSON.parse(opts.body).name, warnings: [] })
    if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson(DETAIL)
    if (url.includes('/agents/profiles')) return okJson(CATALOG)
    return okJson([])
  })
}

afterEach(() => vi.restoreAllMocks())

async function openDetail(name: string) {
  render(<ProfilesPanel />)
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(name) }))
  return await screen.findByTestId('profile-detail')
}

describe('Stage 4 — action visibility by source', () => {
  it('local profile shows Edit, Clone, and Delete', async () => {
    vi.stubGlobal('fetch', routedFetch())
    const detail = await openDetail('local-agent')
    expect(within(detail).getByRole('button', { name: /edit/i })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: /^clone$/i })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: /delete/i })).toBeInTheDocument()
  })

  it('built-in profile is read-only: only Clone to customise', async () => {
    vi.stubGlobal('fetch', routedFetch())
    const detail = await openDetail('builtin-agent')
    expect(within(detail).queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: /delete/i })).not.toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: /clone to customise/i })).toBeInTheDocument()
  })
})

describe('Stage 4 — edit flow', () => {
  it('loads the unresolved source (placeholders intact) and PUTs the edited document', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))

    const editor = await screen.findByRole('textbox', { name: /profile source/i })
    // The ${MY_KEY} placeholder must come from /source, not the resolved parse
    expect(editor).toHaveValue(SOURCE)
    const sourceCalls = mock.mock.calls.filter(([u]) => String(u).includes('/source'))
    expect(sourceCalls).toHaveLength(1)

    fireEvent.change(editor, { target: { value: SOURCE.replace('Do local things.', 'Do MORE things.') } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})

    const put = mock.mock.calls.find(([, o]) => o?.method === 'PUT')
    expect(put).toBeTruthy()
    expect(String(put![0])).toContain('/agents/profiles/local-agent')
    const body = JSON.parse(put![1].body)
    expect(body.content).toContain('Do MORE things.')
    expect(body.content).toContain('api_key: ${MY_KEY}')
    expect(body.name).toBeUndefined() // PUT body carries content only; the path is authoritative
  })

  it('surfaces a 404 when the PUT target is not in the local store', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/local-agent': (url, opts) =>
        opts?.method === 'PUT'
          ? errJson(404, "Profile 'local-agent' not found in the local store.")
          : String(url).includes('/source')
            ? okJson({ name: 'local-agent', content: SOURCE })
            : okJson(DETAIL),
    }))
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent(/not found in the local store/)
  })

  it('a transport failure on the pre-save validate falls through to the authoritative save', async () => {
    // The pre-save validate is a UX gate; the write route re-validates.
    // A network failure/timeout on the gate must not hard-block the save
    // behind a phantom 'Validation failed' -- real 4xx findings still block.
    const mock = routedFetch({
      '/agents/profiles/validate': () => Promise.reject(new TypeError('network down')),
    })
    vi.stubGlobal('fetch', mock)
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    // The PUT was issued despite the failed pre-check
    expect(mock.mock.calls.some(([, o]) => o?.method === 'PUT')).toBe(true)
    expect(screen.queryByText(/validation failed/i)).not.toBeInTheDocument()
  })
})

describe('Stage 4 — clone flow', () => {
  it('clones a built-in: POSTs under the new name with the frontmatter name rewritten', async () => {
    const builtinSource = `---\nname: builtin-agent\ndescription: Shipped\n---\n\n# Built in.\n`
    const mock = routedFetch({
      '/source': () => okJson({ name: 'builtin-agent', content: builtinSource }),
    })
    vi.stubGlobal('fetch', mock)
    const detail = await openDetail('builtin-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /clone to customise/i }))

    const nameBox = await screen.findByRole('textbox', { name: /new profile name/i })
    expect(nameBox).toHaveValue('builtin-agent-copy') // sensible default
    fireEvent.change(nameBox, { target: { value: 'my-variant' } })
    fireEvent.click(screen.getByRole('button', { name: /create copy/i }))
    await act(async () => {})

    const post = mock.mock.calls.find(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(post![1].body)
    expect(body.name).toBe('my-variant')
    expect(body.content).toContain('name: "my-variant"')
    expect(body.content).not.toContain('name: builtin-agent')
    expect(body.content).toContain('# Built in.')
  })

  it('surfaces a 409 when the clone name already exists', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles': (url, opts) => {
        if (String(url).includes('/validate')) return okJson({ valid: true, messages: [] })
        if (String(url).includes('/source')) return okJson({ name: 'builtin-agent', content: SOURCE })
        if (String(url).endsWith('/agents/profiles') && opts?.method === 'POST') {
          return errJson(409, "Profile 'local-agent' already exists in the local store.")
        }
        if (/\/agents\/profiles\/[^/?]+$/.test(String(url))) return okJson(DETAIL)
        return okJson(CATALOG)
      },
    }))
    const detail = await openDetail('builtin-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /clone to customise/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /create copy/i }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent(/already exists/)
  })
})

describe('Stage 4 — delete flow', () => {
  it('deletes only after ConfirmModal confirmation and clears the selection', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /delete/i }))

    // Nothing deleted yet — the confirm gate is up
    expect(mock.mock.calls.filter(([, o]) => o?.method === 'DELETE')).toHaveLength(0)
    expect(screen.getByText('Delete profile')).toBeInTheDocument()

    // Two "Delete" buttons exist now (detail pane + modal); confirm inside the modal.
    const modal = screen.getByText('Delete profile').closest('.fixed') as HTMLElement
    // Type-to-confirm gate: Delete stays disabled until the exact name is typed
    expect(within(modal).getByRole('button', { name: 'Delete' })).toBeDisabled()
    fireEvent.change(within(modal).getByRole('textbox', { name: /confirmation text/i }), { target: { value: 'wrong-name' } })
    expect(within(modal).getByRole('button', { name: 'Delete' })).toBeDisabled()
    fireEvent.change(within(modal).getByRole('textbox', { name: /confirmation text/i }), { target: { value: 'local-agent' } })
    expect(within(modal).getByRole('button', { name: 'Delete' })).toBeEnabled()
    fireEvent.click(within(modal).getByRole('button', { name: 'Delete' }))
    await act(async () => {})

    const del = mock.mock.calls.find(([, o]) => o?.method === 'DELETE')
    expect(del).toBeTruthy()
    expect(String(del![0])).toContain('/agents/profiles/local-agent')
    // Selection cleared after delete
    expect(screen.queryByTestId('profile-detail')).not.toBeInTheDocument()
  })

  it('removes the profile from the list immediately, before the catalog refetch lands', async () => {
    // Regression: the row must disappear synchronously on DELETE success —
    // not only after refreshCatalog()'s round trip. The refetch is held
    // pending forever so the test fails if removal depends on it.
    let deleteDone = false
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (opts?.method === 'DELETE') { deleteDone = true; return noContent() }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson(DETAIL)
      if (url.includes('/agents/profiles')) {
        // First catalog fetch resolves; the post-delete refetch never does.
        if (deleteDone) return new Promise(() => {})
        return okJson(CATALOG)
      }
      return okJson([])
    }))
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /delete/i }))
    const modal = screen.getByText('Delete profile').closest('.fixed') as HTMLElement
    fireEvent.change(within(modal).getByRole('textbox', { name: /confirmation text/i }), { target: { value: 'local-agent' } })
    fireEvent.click(within(modal).getByRole('button', { name: 'Delete' }))
    await act(async () => {})

    expect(screen.queryByRole('option', { name: /local-agent/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /builtin-agent/ })).toBeInTheDocument()
  })

  it('removes the profile from active search results, not only the catalog', async () => {
    // Regression: the visible list is `results ?? catalog`; refreshCatalog()
    // alone leaves a stale search result showing the deleted profile.
    // Real timers: the 300 ms debounce elapses inside findByRole's poll window.
    const SEARCH = [
      { name: 'local-agent', description: 'Mine', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1.5 },
      { name: 'builtin-agent', description: 'Shipped', capabilities: [], tags: [], role: '', source: 'built-in', coverage: 1, score: 1.1 },
    ]
    let searched = false
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/search')) { searched = true; return okJson(SEARCH) }
      if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (opts?.method === 'DELETE') return noContent()
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson(DETAIL)
      if (url.includes('/agents/profiles')) return okJson(CATALOG)
      return okJson([])
    }))
    render(<ProfilesPanel />)
    fireEvent.change(await screen.findByRole('searchbox', { name: /search profiles/i }), { target: { value: 'agent' } })
    // Wait for the debounced search to land and render the ranked rows
    await screen.findByText('2 matches', undefined, { timeout: 2000 })
    expect(searched).toBe(true)

    fireEvent.click(screen.getByRole('option', { name: /local-agent/ }))
    const detail = await screen.findByTestId('profile-detail')
    fireEvent.click(within(detail).getByRole('button', { name: /delete/i }))
    const modal = screen.getByText('Delete profile').closest('.fixed') as HTMLElement
    fireEvent.change(within(modal).getByRole('textbox', { name: /confirmation text/i }), { target: { value: 'local-agent' } })
    fireEvent.click(within(modal).getByRole('button', { name: 'Delete' }))
    await act(async () => {})

    expect(screen.queryByRole('option', { name: /local-agent/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /builtin-agent/ })).toBeInTheDocument()
  })

  it('cancelling the ConfirmModal issues no DELETE', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /delete/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(mock.mock.calls.filter(([, o]) => o?.method === 'DELETE')).toHaveLength(0)
    expect(screen.queryByText('Delete profile')).not.toBeInTheDocument()
  })

  it('a DELETE failure shows an error snackbar and keeps the row listed', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/local-agent': (url, opts) => {
        if (opts?.method === 'DELETE') return errJson(500, 'store locked')
        return okJson(DETAIL)
      },
    }))
    useStore.setState({ snackbar: null })
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /delete/i }))
    const modal = screen.getByText('Delete profile').closest('.fixed') as HTMLElement
    fireEvent.change(within(modal).getByRole('textbox', { name: /confirmation text/i }), { target: { value: 'local-agent' } })
    fireEvent.click(within(modal).getByRole('button', { name: 'Delete' }))
    await act(async () => {})

    expect(useStore.getState().snackbar?.type).toBe('error')
    expect(useStore.getState().snackbar?.message).toContain('store locked')
    // The failed delete must not remove the row (optimistic removal happens
    // only on DELETE success).
    expect(screen.getByRole('option', { name: /local-agent/ })).toBeInTheDocument()
  })
})

describe('post-save warnings surface via snackbar', () => {
  it('a save that returns warnings shows an info snackbar naming the count and first warning', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/local-agent': (url, opts) => {
        if (String(url).includes('/validate')) return okJson({ valid: true, messages: [] })
        if (String(url).includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
        if (opts?.method === 'PUT') {
          return okJson({ name: 'local-agent', warnings: [{ severity: 'warning', message: 'description is recommended' }] })
        }
        return okJson(DETAIL)
      },
    }))
    useStore.setState({ snackbar: null })
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})

    const snackbar = useStore.getState().snackbar
    expect(snackbar?.type).toBe('info')
    expect(snackbar?.message).toContain("Profile 'local-agent' saved with 1 warning")
    expect(snackbar?.message).toContain('description is recommended')
  })

  it('a clean save shows a success snackbar', async () => {
    vi.stubGlobal('fetch', routedFetch())
    useStore.setState({ snackbar: null })
    const detail = await openDetail('local-agent')
    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    expect(useStore.getState().snackbar?.type).toBe('success')
  })
})

describe('editor textarea red boundary on validation errors', () => {
  it('the source textarea gets the thick red boundary when validate returns errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: false, messages: [{ severity: 'error', message: 'name is required', path: 'name' }] })
      if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
      return okJson([])
    }))
    render(<ProfileEditorModal open={true} mode="edit" name="local-agent" onClose={() => {}} onSaved={() => {}} />)
    const box = await screen.findByRole('textbox', { name: /profile source/i })
    expect(box.className).not.toContain('!border-red-500')
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    expect(box.className).toContain('!border-red-500')
    expect(screen.getByTestId('validation-findings')).toHaveTextContent('name is required')
  })
})

describe('backdrop close gating during save', () => {
  it('clicking the backdrop mid-save does NOT close the modal; after save it does', async () => {
    let resolvePut: (v: any) => void
    const putGate = new Promise(r => { resolvePut = r })
    const onClose = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
      if (opts?.method === 'PUT') { await putGate; return okJson({ name: 'local-agent', warnings: [] }) }
      return okJson([])
    }))
    render(<ProfileEditorModal open={true} mode="edit" name="local-agent" onClose={onClose} onSaved={() => {}} />)
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})

    // Save is in flight (PUT parked on the gate): backdrop click must be inert
    const dialog = screen.getByRole('dialog', { name: /edit profile/i })
    const backdrop = dialog.querySelector('.absolute.inset-0') as HTMLElement
    fireEvent.click(backdrop)
    expect(onClose).not.toHaveBeenCalled()

    // The header X is gated the same way: the panel renders the editor
    // conditionally, so a mid-flight close UNMOUNTS it and a late 400
    // rejection lands nowhere (#692 review)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(onClose).not.toHaveBeenCalled()

    // Release the save; modal closes through the normal success path
    resolvePut!(undefined)
    await act(async () => {})
    expect(onClose).toHaveBeenCalledTimes(1)

    // And once idle, the backdrop and X work again on a fresh modal
    fireEvent.click(backdrop)
    expect(onClose).toHaveBeenCalledTimes(2)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(3)
  })
})

describe('Stage 4 — editor source loading error', () => {
  it('shows the load error and disables save', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/source')) return errJson(404, "Agent profile 'ghost' not found")
      return okJson([])
    }))
    render(<ProfileEditorModal open={true} mode="edit" name="ghost" onClose={() => {}} onSaved={() => {}} />)
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent(/not found/)
    expect(screen.getByRole('button', { name: /save changes/i })).toBeDisabled()
  })
})

describe('detail pane refreshes after an in-place edit (#692 review)', () => {
  it('refetches the parsed profile and shows the post-edit values', async () => {
    // The detail fetch was keyed on the profile NAME, which an in-place edit
    // does not change -- the pane kept showing pre-edit provider/model until
    // the user selected away and back.
    let saved = false
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
      if (opts?.method === 'PUT') { saved = true; return okJson({ name: 'local-agent', warnings: [] }) }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) {
        return okJson({ name: 'local-agent', description: 'Mine', model: saved ? 'claude-opus-5' : 'claude-sonnet-4' })
      }
      if (url.includes('/agents/profiles')) return okJson(CATALOG)
      return okJson([])
    }))
    const detail = await openDetail('local-agent')
    await screen.findByText('claude-sonnet-4')

    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})

    // The pane must show the post-edit model, proving a refetch happened
    expect(await screen.findByText('claude-opus-5')).toBeInTheDocument()
    expect(screen.queryByText('claude-sonnet-4')).not.toBeInTheDocument()
  })
})

describe('panel-level create flow (#692 review)', () => {
  it('New profile -> from scratch -> create wires snackbar, refresh, and selection', async () => {
    // Every prior create test rendered the modal in isolation; this pins the
    // handleCreated wiring: snackbar, catalog refetch, and selection of the
    // new row only after the refetch lands.
    let created = false
    const NEW_ROW = { name: 'fresh-agent', description: 'Newly made', source: 'local' }
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/agents/profiles/schema')) return okJson({
        type: 'object', required: ['name'],
        properties: { name: { type: 'string' }, description: { type: 'string' } },
      })
      if (url.includes('/agents/profiles/templates')) return okJson([])
      if (url.includes('/agents/providers')) return okJson([])
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') {
        created = true
        return okJson({ name: 'fresh-agent', warnings: [] })
      }
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return okJson({ name: 'fresh-agent', description: 'Newly made' })
      if (url.includes('/agents/profiles')) return okJson(created ? [...CATALOG, NEW_ROW] : CATALOG)
      return okJson([])
    }))
    useStore.setState({ snackbar: null })
    render(<ProfilesPanel />)
    await screen.findByRole('option', { name: /local-agent/ })

    fireEvent.click(screen.getByRole('button', { name: /new profile/i }))
    fireEvent.click(await screen.findByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'fresh-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    expect(useStore.getState().snackbar?.type).toBe('success')
    expect(useStore.getState().snackbar?.message).toContain("'fresh-agent' created")
    // Selection landed AFTER the refetch, so the new row renders in both panes
    expect(await screen.findByRole('option', { name: /fresh-agent/ })).toBeInTheDocument()
    expect(within(screen.getByTestId('profile-detail')).getByText('Newly made')).toBeInTheDocument()
  })
})
