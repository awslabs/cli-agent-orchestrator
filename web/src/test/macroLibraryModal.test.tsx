import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MacroLibraryModal, type MacroLibraryModalProps } from '../components/MacroLibraryModal'
import type { MacroRecord } from '../api'

// Representative visible set, in the pinned §5.4 server order: favorites
// first (built-ins sort first within the provider group), then non-favorites.
const COMPACT: MacroRecord = {
  id: 'builtin:kimi_cli:compact',
  name: 'Compact',
  description: 'Provider-native compact',
  scope: { kind: 'provider', provider: 'kimi_cli' },
  events: [
    { type: 'text', text: '/compact' },
    { type: 'key', key: 'Enter' },
  ],
  favorite: true,
  origin: 'builtin',
  mutable: false,
  builtin_kind: 'compact',
  created_at: null,
  updated_at: null,
}

const STOP: MacroRecord = {
  id: 'builtin:kimi_cli:stop',
  name: 'Stop',
  description: null,
  scope: { kind: 'provider', provider: 'kimi_cli' },
  events: [{ type: 'key', key: 'Escape' }],
  favorite: true,
  origin: 'builtin',
  mutable: false,
  builtin_kind: 'stop',
  created_at: null,
  updated_at: null,
}

const USER_GLOBAL: MacroRecord = {
  id: 'u-global-1',
  name: 'Model K2.7',
  description: 'switch model',
  scope: { kind: 'global' },
  events: [
    { type: 'text', text: '/model' },
    { type: 'key', key: 'Enter' },
    { type: 'key', key: 'Up' },
    { type: 'key', key: 'Up' },
    { type: 'key', key: 'Up' },
    { type: 'key', key: 'Enter' },
  ],
  favorite: true,
  origin: 'user',
  mutable: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const USER_PROFILE: MacroRecord = {
  id: 'u-profile-1',
  name: 'Spec notes',
  description: null,
  scope: { kind: 'profile', profile: 'spec-writer-k3' },
  events: [
    { type: 'text', text: '/notes' },
    { type: 'key', key: 'Enter' },
  ],
  favorite: false,
  origin: 'user',
  mutable: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const MACROS = [COMPACT, STOP, USER_GLOBAL, USER_PROFILE]

const COMPACT_COPY: MacroRecord = {
  id: 'u-copy-1',
  name: 'Compact copy',
  description: null,
  scope: { kind: 'provider', provider: 'kimi_cli' },
  events: COMPACT.events,
  favorite: false,
  origin: 'user',
  mutable: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const SAVED: MacroRecord = {
  id: 'u-saved-1',
  name: 'My macro',
  description: null,
  scope: { kind: 'global' },
  events: [
    { type: 'text', text: 'hi' },
    { type: 'key', key: 'Enter' },
  ],
  favorite: false,
  origin: 'user',
  mutable: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function okJson(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, statusText: '', json: () => Promise.resolve(body) }
}

function renderModal(overrides: Partial<MacroLibraryModalProps> = {}) {
  const props: MacroLibraryModalProps = {
    provider: 'kimi_cli',
    agentProfile: 'spec-writer-k3',
    macros: MACROS,
    builtinsVisible: true,
    commandGuardAvailable: true,
    advertisedChords: new Set(['C-s']),
    busy: false,
    onClose: vi.fn(),
    onSend: vi.fn(),
    onChanged: vi.fn(),
    ...overrides,
  }
  render(<MacroLibraryModal {...props} />)
  return props
}

function rows(): HTMLElement[] {
  return screen.getAllByTestId('macro-row')
}

function openRow(name: RegExp) {
  const row = rows().find(r => within(r).queryByRole('button', { name }) != null)
  if (!row) throw new Error(`row not found: ${name}`)
  fireEvent.click(within(row).getByRole('button', { name }))
}

describe('MacroLibraryModal', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({})))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('renders the dialog with list rows in server order, built-in badges, and direct Send', () => {
    const props = renderModal()
    expect(screen.getByRole('dialog', { name: 'Macro library' })).toBeInTheDocument()

    const list = rows()
    expect(list).toHaveLength(4)
    expect(list[0]).toHaveTextContent('Compact')
    expect(list[1]).toHaveTextContent('Stop')
    expect(list[2]).toHaveTextContent('Model K2.7')
    expect(list[3]).toHaveTextContent('Spec notes')

    // Built-in rows carry the badge; user rows do not.
    expect(within(list[0]).getByText('built-in')).toBeInTheDocument()
    expect(within(list[1]).getByText('built-in')).toBeInTheDocument()
    expect(within(list[2]).queryByText('built-in')).toBeNull()
    // No delete affordance on built-in rows.
    expect(within(list[0]).queryByRole('button', { name: /delete/i })).toBeNull()

    // Direct Send on a row sends exactly that record.
    fireEvent.click(within(list[0]).getByRole('button', { name: 'Send Compact' }))
    expect(props.onSend).toHaveBeenCalledWith(COMPACT)
    fireEvent.click(within(list[2]).getByRole('button', { name: 'Send Model K2.7' }))
    expect(props.onSend).toHaveBeenCalledWith(USER_GLOBAL)
  })

  it('disables row Send buttons while a send is in flight', () => {
    renderModal({ busy: true })
    for (const row of rows()) {
      for (const button of within(row).getAllByRole('button', { name: /^Send / })) {
        expect(button).toBeDisabled()
      }
    }
  })

  it('opens a built-in in the editor with Send Test and Duplicate only — no Save or Delete', () => {
    renderModal()
    openRow(/^Compact/)
    expect(screen.getByText(/Built-in macro — it cannot be edited or deleted/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send Test' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Duplicate' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('narrows the list by case-insensitive name search', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('Search macros'), { target: { value: 'COMPACT' } })
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toHaveTextContent('Compact')
  })

  it('narrows the list by scope filter', () => {
    renderModal()
    const filter = screen.getByLabelText('Scope filter')

    fireEvent.change(filter, { target: { value: 'global' } })
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toHaveTextContent('Model K2.7')

    fireEvent.change(filter, { target: { value: 'provider' } })
    expect(rows()).toHaveLength(2)
    expect(rows()[0]).toHaveTextContent('Compact')
    expect(rows()[1]).toHaveTextContent('Stop')

    fireEvent.change(filter, { target: { value: 'profile' } })
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toHaveTextContent('Spec notes')
  })

  it('narrows the list with the favorites-only toggle', () => {
    renderModal()
    fireEvent.click(screen.getByRole('switch', { name: 'Favorites only' }))
    expect(rows()).toHaveLength(3)
    expect(screen.queryByText('Spec notes')).toBeNull()
    fireEvent.click(screen.getByRole('switch', { name: 'Favorites only' }))
    expect(rows()).toHaveLength(4)
  })

  it('derives notation from the selected macro, updates the preview on valid edits, and pins parse errors', () => {
    renderModal()
    openRow(/^Model K2\.7/)

    const notation = screen.getByLabelText('Notation')
    expect(notation).toHaveValue('"/model" enter up*3 enter')

    // Valid notation re-derives events and shows the live normalized preview.
    fireEvent.change(notation, { target: { value: '"fix" enter' } })
    expect(screen.getByTestId('macro-preview')).toHaveTextContent('"fix" [Enter]')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()

    // Invalid notation shows the pinned (offset, message) error, keeps the
    // previous valid events, and disables Save.
    fireEvent.change(notation, { target: { value: 'up*0' } })
    expect(
      screen.getByText(
        /a repeat count is a positive integer written \[1-9\]\[0-9\]\* \(zero and empty counts are malformed, not no-ops\)/,
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('records keys into tokens and notation, and refuses unrepresentable keys with an amber notice', () => {
    renderModal()
    openRow(/^Model K2\.7/)

    fireEvent.click(screen.getByRole('button', { name: 'Record' }))
    const capture = screen.getByTestId('macro-capture-surface')

    fireEvent.keyDown(capture, { key: 'ArrowUp' })
    fireEvent.keyDown(capture, { key: 'Enter' })

    expect(within(capture).getByText('[Up]')).toBeInTheDocument()
    expect(within(capture).getByText('[Enter]')).toBeInTheDocument()
    expect(screen.getByLabelText('Notation')).toHaveValue('up enter')

    // A key the wire contract cannot express is refused and records nothing.
    fireEvent.keyDown(capture, { key: 'F5' })
    expect(screen.getByText(/F5 cannot be represented/)).toBeInTheDocument()
    expect(screen.getByLabelText('Notation')).toHaveValue('up enter')
    expect(within(capture).queryByText('[F5]')).toBeNull()
  })

  it('saves a draft via the notation round-trip and fires onChanged', async () => {
    let postedBody: any = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, opts?: any) => {
        if (url === '/macros' && opts?.method === 'POST') {
          postedBody = JSON.parse(opts.body)
          return okJson(SAVED, 201)
        }
        return okJson({})
      }),
    )
    const props = renderModal()

    fireEvent.click(
      within(screen.getByTestId('macro-list-pane')).getByRole('button', { name: 'New Macro' }),
    )
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'My macro' } })
    fireEvent.change(screen.getByLabelText('Notation'), { target: { value: '"hi" enter' } })

    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(postedBody).not.toBeNull())
    // The notation round-trip path: notation is sent (the server re-parses
    // authoritatively), not the raw events.
    expect(postedBody.notation).toBe('"hi" enter')
    expect(postedBody.events).toBeUndefined()
    expect(postedBody.name).toBe('My macro')
    expect(postedBody.scope).toEqual({ kind: 'global' })
    await waitFor(() => expect(props.onChanged).toHaveBeenCalled())
  })

  it("displays the server's 422 errors and does not fire onChanged", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, opts?: any) => {
        if (url === '/macros' && opts?.method === 'POST') {
          return {
            ok: false,
            status: 422,
            statusText: 'Unprocessable Entity',
            json: () => Promise.resolve({ errors: [{ offset: 0, message: 'server says no' }] }),
          }
        }
        return okJson({})
      }),
    )
    const props = renderModal()

    fireEvent.click(
      within(screen.getByTestId('macro-list-pane')).getByRole('button', { name: 'New Macro' }),
    )
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'My macro' } })
    fireEvent.change(screen.getByLabelText('Notation'), { target: { value: '"hi" enter' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/server says no/)).toBeInTheDocument()
    expect(props.onChanged).not.toHaveBeenCalled()
  })

  it('duplicates a built-in through the store route and fires onChanged', async () => {
    const fetchMock = vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/duplicate')) return okJson(COMPACT_COPY, 201)
      return okJson({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const props = renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Duplicate Compact' }))

    await waitFor(() => expect(props.onChanged).toHaveBeenCalled())
    const duplicateCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/duplicate'))
    expect(duplicateCall).toBeDefined()
    expect(decodeURIComponent(String(duplicateCall![0]))).toContain('builtin:kimi_cli:compact')
    expect(duplicateCall![1]?.method).toBe('POST')
  })

  it('sends the current editor events via Send Test', () => {
    const props = renderModal()
    openRow(/^Model K2\.7/)
    fireEvent.click(screen.getByRole('button', { name: 'Send Test' }))
    expect(props.onSend).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'u-global-1', name: 'Model K2.7', events: USER_GLOBAL.events }),
    )
  })

  it('deletes a user macro behind an inline confirm step', async () => {
    const fetchMock = vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/macros/u-global-1') && opts?.method === 'DELETE') {
        return okJson({ deleted: 'u-global-1' })
      }
      return okJson({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const props = renderModal()

    openRow(/^Model K2\.7/)
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    // Inline confirm — nothing is deleted on the first click.
    expect(fetchMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))

    await waitFor(() => expect(props.onChanged).toHaveBeenCalled())
    expect(
      fetchMock.mock.calls.some(
        ([url, opts]) => String(url).includes('/macros/u-global-1') && opts?.method === 'DELETE',
      ),
    ).toBe(true)
    // Selection cleared (desktop) and back to the list view (mobile).
    expect(screen.getByText(/Select a macro from the list/)).toBeInTheDocument()
    expect(screen.getByTestId('macro-list-pane').className).not.toContain('hidden')
  })

  it('closes on Escape when the recorder is idle', () => {
    const props = renderModal()
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Macro library' }), { key: 'Escape' })
    expect(props.onClose).toHaveBeenCalledTimes(1)
  })

  it('records Escape as input while recording instead of closing', () => {
    const props = renderModal()
    openRow(/^Model K2\.7/)
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))
    const capture = screen.getByTestId('macro-capture-surface')

    fireEvent.keyDown(capture, { key: 'Escape' })

    expect(props.onClose).not.toHaveBeenCalled()
    expect(within(capture).getByText('[Escape]')).toBeInTheDocument()
    expect(screen.getByLabelText('Notation')).toHaveValue('escape')
  })

  it('mobile sheet: entering the editor from a row swaps views and Back returns to the list', () => {
    renderModal()
    const listPane = screen.getByTestId('macro-list-pane')
    const editorPane = screen.getByTestId('macro-editor-pane')

    // Both views exist; on the sheet the editor starts hidden.
    expect(listPane.className).not.toContain('hidden')
    expect(editorPane.className).toContain('hidden')
    expect(screen.queryByRole('button', { name: 'Back to macro list' })).toBeNull()

    openRow(/^Spec notes/)
    expect(listPane.className).toContain('hidden')
    expect(editorPane.className).not.toContain('hidden')
    expect(screen.getByLabelText('Name')).toHaveValue('Spec notes')

    fireEvent.click(screen.getByRole('button', { name: 'Back to macro list' }))
    expect(listPane.className).not.toContain('hidden')
    expect(editorPane.className).toContain('hidden')
  })

  it('shows the §5.2 quarantine notice with the count and path', () => {
    renderModal({
      quarantine: { count: 2, path: '/state/macros.quarantine-2026-07-28T00-00-00Z.json' },
    })
    expect(screen.getByText(/quarantined 2 macro records/)).toBeInTheDocument()
    expect(
      screen.getByText('/state/macros.quarantine-2026-07-28T00-00-00Z.json'),
    ).toBeInTheDocument()
  })

  it('hides built-in rows with a stated reason when provider controls are not advertised', () => {
    renderModal({ builtinsVisible: false })
    expect(rows()).toHaveLength(2)
    expect(screen.queryByText('Compact')).toBeNull()
    expect(
      screen.getByText(
        /provider-native built-ins unavailable — this server did not advertise provider controls for kimi_cli/,
      ),
    ).toBeInTheDocument()
  })

  it('states the prefill-concatenation guard absence when a Compact built-in is shown', () => {
    renderModal({ commandGuardAvailable: false })
    expect(
      screen.getByText('prefill-concatenation guard unavailable on this server'),
    ).toBeInTheDocument()
  })
})
