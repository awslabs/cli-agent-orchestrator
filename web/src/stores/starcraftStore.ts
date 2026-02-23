import { create } from 'zustand'

export interface Position { x: number; y: number }

export interface AgentOnMap {
  id: string
  name: string
  icon: string
  status: 'IDLE' | 'PROCESSING' | 'WAITING_INPUT' | 'ERROR'
  position: Position
  assignedBeadId: string | null
  color: string
  parentSession: string | null
}

export interface BeadOnMap {
  id: string
  title: string
  priority: 1 | 2 | 3
  status: 'open' | 'wip' | 'closed'
  assigneeId: string | null
  position: Position | null
  isOrphaned: boolean
}

export interface RalphLoop {
  id: string
  prompt: string
  currentIteration: number
  maxIterations: number
  minIterations: number
  status: 'running' | 'paused' | 'completed'
  beadId: string
  agentQueue: string[]
  activeAgentIndex: number
  qualityScore: number | null
  position: Position
}

interface ContextMenu {
  x: number
  y: number
  type: 'agent' | 'bead' | 'empty' | 'ralph'
  targetId: string | null
}

interface DeleteProgress {
  parentId: string
  children: string[]
  deletedIds: string[]
  currentTarget: string | null
}

interface StarCraftStore {
  // Map state
  zoom: number
  panX: number
  panY: number
  
  // Selection
  selectedId: string | null
  selectedType: 'agent' | 'bead' | 'ralph' | null
  hoveredId: string | null
  
  // Data
  agentsOnMap: AgentOnMap[]
  beadsOnMap: BeadOnMap[]
  beadsInQueue: BeadOnMap[]
  ralphLoops: RalphLoop[]
  
  // UI state
  terminalOpen: boolean
  terminalAgentId: string | null
  contextMenu: ContextMenu | null
  deleteProgress: DeleteProgress | null
  
  // Actions
  setZoom: (zoom: number) => void
  setPan: (x: number, y: number) => void
  pan: (dx: number, dy: number) => void
  selectItem: (id: string | null, type: 'agent' | 'bead' | 'ralph' | null) => void
  setHovered: (id: string | null) => void
  moveAgent: (id: string, position: Position) => void
  moveBead: (id: string, position: Position) => void
  assignBead: (beadId: string, agentId: string) => void
  unassignBead: (beadId: string) => void
  openTerminal: (agentId: string) => void
  closeTerminal: () => void
  showContextMenu: (menu: ContextMenu) => void
  hideContextMenu: () => void
  setAgents: (agents: AgentOnMap[]) => void
  setBeads: (beads: BeadOnMap[]) => void
  setRalphLoops: (loops: RalphLoop[]) => void
  addBead: (bead: BeadOnMap) => void
  addRalphLoop: (loop: RalphLoop) => void
  updateAgentStatus: (id: string, status: AgentOnMap['status']) => void
  setDeleteProgress: (progress: DeleteProgress | null) => void
  markDeleted: (id: string) => void
}

const AGENT_ICONS: Record<string, string> = {
  generalist: '🤖', 'bob-the-builder': '🔧', 'log-diver': '🔍',
  'oncall-buddy': '🛠️', 'ticket-ninja': '🥷', 'sns-ticket-ninja': '📨',
  atlas: '🗺️', 'ralph-wiggum': '🔄'
}

const AGENT_COLORS = ['#00ff88', '#00d4ff', '#ff00ff', '#ffcc00', '#ff4444', '#88ff00']

export const getAgentIcon = (name: string) => AGENT_ICONS[name] || '👤'
export const getAgentColor = (index: number) => AGENT_COLORS[index % AGENT_COLORS.length]

export const useStarCraftStore = create<StarCraftStore>((set) => ({
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
  contextMenu: null,
  deleteProgress: null,

  setZoom: (zoom) => set({ zoom: Math.max(0.25, Math.min(2, zoom)) }),
  setPan: (panX, panY) => set({ panX, panY }),
  pan: (dx, dy) => set((s) => ({ panX: s.panX + dx, panY: s.panY + dy })),
  selectItem: (selectedId, selectedType) => set({ selectedId, selectedType }),
  setHovered: (hoveredId) => set({ hoveredId }),
  
  moveAgent: (id, position) => set((s) => ({
    agentsOnMap: s.agentsOnMap.map(a => a.id === id ? { ...a, position } : a)
  })),
  
  moveBead: (id, position) => set((s) => ({
    beadsOnMap: s.beadsOnMap.map(b => b.id === id ? { ...b, position } : b)
  })),
  
  assignBead: (beadId, agentId) => set((s) => {
    const bead = s.beadsInQueue.find(b => b.id === beadId) || s.beadsOnMap.find(b => b.id === beadId)
    if (!bead) return s
    const agent = s.agentsOnMap.find(a => a.id === agentId)
    if (!agent) return s
    
    const updatedBead: BeadOnMap = { 
      ...bead, 
      assigneeId: agentId, 
      position: { x: agent.position.x + 60, y: agent.position.y + 80 } 
    }
    
    return {
      beadsInQueue: s.beadsInQueue.filter(b => b.id !== beadId),
      beadsOnMap: [...s.beadsOnMap.filter(b => b.id !== beadId), updatedBead],
      agentsOnMap: s.agentsOnMap.map(a => a.id === agentId ? { ...a, assignedBeadId: beadId } : a)
    }
  }),
  
  unassignBead: (beadId) => set((s) => {
    const bead = s.beadsOnMap.find(b => b.id === beadId)
    if (!bead) return s
    
    return {
      beadsOnMap: s.beadsOnMap.filter(b => b.id !== beadId),
      beadsInQueue: [...s.beadsInQueue, { ...bead, assigneeId: null, position: null }],
      agentsOnMap: s.agentsOnMap.map(a => a.assignedBeadId === beadId ? { ...a, assignedBeadId: null } : a)
    }
  }),
  
  openTerminal: (terminalAgentId) => set({ terminalOpen: true, terminalAgentId }),
  closeTerminal: () => set({ terminalOpen: false, terminalAgentId: null }),
  showContextMenu: (contextMenu) => set({ contextMenu }),
  hideContextMenu: () => set({ contextMenu: null }),
  setAgents: (agentsOnMap) => set({ agentsOnMap }),
  setBeads: (beads) => {
    const beadsOnMap = beads.filter(b => b.position !== null)
    const beadsInQueue = beads.filter(b => b.position === null)
    return set({ beadsOnMap, beadsInQueue })
  },
  setRalphLoops: (ralphLoops) => set({ ralphLoops }),
  addBead: (bead) => set((s) => ({
    beadsInQueue: [...s.beadsInQueue, bead]
  })),
  addRalphLoop: (loop) => set((s) => ({
    ralphLoops: [...s.ralphLoops, loop]
  })),
  updateAgentStatus: (id, status) => set((s) => ({
    agentsOnMap: s.agentsOnMap.map(a => a.id === id ? { ...a, status } : a)
  })),
  setDeleteProgress: (deleteProgress) => set({ deleteProgress }),
  markDeleted: (id) => set((s) => ({
    deleteProgress: s.deleteProgress ? {
      ...s.deleteProgress,
      deletedIds: [...s.deleteProgress.deletedIds, id],
      currentTarget: null
    } : null,
    agentsOnMap: s.agentsOnMap.filter(a => a.id !== id)
  }))
}))
