import { useState, useEffect, useRef, useMemo } from 'react'
import { useStore } from '../store'
import { api, TerminalMeta } from '../api'
import { Bot, Zap, Package, Monitor, Terminal as TermIcon, Trash2, Mail, FileText, LogOut, Send, ChevronRight, ChevronDown, Users, Filter, ArrowDownUp } from 'lucide-react'
import { TerminalView } from './TerminalView'
import { ConfirmModal } from './ConfirmModal'
import { InboxPanel } from './InboxPanel'
import { StatusBadge } from './StatusBadge'
import { OutputViewer } from './OutputViewer'

const STATUS_ORDER = ['PROCESSING', 'IDLE', 'WAITING_USER_ANSWER', 'ERROR', 'COMPLETED', 'UNKNOWN']

function fmtRel(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return null
  const diff = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (diff < 60) return 'just now'
  const m = Math.floor(diff / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  const rm = m % 60
  if (h < 24) return rm ? `${h}h ${rm}m ago` : `${h}h ago`
  const days = Math.floor(h / 24)
  const rh = h % 24
  return rh ? `${days}d ${rh}h ago` : `${days}d ago`
}

function fmtAbs(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return null
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const STATUS_META: Record<string, { label: string; dot: string; text: string; pulse?: boolean }> = {
  PROCESSING: { label: 'Processing', dot: 'bg-amber-400', text: 'text-amber-400', pulse: true },
  IDLE: { label: 'Idle', dot: 'bg-emerald-400', text: 'text-emerald-400' },
  WAITING_USER_ANSWER: { label: 'Waiting', dot: 'bg-purple-400', text: 'text-purple-400' },
  ERROR: { label: 'Error', dot: 'bg-red-400', text: 'text-red-400' },
  COMPLETED: { label: 'Completed', dot: 'bg-blue-400', text: 'text-blue-400' },
  UNKNOWN: { label: 'Unknown', dot: 'bg-gray-500', text: 'text-gray-500' },
}

const STATUS_ACTIVE_BG: Record<string, string> = {
  PROCESSING: 'bg-amber-900/40 border-amber-500/50 text-amber-300',
  IDLE: 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300',
  WAITING_USER_ANSWER: 'bg-purple-900/40 border-purple-500/50 text-purple-300',
  ERROR: 'bg-red-900/40 border-red-500/50 text-red-300',
  COMPLETED: 'bg-blue-900/40 border-blue-500/50 text-blue-300',
  UNKNOWN: 'bg-gray-800/40 border-gray-500/50 text-gray-300',
}

function StatusSummary({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      {STATUS_ORDER.filter(s => counts[s] > 0).map(s => {
        const meta = STATUS_META[s]
        return (
          <span key={s} className="flex items-center gap-1 text-xs">
            <span className={`w-1.5 h-1.5 rounded-full ${meta.dot} ${meta.pulse ? 'animate-pulse' : ''}`} />
            <span className={meta.text}>{counts[s]}</span>
            <span className="text-gray-500">{meta.label}</span>
          </span>
        )
      })}
    </div>
  )
}

interface SessionWithTerminals {
  name: string
  status: string
  terminals: TerminalMeta[]
}

export function DashboardHome({ onNavigate }: { onNavigate: (tab: string) => void }) {
  const { sessions, terminalStatuses, setTerminalStatus, clearTerminalStatuses, showSnackbar, deleteSession } = useStore()
  const [profileCount, setProfileCount] = useState(0)
  const [sessionData, setSessionData] = useState<SessionWithTerminals[]>([])
  const [expandedSessions, setExpandedSessions] = useState<Set<string>>(new Set())
  const [liveTerminal, setLiveTerminal] = useState<{ id: string; provider?: string; agentProfile?: string | null } | null>(null)
  const [pendingClose, setPendingClose] = useState<TerminalMeta | null>(null)
  const [closingTerminal, setClosingTerminal] = useState<string | null>(null)
  const [inboxTerminalId, setInboxTerminalId] = useState<string | null>(null)
  const [outputTerminalId, setOutputTerminalId] = useState<string | null>(null)
  const [pendingExit, setPendingExit] = useState<TerminalMeta | null>(null)
  const [exitingTerminal, setExitingTerminal] = useState<string | null>(null)
  const [sendInputOpen, setSendInputOpen] = useState<Record<string, boolean>>({})
  const [sendInputValues, setSendInputValues] = useState<Record<string, string>>({})
  const [sendingInput, setSendingInput] = useState<string | null>(null)
  const [agentTypeFilter, setAgentTypeFilter] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string | null>(null)
  const [sortOrder, setSortOrder] = useState<'desc' | 'asc'>('desc')
  const [pendingDeleteSession, setPendingDeleteSession] = useState<string | null>(null)
  const [deletingSession, setDeletingSession] = useState(false)
  const seenSessionsRef = useRef<Set<string>>(new Set())

  const totalTerminals = sessionData.reduce((sum, s) => sum + s.terminals.length, 0)

  const allAgentTypes = useMemo(() => {
    const types = new Set<string>()
    sessionData.forEach(s => s.terminals.forEach(t => { if (t.agent_profile) types.add(t.agent_profile) }))
    return [...types].sort()
  }, [sessionData])

  const filteredSessions = useMemo(() => {
    const filtered = sessionData.filter(s =>
      s.terminals.some(t => {
        const matchAgent = !agentTypeFilter || t.agent_profile === agentTypeFilter
        const matchStatus = !statusFilter || (terminalStatuses[t.id] || 'UNKNOWN') === statusFilter
        return matchAgent && matchStatus
      })
    )
    return filtered.sort((a, b) => {
      const latestA = Math.max(...a.terminals.map(t => t.last_active ? new Date(t.last_active).getTime() : 0))
      const latestB = Math.max(...b.terminals.map(t => t.last_active ? new Date(t.last_active).getTime() : 0))
      return sortOrder === 'desc' ? latestB - latestA : latestA - latestB
    })
  }, [sessionData, agentTypeFilter, statusFilter, sortOrder, terminalStatuses])

  const getStatusCounts = (terminals: TerminalMeta[]) => {
    const counts: Record<string, number> = {}
    terminals.forEach(t => {
      const s = terminalStatuses[t.id] || 'UNKNOWN'
      counts[s] = (counts[s] || 0) + 1
    })
    return counts
  }

  // Fetch session details with terminals
  useEffect(() => {
    const fetchAll = async () => {
      try {
        const sessionDetails = await Promise.all(
          sessions.map(async s => {
            try {
              const detail = await api.getSession(s.name)
              return { name: s.name, status: s.status, terminals: detail.terminals || [] }
            } catch {
              return { name: s.name, status: s.status, terminals: [] }
            }
          })
        )
        setSessionData(prev => sessionDetails.length === 0 && prev.length > 0 ? prev : sessionDetails)
        // Auto-expand only newly seen sessions
        const newNames = sessionDetails.map(s => s.name).filter(n => !seenSessionsRef.current.has(n))
        newNames.forEach(n => seenSessionsRef.current.add(n))
        if (newNames.length > 0) {
          setExpandedSessions(prev => {
            const next = new Set(prev)
            newNames.forEach(n => next.add(n))
            return next
          })
        }
      } catch {}
    }
    fetchAll()
    const interval = setInterval(fetchAll, 5000)
    return () => clearInterval(interval)
  }, [sessions.map(s => s.id).join(',')])

  // Poll statuses
  useEffect(() => {
    const allIds = sessionData.flatMap(s => s.terminals.map(t => t.id))
    if (!allIds.length) return
    clearTerminalStatuses(allIds)
    const fetch = () => {
      allIds.forEach(id => {
        api.getTerminalStatus(id)
          .then(status => { if (status) setTerminalStatus(id, status) })
          .catch(() => {})
      })
    }
    fetch()
    const interval = setInterval(fetch, 3000)
    return () => clearInterval(interval)
  }, [sessionData.flatMap(s => s.terminals.map(t => t.id)).join(',')])

  useEffect(() => {
    api.listProfiles().then(p => setProfileCount(p.length)).catch(() => {})
  }, [])

  const handleDeleteTerminal = async () => {
    if (!pendingClose) return
    setClosingTerminal(pendingClose.id)
    try {
      await api.deleteTerminal(pendingClose.id)
      if (liveTerminal?.id === pendingClose.id) setLiveTerminal(null)
      showSnackbar({ type: 'success', message: `Terminal ${pendingClose.id} closed` })
    } catch {
      showSnackbar({ type: 'error', message: `Failed to close terminal` })
    }
    setClosingTerminal(null)
    setPendingClose(null)
  }

  const handleExitTerminal = async () => {
    if (!pendingExit) return
    setExitingTerminal(pendingExit.id)
    try {
      await api.exitTerminal(pendingExit.id)
      showSnackbar({ type: 'success', message: `Graceful exit sent` })
    } catch {
      showSnackbar({ type: 'error', message: `Failed to send exit` })
    }
    setExitingTerminal(null)
    setPendingExit(null)
  }

  const handleDeleteSession = async () => {
    if (!pendingDeleteSession) return
    setDeletingSession(true)
    try {
      await deleteSession(pendingDeleteSession)
    } catch {}
    setDeletingSession(false)
    setPendingDeleteSession(null)
  }

  const handleSendInput = async (terminalId: string) => {
    const message = (sendInputValues[terminalId] || '').trim()
    if (!message) return
    setSendingInput(terminalId)
    try {
      await api.sendInput(terminalId, message)
      setSendInputValues(prev => ({ ...prev, [terminalId]: '' }))
      showSnackbar({ type: 'success', message: 'Message sent' })
    } catch {
      showSnackbar({ type: 'error', message: 'Failed to send message' })
    }
    setSendingInput(null)
  }

  const toggleSession = (name: string) => {
    setExpandedSessions(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-emerald-900/50 flex items-center justify-center">
              <Users size={20} className="text-emerald-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{sessions.length}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Sessions</div>
            </div>
          </div>
        </div>
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-cyan-900/50 flex items-center justify-center">
              <TermIcon size={20} className="text-cyan-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{totalTerminals}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Running Agents</div>
            </div>
          </div>
        </div>
        <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 rounded-xl p-5 border border-gray-700/50">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-blue-900/50 flex items-center justify-center">
              <Package size={20} className="text-blue-400" />
            </div>
            <div>
              <div className="text-2xl font-bold text-white">{profileCount}</div>
              <div className="text-xs text-gray-400 uppercase tracking-wide">Profiles</div>
            </div>
          </div>
        </div>
      </div>

      {/* Quick Actions */}
      <div className="flex gap-3 flex-wrap">
        <button onClick={() => onNavigate('agents')} className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors">
          <Bot size={16} /> Spawn Agent
        </button>
        <button onClick={() => onNavigate('flows')} className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors">
          <Zap size={16} /> Manage Flows
        </button>
      </div>

      {/* Header with sort toggle */}
      <div className="mb-1">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Active Sessions</h3>
            <p className="text-xs text-gray-500 mt-1">
              Each session is a workspace where one or more AI agents run and collaborate.
            </p>
          </div>
          <button onClick={() => setSortOrder(o => o === 'desc' ? 'asc' : 'desc')} className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-200 bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-lg transition-colors">
            <ArrowDownUp size={12} />
            {sortOrder === 'desc' ? 'Newest first' : 'Oldest first'}
          </button>
        </div>
      </div>

      {/* Agent type filter */}
      {allAgentTypes.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <Filter size={12} className="text-gray-500" />
          <button onClick={() => setAgentTypeFilter(null)} className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${!agentTypeFilter ? 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300' : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>All</button>
          {allAgentTypes.map(t => (
            <button key={t} onClick={() => setAgentTypeFilter(agentTypeFilter === t ? null : t)} className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${agentTypeFilter === t ? 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300' : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>{t}</button>
          ))}
        </div>
      )}

      {/* Status filter */}
      <div className="flex items-center gap-2 flex-wrap -mt-3">
        <Filter size={12} className="text-gray-500" />
        <button onClick={() => setStatusFilter(null)} className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${!statusFilter ? 'bg-gray-700 border-gray-500/50 text-gray-200' : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>Any status</button>
        {STATUS_ORDER.map(s => {
          const meta = STATUS_META[s]
          return (
            <button key={s} onClick={() => setStatusFilter(statusFilter === s ? null : s)} className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition-colors ${statusFilter === s ? STATUS_ACTIVE_BG[s] : 'border-gray-700 text-gray-400 hover:text-gray-200'}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${meta.dot}`} />
              {meta.label}
            </button>
          )
        })}
      </div>

      {/* Sessions */}
      {filteredSessions.length === 0 ? (
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-8 text-center">
          <Bot size={32} className="mx-auto text-gray-600 mb-3" />
          {sessionData.length === 0 ? (
            <>
              <p className="text-gray-400 text-sm">No active sessions.</p>
              <p className="text-gray-600 text-xs mt-1">Go to the <span className="text-emerald-400 cursor-pointer" onClick={() => onNavigate('agents')}>Agents tab</span> to spawn your first agent.</p>
            </>
          ) : (
            <p className="text-gray-400 text-sm">No sessions match the current filter.</p>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {filteredSessions.map(session => {
            const visibleTerminals = session.terminals.filter(t => {
              const matchAgent = !agentTypeFilter || t.agent_profile === agentTypeFilter
              const matchStatus = !statusFilter || (terminalStatuses[t.id] || 'UNKNOWN') === statusFilter
              return matchAgent && matchStatus
            })
            const statusCounts = getStatusCounts(session.terminals)
            const sortedTerminals = [...visibleTerminals].sort((a, b) => {
              const ta = a.last_active ? new Date(a.last_active).getTime() : 0
              const tb = b.last_active ? new Date(b.last_active).getTime() : 0
              return sortOrder === 'desc' ? tb - ta : ta - tb
            })
            const grouped: Record<string, TerminalMeta[]> = {}
            sortedTerminals.forEach(t => {
              const key = t.agent_profile || 'default'
              ;(grouped[key] ??= []).push(t)
            })
            const typeSummary = Object.entries(
              session.terminals.reduce<Record<string, number>>((acc, t) => {
                const k = t.agent_profile || 'default'
                acc[k] = (acc[k] || 0) + 1
                return acc
              }, {})
            ).sort((a, b) => b[1] - a[1])
            const sessionStart = session.terminals.reduce<string | null>((earliest, t) => {
              if (!t.created_at) return earliest
              if (!earliest) return t.created_at
              return new Date(t.created_at) < new Date(earliest) ? t.created_at : earliest
            }, null)
            const sessionLastActive = session.terminals.reduce<string | null>((latest, t) => {
              if (!t.last_active) return latest
              if (!latest) return t.last_active
              return new Date(t.last_active) > new Date(latest) ? t.last_active : latest
            }, null)

            return (
              <div key={session.name} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden relative">
                {/* Delete session button */}
                <button
                  onClick={(e) => { e.stopPropagation(); setPendingDeleteSession(session.name) }}
                  className="absolute top-3 right-3 p-1.5 text-gray-600 hover:text-red-400 bg-gray-800/80 hover:bg-gray-700 rounded-lg transition-colors z-10"
                  title="Delete session"
                >
                  <Trash2 size={12} />
                </button>

                {/* Session header */}
                <button onClick={() => toggleSession(session.name)} className="w-full text-left p-4 pr-12 hover:bg-gray-800/40 transition-colors">
                  <div className="flex items-center gap-3">
                    {expandedSessions.has(session.name) ? <ChevronDown size={14} className="text-gray-500" /> : <ChevronRight size={14} className="text-gray-500" />}
                    <Users size={14} className="text-emerald-400" />
                    <span className="text-sm font-mono text-gray-200">{session.name}</span>
                    <span className="text-xs text-gray-500">{session.terminals.length} agent{session.terminals.length !== 1 ? 's' : ''}</span>
                  </div>
                  <div className="ml-8 mt-1.5 flex flex-col gap-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      {typeSummary.map(([type, count]) => (
                        <span key={type} className="text-[10px] bg-gray-700/60 text-gray-400 px-1.5 py-0.5 rounded">{type}{count > 1 ? ` ×${count}` : ''}</span>
                      ))}
                    </div>
                    <StatusSummary counts={statusCounts} />
                    <div className="flex items-center gap-3 text-[10px] text-gray-600">
                      {sessionStart && <span title={fmtAbs(sessionStart) || ''}>Started {fmtRel(sessionStart)}</span>}
                      {sessionLastActive && <span title={fmtAbs(sessionLastActive) || ''}>Active {fmtRel(sessionLastActive)}</span>}
                    </div>
                  </div>
                </button>

                {/* Terminals grouped by agent type */}
                {expandedSessions.has(session.name) && (
                  <div className="border-t border-gray-700/30 px-4 pb-4 space-y-3 pt-3">
                    {Object.entries(grouped).map(([agentType, terminals]) => (
                      <div key={agentType}>
                        <div className="flex items-center gap-2 mb-2">
                          <Bot size={11} className="text-gray-500" />
                          <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">{agentType}</span>
                          <span className="text-[10px] text-gray-600">({terminals.length})</span>
                        </div>
                        <div className="space-y-1.5">
                          {terminals.map(t => {
                            const relCreated = fmtRel(t.created_at)
                            const relActive = fmtRel(t.last_active)
                            const showActive = relActive && relActive !== relCreated
                            return (
                              <div key={t.id} className="bg-gray-900/50 border border-gray-700/30 rounded-lg px-3 py-2 space-y-1.5">
                                <div className="flex items-center justify-between">
                                  <div className="flex items-center gap-2 min-w-0">
                                    <TermIcon size={12} className="text-gray-500 shrink-0" />
                                    <span className="text-xs font-medium text-gray-300 truncate">{t.agent_profile || 'default'}</span>
                                    <span className="text-[10px] font-mono text-gray-600">{t.id.slice(0, 8)}</span>
                                    <StatusBadge status={terminalStatuses[t.id] || null} />
                                    <span className="text-[10px] text-gray-600">{t.provider}</span>
                                  </div>
                                  <div className="flex items-center gap-1 shrink-0">
                                    <button onClick={() => setInboxTerminalId(t.id)} className="p-1 text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Inbox"><Mail size={12} /></button>
                                    <button onClick={() => setOutputTerminalId(t.id)} className="p-1 text-gray-500 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Output"><FileText size={12} /></button>
                                    <button onClick={() => setLiveTerminal({ id: t.id, provider: t.provider, agentProfile: t.agent_profile })} className="flex items-center gap-1 px-2 py-1 bg-emerald-600 hover:bg-emerald-500 text-white text-[10px] font-medium rounded transition-colors"><Monitor size={12} />Terminal</button>
                                    <button onClick={() => setPendingExit(t)} disabled={exitingTerminal === t.id} className="p-1 text-gray-500 hover:text-amber-400 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Graceful Exit"><LogOut size={12} /></button>
                                    <button onClick={() => setPendingClose(t)} disabled={closingTerminal === t.id} className="p-1 text-gray-500 hover:text-red-400 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Close"><Trash2 size={12} /></button>
                                  </div>
                                </div>
                                {/* Timestamps */}
                                <div className="flex items-center gap-3 text-[10px] text-gray-600">
                                  {relCreated && <span title={fmtAbs(t.created_at) || ''}>{relCreated}</span>}
                                  {showActive && <span title={fmtAbs(t.last_active) || ''}>↻ {relActive}</span>}
                                </div>
                                {/* Quick Send */}
                                {!sendInputOpen[t.id] ? (
                                  <button onClick={() => setSendInputOpen(prev => ({ ...prev, [t.id]: true }))} className="text-[10px] text-gray-600 hover:text-gray-300 transition-colors">Message agent...</button>
                                ) : (
                                  <div className="flex items-center gap-1.5">
                                    <input type="text" value={sendInputValues[t.id] || ''} onChange={e => setSendInputValues(prev => ({ ...prev, [t.id]: e.target.value }))} onKeyDown={e => { if (e.key === 'Enter') handleSendInput(t.id) }} placeholder="Type a message..." className="flex-1 bg-gray-900 border border-gray-700 text-gray-200 text-[11px] font-mono rounded px-2 py-1 focus:border-emerald-500 focus:outline-none" autoFocus />
                                    <button onClick={() => handleSendInput(t.id)} disabled={sendingInput === t.id || !(sendInputValues[t.id] || '').trim()} className="flex items-center gap-1 px-2 py-1 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-[10px] font-medium rounded transition-colors"><Send size={10} /></button>
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Modals */}
      {inboxTerminalId && <InboxPanel terminalId={inboxTerminalId} onClose={() => setInboxTerminalId(null)} />}
      {liveTerminal && (
        <TerminalView terminalId={liveTerminal.id} provider={liveTerminal.provider} agentProfile={liveTerminal.agentProfile} onClose={() => setLiveTerminal(null)} />
      )}
      {outputTerminalId && <OutputViewer terminalId={outputTerminalId} onClose={() => setOutputTerminalId(null)} />}
      <ConfirmModal
        open={!!pendingClose}
        title="Close Terminal"
        message="This will kill the tmux window and terminate the agent process."
        details={pendingClose ? [
          { label: 'Terminal', value: `${pendingClose.agent_profile || 'default'} (${pendingClose.id})` },
          { label: 'Session', value: pendingClose.tmux_session },
        ] : []}
        confirmLabel="Close Terminal"
        variant="danger"
        loading={!!closingTerminal}
        onConfirm={handleDeleteTerminal}
        onCancel={() => setPendingClose(null)}
      />
      <ConfirmModal
        open={!!pendingExit}
        title="Graceful Exit"
        message="This will send the provider-specific exit command (e.g., /exit)."
        details={pendingExit ? [
          { label: 'Terminal', value: `${pendingExit.agent_profile || 'default'} (${pendingExit.id})` },
          { label: 'Provider', value: pendingExit.provider },
        ] : []}
        confirmLabel="Send Exit"
        variant="warning"
        loading={!!exitingTerminal}
        onConfirm={handleExitTerminal}
        onCancel={() => setPendingExit(null)}
      />
      <ConfirmModal
        open={!!pendingDeleteSession}
        title="Delete Session"
        message="This will terminate all agents in this session and remove it."
        details={pendingDeleteSession ? [
          { label: 'Session', value: pendingDeleteSession },
        ] : []}
        confirmLabel="Delete Session"
        variant="danger"
        loading={deletingSession}
        onConfirm={handleDeleteSession}
        onCancel={() => setPendingDeleteSession(null)}
      />
    </div>
  )
}
