import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ContextMenu } from './ContextMenu'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

vi.mock('../../api', () => ({
  api: {
    sessions: { delete: vi.fn(() => Promise.resolve()) },
    tasks: { update: vi.fn(() => Promise.resolve()), close: vi.fn(() => Promise.resolve()), delete: vi.fn(() => Promise.resolve()) },
    ralph: { delete: vi.fn(() => Promise.resolve()) }
  }
}))

describe('ContextMenu', () => {
  const mockStore = {
    contextMenu: null,
    hideContextMenu: vi.fn(),
    openTerminal: vi.fn(),
    unassignBead: vi.fn(),
    agentsOnMap: [{ id: 'a1', assignedBeadId: 'b1' }],
    beadsOnMap: [{ id: 'b1', assigneeId: 'a1' }],
    setZoom: vi.fn(),
    zoom: 1,
    setPan: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('returns null when no context menu', () => {
    const { container } = render(<ContextMenu />)
    expect(container.firstChild).toBeNull()
  })

  it('renders agent menu items', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'agent', targetId: 'a1' }
    })
    render(<ContextMenu />)
    expect(screen.getByText('Open Terminal')).toBeTruthy()
    expect(screen.getByText('Unassign Bead')).toBeTruthy()
    expect(screen.getByText('Delete Session')).toBeTruthy()
  })

  it('renders bead menu items', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'bead', targetId: 'b1' }
    })
    render(<ContextMenu />)
    expect(screen.getByText('Set Priority P1')).toBeTruthy()
    expect(screen.getByText('Mark Complete')).toBeTruthy()
    expect(screen.getByText('Delete Bead')).toBeTruthy()
  })

  it('renders empty space menu items', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'empty', targetId: null }
    })
    render(<ContextMenu />)
    expect(screen.getByText('New Agent Here')).toBeTruthy()
    expect(screen.getByText('Zoom In')).toBeTruthy()
    expect(screen.getByText('Reset View')).toBeTruthy()
  })

  it('calls openTerminal on click', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'agent', targetId: 'a1' }
    })
    render(<ContextMenu />)
    fireEvent.click(screen.getByText('Open Terminal'))
    expect(mockStore.openTerminal).toHaveBeenCalledWith('a1')
    expect(mockStore.hideContextMenu).toHaveBeenCalled()
  })

  it('calls setZoom on zoom in', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'empty', targetId: null }
    })
    render(<ContextMenu />)
    fireEvent.click(screen.getByText('Zoom In'))
    expect(mockStore.setZoom).toHaveBeenCalledWith(1.1)
  })

  it('resets view on Reset View click', () => {
    ;(useStarCraftStore as any).mockReturnValue({
      ...mockStore,
      contextMenu: { x: 100, y: 100, type: 'empty', targetId: null }
    })
    render(<ContextMenu />)
    fireEvent.click(screen.getByText('Reset View'))
    expect(mockStore.setZoom).toHaveBeenCalledWith(1)
    expect(mockStore.setPan).toHaveBeenCalledWith(0, 0)
  })
})
