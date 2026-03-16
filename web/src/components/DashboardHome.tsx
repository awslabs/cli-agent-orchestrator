import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api, TerminalMeta } from '../api'
import { Bot, Zap, Package, Monitor, Terminal as TermIcon, Trash2, Mail, FileText, LogOut, Send, ChevronRight, ChevronDown, Users } from 'lucide-react'
import { TerminalView } from './TerminalView'
import { ConfirmModal } from './ConfirmModal'
import { InboxPanel } from './InboxPanel'
import { StatusBadge } from './StatusBadge'
import { OutputViewer } from './OutputViewer'

interface SessionWithTerminals {
  name: string
  status: string
  terminals: TerminalMeta[]
}

export function DashboardHome({ onNavigate }: { onNavigate: (tab: string) => void }) {
  const { sessions, terminalStatuses, setTerminalStatus, clearTerminalStatuses, showSnackbar } = useStore()
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

  const totalTerminals = sessionData.reduce((sum, s) => sum + s.terminals.length, 0)

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
        setSessionData(sessionDetails)
        // Auto-expand all sessions
        setExpandedSessions(new Set(sessionDetails.map(s => s.name)))
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
        <button
          onClick={() => onNavigate('agents')}
          className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors"
        >
          <Bot size={16} /> Spawn Agent
        </button>
        <button
          onClick={() => onNavigate('flows')}
          className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors"
        >
          <Zap size={16} /> Manage Flows
        </button>
      </div>

      {/* Sessions — grouped view */}
      <div className="mb-1">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Active Sessions</h3>
        <p className="text-xs text-gray-500 mt-1">
          Each session is a workspace where one or more AI agents run and collaborate. Agents within a session can send each other messages, hand off tasks, and work in parallel. Open a terminal to interact with any agent directly.
        </p>
      </div>
      {sessionData.length === 0 ? (
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-8 text-center">
          <Bot size={32} className="mx-auto text-gray-600 mb-3" />
          <p className="text-gray-400 text-sm">No active sessions.</p>
          <p className="text-gray-600 text-xs mt-1">Go to the <span className="text-emerald-400 cursor-pointer" onClick={() => onNavigate('agents')}>Agents tab</span> to spawn your first agent.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {sessionData.map(session => (
            <div key={session.name} className="bg-gray-800/60 border border-gray-700/50 rounded-xl overflow-hidden">
              {/* Session header */}
              <button
                onClick={() => toggleSession(session.name)}
                className="w-full flex items-center justify-between p-4 hover:bg-gray-800/40 transition-colors"
              >
                <div className="flex items-center gap-3">
                  {expandedSessions.has(session.name) ? (
                    <ChevronDown size={14} className="text-gray-500" />
                  ) : (
                    <ChevronRight size={14} className="text-gray-500" />
                  )}
                  <Users size={14} className="text-emerald-400" />
                  <span className="text-sm font-mono text-gray-200">{session.name}</span>
                  <span className="text-xs text-gray-500">{session.terminals.length} agent{session.terminals.length !== 1 ? 's' : ''}</span>
                </div>
                <div className="flex items-center gap-2">
                  {/* Show status summary */}
                  {session.terminals.map(t => (
                    <StatusBadge key={t.id} status={terminalStatuses[t.id] || null} />
                  ))}
                </div>
              </button>

              {/* Terminals inside session */}
              {expandedSessions.has(session.name) && (
                <div className="border-t border-gray-700/30 px-4 pb-4 space-y-2 pt-3">
                  {session.terminals.map(t => (
                    <div key={t.id} className="bg-gray-900/50 border border-gray-700/30 rounded-lg p-3 space-y-2">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3 min-w-0">
                          <TermIcon size={14} className="text-gray-400 shrink-0" />
                          <span className="text-sm font-medium text-gray-200 truncate">{t.agent_profile || 'default'}</span>
                          <span className="text-xs font-mono text-gray-500">{t.id}</span>
                          <StatusBadge status={terminalStatuses[t.id] || null} />
                          <span className="text-[10px] text-gray-600">{t.provider}</span>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            onClick={() => setInboxTerminalId(t.id)}
                            className="p-1.5 text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
                            title="Inbox"
                          >
                            <Mail size={14} />
                          </button>
                          <button
                            onClick={() => setOutputTerminalId(t.id)}
                            className="p-1.5 text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
                            title="Output"
                          >
                            <FileText size={14} />
                          </button>
                          <button
                            onClick={() => setLiveTerminal({ id: t.id, provider: t.provider, agentProfile: t.agent_profile })}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium rounded-lg transition-colors"
                          >
                            <Monitor size={12} />
                            Terminal
                          </button>
                          <button
                            onClick={() => setPendingExit(t)}
                            disabled={exitingTerminal === t.id}
                            className="p-1.5 text-gray-400 hover:text-amber-400 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
                            title="Graceful Exit"
                          >
                            <LogOut size={14} />
                          </button>
                          <button
                            onClick={() => setPendingClose(t)}
                            disabled={closingTerminal === t.id}
                            className="p-1.5 text-gray-400 hover:text-red-400 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
                            title="Close"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      </div>
                      {/* Quick Send */}
                      {!sendInputOpen[t.id] ? (
                        <button
                          onClick={() => setSendInputOpen(prev => ({ ...prev, [t.id]: true }))}
                          className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
                        >
                          Message agent...
                        </button>
                      ) : (
                        <div className="flex items-center gap-2">
                          <input
                            type="text"
                            value={sendInputValues[t.id] || ''}
                            onChange={e => setSendInputValues(prev => ({ ...prev, [t.id]: e.target.value }))}
                            onKeyDown={e => { if (e.key === 'Enter') handleSendInput(t.id) }}
                            placeholder="Type a message..."
                            className="flex-1 bg-gray-900 border border-gray-700 text-gray-200 text-sm font-mono rounded-lg px-3 py-1.5 focus:border-emerald-500 focus:outline-none"
                            autoFocus
                          />
                          <button
                            onClick={() => handleSendInput(t.id)}
                            disabled={sendingInput === t.id || !(sendInputValues[t.id] || '').trim()}
                            className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-medium rounded-lg transition-colors"
                          >
                            <Send size={12} />
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
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
    </div>
  )
}
