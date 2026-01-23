import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Agent } from './Agent'
import { useStarCraftStore, AgentOnMap } from '../../stores/starcraftStore'

// Mock the store
vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return {
    ...actual,
    useStarCraftStore: vi.fn()
  }
})

vi.mock('../../api', () => ({
  api: { sessions: { updatePosition: vi.fn(() => Promise.resolve()) } }
}))

const mockAgent: AgentOnMap = {
  id: 'agent-1',
  name: 'bob-the-builder',
  icon: '🔧',
  status: 'IDLE',
  position: { x: 100, y: 100 },
  assignedBeadId: null,
  color: '#00ff88'
}

describe('Agent component', () => {
  const mockStore = {
    selectedId: null,
    selectItem: vi.fn(),
    openTerminal: vi.fn(),
    showContextMenu: vi.fn(),
    setHovered: vi.fn(),
    assignBead: vi.fn(),
    moveAgent: vi.fn(),
    zoom: 1
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders agent with icon and name', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    expect(container.querySelector('text')).toBeTruthy()
  })

  it('shows selection ring when selected', () => {
    ;(useStarCraftStore as any).mockReturnValue({ ...mockStore, selectedId: 'agent-1' })
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const circles = container.querySelectorAll('circle')
    expect(circles.length).toBeGreaterThan(0)
  })

  it('shows bounce animation for WAITING_INPUT status', () => {
    const waitingAgent = { ...mockAgent, status: 'WAITING_INPUT' as const }
    const { container } = render(
      <svg><Agent agent={waitingAgent} /></svg>
    )
    const animates = container.querySelectorAll('animate')
    expect(animates.length).toBeGreaterThan(0)
  })

  it('shows flash animation for ERROR status', () => {
    const errorAgent = { ...mockAgent, status: 'ERROR' as const }
    const { container } = render(
      <svg><Agent agent={errorAgent} /></svg>
    )
    const animates = container.querySelectorAll('animate')
    expect(animates.length).toBeGreaterThan(0)
  })

  it('shows pulse animation for PROCESSING status', () => {
    const processingAgent = { ...mockAgent, status: 'PROCESSING' as const }
    const { container } = render(
      <svg><Agent agent={processingAgent} /></svg>
    )
    const animates = container.querySelectorAll('animate')
    expect(animates.length).toBeGreaterThan(0)
  })

  it('calls selectItem on click', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const g = container.querySelector('g')
    fireEvent.click(g!)
    expect(mockStore.selectItem).toHaveBeenCalledWith('agent-1', 'agent')
  })

  it('calls openTerminal on double click', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const g = container.querySelector('g')
    fireEvent.doubleClick(g!)
    expect(mockStore.openTerminal).toHaveBeenCalledWith('agent-1')
  })

  it('shows context menu on right click', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const g = container.querySelector('g')
    fireEvent.contextMenu(g!)
    expect(mockStore.showContextMenu).toHaveBeenCalled()
  })

  it('handles drag over for bead assignment', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const g = container.querySelector('g')
    fireEvent.dragOver(g!)
    // Should show drop indicator (selection ring)
  })

  it('handles drop to assign bead', () => {
    const { container } = render(
      <svg><Agent agent={mockAgent} /></svg>
    )
    const g = container.querySelector('g')
    const dataTransfer = { getData: () => 'bead-1' }
    fireEvent.drop(g!, { dataTransfer })
    expect(mockStore.assignBead).toHaveBeenCalledWith('bead-1', 'agent-1')
  })
})
