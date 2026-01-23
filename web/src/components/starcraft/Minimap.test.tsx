import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { Minimap } from './Minimap'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

describe('Minimap component', () => {
  const mockStore = {
    agentsOnMap: [
      { id: 'a1', name: 'agent-1', icon: '🤖', status: 'IDLE', position: { x: 100, y: 100 }, assignedBeadId: null, color: '#00ff88' },
      { id: 'a2', name: 'agent-2', icon: '🔧', status: 'PROCESSING', position: { x: -200, y: 50 }, assignedBeadId: null, color: '#00d4ff' }
    ],
    beadsOnMap: [
      { id: 'b1', title: 'Task', priority: 1, status: 'open', assigneeId: null, position: { x: 50, y: -100 }, isOrphaned: false },
      { id: 'b2', title: 'Task2', priority: 2, status: 'open', assigneeId: null, position: { x: -50, y: 200 }, isOrphaned: false }
    ],
    ralphLoops: [
      { id: 'r1', prompt: 'Test', currentIteration: 0, maxIterations: 10, minIterations: 3, status: 'running', beadId: 'b1', agentQueue: [], activeAgentIndex: 0, qualityScore: null, position: { x: 0, y: 0 } }
    ],
    panX: 0,
    panY: 0,
    zoom: 1,
    setPan: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders minimap container', () => {
    const { container } = render(<Minimap />)
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('width')).toBe('150')
    expect(svg?.getAttribute('height')).toBe('100')
  })

  it('renders agents as circles', () => {
    const { container } = render(<Minimap />)
    const circles = container.querySelectorAll('circle')
    // 2 agents + 1 ralph loop
    expect(circles.length).toBe(3)
  })

  it('renders beads as rectangles', () => {
    const { container } = render(<Minimap />)
    const rects = container.querySelectorAll('rect')
    // 2 beads + 1 viewport rect
    expect(rects.length).toBe(3)
  })

  it('renders ralph loops as circles with stroke', () => {
    const { container } = render(<Minimap />)
    const ralphCircle = container.querySelector('circle[stroke="#ff00ff"]')
    expect(ralphCircle).toBeTruthy()
  })

  it('renders viewport rectangle', () => {
    const { container } = render(<Minimap />)
    const viewportRect = container.querySelector('rect[stroke="white"]')
    expect(viewportRect).toBeTruthy()
  })

  it('calls setPan on click', () => {
    const { container } = render(<Minimap />)
    const svg = container.querySelector('svg')
    fireEvent.click(svg!, { clientX: 100, clientY: 50 })
    expect(mockStore.setPan).toHaveBeenCalled()
  })

  it('colors agents by status', () => {
    const { container } = render(<Minimap />)
    const circles = container.querySelectorAll('circle')
    const idleCircle = Array.from(circles).find(c => c.getAttribute('fill') === '#00ff88')
    const processingCircle = Array.from(circles).find(c => c.getAttribute('fill') === '#00d4ff')
    expect(idleCircle).toBeTruthy()
    expect(processingCircle).toBeTruthy()
  })

  it('colors beads by priority', () => {
    const { container } = render(<Minimap />)
    const rects = container.querySelectorAll('rect')
    const p1Rect = Array.from(rects).find(r => r.getAttribute('fill') === '#ff4444')
    const p2Rect = Array.from(rects).find(r => r.getAttribute('fill') === '#ffcc00')
    expect(p1Rect).toBeTruthy()
    expect(p2Rect).toBeTruthy()
  })
})
