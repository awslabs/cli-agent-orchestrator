// Split-pane layout for one session: 1, 2 (side by side) or 4 (2x2) panes,
// each showing one terminal or empty, plus which pane has focus.
//
// Pure functions so the rules are unit-testable without a DOM.

export type PaneCount = 1 | 2 | 4

export interface Layout {
  count: PaneCount
  /** Length is always `count`. */
  panes: (string | null)[]
  focused: number
}

export const EMPTY_LAYOUT: Layout = { count: 1, panes: [null], focused: 0 }

function resize(panes: (string | null)[], count: PaneCount): (string | null)[] {
  const out = panes.slice(0, count)
  while (out.length < count) out.push(null)
  return out
}

/**
 * Change the pane count. Shrinking keeps the visible terminals that fit,
 * preferring the focused one, so going from 4 panes to 1 keeps what you were
 * working in.
 */
export function setPaneCount(layout: Layout, count: PaneCount): Layout {
  if (count === layout.count) return layout
  if (count > layout.count) {
    return { count, panes: resize(layout.panes, count), focused: layout.focused }
  }
  const focusedId = layout.panes[layout.focused]
  const others = layout.panes.filter((id, i) => id && i !== layout.focused)
  const keep = (focusedId ? [focusedId, ...others] : others).slice(0, count)
  return { count, panes: resize(keep, count), focused: 0 }
}

/** Show a terminal in the focused pane. Already visible elsewhere: focus that pane. */
export function openInFocused(layout: Layout, id: string): Layout {
  const at = layout.panes.indexOf(id)
  if (at >= 0) return at === layout.focused ? layout : { ...layout, focused: at }
  const panes = [...layout.panes]
  panes[layout.focused] = id
  return { ...layout, panes }
}

/**
 * Show a terminal in another pane: the first empty one, else grow the layout,
 * else (already 4 panes) replace the pane after the focused one.
 */
export function openInSplit(layout: Layout, id: string): Layout {
  const at = layout.panes.indexOf(id)
  if (at >= 0) return { ...layout, focused: at }
  let next = layout
  let target = next.panes.indexOf(null)
  if (target < 0 && next.count < 4) {
    next = setPaneCount(next, next.count === 1 ? 2 : 4)
    target = next.panes.indexOf(null)
  }
  if (target < 0) target = (next.focused + 1) % next.count
  const panes = [...next.panes]
  panes[target] = id
  return { ...next, panes, focused: target }
}

/** Empty a pane. Closing the only content of a split collapses it a level. */
export function closePane(layout: Layout, index: number): Layout {
  const panes = [...layout.panes]
  panes[index] = null
  const filled = panes.filter(Boolean).length
  const collapsed: PaneCount = filled <= 1 ? 1 : filled <= 2 ? 2 : layout.count
  if (collapsed < layout.count) {
    const kept = panes.filter(Boolean) as string[]
    return { count: collapsed, panes: resize(kept, collapsed), focused: 0 }
  }
  return { ...layout, panes, focused: index }
}

export function focusPane(layout: Layout, index: number): Layout {
  return index === layout.focused || index < 0 || index >= layout.count ? layout : { ...layout, focused: index }
}

/** Clear panes whose terminal has gone away. */
export function retainTerminals(layout: Layout, live: Set<string>): Layout {
  if (layout.panes.every(id => id === null || live.has(id))) return layout
  return { ...layout, panes: layout.panes.map(id => (id && live.has(id) ? id : null)) }
}

export function visibleTerminals(layout: Layout): string[] {
  return layout.panes.filter((id): id is string => !!id)
}
