import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { Bead } from './Bead'
import { useStarCraftStore, BeadOnMap } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

vi.mock('../../api', () => ({
  api: { tasks: { updatePosition: vi.fn(() => Promise.resolve()) } }
}))

const mockBead: BeadOnMap = {
  id: 'bead-1',
  title: 'Fix login bug',
  priority: 1,
  status: 'open',
  assigneeId: null,
  position: { x: 200, y: 200 },
  isOrphaned: false
}

describe('Bead component', () => {
  const mockStore = {
    selectedId: null,
    selectItem: vi.fn(),
    showContextMenu: vi.fn(),
    setHovered: vi.fn(),
    moveBead: vi.fn(),
    zoom: 1
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders bead with title', () => {
    const { container } = render(<svg><Bead bead={mockBead} /></svg>)
    const texts = container.querySelectorAll('text')
    expect(texts.length).toBeGreaterThan(0)
  })

  it('returns null if no position', () => {
    const noPosBead = { ...mockBead, position: null }
    const { container } = render(<svg><Bead bead={noPosBead} /></svg>)
    expect(container.querySelector('g')).toBeNull()
  })

  it('renders P1 bead with red border', () => {
    const { container } = render(<svg><Bead bead={mockBead} /></svg>)
    const rects = container.querySelectorAll('rect')
    // First rect is glow, second is main rect
    const mainRect = rects[1]
    expect(mainRect?.getAttribute('stroke')).toBe('#ff4444')
  })

  it('renders P2 bead with yellow border', () => {
    const p2Bead = { ...mockBead, priority: 2 as const }
    const { container } = render(<svg><Bead bead={p2Bead} /></svg>)
    const rects = container.querySelectorAll('rect')
    const mainRect = rects[1]
    expect(mainRect?.getAttribute('stroke')).toBe('#ffcc00')
  })

  it('renders P3 bead with gray border', () => {
    const p3Bead = { ...mockBead, priority: 3 as const }
    const { container } = render(<svg><Bead bead={p3Bead} /></svg>)
    const rect = container.querySelector('rect')
    expect(rect?.getAttribute('stroke')).toBe('#666666')
  })

  it('shows checkmark for closed bead', () => {
    const closedBead = { ...mockBead, status: 'closed' as const }
    const { container } = render(<svg><Bead bead={closedBead} /></svg>)
    expect(container.textContent).toContain('✅')
  })

  it('shows warning for orphaned bead', () => {
    const orphanedBead = { ...mockBead, isOrphaned: true }
    const { container } = render(<svg><Bead bead={orphanedBead} /></svg>)
    expect(container.textContent).toContain('⚠️')
  })

  it('has reduced opacity when closed', () => {
    const closedBead = { ...mockBead, status: 'closed' as const }
    const { container } = render(<svg><Bead bead={closedBead} /></svg>)
    const g = container.querySelector('g')
    expect(g?.getAttribute('opacity')).toBe('0.5')
  })

  it('calls selectItem on click', () => {
    const { container } = render(<svg><Bead bead={mockBead} /></svg>)
    fireEvent.click(container.querySelector('g')!)
    expect(mockStore.selectItem).toHaveBeenCalledWith('bead-1', 'bead')
  })

  it('shows context menu on right click', () => {
    const { container } = render(<svg><Bead bead={mockBead} /></svg>)
    fireEvent.contextMenu(container.querySelector('g')!)
    expect(mockStore.showContextMenu).toHaveBeenCalled()
  })

  it('shows selection ring when selected', () => {
    ;(useStarCraftStore as any).mockReturnValue({ ...mockStore, selectedId: 'bead-1' })
    const { container } = render(<svg><Bead bead={mockBead} /></svg>)
    const rects = container.querySelectorAll('rect')
    const selectedRect = Array.from(rects).find(r => r.getAttribute('stroke') === '#00ff88')
    expect(selectedRect).toBeTruthy()
  })

  it('shows pulsing border for WIP status', () => {
    const wipBead = { ...mockBead, status: 'wip' as const }
    const { container } = render(<svg><Bead bead={wipBead} /></svg>)
    const animate = container.querySelector('animate')
    expect(animate).toBeTruthy()
  })
})
