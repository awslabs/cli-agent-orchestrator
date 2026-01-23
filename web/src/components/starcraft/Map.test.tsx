import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { Map } from './Map'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

vi.mock('./Agent', () => ({ Agent: () => <g data-testid="agent" /> }))
vi.mock('./Bead', () => ({ Bead: () => <g data-testid="bead" /> }))
vi.mock('./AssignmentLine', () => ({ AssignmentLine: () => <line data-testid="line" /> }))
vi.mock('./RalphLoop', () => ({ RalphLoop: () => <g data-testid="ralph" /> }))
vi.mock('./Minimap', () => ({ Minimap: () => <div data-testid="minimap" /> }))

describe('Map', () => {
  const mockStore = {
    zoom: 1,
    panX: 0,
    panY: 0,
    setZoom: vi.fn(),
    pan: vi.fn(),
    agentsOnMap: [{ id: 'a1', assignedBeadId: 'b1', position: { x: 100, y: 100 } }],
    beadsOnMap: [{ id: 'b1', position: { x: 160, y: 180 } }],
    ralphLoops: [],
    selectItem: vi.fn(),
    showContextMenu: vi.fn(),
    hideContextMenu: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders SVG map', () => {
    const { container } = render(<Map />)
    expect(container.querySelector('svg')).toBeTruthy()
  })

  it('renders agents', () => {
    const { container } = render(<Map />)
    expect(container.querySelector('[data-testid="agent"]')).toBeTruthy()
  })

  it('renders beads with position', () => {
    const { container } = render(<Map />)
    expect(container.querySelector('[data-testid="bead"]')).toBeTruthy()
  })

  it('renders assignment lines', () => {
    const { container } = render(<Map />)
    expect(container.querySelector('[data-testid="line"]')).toBeTruthy()
  })

  it('shows zoom indicator', () => {
    const { container } = render(<Map />)
    expect(container.textContent).toContain('100%')
  })

  it('handles pan via drag', () => {
    const { container } = render(<Map />)
    const svg = container.querySelector('svg')!
    fireEvent.mouseDown(svg, { button: 0, clientX: 100, clientY: 100 })
    fireEvent.mouseMove(svg, { clientX: 150, clientY: 120 })
    fireEvent.mouseUp(svg)
    expect(mockStore.pan).toHaveBeenCalledWith(50, 20)
  })

  it('handles keyboard pan', () => {
    render(<Map />)
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(mockStore.pan).toHaveBeenCalledWith(-50, 0)
  })

  it('handles keyboard zoom', () => {
    render(<Map />)
    fireEvent.keyDown(window, { key: '+' })
    expect(mockStore.setZoom).toHaveBeenCalledWith(1.1)
  })

  it('handles escape to deselect', () => {
    render(<Map />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(mockStore.selectItem).toHaveBeenCalledWith(null, null)
    expect(mockStore.hideContextMenu).toHaveBeenCalled()
  })

  it('shows context menu on right click', () => {
    const { container } = render(<Map />)
    const svg = container.querySelector('svg')!
    fireEvent.contextMenu(svg)
    expect(mockStore.showContextMenu).toHaveBeenCalled()
  })

  it('deselects on click empty space', () => {
    const { container } = render(<Map />)
    const svg = container.querySelector('svg')!
    fireEvent.click(svg)
    expect(mockStore.selectItem).toHaveBeenCalledWith(null, null)
  })

  it('renders grid pattern', () => {
    const { container } = render(<Map />)
    expect(container.querySelector('pattern#grid')).toBeTruthy()
  })

  it('hides grid at low zoom', () => {
    ;(useStarCraftStore as any).mockReturnValue({ ...mockStore, zoom: 0.4 })
    const { container } = render(<Map />)
    const gridRect = container.querySelector('rect[fill="url(#grid)"]')
    expect(gridRect?.getAttribute('opacity')).toBe('0')
  })
})
