import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { EditCloneModal } from '../components/EditCloneModal'
import { ProfilesBrowser } from '../components/ProfilesBrowser'
import { api } from '../api'

// #510 U4: edit + clone. The modal prefills content from getProfile, exposes
// explicit required provider/model, and routes edit → PUT / clone → from-content.
// The key FR6 guarantee: clone calls createProfileFromContent (a NEW local
// profile) and NEVER updateProfile on the built-in (built-in unmutated).

const DEV_PROFILE = {
  name: 'developer', description: 'Dev agent', role: 'developer',
  provider: 'claude_code', model: 'opus', system_prompt: 'Do the work.',
}

describe('EditCloneModal', () => {
  let updateSpy: any
  let fromContentSpy: any

  beforeEach(() => {
    vi.spyOn(api, 'getProfile').mockResolvedValue(DEV_PROFILE as any)
    vi.spyOn(api, 'listProviders').mockResolvedValue([
      { name: 'claude_code', binary: 'claude', installed: true },
    ] as any)
    updateSpy = vi.spyOn(api, 'updateProfile').mockResolvedValue({ name: 'my-local', source: 'local', path: '/s/my-local.md' })
    fromContentSpy = vi.spyOn(api, 'createProfileFromContent').mockResolvedValue({ name: 'developer-copy', source: 'local', path: '/s/developer-copy.md' })
  })

  afterEach(() => vi.restoreAllMocks())

  it('edit mode prefills content from getProfile and saves via updateProfile (PUT)', async () => {
    const onClose = vi.fn()
    render(<EditCloneModal mode="edit" sourceName="my-local" onClose={onClose} />)
    // content textarea prefilled from the loaded profile
    const textarea = await screen.findByLabelText('Profile content') as HTMLTextAreaElement
    await waitFor(() => expect(textarea.value).toContain('"name": "developer"'))
    // provider/model seeded but editable+required
    expect((screen.getByLabelText(/Model/) as HTMLInputElement).value).toBe('opus')

    fireEvent.click(screen.getByText('Save Changes'))
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith('my-local', expect.objectContaining({
      provider: 'claude_code', model: 'opus',
    })))
    // Edit must NOT call the clone route.
    expect(fromContentSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(expect.objectContaining({ source: 'local' })))
  })

  it('clone mode defaults a new name, saves via from-content, and never mutates the built-in', async () => {
    const onClose = vi.fn()
    render(<EditCloneModal mode="clone" sourceName="developer" onClose={onClose} />)
    await screen.findByLabelText('Profile content')
    // new-name field defaults to <source>-copy
    const nameInput = screen.getByLabelText(/New profile name/) as HTMLInputElement
    expect(nameInput.value).toBe('developer-copy')

    fireEvent.click(screen.getByText('Create Clone'))
    await waitFor(() => expect(fromContentSpy).toHaveBeenCalledWith(expect.objectContaining({
      name: 'developer-copy', provider: 'claude_code', model: 'opus',
    })))
    // FR6/AC6.2: clone is a CREATE, never a PUT on the built-in.
    expect(updateSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(expect.objectContaining({ name: 'developer-copy' })))
  })

  it('save is blocked until provider AND model are present [ADR-006]', async () => {
    vi.spyOn(api, 'getProfile').mockResolvedValueOnce({ name: 'x', description: 'y' } as any) // no provider/model
    render(<EditCloneModal mode="edit" sourceName="x" onClose={vi.fn()} />)
    await screen.findByLabelText('Profile content')
    const saveBtn = screen.getByText('Save Changes').closest('button')!
    expect(saveBtn).toBeDisabled()
    // fill model only
    fireEvent.change(screen.getByLabelText(/Model/), { target: { value: 'opus' } })
    expect(saveBtn).toBeDisabled() // provider still empty (free-text since providers load async)
  })

  it('surfaces a server save error (e.g. overwrite refusal) inline', async () => {
    fromContentSpy.mockRejectedValueOnce(Object.assign(new Error('400'), { detail: "name 'developer-copy' already exists" }))
    render(<EditCloneModal mode="clone" sourceName="developer" onClose={vi.fn()} />)
    await screen.findByLabelText('Profile content')
    fireEvent.click(screen.getByText('Create Clone'))
    expect(await screen.findByText(/Save failed: name 'developer-copy' already exists/)).toBeInTheDocument()
  })

  it('edit mode surfaces a server re-validation 400 inline and does not close', async () => {
    // Symmetry with the clone error path: an edit whose content fails server
    // re-validation (FR5/AC5.1) must surface the [error] and keep the modal open.
    const onClose = vi.fn()
    updateSpy.mockRejectedValueOnce(Object.assign(new Error('400'), { detail: '[error] name: does not match pattern' }))
    render(<EditCloneModal mode="edit" sourceName="my-local" onClose={onClose} />)
    await screen.findByLabelText('Profile content')
    fireEvent.click(screen.getByText('Save Changes'))
    expect(await screen.findByText(/Save failed: \[error\] name: does not match pattern/)).toBeInTheDocument()
    // The failed save must NOT close the modal (onClose is only called on success).
    expect(onClose).not.toHaveBeenCalled()
  })
})

// Integration: from ProfilesBrowser, a built-in offers Clone (not Edit) and a
// local+loadable profile offers Edit (not Clone) — the FR5/FR6 affordance split.
describe('ProfilesBrowser edit/clone affordances', () => {
  const GROUPED: any[] = [
    { name: 'developer', description: 'Dev', source: 'built-in', loadable: true, tags: [], capabilities: [] },
    { name: 'my-local', description: 'Local', source: 'local', loadable: true, tags: [], capabilities: [] },
  ]

  beforeEach(() => {
    vi.spyOn(api, 'listProfiles').mockResolvedValue(GROUPED as any)
    vi.spyOn(api, 'searchProfiles').mockResolvedValue([])
    vi.spyOn(api, 'listProviders').mockResolvedValue([{ name: 'claude_code', binary: 'claude', installed: true }] as any)
    vi.spyOn(api, 'getProfile').mockResolvedValue(DEV_PROFILE as any)
  })
  afterEach(() => vi.restoreAllMocks())

  it('built-in detail offers Clone to customize, not Edit', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.click(within(screen.getByTestId('profile-row-developer')).getByText('View'))
    await screen.findByText('Validate')
    expect(screen.getByText('Clone to customize')).toBeInTheDocument()
    expect(screen.queryByText(/^Edit$/)).not.toBeInTheDocument()
  })

  it('local+loadable detail offers Edit, not Clone', async () => {
    render(<ProfilesBrowser />)
    await screen.findByTestId('grouped-list')
    fireEvent.click(within(screen.getByTestId('profile-row-my-local')).getByText('View'))
    await screen.findByText('Validate')
    expect(screen.getByText('Edit')).toBeInTheDocument()
    expect(screen.queryByText('Clone to customize')).not.toBeInTheDocument()
  })
})
