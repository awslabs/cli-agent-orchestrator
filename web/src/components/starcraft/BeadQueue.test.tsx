import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { BeadQueue } from './BeadQueue'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

describe('BeadQueue component', () => {
  const mockStore = {
    beadsInQueue: [
      { id: 'b1', title: 'P1 Task', priority: 1, status: 'open', assigneeId: null, position: null, isOrphaned: false },
      { id: 'b2', title: 'P3 Task', priority: 3, status: 'open', assigneeId: null, position: null, isOrphaned: false },
      { id: 'b3', title: 'P2 Task', priority: 2, status: 'open', assigneeId: null, position: null, isOrphaned: false }
    ],
    beadsOnMap: [
      { id: 'b4', title: 'Assigned', priority: 1, status: 'wip', assigneeId: 'a1', position: { x: 0, y: 0 }, isOrphaned: false }
    ],
    agentsOnMap: [
      { id: 'a1', name: 'agent-1', icon: '🤖', status: 'PROCESSING', position: { x: 0, y: 0 }, assignedBeadId: 'b4', color: '#00ff88' }
    ],
    assignBead: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders queue header', () => {
    const { container } = render(<BeadQueue />)
    expect(container.textContent).toContain('BEAD QUEUE')
  })

  it('shows bead count', () => {
    const { container } = render(<BeadQueue />)
    expect(container.textContent).toContain('3')
  })

  it('sorts beads by priority (P1 first)', () => {
    const { container } = render(<BeadQueue />)
    const beadCards = container.querySelectorAll('[draggable="true"]')
    expect(beadCards[0].textContent).toContain('P1')
    expect(beadCards[1].textContent).toContain('P2')
    expect(beadCards[2].textContent).toContain('P3')
  })

  it('shows assigned section', () => {
    const { container } = render(<BeadQueue />)
    expect(container.textContent).toContain('Assigned (1)')
    expect(container.textContent).toContain('Assigned')
    expect(container.textContent).toContain('agent-1')
  })

  it('shows empty message when no beads', () => {
    ;(useStarCraftStore as any).mockReturnValue({ ...mockStore, beadsInQueue: [], beadsOnMap: [], agentsOnMap: [] })
    const { container } = render(<BeadQueue />)
    expect(container.textContent).toContain('No beads in queue')
  })

  it('bead cards are draggable', () => {
    const { container } = render(<BeadQueue />)
    const beadCard = container.querySelector('[draggable="true"]')
    expect(beadCard).toBeTruthy()
  })

  it('sets bead id on drag start', () => {
    const { container } = render(<BeadQueue />)
    const beadCard = container.querySelector('[draggable="true"]')
    const dataTransfer = { setData: vi.fn(), effectAllowed: '' }
    fireEvent.dragStart(beadCard!, { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith('beadId', 'b1')
  })

  it('renders P1 beads with red styling', () => {
    const { container } = render(<BeadQueue />)
    const beadCards = container.querySelectorAll('[draggable="true"]')
    const p1Card = beadCards[0]
    expect(p1Card.getAttribute('style')).toContain('#ff4444')
  })

  it('renders P2 beads with yellow styling', () => {
    const { container } = render(<BeadQueue />)
    const beadCards = container.querySelectorAll('[draggable="true"]')
    const p2Card = beadCards[1]
    expect(p2Card.getAttribute('style')).toContain('#ffcc00')
  })

  it('renders P3 beads with gray styling', () => {
    const { container } = render(<BeadQueue />)
    const beadCards = container.querySelectorAll('[draggable="true"]')
    const p3Card = beadCards[2]
    expect(p3Card.getAttribute('style')).toContain('#666666')
  })
})
