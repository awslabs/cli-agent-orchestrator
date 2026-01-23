import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { RalphLoop } from './RalphLoop'
import { useStarCraftStore, RalphLoop as RalphLoopType } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

const mockLoop: RalphLoopType = {
  id: 'ralph-1',
  prompt: 'Build REST API',
  currentIteration: 3,
  maxIterations: 10,
  minIterations: 3,
  status: 'running',
  beadId: 'b1',
  agentQueue: ['a1', 'a2'],
  activeAgentIndex: 0,
  qualityScore: 7,
  position: { x: 200, y: 200 }
}

describe('RalphLoop', () => {
  const mockStore = {
    agentsOnMap: [
      { id: 'a1', icon: '🔧' },
      { id: 'a2', icon: '🤖' }
    ],
    selectItem: vi.fn(),
    selectedId: null,
    showContextMenu: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders ralph loop with prompt', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    expect(container.textContent).toContain('RALPH')
    expect(container.textContent).toContain('Build REST')
  })

  it('shows iteration count', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    expect(container.textContent).toContain('3/10')
  })

  it('shows quality score', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    expect(container.textContent).toContain('Quality: 7/10')
  })

  it('renders orbiting agents', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    expect(container.textContent).toContain('🔧')
    expect(container.textContent).toContain('🤖')
  })

  it('marks active agent', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    expect(container.textContent).toContain('ACTIVE')
  })

  it('shows selection ring when selected', () => {
    ;(useStarCraftStore as any).mockReturnValue({ ...mockStore, selectedId: 'ralph-1' })
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    const circles = container.querySelectorAll('circle')
    const selectionRing = Array.from(circles).find(c => c.getAttribute('stroke') === '#ff00ff')
    expect(selectionRing).toBeTruthy()
  })

  it('calls selectItem on click', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    const g = container.querySelector('g')
    fireEvent.click(g!)
    expect(mockStore.selectItem).toHaveBeenCalledWith('ralph-1', 'ralph')
  })

  it('shows context menu on right click', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    const g = container.querySelector('g')
    fireEvent.contextMenu(g!)
    expect(mockStore.showContextMenu).toHaveBeenCalled()
  })

  it('renders progress bar', () => {
    const { container } = render(<svg><RalphLoop loop={mockLoop} /></svg>)
    const rects = container.querySelectorAll('rect')
    expect(rects.length).toBeGreaterThanOrEqual(2) // background + progress
  })

  it('hides quality score when null', () => {
    const loopNoScore = { ...mockLoop, qualityScore: null }
    const { container } = render(<svg><RalphLoop loop={loopNoScore} /></svg>)
    expect(container.textContent).not.toContain('Quality:')
  })
})
