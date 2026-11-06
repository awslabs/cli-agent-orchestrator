// Workspace: sessions in a left sidebar, the selected session's agents as tabs,
// and live terminals in 1/2/4 split panes. Agents that finish, error, or wait
// for an answer while off screen are flagged until viewed.
//
// Read/observe surface only: closing a tab or pane never exits or deletes an
// agent. Destructive actions stay in the Agents tab, behind its confirmations.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Bell, Bot, CircleAlert, Columns2, Grid2x2, Search, Square, SquareTerminal, X } from 'lucide-react'
import { TerminalMeta } from '../api'
import { useStore } from '../store'
import { StatusBadge } from './StatusBadge'
import { STATUS_CONFIG, UNKNOWN_CONFIG } from '../status.generated'
import { createXtermSession } from './terminal/xtermSession'
import { TerminalPool } from '../workspace/terminalPool'
import { ATTENTION_LABEL, Attention, strongest, summaryStatus } from '../workspace/attention'
import { EMPTY_LAYOUT, PaneCount } from '../workspace/layout'
import { FONT_SIZES, WORKSPACE_POLL_MS, useWorkspace } from '../workspace/workspaceStore'

export function agentLabel(t: Pick<TerminalMeta, 'agent_profile' | 'provider'>): string {
  return t.agent_profile || t.provider
}

function StatusDot({ status, className = '' }: { status: string | null | undefined; className?: string }) {
  const config = (status && STATUS_CONFIG[status.toUpperCase()]) || UNKNOWN_CONFIG
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${config.dotClass} ${config.pulse ? 'animate-pulse' : ''} ${className}`}
      title={config.label}
    />
  )
}

const ATTENTION_STYLE: Record<Attention, string> = {
  waiting: 'bg-cao-warning/20 text-cao-warning',
  error: 'bg-cao-danger/20 text-cao-danger',
  done: 'bg-cao-accent/20 text-cao-accent',
}
const ATTENTION_SHORT: Record<Attention, string> = { waiting: 'input', error: 'error', done: 'new' }

function AttentionBadge({ flag, count }: { flag: Attention; count?: number }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-px rounded-full text-[10px] font-semibold ${ATTENTION_STYLE[flag]}`}
      title={ATTENTION_LABEL[flag]}
      data-attention={flag}
    >
      {flag === 'waiting' ? <Bell size={10} /> : flag === 'error' ? <CircleAlert size={10} /> : null}
      {count && count > 1 ? count : ATTENTION_SHORT[flag]}
    </span>
  )
}

/** True when keyboard focus is inside an xterm (its hidden textarea). */
function focusInTerminal(): boolean {
  const el = document.activeElement
  return !!(el && el.closest('.xterm'))
}

// ── Panes ────────────────────────────────────────────────────────────────

function Pane({
  pool,
  index,
  terminal,
  status,
  focused,
  highlight,
  canClose,
  onFocus,
  onClose,
}: {
  pool: TerminalPool
  index: number
  terminal: TerminalMeta | null
  status: string | undefined
  /** Owns keyboard focus. */
  focused: boolean
  /** Draw the focus border (only meaningful with more than one pane). */
  highlight: boolean
  canClose: boolean
  onFocus: () => void
  onClose: () => void
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const id = terminal?.id ?? null

  useEffect(() => {
    const el = hostRef.current
    if (!el || !id) return
    const session = pool.attach(id, el)
    const raf = requestAnimationFrame(() => session.fit())
    return () => {
      cancelAnimationFrame(raf)
      pool.detach(id, el)
    }
  }, [pool, id])

  // Keyboard focus follows the focused pane.
  useEffect(() => {
    if (!focused || !id || !pool.has(id) || !hostRef.current) return
    pool.attach(id, hostRef.current).focus()
  }, [focused, id, pool])

  return (
    <div
      className={`flex flex-col min-w-0 min-h-0 rounded-md overflow-hidden border ${
        highlight ? 'border-emerald-500/70' : 'border-gray-800'
      }`}
      onMouseDown={onFocus}
      data-testid={`pane-${index}`}
    >
      <div className="flex items-center justify-between gap-2 px-3 py-1.5 bg-gray-900 border-b border-gray-800 shrink-0">
        {terminal ? (
          <div className="flex items-center gap-2 min-w-0">
            <SquareTerminal size={14} className="text-emerald-400 shrink-0" />
            <span className="text-xs text-gray-200 truncate">{agentLabel(terminal)}</span>
            <span className="text-[10px] font-mono text-gray-500 truncate">{terminal.id}</span>
            <StatusBadge status={status ?? null} />
          </div>
        ) : (
          <span className="text-xs text-gray-500">Empty pane</span>
        )}
        {canClose && (
          <button
            onClick={e => { e.stopPropagation(); onClose() }}
            className="p-0.5 text-gray-500 hover:text-white rounded-sm"
            title="Close pane (the agent keeps running)"
            aria-label={`Close pane ${index + 1}`}
          >
            <X size={14} />
          </button>
        )}
      </div>
      <div className="relative flex-1 min-h-0" style={{ background: '#0d1117' }}>
        <div ref={hostRef} className="absolute inset-0" />
        {!terminal && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-gray-600 p-4 text-center">
            Pick an agent tab to show it here. Shift+click a tab to open it in another pane.
          </div>
        )}
      </div>
    </div>
  )
}

// ── Quick switcher ──────────────────────────────────────────────────────

interface SwitcherItem {
  session: string
  terminal: TerminalMeta
  status: string | undefined
  flag: Attention | undefined
}

function QuickSwitcher({ items, onPick, onClose }: {
  items: SwitcherItem[]
  onPick: (item: SwitcherItem, split: boolean) => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const match = q
      ? items.filter(i =>
          `${i.session} ${agentLabel(i.terminal)} ${i.terminal.id} ${i.terminal.description ?? ''}`.toLowerCase().includes(q),
        )
      : items
    // Flagged agents first: the switcher doubles as the "what needs me" list.
    return [...match].sort((a, b) => Number(!!b.flag) - Number(!!a.flag))
  }, [items, query])

  useEffect(() => { inputRef.current?.focus() }, [])
  useEffect(() => { setIndex(0) }, [query])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setIndex(i => Math.min(i + 1, filtered.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setIndex(i => Math.max(i - 1, 0)) }
    else if (e.key === 'Enter' && filtered[index]) { e.preventDefault(); onPick(filtered[index], e.shiftKey) }
    else if (e.key === 'Escape') { e.preventDefault(); onClose() }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-24 bg-black/50" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label="Jump to agent"
        className="w-full max-w-xl bg-gray-900 border border-gray-700 rounded-xl shadow-2xl overflow-hidden"
        onMouseDown={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-800">
          <Search size={16} className="text-gray-500" />
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Jump to an agent: session, profile, or id"
            className="flex-1 bg-transparent text-sm text-gray-200 focus:outline-hidden"
            aria-label="Search agents"
          />
          <span className="text-[10px] text-gray-600">Enter open · Shift+Enter split · Esc close</span>
        </div>
        <ul className="max-h-80 overflow-y-auto py-1" role="listbox">
          {filtered.length === 0 && <li className="px-4 py-3 text-sm text-gray-500">No matching agents</li>}
          {filtered.map((item, i) => (
            <li
              key={item.terminal.id}
              role="option"
              aria-selected={i === index}
              className={`flex items-center gap-3 px-4 py-2 cursor-pointer text-sm ${i === index ? 'bg-emerald-900/30' : 'hover:bg-gray-800/60'}`}
              onMouseEnter={() => setIndex(i)}
              onClick={e => onPick(item, e.shiftKey)}
            >
              <StatusDot status={item.status} />
              <span className="text-gray-200">{agentLabel(item.terminal)}</span>
              <span className="text-xs font-mono text-gray-500">{item.terminal.id}</span>
              <span className="ml-auto text-xs text-gray-500 font-mono">{item.session}</span>
              {item.flag && <AttentionBadge flag={item.flag} />}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

// ── Panel ────────────────────────────────────────────────────────────────

const PANE_GRID: Record<PaneCount, string> = {
  1: 'grid-cols-1 grid-rows-1',
  2: 'grid-cols-2 grid-rows-1',
  4: 'grid-cols-2 grid-rows-2',
}

export function WorkspacePanel() {
  const sessions = useStore(s => s.sessions)
  const fetchSessions = useStore(s => s.fetchSessions)
  const ws = useWorkspace()
  const [filter, setFilter] = useState('')
  const [switcherOpen, setSwitcherOpen] = useState(false)

  const poolRef = useRef<TerminalPool | null>(null)
  if (!poolRef.current) poolRef.current = new TerminalPool(createXtermSession)
  const pool = poolRef.current
  useEffect(() => () => pool.disposeAll(), [pool])
  useEffect(() => pool.setFontSize(ws.fontSize), [pool, ws.fontSize])

  // Poll: refresh the session list, then every session's agents and status.
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      await fetchSessions()
      if (cancelled) return
      await useWorkspace.getState().poll(useStore.getState().sessions.map(s => s.id))
    }
    tick()
    const interval = setInterval(tick, WORKSPACE_POLL_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [fetchSessions])

  // Dispose live terminals whose agent is gone.
  useEffect(() => {
    const live = new Set<string>()
    for (const d of Object.values(ws.details)) for (const t of d.terminals) live.add(t.id)
    pool.retain(live)
  }, [ws.details, pool])

  // Coming back to the browser tab counts as viewing what is on screen.
  useEffect(() => {
    const onVisible = () => { if (document.visibilityState === 'visible') useWorkspace.getState().markVisibleViewed() }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  // Default selection: the first session; a fresh session opens its first
  // (oldest, normally the conductor) agent.
  const selected = ws.selectedSession
  const detail = selected ? ws.details[selected] : undefined
  useEffect(() => {
    if (!selected && sessions.length > 0 && ws.details[sessions[0].id]) ws.selectSession(sessions[0].id)
  }, [selected, sessions, ws.details])
  useEffect(() => {
    if (selected && detail && !ws.layouts[selected] && detail.terminals.length > 0) {
      ws.open(selected, detail.terminals[0].id)
    }
  }, [selected, detail, ws.layouts])

  // Flag count in the browser tab title, so a backgrounded tab still says
  // something needs you.
  const flagCount = Object.keys(ws.attention).length
  useEffect(() => {
    const base = document.title.replace(/^\(\d+\)\s*/, '')
    document.title = flagCount > 0 ? `(${flagCount}) ${base}` : base
    return () => { document.title = document.title.replace(/^\(\d+\)\s*/, '') }
  }, [flagCount])

  // Alt+K anywhere, or Ctrl/Cmd+K outside a terminal (where Ctrl+K is the
  // shell's kill-line), opens the switcher. Capture phase so xterm does not
  // swallow Alt+K first.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase()
      const altK = e.altKey && !e.ctrlKey && !e.metaKey && (key === 'k' || e.code === 'KeyK')
      const modK = (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && key === 'k' && !focusInTerminal()
      if (altK || modK) {
        e.preventDefault()
        e.stopPropagation()
        setSwitcherOpen(o => !o)
      }
    }
    window.addEventListener('keydown', handler, true)
    return () => window.removeEventListener('keydown', handler, true)
  }, [])

  const layout = (selected && ws.layouts[selected]) || EMPTY_LAYOUT
  const terminals = detail?.terminals ?? []
  const byId = new Map(terminals.map(t => [t.id, t]))
  const focusedId = layout.panes[layout.focused]

  const switcherItems: SwitcherItem[] = sessions.flatMap(s =>
    (ws.details[s.id]?.terminals ?? []).map(t => ({
      session: s.id,
      terminal: t,
      status: ws.statuses[t.id],
      flag: ws.attention[t.id],
    })),
  )

  const visibleSessions = sessions.filter(s => !filter || s.id.includes(filter) || s.name.includes(filter))

  return (
    <div className="flex h-full min-h-0 gap-3">
      {/* Sessions sidebar */}
      <aside className="w-64 shrink-0 flex flex-col min-h-0 bg-gray-900/60 border border-gray-800 rounded-lg" aria-label="Sessions">
        <div className="px-3 pt-3 pb-2 border-b border-gray-800">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Sessions ({sessions.length})</h3>
            <button
              onClick={() => setSwitcherOpen(true)}
              className="text-[10px] text-gray-500 hover:text-gray-200 border border-gray-700 rounded-sm px-1.5 py-0.5"
              title="Jump to an agent (Alt+K, or Ctrl+K outside a terminal)"
            >
              Jump · Alt+K
            </button>
          </div>
          {sessions.length > 5 && (
            <input
              value={filter}
              onChange={e => setFilter(e.target.value)}
              placeholder="Filter sessions..."
              aria-label="Filter sessions"
              className="w-full bg-gray-950 border border-gray-700 text-gray-200 text-xs rounded-md px-2 py-1 focus:border-emerald-500 focus:outline-hidden"
            />
          )}
        </div>
        <ul className="flex-1 overflow-y-auto p-2 space-y-1">
          {sessions.length === 0 && (
            <li className="text-xs text-gray-500 p-2">No sessions. Spawn an agent from the Agents tab.</li>
          )}
          {visibleSessions.map(s => {
            const ts = ws.details[s.id]?.terminals ?? []
            const flags = ts.map(t => ws.attention[t.id]).filter(Boolean) as Attention[]
            const flag = strongest(flags)
            return (
              <li key={s.id}>
                <button
                  onClick={() => ws.selectSession(s.id)}
                  aria-current={selected === s.id}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left ${
                    selected === s.id ? 'bg-emerald-900/30 border border-emerald-700/50' : 'border border-transparent hover:bg-gray-800/70'
                  }`}
                >
                  <StatusDot status={summaryStatus(ts.map(t => ws.statuses[t.id]))} />
                  <span className={`text-xs font-mono truncate flex-1 ${flag ? 'text-white font-semibold' : 'text-gray-300'}`}>{s.id}</span>
                  {flag && <AttentionBadge flag={flag} count={flags.length} />}
                  <span className="text-[10px] text-gray-500">{ws.details[s.id] ? ts.length : '…'}</span>
                </button>
              </li>
            )
          })}
        </ul>
      </aside>

      {/* Agents + panes */}
      <section className="flex-1 min-w-0 flex flex-col min-h-0">
        {!selected || !detail ? (
          <div className="flex-1 flex items-center justify-center text-sm text-gray-500 border border-gray-800 rounded-lg">
            {sessions.length === 0
              ? 'No active sessions.'
              : Object.keys(ws.details).length === 0 ? 'Loading agents…' : 'Select a session.'}
          </div>
        ) : (
          <>
            <div className="flex items-end gap-2 border-b border-gray-800 shrink-0">
              <div role="tablist" aria-label={`Agents in ${selected}`} className="flex gap-1 overflow-x-auto flex-1 min-w-0">
                {terminals.map(t => {
                  const flag = ws.attention[t.id]
                  const shown = layout.panes.includes(t.id)
                  return (
                    <button
                      key={t.id}
                      role="tab"
                      aria-selected={focusedId === t.id}
                      onClick={e => ws.open(selected, t.id, e.shiftKey ? 'split' : 'focused')}
                      title={`${t.description ? `${t.description}\n` : ''}${t.id} · ${t.provider}\nClick: show in focused pane · Shift+click: open in another pane`}
                      className={`flex items-center gap-2 px-3 py-1.5 text-xs rounded-t-md border-b-2 whitespace-nowrap ${
                        focusedId === t.id
                          ? 'border-emerald-500 bg-gray-800/80 text-white'
                          : shown
                            ? 'border-emerald-800 text-gray-200 hover:bg-gray-800/50'
                            : 'border-transparent text-gray-400 hover:text-white hover:bg-gray-800/50'
                      }`}
                    >
                      <StatusDot status={ws.statuses[t.id]} />
                      <Bot size={12} className="shrink-0" />
                      <span className={flag ? 'font-semibold text-white' : ''}>{agentLabel(t)}</span>
                      {flag && <AttentionBadge flag={flag} />}
                    </button>
                  )
                })}
              </div>
              <div className="flex items-center gap-1 pb-1 shrink-0" role="group" aria-label="Pane layout">
                {([1, 2, 4] as PaneCount[]).map(n => {
                  const Icon = n === 1 ? Square : n === 2 ? Columns2 : Grid2x2
                  return (
                    <button
                      key={n}
                      onClick={() => ws.setPaneCount(n)}
                      aria-pressed={layout.count === n}
                      title={`${n} pane${n > 1 ? 's' : ''}`}
                      className={`p-1.5 rounded-sm ${layout.count === n ? 'bg-gray-700 text-white' : 'text-gray-500 hover:text-white'}`}
                    >
                      <Icon size={14} />
                    </button>
                  )
                })}
                <span className="w-px h-4 bg-gray-700 mx-1" aria-hidden="true" />
                <div role="group" aria-label="Terminal font size" className="flex items-center gap-1">
                  <button
                    onClick={() => ws.stepFontSize(-1)}
                    disabled={ws.fontSize <= FONT_SIZES[0]}
                    title={`Smaller terminal text (now ${ws.fontSize}px)`}
                    aria-label="Smaller terminal text"
                    className="w-6 h-6 flex items-center justify-center rounded-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:hover:text-gray-400"
                  >
                    <span className="text-[11px] font-semibold leading-none">a</span>
                  </button>
                  <button
                    onClick={() => ws.stepFontSize(1)}
                    disabled={ws.fontSize >= FONT_SIZES[FONT_SIZES.length - 1]}
                    title={`Larger terminal text (now ${ws.fontSize}px)`}
                    aria-label="Larger terminal text"
                    className="w-6 h-6 flex items-center justify-center rounded-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:hover:text-gray-400"
                  >
                    <span className="text-base font-semibold leading-none">A</span>
                  </button>
                </div>
              </div>
            </div>
            <div className={`flex-1 min-h-0 grid gap-2 pt-2 ${PANE_GRID[layout.count]}`}>
              {layout.panes.map((id, i) => (
                <Pane
                  key={i}
                  pool={pool}
                  index={i}
                  terminal={id ? byId.get(id) ?? null : null}
                  status={id ? ws.statuses[id] : undefined}
                  focused={i === layout.focused}
                  highlight={layout.count > 1 && i === layout.focused}
                  canClose={!!id}
                  onFocus={() => ws.focusPane(i)}
                  onClose={() => ws.closePane(i)}
                />
              ))}
            </div>
          </>
        )}
      </section>

      {switcherOpen && (
        <QuickSwitcher
          items={switcherItems}
          onClose={() => setSwitcherOpen(false)}
          onPick={(item, split) => {
            setSwitcherOpen(false)
            useWorkspace.getState().open(item.session, item.terminal.id, split ? 'split' : 'focused')
          }}
        />
      )}
    </div>
  )
}
