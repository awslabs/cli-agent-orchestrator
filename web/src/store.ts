import { create } from 'zustand'

export interface Task { 
  id: string; title: string; description: string; priority: number; 
  status: string; assignee?: string; created_at?: string 
}

export interface Agent { 
  name: string; description: string; path: string; last_modified?: string
  steering?: string; config?: Record<string, unknown>
}

export interface Session { 
  id: string; name: string; status: 'IDLE' | 'PROCESSING' | 'WAITING_INPUT' | 'ERROR'
  terminals: Terminal[]; agent?: string
}

export interface Terminal { 
  id: string; name: string; agent_profile: string; status: string; session_name: string 
}

export interface Activity { 
  type: string; session_id?: string; timestamp: string; detail?: string; tool?: string 
}

export interface RalphState { 
  id: string; prompt: string; iteration: number; minIterations: number
  maxIterations: number; status: string; active: boolean
  previousFeedback?: { qualityScore: number; qualitySummary: string } 
}

interface Store {
  // Data
  tasks: Task[]; agents: Agent[]; sessions: Session[]
  activity: Activity[]; ralph: RalphState | null
  activeSession: string | null; autoModeSessions: Set<string>
  
  // Actions
  setTasks: (t: Task[]) => void
  setAgents: (a: Agent[]) => void
  setSessions: (s: Session[]) => void
  setRalph: (r: RalphState | null) => void
  setActiveSession: (id: string | null) => void
  addActivity: (a: Activity) => void
  toggleAutoMode: (sessionId: string) => void
}

export const useStore = create<Store>((set) => ({
  tasks: [], agents: [], sessions: [], activity: [], ralph: null,
  activeSession: null, autoModeSessions: new Set(),
  
  setTasks: (tasks) => set({ tasks }),
  setAgents: (agents) => set({ agents }),
  setSessions: (sessions) => set({ sessions }),
  setRalph: (ralph) => set({ ralph }),
  setActiveSession: (activeSession) => set({ activeSession }),
  addActivity: (a) => set((s) => ({ activity: [a, ...s.activity].slice(0, 100) })),
  toggleAutoMode: (sessionId) => set((s) => {
    const next = new Set(s.autoModeSessions)
    if (next.has(sessionId)) next.delete(sessionId)
    else next.add(sessionId)
    return { autoModeSessions: next }
  })
}))
