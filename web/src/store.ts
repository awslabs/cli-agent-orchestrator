import { create } from 'zustand'

export interface Task {
  id: string; title: string; description: string; priority: number;
  status: string; assignee?: string; created_at?: string
  parent_id?: string; blocked_by?: string[]
  labels?: string[]; type?: string
}

export interface Agent {
  name: string; description: string; path: string; last_modified?: string
  steering?: string; config?: Record<string, unknown>
}

export interface Session {
  id: string; name: string; status: 'IDLE' | 'PROCESSING' | 'WAITING_INPUT' | 'ERROR'
  terminals: Terminal[]; agent?: string; agent_name: string
  parent_session?: string
}

export interface Terminal {
  id: string; name: string; agent_profile: string; status: string; session_name: string
}

export interface Activity {
  type: string; session_id?: string; timestamp: string; detail?: string; tool?: string; message?: string
}

export interface RalphState {
  id: string; prompt: string; iteration: number; minIterations: number
  maxIterations: number; status: string; active: boolean
  previousFeedback?: { qualityScore: number; qualitySummary: string }
}

export interface Flow {
  name: string; schedule: string; agent_profile: string; provider: string
  enabled: boolean; next_run?: string; last_run?: string
}

export interface InboxMessage {
  id: number; sender_id: string; receiver_id: string
  message: string; status: 'pending' | 'delivered' | 'failed'
  created_at: string
}

export interface Snackbar {
  message: string
  type: 'success' | 'error' | 'info'
}

interface Store {
  // Data
  tasks: Task[]; agents: Agent[]; sessions: Session[]
  activity: Activity[]; ralph: RalphState | null; flows: Flow[]
  messages: InboxMessage[]
  activeSession: string | null; autoModeSessions: Set<string>
  snackbar: Snackbar | null

  // Actions
  setTasks: (t: Task[]) => void
  setAgents: (a: Agent[]) => void
  setSessions: (s: Session[]) => void
  setRalph: (r: RalphState | null) => void
  setFlows: (f: Flow[]) => void
  setMessages: (m: InboxMessage[]) => void
  setActiveSession: (id: string | null) => void
  addActivity: (a: Activity) => void
  toggleAutoMode: (sessionId: string) => void
  showSnackbar: (message: string, type: Snackbar['type']) => void
  hideSnackbar: () => void

  // Granular mutations for WS-driven updates
  addTask: (t: Task) => void
  updateTask: (id: string, updates: Partial<Task>) => void
  removeTask: (id: string) => void
  addSession: (s: Session) => void
  removeSession: (id: string) => void
  updateSession: (id: string, updates: Partial<Session>) => void
  setActivity: (a: Activity[]) => void
}

export const useStore = create<Store>((set) => ({
  tasks: [], agents: [], sessions: [], activity: [], ralph: null, flows: [],
  messages: [],
  activeSession: null, autoModeSessions: new Set(),
  snackbar: null,

  setTasks: (tasks) => set({ tasks }),
  setAgents: (agents) => set({ agents }),
  setSessions: (sessions) => set({ sessions }),
  setRalph: (ralph) => set({ ralph }),
  setFlows: (flows) => set({ flows }),
  setMessages: (messages) => set({ messages }),
  setActiveSession: (activeSession) => set({ activeSession }),
  addActivity: (a) => set((s) => ({ activity: [a, ...s.activity].slice(0, 100) })),
  toggleAutoMode: (sessionId) => set((s) => {
    const next = new Set(s.autoModeSessions)
    if (next.has(sessionId)) next.delete(sessionId)
    else next.add(sessionId)
    return { autoModeSessions: next }
  }),
  showSnackbar: (message, type) => set({ snackbar: { message, type } }),
  hideSnackbar: () => set({ snackbar: null }),

  // Granular mutations
  addTask: (t) => set((s) => ({ tasks: [...s.tasks, t] })),
  updateTask: (id, updates) => set((s) => ({ tasks: s.tasks.map(t => t.id === id ? { ...t, ...updates } : t) })),
  removeTask: (id) => set((s) => ({ tasks: s.tasks.filter(t => t.id !== id) })),
  addSession: (sess) => set((s) => ({ sessions: [...s.sessions, sess] })),
  removeSession: (id) => set((s) => ({ sessions: s.sessions.filter(sess => sess.id !== id) })),
  updateSession: (id, updates) => set((s) => ({ sessions: s.sessions.map(sess => sess.id === id ? { ...sess, ...updates } : sess) })),
  setActivity: (activity) => set({ activity })
}))
