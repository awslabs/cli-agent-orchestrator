import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { StarCraftView } from './StarCraftView'
import { useStarCraftStore } from '../../stores/starcraftStore'

// Mock API
vi.mock('../../api', () => ({
  api: {
    sessions: { list: vi.fn().mockResolvedValue([]) },
    tasks: { list: vi.fn().mockResolvedValue([]), delete: vi.fn().mockResolvedValue({}) },
  },
  createActivityStream: vi.fn(() => ({ close: vi.fn() })),
}))

describe('StarCraftView', () => {
  beforeEach(() => {
    useStarCraftStore.setState({
      agentsOnMap: [],
      beadsOnMap: [],
      beadsInQueue: [],
      ralphLoops: [],
      selectedId: null,
      selectedType: null,
      terminalOpen: false,
      terminalAgentId: null,
      contextMenu: null,
      zoom: 1,
      panX: 0,
      panY: 0,
    })
  })

  it('renders main layout with header', () => {
    render(<StarCraftView />)
    expect(screen.getByText('🎮 CAO COMMAND')).toBeInTheDocument()
    expect(screen.getByText('StarCraft Mode')).toBeInTheDocument()
  })

  it('renders zoom indicator', () => {
    render(<StarCraftView />)
    // Zoom indicator is in the bottom-left area
    expect(screen.getAllByText('100%').length).toBeGreaterThan(0)
  })

  it('renders bead queue panel', () => {
    render(<StarCraftView />)
    expect(screen.getByText('📋 BEAD QUEUE')).toBeInTheDocument()
  })

  it('opens help modal on ? key', async () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: '?' })
    await waitFor(() => {
      expect(screen.getByText('⌨️ Keyboard Shortcuts')).toBeInTheDocument()
    })
  })

  it('opens new bead modal on N key', async () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => {
      expect(screen.getByText('📋 New Bead')).toBeInTheDocument()
    })
  })

  it('opens new ralph modal on R key', async () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: 'r' })
    await waitFor(() => {
      expect(screen.getByText('🔄 New Ralph Loop')).toBeInTheDocument()
    })
  })

  it('closes modals on Escape', async () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: 'n' })
    await waitFor(() => expect(screen.getByText('📋 New Bead')).toBeInTheDocument())
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('📋 New Bead')).not.toBeInTheDocument())
  })

  it('cycles agents with Tab', () => {
    useStarCraftStore.setState({
      agentsOnMap: [
        { id: 'a1', name: 'agent1', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' },
        { id: 'a2', name: 'agent2', icon: '🔧', status: 'IDLE', position: { x: 100, y: 0 }, assignedBeadId: null, color: '#00d4ff' },
      ],
    })
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(useStarCraftStore.getState().selectedId).toBe('a1')
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(useStarCraftStore.getState().selectedId).toBe('a2')
  })

  it('selects agent by number key', () => {
    useStarCraftStore.setState({
      agentsOnMap: [
        { id: 'a1', name: 'agent1', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' },
        { id: 'a2', name: 'agent2', icon: '🔧', status: 'IDLE', position: { x: 100, y: 0 }, assignedBeadId: null, color: '#00d4ff' },
      ],
    })
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: '2' })
    expect(useStarCraftStore.getState().selectedId).toBe('a2')
  })

  it('jumps to stuck agent on Space', () => {
    useStarCraftStore.setState({
      agentsOnMap: [
        { id: 'a1', name: 'agent1', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' },
        { id: 'a2', name: 'stuck', icon: '🔧', status: 'WAITING_INPUT', position: { x: 100, y: 0 }, assignedBeadId: null, color: '#00d4ff' },
      ],
    })
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: ' ' })
    expect(useStarCraftStore.getState().selectedId).toBe('a2')
    expect(useStarCraftStore.getState().terminalOpen).toBe(true)
  })

  it('opens terminal on Enter when agent selected', () => {
    useStarCraftStore.setState({
      agentsOnMap: [
        { id: 'a1', name: 'agent1', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' },
      ],
      selectedId: 'a1',
      selectedType: 'agent',
    })
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(useStarCraftStore.getState().terminalOpen).toBe(true)
  })

  it('applies dark terminal theme', () => {
    const { container } = render(<StarCraftView />)
    const mainDiv = container.firstChild as HTMLElement
    expect(mainDiv.style.background).toBe('#0a0a0f')
    expect(mainDiv.style.color).toBe('#e0e0e0')
  })

  it('updates zoom on + key', () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: '+' })
    expect(useStarCraftStore.getState().zoom).toBeCloseTo(1.1, 1)
  })

  it('updates zoom on - key', () => {
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: '-' })
    expect(useStarCraftStore.getState().zoom).toBeCloseTo(0.9, 1)
  })

  it('resets zoom on 0 key', () => {
    useStarCraftStore.setState({ zoom: 1.5 })
    render(<StarCraftView />)
    fireEvent.keyDown(window, { key: '0' })
    expect(useStarCraftStore.getState().zoom).toBe(1)
  })
})
