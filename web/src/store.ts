import { create } from 'zustand'

export interface Task { id: string; title: string; description: string; priority: number; status: string; assignee?: string; created_at?: string }
export interface RalphState { id: string; prompt: string; iteration: number; minIterations: number; maxIterations: number; status: string; active: boolean; previousFeedback?: { qualityScore: number; qualitySummary: string } }
export interface Agent { id: string; name: string; status: string; provider: string }

interface Store {
  tasks: Task[]; ralph: RalphState | null; agents: Agent[]; activity: string[]
  setTasks: (t: Task[]) => void; setRalph: (r: RalphState | null) => void; setAgents: (a: Agent[]) => void; addActivity: (a: string) => void
}

export const useStore = create<Store>((set) => ({
  tasks: [], ralph: null, agents: [], activity: [],
  setTasks: (tasks) => set({ tasks }),
  setRalph: (ralph) => set({ ralph }),
  setAgents: (agents) => set({ agents }),
  addActivity: (msg) => set((s) => ({ activity: [msg, ...s.activity].slice(0, 50) }))
}))
