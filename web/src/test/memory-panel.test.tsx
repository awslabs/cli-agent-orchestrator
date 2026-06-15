import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryPanel } from '../components/MemoryPanel'

const MEMORIES = [
  {
    key: 'project-conventions',
    scope: 'project',
    scope_id: 'my-proj',
    memory_type: 'project',
    tags: 'style,conventions',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-10T00:00:00Z',
  },
  {
    key: 'user-preferences',
    scope: 'global',
    scope_id: null,
    memory_type: 'user',
    tags: '',
    created_at: '2026-06-02T00:00:00Z',
    updated_at: '2026-06-11T00:00:00Z',
  },
]

describe('MemoryPanel', () => {
  const mockFetch = vi.fn()

  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  function mockListResponse(data: unknown) {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve(data),
    })
  }

  it('renders memory rows after fetch', async () => {
    mockListResponse(MEMORIES)
    render(<MemoryPanel />)
    expect(await screen.findByText('project-conventions')).toBeInTheDocument()
    expect(screen.getByText('user-preferences')).toBeInTheDocument()
    expect(screen.getByText('global')).toBeInTheDocument()
    expect(screen.getByText('style,conventions')).toBeInTheDocument()
  })

  it('shows empty state when no memories', async () => {
    mockListResponse([])
    render(<MemoryPanel />)
    expect(await screen.findByText('No memories stored.')).toBeInTheDocument()
  })

  it('shows ConfirmModal when delete is clicked', async () => {
    mockListResponse(MEMORIES)
    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    const deleteButtons = screen.getAllByTitle('Delete memory')
    fireEvent.click(deleteButtons[0])
    await waitFor(() => {
      expect(screen.getByText(/permanently remove the memory/i)).toBeInTheDocument()
    })
    // Modal details echo the row's key (row + modal both show it)
    expect(screen.getAllByText('project-conventions').length).toBe(2)
  })

  it('disables Clear scope button when no scope filter is selected', async () => {
    mockListResponse(MEMORIES)
    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    const clearButton = screen.getByText('Clear scope…').closest('button')
    expect(clearButton).toBeDisabled()
  })

  it('filters rows by key search client-side', async () => {
    mockListResponse(MEMORIES)
    render(<MemoryPanel />)
    await screen.findByText('project-conventions')
    fireEvent.change(screen.getByPlaceholderText('Filter keys...'), { target: { value: 'user-pref' } })
    expect(screen.queryByText('project-conventions')).not.toBeInTheDocument()
    expect(screen.getByText('user-preferences')).toBeInTheDocument()
  })
})
