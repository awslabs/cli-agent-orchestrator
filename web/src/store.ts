import { create } from 'zustand'
import {
  api,
  Session,
  SessionDetail,
  TerminalMeta,
  RunSummaryRow,
  RunInspection,
  WorkflowEvent,
  GapMarker,
} from './api'

// Only trigger React re-renders when data actually changed
function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

interface Snackbar {
  type: 'success' | 'error' | 'info'
  message: string
}

interface Store {
  sessions: Session[]
  activeSession: string | null
  activeSessionDetail: SessionDetail | null
  connected: boolean
  snackbar: Snackbar | null
  terminalStatuses: Record<string, string>

  fetchSessions: () => Promise<void>
  selectSession: (name: string | null) => Promise<void>
  createSession: (provider: string, agentProfile: string, workingDirectory?: string, sessionName?: string) => Promise<void>
  deleteSession: (name: string) => Promise<void>
  showSnackbar: (snackbar: Snackbar) => void
  hideSnackbar: () => void
  setConnected: (connected: boolean) => void
  setTerminalStatus: (id: string, status: string) => void
  clearTerminalStatuses: (ids: string[]) => void

  // ── Workflow run journal slice (Issue #504 / U8) ──────────────────────
  // Additive. The Workflows surface's run-list + selected-run playback state.
  // Events are seq-ordered and gaps are the DECLARED holes the API returns
  // (never inferred from numbering). `selectedIndex` is the playback scrub
  // position into `wfEvents`; `followConnected` reflects the live SSE stream.
  workflowRuns: RunSummaryRow[]
  selectedRun: RunInspection | null
  // The run id the CURRENT selection is for, set synchronously the moment
  // `selectWorkflowRun` is called — before its awaits. It is the request-generation
  // token that lets an in-flight fetch tell whether it is still the live selection
  // (PR #526 review round 3). Distinct from `selectedRun?.run_id`, which is null
  // while a fetch is in flight and therefore useless as a guard.
  selectedRunId: string | null
  wfEvents: WorkflowEvent[]
  wfGaps: GapMarker[]
  selectedIndex: number
  followConnected: boolean

  // Resolves to an error message when the list read failed, or null on success.
  // The snackbar still fires here; the returned signal is what lets a caller
  // (WorkflowsPanel) also render an inline error. It never rejects, so the 10s
  // poll cannot produce an unhandled rejection.
  fetchWorkflowRuns: () => Promise<string | null>
  selectWorkflowRun: (runId: string | null) => Promise<void>
  setWorkflowEvents: (events: WorkflowEvent[], gaps: GapMarker[]) => void
  appendWorkflowEvent: (event: WorkflowEvent) => void
  addWorkflowGap: (gap: GapMarker) => void
  setSelectedIndex: (index: number) => void
  setFollowConnected: (connected: boolean) => void
  clearSelectedRun: () => void
}

export const useStore = create<Store>((set, get) => ({
  sessions: [],
  activeSession: null,
  activeSessionDetail: null,
  connected: false,
  snackbar: null,
  terminalStatuses: {},

  workflowRuns: [],
  selectedRun: null,
  selectedRunId: null,
  wfEvents: [],
  wfGaps: [],
  selectedIndex: 0,
  followConnected: false,

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
      get().showSnackbar({
        type: 'error',
        message: e.detail || e.message || 'Failed to delete session; retry after cleanup finishes',
      })
    }
  },

  showSnackbar: (snackbar) => set({ snackbar }),
  hideSnackbar: () => set({ snackbar: null }),
  setConnected: (connected) => set({ connected }),
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

  // ── Workflow run journal actions (Issue #504 / U8) ────────────────────
  fetchWorkflowRuns: async () => {
    try {
      const runs = await api.listWorkflowRuns()
      if (!jsonEqual(get().workflowRuns, runs)) set({ workflowRuns: runs })
      return null
    } catch (e: any) {
      // Surface, do not swallow: an unreachable list endpoint is a real error
      // the RunList renders. Keep any prior list so a transient blip is inert.
      const message = e?.message || 'Failed to load workflow runs'
      get().showSnackbar({ type: 'error', message })
      // Returned (not thrown) so the caller can mirror it into an inline error
      // without every call site needing a .catch to stay rejection-safe.
      return message
    }
  },

  selectWorkflowRun: async (runId) => {
    if (!runId) {
      get().clearSelectedRun()
      return
    }
    // Reset the playback view before loading a new run so stale events/gaps
    // from the previously selected run never bleed into the new timeline.
    // `selectedRunId` is set HERE, synchronously before the awaits below, so it is
    // the request-generation token both branches compare against.
    set({
      selectedRun: null,
      selectedRunId: runId,
      wfEvents: [],
      wfGaps: [],
      selectedIndex: 0,
      followConnected: false,
    })
    try {
      const [inspection, page] = await Promise.all([
        api.inspectWorkflowRun(runId),
        api.getWorkflowRunEvents(runId),
      ])
      // STALE-RESPONSE GUARD (PR #526 review round 3). Selecting run A then quickly
      // run B leaves two fetches in flight; without this, whichever RESOLVES last
      // wins, so A's response can overwrite B's and leave the detail pane and the
      // live-follow keyed to different runs. Bail before touching state.
      if (get().selectedRunId !== runId) return
      set({
        selectedRun: inspection,
        wfEvents: page.events,
        wfGaps: page.gaps,
        // Start scrubbed to the latest event so a freshly opened run shows its
        // most recent state rather than an empty pending fold.
        selectedIndex: Math.max(0, page.events.length - 1),
      })
    } catch (e: any) {
      // The SAME guard on the error path, which is not symmetric decoration: this
      // branch clears `selectedRun` and raises a snackbar, so a FAILED fetch for A
      // arriving after B succeeded would blank B's loaded detail pane and blame it
      // for an error that belongs to a run the user already navigated away from.
      if (get().selectedRunId !== runId) return
      set({ selectedRun: null })
      get().showSnackbar({ type: 'error', message: e?.message || 'Failed to load run' })
    }
  },

  setWorkflowEvents: (events, gaps) => set({ wfEvents: events, wfGaps: gaps }),

  appendWorkflowEvent: (event) =>
    set(state => {
      // Dedupe by seq (a reconnect can replay the boundary event); keep the
      // list seq-ordered. seq is the sole ordering authority (never ts).
      if (state.wfEvents.some(e => e.seq === event.seq)) return state
      // ORDERED INSERT, not append-then-sort. The list is already sorted, so the
      // arrival position is found by scanning from the END — an SSE frame is almost
      // always the newest event, making the common case O(1) instead of the
      // O(n log n) full re-sort this ran on EVERY frame (a live follow re-sorted
      // the whole timeline hundreds of times). Out-of-order arrival still lands
      // correctly: the scan walks back to the right slot.
      const events = [...state.wfEvents]
      let i = events.length
      while (i > 0 && events[i - 1].seq > event.seq) i--
      events.splice(i, 0, event)
      return { wfEvents: events }
    }),

  addWorkflowGap: (gap) =>
    set(state => {
      // A declared gap the API sent — render it, never infer from numbering.
      // Dedupe on the (after_seq, before_seq) span.
      if (state.wfGaps.some(g => g.after_seq === gap.after_seq && g.before_seq === gap.before_seq)) {
        return state
      }
      return { wfGaps: [...state.wfGaps, gap] }
    }),

  setSelectedIndex: (index) =>
    set(state => {
      const max = Math.max(0, state.wfEvents.length - 1)
      const clamped = Math.min(Math.max(0, index), max)
      if (clamped === state.selectedIndex) return state
      return { selectedIndex: clamped }
    }),

  setFollowConnected: (connected) =>
    set(state => (state.followConnected === connected ? state : { followConnected: connected })),

  clearSelectedRun: () =>
    set({
      selectedRun: null,
      // Cleared too, so an in-flight fetch for the run being closed cannot resolve
      // afterwards and re-populate a pane the user has dismissed.
      selectedRunId: null,
      wfEvents: [],
      wfGaps: [],
      selectedIndex: 0,
      followConnected: false,
    }),
}))
