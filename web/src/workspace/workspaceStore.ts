// State for the Workspace view: every session's agents with live status, the
// attention flags derived from status transitions, and a split layout per
// session. Separate from the main store so the Workspace's faster poll and
// per-terminal bookkeeping do not touch the other panels.
import { create } from 'zustand'
import { api, ApiError, SessionDetail } from '../api'
import { Attention, nextAttention, normalizeStatus } from './attention'
import {
  EMPTY_LAYOUT,
  Layout,
  PaneCount,
  closePane,
  focusPane,
  openInFocused,
  openInSplit,
  retainTerminals,
  setPaneCount,
  visibleTerminals,
} from './layout'

export const WORKSPACE_POLL_MS = 3000

/** Terminal font sizes the a/A buttons step through. */
export const FONT_SIZES = [9, 10, 11, 12, 13, 14, 16, 18] as const
export const DEFAULT_WORKSPACE_FONT_SIZE = 14
const FONT_SIZE_KEY = 'cao.workspace.fontSize'

function loadFontSize(): number {
  try {
    const n = Number(localStorage.getItem(FONT_SIZE_KEY))
    return (FONT_SIZES as readonly number[]).includes(n) ? n : DEFAULT_WORKSPACE_FONT_SIZE
  } catch {
    return DEFAULT_WORKSPACE_FONT_SIZE
  }
}

/** Next size up (+1) or down (-1) from `current`, clamped to FONT_SIZES. */
export function stepFontSize(current: number, dir: 1 | -1): number {
  const sizes = FONT_SIZES as readonly number[]
  const i = sizes.indexOf(current)
  if (i < 0) return DEFAULT_WORKSPACE_FONT_SIZE
  return sizes[Math.min(Math.max(i + dir, 0), sizes.length - 1)]
}

interface WorkspaceState {
  details: Record<string, SessionDetail>
  /** Terminal id -> normalized (UPPERCASE) status. */
  statuses: Record<string, string>
  attention: Record<string, Attention>
  selectedSession: string | null
  layouts: Record<string, Layout>
  /** Terminal font size in px, shared by every pane; persisted per browser. */
  fontSize: number

  /** Refresh every listed session's agents. Sessions not listed are dropped. */
  poll: (sessionNames: string[]) => Promise<void>
  selectSession: (name: string | null) => void
  open: (sessionName: string, terminalId: string, mode?: 'focused' | 'split') => void
  closePane: (index: number) => void
  focusPane: (index: number) => void
  setPaneCount: (count: PaneCount) => void
  /** Clear flags for whatever is on screen now (call when the page becomes visible). */
  markVisibleViewed: () => void
  stepFontSize: (dir: 1 | -1) => void
  reset: () => void
}

function pageVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden'
}

/** Terminals the operator is looking at right now. */
export function viewedTerminals(state: Pick<WorkspaceState, 'selectedSession' | 'layouts'>): Set<string> {
  if (!pageVisible() || !state.selectedSession) return new Set()
  const layout = state.layouts[state.selectedSession]
  return new Set(layout ? visibleTerminals(layout) : [])
}

function withoutFlags(attention: Record<string, Attention>, ids: Iterable<string>) {
  let next = attention
  for (const id of ids) {
    if (next[id]) {
      if (next === attention) next = { ...attention }
      delete next[id]
    }
  }
  return next
}

let polling = false

const initial = {
  details: {},
  statuses: {},
  attention: {},
  selectedSession: null,
  layouts: {},
}

export const useWorkspace = create<WorkspaceState>((set, get) => {
  // Apply a layout change to the selected session and clear flags on anything
  // it brings on screen.
  const updateLayout = (fn: (l: Layout) => Layout) => {
    const { selectedSession, layouts } = get()
    if (!selectedSession) return
    const layout = fn(layouts[selectedSession] ?? EMPTY_LAYOUT)
    const nextLayouts = { ...layouts, [selectedSession]: layout }
    set({
      layouts: nextLayouts,
      attention: withoutFlags(get().attention, viewedTerminals({ selectedSession, layouts: nextLayouts })),
    })
  }

  return {
    ...initial,

    poll: async (sessionNames) => {
      if (polling) return
      polling = true
      try {
        const results = await Promise.all(
          sessionNames.map(name =>
            api.getSession(name).then(
              detail => ({ name, detail, gone: false }),
              (e: ApiError) => ({ name, detail: null, gone: e?.status === 404 }),
            ),
          ),
        )
        const prev = get()
        const details: Record<string, SessionDetail> = {}
        for (const r of results) {
          if (r.detail) details[r.name] = r.detail
          // A transient failure keeps the last good view instead of blanking it.
          else if (!r.gone && prev.details[r.name]) details[r.name] = prev.details[r.name]
        }

        const viewed = viewedTerminals(prev)
        const statuses: Record<string, string> = {}
        const attention: Record<string, Attention> = {}
        const live = new Set<string>()
        for (const detail of Object.values(details)) {
          for (const t of detail.terminals) {
            live.add(t.id)
            const status = normalizeStatus(t.status)
            statuses[t.id] = status
            const flag = nextAttention(prev.statuses[t.id], status, prev.attention[t.id], viewed.has(t.id))
            if (flag) attention[t.id] = flag
          }
        }

        const layouts: Record<string, Layout> = {}
        for (const [name, layout] of Object.entries(prev.layouts)) {
          if (details[name]) layouts[name] = retainTerminals(layout, live)
        }
        const selectedSession = prev.selectedSession && details[prev.selectedSession] ? prev.selectedSession : null

        set({ details, statuses, attention, layouts, selectedSession })
      } finally {
        polling = false
      }
    },

    selectSession: (name) => {
      const { layouts, attention } = get()
      set({
        selectedSession: name,
        attention: withoutFlags(attention, viewedTerminals({ selectedSession: name, layouts })),
      })
    },

    open: (sessionName, terminalId, mode = 'focused') => {
      if (get().selectedSession !== sessionName) set({ selectedSession: sessionName })
      updateLayout(l => (mode === 'split' ? openInSplit(l, terminalId) : openInFocused(l, terminalId)))
    },

    closePane: (index) => updateLayout(l => closePane(l, index)),
    focusPane: (index) => updateLayout(l => focusPane(l, index)),
    setPaneCount: (count) => updateLayout(l => setPaneCount(l, count)),

    markVisibleViewed: () => set({ attention: withoutFlags(get().attention, viewedTerminals(get())) }),

    stepFontSize: (dir) => {
      const fontSize = stepFontSize(get().fontSize, dir)
      if (fontSize === get().fontSize) return
      try { localStorage.setItem(FONT_SIZE_KEY, String(fontSize)) } catch { /* private mode: session-only */ }
      set({ fontSize })
    },

    reset: () => set({ ...initial }),
    fontSize: loadFontSize(),
  }
})
