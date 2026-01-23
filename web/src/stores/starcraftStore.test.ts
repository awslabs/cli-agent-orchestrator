import { describe, it, expect, beforeEach } from 'vitest'
import { useStarCraftStore, getAgentIcon, getAgentColor } from './starcraftStore'

describe('starcraftStore', () => {
  beforeEach(() => {
    useStarCraftStore.setState({
      zoom: 1,
      panX: 0,
      panY: 0,
      selectedId: null,
      selectedType: null,
      hoveredId: null,
      agentsOnMap: [],
      beadsOnMap: [],
      beadsInQueue: [],
      ralphLoops: [],
      terminalOpen: false,
      terminalAgentId: null,
      contextMenu: null
    })
  })

  describe('zoom', () => {
    it('sets zoom within bounds', () => {
      const { setZoom } = useStarCraftStore.getState()
      setZoom(1.5)
      expect(useStarCraftStore.getState().zoom).toBe(1.5)
    })

    it('clamps zoom to min 0.25', () => {
      const { setZoom } = useStarCraftStore.getState()
      setZoom(0.1)
      expect(useStarCraftStore.getState().zoom).toBe(0.25)
    })

    it('clamps zoom to max 2', () => {
      const { setZoom } = useStarCraftStore.getState()
      setZoom(3)
      expect(useStarCraftStore.getState().zoom).toBe(2)
    })
  })

  describe('pan', () => {
    it('pans by delta', () => {
      const { pan } = useStarCraftStore.getState()
      pan(50, -30)
      const state = useStarCraftStore.getState()
      expect(state.panX).toBe(50)
      expect(state.panY).toBe(-30)
    })

    it('accumulates pan', () => {
      const { pan } = useStarCraftStore.getState()
      pan(10, 10)
      pan(20, 20)
      const state = useStarCraftStore.getState()
      expect(state.panX).toBe(30)
      expect(state.panY).toBe(30)
    })
  })

  describe('selection', () => {
    it('selects item', () => {
      const { selectItem } = useStarCraftStore.getState()
      selectItem('agent-1', 'agent')
      const state = useStarCraftStore.getState()
      expect(state.selectedId).toBe('agent-1')
      expect(state.selectedType).toBe('agent')
    })

    it('deselects', () => {
      const { selectItem } = useStarCraftStore.getState()
      selectItem('agent-1', 'agent')
      selectItem(null, null)
      const state = useStarCraftStore.getState()
      expect(state.selectedId).toBeNull()
      expect(state.selectedType).toBeNull()
    })
  })

  describe('agents', () => {
    it('moves agent', () => {
      const { setAgents, moveAgent } = useStarCraftStore.getState()
      setAgents([{ id: 'a1', name: 'test', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' }])
      moveAgent('a1', { x: 100, y: 200 })
      const agent = useStarCraftStore.getState().agentsOnMap[0]
      expect(agent.position).toEqual({ x: 100, y: 200 })
    })

    it('updates agent status', () => {
      const { setAgents, updateAgentStatus } = useStarCraftStore.getState()
      setAgents([{ id: 'a1', name: 'test', icon: '🤖', status: 'IDLE', position: { x: 0, y: 0 }, assignedBeadId: null, color: '#00ff88' }])
      updateAgentStatus('a1', 'PROCESSING')
      expect(useStarCraftStore.getState().agentsOnMap[0].status).toBe('PROCESSING')
    })
  })

  describe('beads', () => {
    it('assigns bead to agent', () => {
      const { setAgents, setBeads, assignBead } = useStarCraftStore.getState()
      setAgents([{ id: 'a1', name: 'test', icon: '🤖', status: 'IDLE', position: { x: 100, y: 100 }, assignedBeadId: null, color: '#00ff88' }])
      useStarCraftStore.setState({
        beadsInQueue: [{ id: 'b1', title: 'Task', priority: 1, status: 'open', assigneeId: null, position: null, isOrphaned: false }]
      })
      assignBead('b1', 'a1')
      const state = useStarCraftStore.getState()
      expect(state.beadsInQueue).toHaveLength(0)
      expect(state.beadsOnMap).toHaveLength(1)
      expect(state.beadsOnMap[0].assigneeId).toBe('a1')
      expect(state.agentsOnMap[0].assignedBeadId).toBe('b1')
    })

    it('unassigns bead', () => {
      const { setAgents, unassignBead } = useStarCraftStore.getState()
      setAgents([{ id: 'a1', name: 'test', icon: '🤖', status: 'IDLE', position: { x: 100, y: 100 }, assignedBeadId: 'b1', color: '#00ff88' }])
      useStarCraftStore.setState({
        beadsOnMap: [{ id: 'b1', title: 'Task', priority: 1, status: 'open', assigneeId: 'a1', position: { x: 160, y: 180 }, isOrphaned: false }]
      })
      unassignBead('b1')
      const state = useStarCraftStore.getState()
      expect(state.beadsOnMap).toHaveLength(0)
      expect(state.beadsInQueue).toHaveLength(1)
      expect(state.agentsOnMap[0].assignedBeadId).toBeNull()
    })

    it('moves bead', () => {
      const { moveBead } = useStarCraftStore.getState()
      useStarCraftStore.setState({
        beadsOnMap: [{ id: 'b1', title: 'Task', priority: 1, status: 'open', assigneeId: null, position: { x: 0, y: 0 }, isOrphaned: false }]
      })
      moveBead('b1', { x: 50, y: 75 })
      expect(useStarCraftStore.getState().beadsOnMap[0].position).toEqual({ x: 50, y: 75 })
    })
  })

  describe('terminal', () => {
    it('opens terminal', () => {
      const { openTerminal } = useStarCraftStore.getState()
      openTerminal('a1')
      const state = useStarCraftStore.getState()
      expect(state.terminalOpen).toBe(true)
      expect(state.terminalAgentId).toBe('a1')
    })

    it('closes terminal', () => {
      const { openTerminal, closeTerminal } = useStarCraftStore.getState()
      openTerminal('a1')
      closeTerminal()
      const state = useStarCraftStore.getState()
      expect(state.terminalOpen).toBe(false)
      expect(state.terminalAgentId).toBeNull()
    })
  })

  describe('context menu', () => {
    it('shows context menu', () => {
      const { showContextMenu } = useStarCraftStore.getState()
      showContextMenu({ x: 100, y: 200, type: 'agent', targetId: 'a1' })
      const menu = useStarCraftStore.getState().contextMenu
      expect(menu).toEqual({ x: 100, y: 200, type: 'agent', targetId: 'a1' })
    })

    it('hides context menu', () => {
      const { showContextMenu, hideContextMenu } = useStarCraftStore.getState()
      showContextMenu({ x: 100, y: 200, type: 'agent', targetId: 'a1' })
      hideContextMenu()
      expect(useStarCraftStore.getState().contextMenu).toBeNull()
    })
  })

  describe('ralph loops', () => {
    it('adds ralph loop', () => {
      const { addRalphLoop } = useStarCraftStore.getState()
      addRalphLoop({
        id: 'r1', prompt: 'Test', currentIteration: 0, maxIterations: 10, minIterations: 3,
        status: 'running', beadId: 'b1', agentQueue: [], activeAgentIndex: 0, qualityScore: null, position: { x: 0, y: 0 }
      })
      expect(useStarCraftStore.getState().ralphLoops).toHaveLength(1)
    })
  })
})

describe('helper functions', () => {
  it('getAgentIcon returns correct icon', () => {
    expect(getAgentIcon('generalist')).toBe('🤖')
    expect(getAgentIcon('bob-the-builder')).toBe('🔧')
    expect(getAgentIcon('unknown')).toBe('👤')
  })

  it('getAgentColor cycles through colors', () => {
    expect(getAgentColor(0)).toBe('#00ff88')
    expect(getAgentColor(6)).toBe('#00ff88') // wraps
  })
})
