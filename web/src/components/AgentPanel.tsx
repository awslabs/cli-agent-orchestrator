import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { TerminalView } from './TerminalView'
import { Bot, Wrench, Search, Shield, Swords, Mail, Map, RefreshCw, Package, User, Play, Pause, X, ChevronDown, Loader2, Terminal, Zap, Info } from 'lucide-react'

const AGENT_ICONS: Record<string, React.ReactNode> = {
  'generalist': <Bot size={20} />,
  'bob-the-builder': <Wrench size={20} />,
  'log-diver': <Search size={20} />,
  'oncall-buddy': <Shield size={20} />,
  'ticket-ninja': <Swords size={20} />,
  'sns-ticket-ninja': <Mail size={20} />,
  'atlas': <Map size={20} />,
  'ralph-wiggum': <RefreshCw size={20} />,
  'amzn-builder': <Package size={20} />
}

const STATUS_CONFIG: Record<string, { color: string; text: string; label: string; animate?: string }> = {
  IDLE: { color: 'bg-emerald-500', text: 'text-emerald-400', label: 'Ready' },
  PROCESSING: { color: 'bg-amber-500', text: 'text-amber-400', label: 'Working', animate: 'animate-pulse' },
  WAITING_INPUT: { color: 'bg-emerald-500', text: 'text-emerald-400', label: 'Waiting' },
  ERROR: { color: 'bg-red-500', text: 'text-red-400', label: 'Error' }
}

export function AgentPanel() {
  const { agents, setAgents, sessions, setSessions, activeSession, setActiveSession, tasks, setTasks, autoModeSessions, toggleAutoMode } = useStore()
  const [spawning, setSpawning] = useState<{ agent: string; logs: string[] } | null>(null)
  const [sessionStatuses, setSessionStatuses] = useState<Record<string, string>>({})
  const [sessionContext, setSessionContext] = useState<Record<string, { total: number; tools: number; files: number; responses: number; prompts: number }>>({})
  const [view, setView] = useState<'agents' | 'sessions'>('sessions')
  const [editingAgent, setEditingAgent] = useState<{ name: string; context: string; contextPath?: string } | null>(null)
  const [closingSession, setClosingSession] = useState<{ id: string; logs: string[] } | null>(null)
  const [focusedSession, setFocusedSession] = useState<string | null>(null)

  const fetchContextUsage = async (sessionId: string) => {
    try {
      const ctx = await api.sessions.context(sessionId)
      setSessionContext(prev => ({ ...prev, [sessionId]: { total: ctx.total_percent, tools: ctx.tools_percent, files: ctx.files_percent, responses: ctx.responses_percent, prompts: ctx.prompts_percent } }))
    } catch {}
  }

  const refresh = async () => {
    const [agentList, sessionList, taskList] = await Promise.all([
      api.agents.list().catch(() => []),
      api.sessions.list().catch(() => []),
      api.tasks.list().catch(() => [])
    ])
    setAgents(agentList)
    setSessions(sessionList)
    setTasks(taskList)
  }

  const getAssignedBead = (sessionId: string) => tasks.find(t => t.assignee === sessionId)

  const loadAgentContext = async (agentName: string) => {
    try {
      const agent = await api.agents.get(agentName)
      setEditingAgent({ name: agentName, context: agent.context || '', contextPath: agent.context_path })
    } catch {
      setEditingAgent({ name: agentName, context: '' })
    }
  }

  const saveAgentContext = async () => {
    if (!editingAgent) return
    try {
      await api.agents.update(editingAgent.name, { name: editingAgent.name, steering: editingAgent.context })
      setEditingAgent(null)
      refresh()
    } catch (e) {
      alert('Failed to save')
    }
  }

  // Try to assign highest priority bead to a session
  const tryAssignBead = async (sessionId: string) => {
    const assignedBead = getAssignedBead(sessionId)
    if (assignedBead) return false
    
    // Find highest priority unassigned bead (OPEN or IN_PROGRESS)
    const availableBeads = tasks
      .filter(t => (t.status === 'open' || t.status === 'wip') && !t.assignee)
      .sort((a, b) => (a.priority || 3) - (b.priority || 3))
    
    if (availableBeads.length > 0) {
      const bead = availableBeads[0]
      try {
        await api.tasks.assign(bead.id, sessionId)
        const prompt = `Please work on this task:\n\nTitle: ${bead.title}\n\n${bead.description || 'No additional details.'}`
        await api.sessions.input(sessionId, prompt + '\n', true)
        refresh()
        return true
      } catch (e) {
        console.error('Auto-assign failed:', e)
      }
    }
    return false
  }

  // Auto mode: assign highest priority bead and send to agent
  const autoModeArray = Array.from(autoModeSessions)
  useEffect(() => {
    if (autoModeArray.length === 0) return
    
    autoModeArray.forEach(async (sessionId) => {
      const session = sessions.find(s => s.id === sessionId)
      if (!session) return
      
      const status = sessionStatuses[sessionId] || session.status
      if (status === 'PROCESSING') return
      
      await tryAssignBead(sessionId)
    })
  }, [autoModeArray.join(','), tasks, sessions, sessionStatuses])

  useEffect(() => { refresh() }, [])

  const spawnSession = async (agentName: string) => {
    if (spawning) return
    setSpawning({ agent: agentName, logs: [`Initializing ${agentName}...`] })
    
    try {
      setSpawning(s => s ? { ...s, logs: [...s.logs, `Creating tmux session...`] } : null)
      await new Promise(r => setTimeout(r, 300))
      
      setSpawning(s => s ? { ...s, logs: [...s.logs, `Loading agent profile...`] } : null)
      await new Promise(r => setTimeout(r, 300))
      
      setSpawning(s => s ? { ...s, logs: [...s.logs, `Spawning kiro-cli agent...`] } : null)
      const session = await api.sessions.create({ agent_name: agentName })
      
      setSpawning(s => s ? { ...s, logs: [...s.logs, `Session ${session.id} created`, `Connecting terminal...`] } : null)
      await new Promise(r => setTimeout(r, 500))
      
      await refresh()
      setActiveSession(session.id)
      setSpawning(null)
      setView('sessions')
    } catch (e) {
      setSpawning(s => s ? { ...s, logs: [...s.logs, `Error: Failed to spawn ${agentName}`] } : null)
      setTimeout(() => setSpawning(null), 2000)
    }
  }

  const deleteSession = async (id: string) => {
    if (closingSession) return
    setClosingSession({ id, logs: [`Killing tmux session ${id}...`] })
    
    try {
      await api.sessions.delete(id)
      setClosingSession(s => s ? { ...s, logs: [...s.logs, 'Verifying session closed...'] } : null)
      await new Promise(r => setTimeout(r, 300))
      
      setClosingSession(s => s ? { ...s, logs: [...s.logs, 'Unassigning beads...'] } : null)
      await api.tasks.unassignSession(id)
      await new Promise(r => setTimeout(r, 300))
      
      setClosingSession(s => s ? { ...s, logs: [...s.logs, 'Done'] } : null)
      await new Promise(r => setTimeout(r, 500))
      
      if (activeSession === id) setActiveSession(null)
      refresh()
    } catch (e) {
      setClosingSession(s => s ? { ...s, logs: [...s.logs, `Error: ${e}`] } : null)
      await new Promise(r => setTimeout(r, 2000))
    }
    setClosingSession(null)
  }

  const handleStatusChange = (sessionId: string, status: string) => {
    setSessionStatuses(prev => ({ ...prev, [sessionId]: status }))
  }

  const getAgentType = (name: string) => {
    if (!name) return 'Agent'
    if (name.includes('ticket') || name.includes('ninja')) return 'Ticket Agent'
    if (name.includes('builder')) return 'Builder Agent'
    if (name.includes('log')) return 'Log Agent'
    if (name.includes('oncall')) return 'Oncall Agent'
    if (name.includes('sns')) return 'SNS Agent'
    return 'Agent'
  }

  return (
    <div className="space-y-4">
      {/* Spawning Modal */}
      {spawning && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-md border border-gray-700 shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-lg bg-emerald-500/20 flex items-center justify-center text-emerald-400 animate-pulse">
                {AGENT_ICONS[spawning.agent] || <Bot size={20} />}
              </div>
              <div>
                <h3 className="font-semibold text-white">Spawning {spawning.agent}</h3>
                <p className="text-xs text-gray-500">Setting up agent session...</p>
              </div>
            </div>
            <div className="bg-black/50 rounded-lg p-3 font-mono text-xs space-y-1 max-h-48 overflow-y-auto">
              {spawning.logs.map((log, i) => (
                <div key={i} className={`flex items-center gap-2 ${log.includes('Error') ? 'text-red-400' : 'text-gray-400'}`}>
                  <Terminal size={12} className="text-gray-600" />
                  {log}
                </div>
              ))}
              <div className="flex items-center gap-2 text-emerald-400">
                <Loader2 size={12} className="animate-spin" />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Closing Session Modal */}
      {closingSession && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-md border border-gray-700 shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-lg bg-red-500/20 flex items-center justify-center text-red-400 animate-pulse">
                <X size={20} />
              </div>
              <div>
                <h3 className="font-semibold text-white">Closing Session</h3>
                <p className="text-xs text-gray-500">{closingSession.id}</p>
              </div>
            </div>
            <div className="bg-black/50 rounded-lg p-3 font-mono text-xs space-y-1 max-h-48 overflow-y-auto">
              {closingSession.logs.map((log, i) => (
                <div key={i} className={`flex items-center gap-2 ${log.includes('Error') ? 'text-red-400' : log === 'Done' ? 'text-emerald-400' : 'text-gray-400'}`}>
                  <Terminal size={12} className="text-gray-600" />
                  {log}
                </div>
              ))}
              {!closingSession.logs.includes('Done') && !closingSession.logs.some(l => l.includes('Error')) && (
                <div className="flex items-center gap-2 text-red-400">
                  <Loader2 size={12} className="animate-spin" />
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Agent Context Modal */}
      {editingAgent && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setEditingAgent(null)}>
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-2xl border border-gray-700 shadow-2xl" onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center text-blue-400">
                {AGENT_ICONS[editingAgent.name] || <Bot size={20} />}
              </div>
              <div className="flex-1">
                <h3 className="font-semibold text-white">{editingAgent.name}</h3>
                <p className="text-xs text-gray-500 font-mono">{editingAgent.contextPath || 'No context file'}</p>
              </div>
            </div>
            <div className="mb-4">
              <label className="block text-sm text-gray-400 mb-2">Agent Context</label>
              <textarea
                value={editingAgent.context}
                onChange={e => setEditingAgent({ ...editingAgent, context: e.target.value })}
                className="w-full h-80 px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm font-mono focus:border-blue-500 focus:outline-none resize-none"
                placeholder="No context file found for this agent"
              />
            </div>
            <div className="flex gap-3">
              <button onClick={() => setEditingAgent(null)} className="flex-1 py-2.5 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-800">
                Cancel
              </button>
              <button onClick={saveAgentContext} className="flex-1 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white">
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h2 className="text-lg font-semibold text-white">
            {view === 'sessions' ? 'Active Sessions' : 'Available Agents'}
          </h2>
          <div className="flex gap-1 p-1 bg-gray-800/50 rounded-lg">
            <button
              onClick={() => setView('sessions')}
              className={`px-3 py-1.5 text-xs rounded-md transition-all flex items-center gap-1.5 ${view === 'sessions' ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'}`}
            >
              <Terminal size={14} /> Sessions ({sessions.length})
            </button>
            <button
              onClick={() => setView('agents')}
              className={`px-3 py-1.5 text-xs rounded-md transition-all flex items-center gap-1.5 ${view === 'agents' ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'}`}
            >
              <Bot size={14} /> Agents ({agents.length})
            </button>
          </div>
        </div>
        <button onClick={refresh} className="p-2 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white transition-all">
          <RefreshCw size={16} />
        </button>
      </div>

      {/* Focused Single Terminal View */}
      {focusedSession && (
        <div className="fixed inset-0 bg-gray-950 z-50 flex flex-col">
          <div className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800">
            <div className="flex items-center gap-3">
              <button
                onClick={() => setFocusedSession(null)}
                className="p-2 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white"
              >
                ← Back
              </button>
              <span className="text-white font-medium">
                {sessions.find(s => s.id === focusedSession)?.agent_name || 'Terminal'}
              </span>
              <span className={`px-2 py-0.5 text-xs rounded-full ${
                (sessionStatuses[focusedSession] || 'IDLE') === 'PROCESSING' ? 'bg-amber-500/20 text-amber-400' : 'bg-emerald-500/20 text-emerald-400'
              }`}>
                {sessionStatuses[focusedSession] || 'IDLE'}
              </span>
            </div>
          </div>
          <div className="flex-1">
            <TerminalView sessionId={focusedSession} onStatusChange={(s) => handleStatusChange(focusedSession, s)} />
          </div>
        </div>
      )}

      {/* Sessions View */}
      {view === 'sessions' && (
        <div className="space-y-4">
          {sessions.length === 0 ? (
            <div className="text-center py-12">
              <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
                <Terminal size={32} />
              </div>
              <p className="text-gray-400 mb-4">No active sessions</p>
              <button
                onClick={() => setView('agents')}
                className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm flex items-center gap-2 mx-auto"
              >
                <Play size={16} /> Spawn an Agent
              </button>
            </div>
          ) : (
            <div className="grid gap-4">
              {sessions.map(session => {
                const agentName = session.agent_name || 'unknown'
                const status = sessionStatuses[session.id] || 'IDLE'
                const config = STATUS_CONFIG[status as keyof typeof STATUS_CONFIG] || STATUS_CONFIG.IDLE
                const icon = AGENT_ICONS[agentName] || <User size={20} />
                const isActive = activeSession === session.id
                const assignedBead = getAssignedBead(session.id)
                const isAutoMode = autoModeSessions.has(session.id)
                const ctx = sessionContext[session.id]
                const contextPct = ctx?.total ?? 0

                return (
                  <div
                    key={session.id}
                    className={`rounded-xl border transition-all ${
                      isActive 
                        ? 'border-emerald-500/50 bg-emerald-500/5' 
                        : 'border-gray-800 bg-gray-900/50 hover:border-gray-700'
                    }`}
                  >
                    <div 
                      className="p-4 cursor-pointer"
                      onClick={() => setActiveSession(isActive ? null : session.id)}
                    >
                      <div className="flex items-center gap-4">
                        <div className="relative">
                          <div className="w-12 h-12 rounded-xl bg-gray-800 flex items-center justify-center text-gray-300">
                            {icon}
                          </div>
                          <div className={`absolute -bottom-1 -right-1 w-4 h-4 rounded-full border-2 border-gray-900 ${config.color} ${config.animate || ''}`}></div>
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <h3 className="font-medium text-white">{agentName}</h3>
                            <span className="px-2 py-0.5 text-xs rounded-full bg-gray-700 text-gray-300">
                              {getAgentType(agentName)}
                            </span>
                            <span className={`px-2 py-0.5 text-xs rounded-full bg-gray-800 ${config.text}`}>
                              {config.label}
                            </span>

                          </div>
                          <p className="text-xs text-gray-500 font-mono mt-0.5">{session.id}</p>
                          {assignedBead && (
                            <div className="mt-2 p-2 rounded-lg bg-amber-500/10 border border-amber-500/20">
                              <div className="flex items-center gap-2">
                                <Zap size={14} className="text-amber-400" />
                                <span className="text-sm text-amber-300 font-medium truncate">{assignedBead.title}</span>
                                <span className="text-xs px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400">P{assignedBead.priority || 3}</span>
                              </div>
                              {assignedBead.description && (
                                <p className="text-xs text-gray-400 mt-1 line-clamp-2">{assignedBead.description}</p>
                              )}
                            </div>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={(e) => { 
                              e.stopPropagation()
                              const wasAutoMode = isAutoMode
                              toggleAutoMode(session.id)
                              // Immediately try to assign bead when turning Auto ON
                              if (!wasAutoMode) tryAssignBead(session.id)
                            }}
                            className={`px-3 py-1.5 text-xs rounded-lg transition-all font-medium flex items-center gap-1.5 ${
                              isAutoMode 
                                ? 'bg-purple-500/20 text-purple-400 border border-purple-500/30' 
                                : 'bg-gray-800 text-gray-400 hover:text-white border border-gray-700'
                            }`}
                          >
                            {isAutoMode ? <Pause size={12} /> : <Play size={12} />}
                            {isAutoMode ? 'Auto ON' : 'Auto'}
                          </button>
                          <button
                            onClick={(e) => { e.stopPropagation(); setFocusedSession(session.id) }}
                            className="p-2 rounded-lg hover:bg-blue-500/20 text-gray-400 hover:text-blue-400 transition-all"
                            title="Focus Terminal"
                          >
                            <Terminal size={16} />
                          </button>
                          <button
                            onClick={(e) => { e.stopPropagation(); deleteSession(session.id) }}
                            className="p-2 rounded-lg hover:bg-red-500/20 text-gray-400 hover:text-red-400 transition-all"
                          >
                            <X size={16} />
                          </button>
                          <ChevronDown size={16} className={`text-gray-400 transition-transform ${isActive ? 'rotate-180' : ''}`} />
                        </div>
                      </div>
                    </div>

                    {isActive && (
                      <div className="border-t border-gray-800 h-80">
                        <TerminalView sessionId={session.id} onStatusChange={(s) => handleStatusChange(session.id, s)} />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Agents View */}
      {view === 'agents' && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {agents.map(agent => {
            const icon = AGENT_ICONS[agent.name] || <User size={20} />
            const isSpawning = spawning?.agent === agent.name
            
            return (
              <div
                key={agent.name}
                className="group p-4 rounded-xl border border-gray-800 bg-gray-900/50 hover:border-emerald-500/50 hover:bg-emerald-500/5 transition-all flex flex-col h-full min-h-[160px]"
              >
                <div className="flex items-center gap-3 mb-2">
                  <div className="w-10 h-10 rounded-lg bg-gray-800 group-hover:bg-emerald-500/20 flex items-center justify-center text-gray-400 group-hover:text-emerald-400 transition-all flex-shrink-0">
                    {isSpawning ? <Loader2 size={20} className="animate-spin" /> : icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h3 className="font-medium text-white text-sm truncate">{agent.name}</h3>
                    <p className="text-xs text-gray-500">{getAgentType(agent.name)}</p>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); loadAgentContext(agent.name) }}
                    className="p-1.5 rounded-lg hover:bg-blue-500/20 text-gray-500 hover:text-blue-400 transition-all flex-shrink-0"
                    title="View/Edit Context"
                  >
                    <Info size={16} />
                  </button>
                </div>
                <p className="text-xs text-gray-500 line-clamp-2 flex-1">{agent.description || 'No description'}</p>
                <button
                  onClick={() => spawnSession(agent.name)}
                  disabled={!!spawning}
                  className="w-full py-2 mt-3 text-xs rounded-lg bg-emerald-600/20 hover:bg-emerald-600 text-emerald-400 hover:text-white transition-all flex items-center justify-center gap-1.5 disabled:opacity-50"
                >
                  <Play size={12} /> Spawn Agent
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
