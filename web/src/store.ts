import { create } from 'zustand'
import { api, Session, SessionDetail, TerminalMeta } from './api'

// Only trigger React re-renders when data actually changed
function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

interface Snackbar {
  type: 'success' | 'error' | 'info'
  message: string
}

// Runs UI: an agent-to-agent message pulse animated on the flow graph.
export interface FlowPulse {
  id: number
  sender: string
  receiver: string
  kind: string // 'handoff' | 'assign' | 'message' | 'task'
  ts: number
}

interface Store {
  sessions: Session[]
  activeSession: string | null
  activeSessionDetail: SessionDetail | null
  connected: boolean
  snackbar: Snackbar | null
  terminalStatuses: Record<string, string>
  flowPulses: FlowPulse[]

  fetchSessions: () => Promise<void>
  selectSession: (name: string | null) => Promise<void>
  createSession: (provider: string, agentProfile: string, workingDirectory?: string, sessionName?: string) => Promise<void>
  deleteSession: (name: string) => Promise<void>
  showSnackbar: (snackbar: Snackbar) => void
  hideSnackbar: () => void
  setConnected: (connected: boolean) => void
  setTerminalStatus: (id: string, status: string) => void
  clearTerminalStatuses: (ids: string[]) => void
  connectStatusStream: () => EventSource
  pushFlowPulse: (pulse: Omit<FlowPulse, 'id' | 'ts'>) => void
}

let pulseSeq = 0

export const useStore = create<Store>((set, get) => ({
  sessions: [],
  activeSession: null,
  activeSessionDetail: null,
  connected: false,
  snackbar: null,
  terminalStatuses: {},
  flowPulses: [],

  fetchSessions: async () => {
    try {
      const sessions = await api.listSessions()
      const prev = get()
      // Only skip empty responses when reconnecting (connected was false),
      // not after intentional deletions.
      if (sessions.length === 0 && prev.sessions.length > 0 && !prev.connected) {
        set({ connected: true })
        return
      }
      if (!prev.connected || !jsonEqual(prev.sessions, sessions)) {
        set({ sessions, connected: true })
      }
    } catch {
      if (get().connected) set({ connected: false })
    }
  },

  selectSession: async (name) => {
    if (!name) {
      set({ activeSession: null, activeSessionDetail: null })
      return
    }
    set({ activeSession: name })
    try {
      const detail = await api.getSession(name)
      if (!jsonEqual(get().activeSessionDetail, detail)) {
        set({ activeSessionDetail: detail })
      }
    } catch {
      set({ activeSessionDetail: null })
    }
  },

  createSession: async (provider, agentProfile, workingDirectory, sessionName) => {
    try {
      await api.createSession(provider, agentProfile, sessionName, workingDirectory)
      get().showSnackbar({ type: 'success', message: 'Session created' })
      await get().fetchSessions()
    } catch (e: any) {
      get().showSnackbar({ type: 'error', message: e.message || 'Failed to create session' })
    }
  },

  deleteSession: async (name) => {
    try {
      await api.deleteSession(name)
      get().showSnackbar({ type: 'success', message: `Deleted ${name}` })
      if (get().activeSession === name) {
        set({ activeSession: null, activeSessionDetail: null })
      }
      await get().fetchSessions()
    } catch (e: any) {
      get().showSnackbar({ type: 'error', message: e.message || 'Failed to delete session' })
    }
  },

  showSnackbar: (snackbar) => set({ snackbar }),
  hideSnackbar: () => set({ snackbar: null }),
  setConnected: (connected) => set({ connected }),
  connectStatusStream: () => {
    // Live status push: one EventSource replaces per-terminal polling.
    // The stream is deltas-only and lossy on reconnect, so components still
    // fetch current state via REST on mount/(re)connect; this just makes
    // status changes land instantly. EventSource auto-reconnects. This is the
    // Runs dashboard's own stream (/events/runs), kept distinct from the
    // fleet-UI /events stream so the two can coexist.
    const es = new EventSource('/events/runs')
    es.addEventListener('status', (e: MessageEvent) => {
      try {
        const { terminal_id, status } = JSON.parse(e.data)
        if (terminal_id && status) get().setTerminalStatus(terminal_id, status)
      } catch {
        // Malformed frame — ignore; REST refresh remains the safety net.
      }
    })
    es.addEventListener('flow', (e: MessageEvent) => {
      try {
        const { sender_id, receiver_id, kind } = JSON.parse(e.data)
        if (sender_id && receiver_id) {
          get().pushFlowPulse({ sender: sender_id, receiver: receiver_id, kind: kind || 'message' })
          // A flow event often means the roster just changed (a handoff
          // spawned a worker) — refresh sooner than the slow reconcile.
          get().fetchSessions()
        }
      } catch {
        // Malformed frame — ignore.
      }
    })
    es.onopen = () => get().setConnected(true)
    // A dropped stream flips to Offline; EventSource auto-reconnects and
    // onopen flips it back. The 10s REST reconcile remains the safety net.
    es.onerror = () => get().setConnected(false)
    return es
  },
  pushFlowPulse: (pulse) =>
    set(state => ({
      flowPulses: [
        ...state.flowPulses.filter(p => Date.now() - p.ts < 30_000),
        { ...pulse, id: ++pulseSeq, ts: Date.now() },
      ],
    })),
  setTerminalStatus: (id, status) =>
    set(state => {
      const normalized = status ? status.toUpperCase() : status
      if (state.terminalStatuses[id] === normalized) return state
      return { terminalStatuses: { ...state.terminalStatuses, [id]: normalized } }
    }),
  clearTerminalStatuses: (ids) =>
    set(state => {
      const next: Record<string, string> = {}
      for (const id of ids) {
        if (state.terminalStatuses[id]) next[id] = state.terminalStatuses[id]
      }
      if (Object.keys(next).length === Object.keys(state.terminalStatuses).length) return state
      return { terminalStatuses: next }
    }),
}))
