import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Calendar, Clock, Bot, Play, Pause, Trash2, ChevronDown, ChevronUp, SkipForward, CheckCircle, RefreshCw, Users, FileText } from 'lucide-react'

interface Flow {
  name: string
  schedule: string
  agent_profile: string
  provider: string
  prompt?: string
  enabled: boolean
  next_run?: string
  last_run?: string
  flow_type?: string
}

export function FlowsPanel({ onNavigateToSession }: { onNavigateToSession?: (sessionId: string) => void }) {
  const { agents, showSnackbar } = useStore()
  const [flows, setFlows] = useState<Flow[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [running, setRunning] = useState<string | null>(null)
  const [viewingLog, setViewingLog] = useState<{ id: number; log: string } | null>(null)
  const [newFlow, setNewFlow] = useState({ name: '', schedule: '0 9 * * *', agent_profile: '', prompt: '', flow_type: 'agent' })

  const refresh = () => api.flows.list().then(setFlows).catch(() => [])
  useEffect(() => { refresh() }, [])

  const createFlow = async () => {
    if (!newFlow.name || !newFlow.agent_profile || !newFlow.prompt) return
    try {
      await api.flows.create({ ...newFlow, provider: 'q_cli' })
      setNewFlow({ name: '', schedule: '0 9 * * *', agent_profile: '', prompt: '', flow_type: 'agent' })
      setShowCreate(false)
      refresh()
    } catch (e) {
      showSnackbar('Failed to create flow', 'error')
    }
  }

  const viewSessionHistory = async (sessionId: string) => {
    try {
      const data = await api.sessions.getHistory(sessionId)
      setViewingLog({ id: 0, log: data.history || 'No history available' })
    } catch {
      setViewingLog({ id: 0, log: 'Failed to load history' })
    }
  }

  const toggleFlow = async (name: string, enabled: boolean) => {
    if (enabled) await api.flows.disable(name)
    else await api.flows.enable(name)
    refresh()
  }

  const runFlow = async (name: string) => {
    setRunning(name)
    try {
      const result = await api.flows.run(name)
      // Show confirmation toast
      const toast = document.createElement('div')
      toast.className = 'fixed bottom-4 right-4 bg-emerald-600 text-white px-4 py-2 rounded-lg shadow-lg z-50 animate-pulse'
      toast.textContent = `Flow started: ${result.session_id || name}`
      document.body.appendChild(toast)
      setTimeout(() => toast.remove(), 3000)
      
      // Refresh and re-expand to show new execution
      await refresh()
      if (expanded === name) {
        setExpanded(null)
        setTimeout(() => toggleExpand(name), 100)
      }
    } catch (e) {
      showSnackbar('Failed to run flow', 'error')
    } finally {
      setRunning(null)
    }
  }

  const deleteFlow = async (name: string) => {
    if (!confirm(`Delete flow "${name}"?`)) return
    await api.flows.delete(name)
    refresh()
  }

  const [executions, setExecutions] = useState<Record<string, any[]>>({})
  const [executionTerminals, setExecutionTerminals] = useState<Record<string, any[]>>({})

  const loadFlowDetails = async (name: string) => {
    if (expanded === name) {
      setExpanded(null)
      return
    }
    try {
      const [details, execData] = await Promise.all([
        api.flows.get(name),
        api.flows.executions(name)
      ])
      setFlows(flows.map(f => f.name === name ? { ...f, ...details } : f))
      setExecutions(prev => ({ ...prev, [name]: execData.executions || [] }))
      setExpanded(name)
    } catch {
      setExpanded(name)
    }
  }

  const loadExecutionTerminals = async (sessionId: string) => {
    // Toggle: if already showing, hide
    if (executionTerminals[sessionId]?.length > 0 || executionTerminals[sessionId] === null) {
      setExecutionTerminals(prev => {
        const copy = { ...prev }
        delete copy[sessionId]
        return copy
      })
      return
    }
    try {
      const data = await api.sessions.get(sessionId)
      if (data.terminals?.length > 0) {
        setExecutionTerminals(prev => ({ ...prev, [sessionId]: data.terminals }))
      } else {
        // Session exists but no terminals, or empty response
        setExecutionTerminals(prev => ({ ...prev, [sessionId]: null })) // null = session gone
      }
    } catch {
      setExecutionTerminals(prev => ({ ...prev, [sessionId]: null })) // null = session gone
    }
  }

  const viewTerminalHistory = async (sessionId: string, terminalId: string, agentName: string) => {
    try {
      const data = await api.sessions.getHistory(sessionId)
      const terminalData = data.terminals?.find((t: any) => t.terminal_id === terminalId)
      setViewingLog({ 
        id: 0, 
        log: terminalData?.history || data.history || 'No history available',
        agent: agentName
      } as any)
    } catch {
      setViewingLog({ id: 0, log: 'Session no longer available. Check stored logs.' })
    }
  }

  const viewStoredLog = async (executionId: number) => {
    try {
      const data = await api.flows.executionLog(executionId)
      setViewingLog({ id: executionId, log: data.log || 'No stored log available' })
    } catch {
      setViewingLog({ id: executionId, log: 'Failed to load stored log' })
    }
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold text-white">Scheduled Flows</h2>
          <button onClick={refresh} className="p-1.5 rounded hover:bg-gray-800 text-gray-400">
            <RefreshCw size={14} />
          </button>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-4 py-2 text-sm rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium"
        >
          + Create Flow
        </button>
      </div>

      {/* Create Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-lg border border-gray-700" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">Create New Flow</h3>
            <div className="space-y-4">
              {/* Step 1: Flow Type Selection */}
              <div>
                <label className="block text-sm text-gray-400 mb-2">Flow Type *</label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setNewFlow({ ...newFlow, flow_type: 'agent', agent_profile: '' })}
                    className={`flex-1 py-3 rounded-lg flex flex-col items-center justify-center gap-1 border-2 transition-all ${newFlow.flow_type === 'agent' ? 'bg-blue-600/20 border-blue-500 text-blue-400' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}
                  >
                    <Bot size={24} />
                    <span className="font-medium">Single Agent</span>
                    <span className="text-xs opacity-70">One agent runs the task</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setNewFlow({ ...newFlow, flow_type: 'orchestrator', agent_profile: '' })}
                    className={`flex-1 py-3 rounded-lg flex flex-col items-center justify-center gap-1 border-2 transition-all ${newFlow.flow_type === 'orchestrator' ? 'bg-purple-600/20 border-purple-500 text-purple-400' : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'}`}
                  >
                    <Users size={24} />
                    <span className="font-medium">Orchestrator</span>
                    <span className="text-xs opacity-70">Supervisor + sub-agents</span>
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Flow Name *</label>
                <input
                  value={newFlow.name}
                  onChange={e => setNewFlow({ ...newFlow, name: e.target.value.replace(/\s/g, '-').toLowerCase() })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
                  placeholder="daily-standup"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">
                  {newFlow.flow_type === 'orchestrator' ? 'Supervisor Agent *' : 'Agent Profile *'}
                </label>
                <select
                  value={newFlow.agent_profile}
                  onChange={e => setNewFlow({ ...newFlow, agent_profile: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
                >
                  <option value="">Select {newFlow.flow_type === 'orchestrator' ? 'supervisor' : 'agent'}...</option>
                  {agents
                    .filter(a => newFlow.flow_type === 'orchestrator' 
                      ? a.name.includes('supervisor') || a.name.includes('orchestrator')
                      : !a.name.includes('supervisor'))
                    .map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
                  {/* Show all if no matches */}
                  {newFlow.flow_type === 'orchestrator' && !agents.some(a => a.name.includes('supervisor')) && 
                    agents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
                </select>
                {newFlow.flow_type === 'orchestrator' && (
                  <p className="text-xs text-purple-400 mt-1">Supervisor will coordinate sub-agents to complete the task</p>
                )}
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Schedule (Cron) *</label>
                <input
                  value={newFlow.schedule}
                  onChange={e => setNewFlow({ ...newFlow, schedule: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white font-mono"
                  placeholder="0 9 * * 1-5"
                />
                <p className="text-xs text-gray-500 mt-1">Example: "0 9 * * 1-5" = 9am weekdays</p>
              </div>

              <div>
                <label className="block text-sm text-gray-400 mb-1">Prompt *</label>
                <textarea
                  value={newFlow.prompt}
                  onChange={e => setNewFlow({ ...newFlow, prompt: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white resize-none"
                  rows={3}
                  placeholder={newFlow.flow_type === 'orchestrator' 
                    ? "Describe the task for the team to complete..."
                    : "What should the agent do?"}
                />
              </div>

              <div className="flex gap-3 pt-2">
                <button onClick={() => setShowCreate(false)} className="flex-1 py-2 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-800">
                  Cancel
                </button>
                <button 
                  onClick={createFlow} 
                  disabled={!newFlow.name || !newFlow.agent_profile || !newFlow.prompt}
                  className={`flex-1 py-2 rounded-lg text-white ${newFlow.flow_type === 'orchestrator' ? 'bg-purple-600 hover:bg-purple-500' : 'bg-emerald-600 hover:bg-emerald-500'} disabled:opacity-50 disabled:cursor-not-allowed`}
                >
                  Create {newFlow.flow_type === 'orchestrator' ? 'Orchestrator' : 'Flow'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Log Viewer Modal */}
      {viewingLog && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50" onClick={() => setViewingLog(null)}>
          <div className="bg-gray-900 rounded-xl p-4 w-full max-w-4xl max-h-[80vh] border border-gray-700 flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <FileText size={18} /> Session History
              </h3>
              <button onClick={() => setViewingLog(null)} className="p-1 hover:bg-gray-800 rounded">✕</button>
            </div>
            <pre className="flex-1 overflow-auto bg-black/50 rounded-lg p-4 text-xs font-mono text-gray-300 whitespace-pre-wrap">
              {viewingLog.log}
            </pre>
          </div>
        </div>
      )}

      {/* Flow List */}
      <div className="space-y-3">
        {flows.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            <Calendar size={40} className="mx-auto mb-2 text-gray-600" />
            <p>No scheduled flows</p>
            <p className="text-sm">Create one to automate agent tasks</p>
          </div>
        ) : (
          flows.map(flow => (
            <div key={flow.name} className={`rounded-xl border overflow-hidden ${flow.enabled ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-gray-700 bg-gray-900/50'}`}>
              <div className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className={`w-2 h-2 rounded-full ${flow.enabled ? 'bg-emerald-500' : 'bg-gray-500'}`} />
                      <h3 className="font-medium text-white">{flow.name}</h3>
                      <span className={`px-2 py-0.5 text-xs rounded-full ${flow.enabled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-gray-500/20 text-gray-400'}`}>
                        {flow.enabled ? 'Enabled' : 'Disabled'}
                      </span>
                      {flow.flow_type === 'orchestrator' && (
                        <span className="px-2 py-0.5 text-xs rounded-full bg-purple-500/20 text-purple-400 flex items-center gap-1">
                          <Users size={10} /> Orchestrator
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-gray-400 space-y-1">
                      <p className="flex items-center gap-2">
                        <Clock size={14} className="text-gray-500" />
                        <span className="font-mono">{flow.schedule}</span>
                      </p>
                      <p className="flex items-center gap-2">
                        {flow.flow_type === 'orchestrator' ? <Users size={14} className="text-purple-400" /> : <Bot size={14} className="text-gray-500" />}
                        {flow.agent_profile}
                      </p>
                      {flow.next_run && (
                        <p className="flex items-center gap-2">
                          <SkipForward size={14} className="text-blue-400" />
                          Next: {new Date(flow.next_run).toLocaleString()}
                        </p>
                      )}
                      {flow.last_run && (
                        <p className="flex items-center gap-2">
                          <CheckCircle size={14} className="text-emerald-400" />
                          Last: {new Date(flow.last_run).toLocaleString()}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button 
                      onClick={() => runFlow(flow.name)} 
                      disabled={running === flow.name}
                      className="px-3 py-1.5 text-xs rounded-lg bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 flex items-center gap-1"
                    >
                      <Play size={12} /> {running === flow.name ? 'Running...' : 'Run'}
                    </button>
                    <button 
                      onClick={() => toggleFlow(flow.name, flow.enabled)} 
                      className={`px-3 py-1.5 text-xs rounded-lg flex items-center gap-1 ${flow.enabled ? 'bg-amber-500/20 text-amber-400' : 'bg-emerald-500/20 text-emerald-400'}`}
                    >
                      {flow.enabled ? <><Pause size={12} /> Disable</> : <><Play size={12} /> Enable</>}
                    </button>
                    <button 
                      onClick={() => deleteFlow(flow.name)} 
                      className="px-3 py-1.5 text-xs rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30"
                    >
                      <Trash2 size={12} />
                    </button>
                    <button 
                      onClick={() => loadFlowDetails(flow.name)}
                      className="px-2 py-1.5 text-xs rounded-lg bg-gray-700/50 text-gray-400 hover:bg-gray-700"
                    >
                      {expanded === flow.name ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                    </button>
                  </div>
                </div>
              </div>
              
              {/* Expanded Details */}
              {expanded === flow.name && (
                <div className="border-t border-gray-800 p-4 bg-gray-900/50 space-y-4">
                  <div>
                    <h4 className="text-xs font-medium text-gray-400 mb-2">Prompt</h4>
                    <pre className="text-sm text-white bg-black/30 rounded-lg p-3 whitespace-pre-wrap max-h-32 overflow-y-auto">
                      {flow.prompt || 'No prompt configured'}
                    </pre>
                  </div>
                  
                  <div className="grid grid-cols-2 gap-4 text-xs">
                    <div>
                      <span className="text-gray-500">Provider:</span>
                      <span className="ml-2 text-white">{flow.provider || 'q_cli'}</span>
                    </div>
                    <div>
                      <span className="text-gray-500">Agent:</span>
                      <span className="ml-2 text-white">{flow.agent_profile}</span>
                    </div>
                  </div>

                  {/* Execution History */}
                  <div>
                    <h4 className="text-xs font-medium text-gray-400 mb-2">Execution History</h4>
                    {(executions[flow.name] || []).length === 0 ? (
                      <p className="text-xs text-gray-500">No executions yet</p>
                    ) : (
                      <div className="space-y-2 max-h-64 overflow-y-auto">
                        {(executions[flow.name] || []).map((ex: any) => (
                          <div key={ex.id} className="rounded bg-black/20 text-xs">
                            <div 
                              className="flex items-center gap-3 p-2 cursor-pointer hover:bg-black/30"
                              onClick={() => ex.session_id && loadExecutionTerminals(ex.session_id)}
                            >
                              <span className={`w-2 h-2 rounded-full ${
                                ex.status === 'completed' ? 'bg-emerald-500' : 
                                ex.status === 'failed' ? 'bg-red-500' : 
                                ex.status === 'running' ? 'bg-amber-500 animate-pulse' : 'bg-gray-500'
                              }`} />
                              <span className="text-gray-400">{new Date(ex.started_at).toLocaleString()}</span>
                              <span className={`${
                                ex.status === 'completed' ? 'text-emerald-400' : 
                                ex.status === 'failed' ? 'text-red-400' : 'text-gray-400'
                              }`}>{ex.status}</span>
                              {ex.session_id && (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    onNavigateToSession?.(ex.session_id)
                                  }}
                                  className="text-blue-400 hover:underline font-mono"
                                >
                                  {ex.session_id}
                                </button>
                              )}
                              {ex.status === 'running' && (
                                <button
                                  onClick={async (e) => {
                                    e.stopPropagation()
                                    await fetch(`/api/v2/flows/executions/${ex.id}/complete`, { method: 'POST' })
                                    toggleExpand(flow.name) // refresh
                                    setTimeout(() => toggleExpand(flow.name), 100)
                                  }}
                                  className="text-amber-400 hover:underline text-xs"
                                >
                                  Mark Done
                                </button>
                              )}
                              {ex.has_log && (
                                <button
                                  onClick={(e) => { e.stopPropagation(); viewStoredLog(ex.id) }}
                                  className="text-blue-400 hover:underline text-xs flex items-center gap-1"
                                >
                                  <FileText size={12} /> Stored Log
                                </button>
                              )}
                              {ex.session_id && (
                                <ChevronDown size={14} className={`text-gray-500 ml-auto transition-transform ${executionTerminals[ex.session_id] !== undefined ? 'rotate-180' : ''}`} />
                              )}
                              {ex.error && (
                                <span className="text-red-400 truncate flex-1" title={ex.error}>{ex.error}</span>
                              )}
                            </div>
                            
                            {/* Session no longer exists */}
                            {ex.session_id && executionTerminals[ex.session_id] === null && (
                              <div className="border-t border-gray-800 p-2 pl-6 text-gray-500 text-xs">
                                Session no longer available
                                {ex.has_log && (
                                  <button
                                    onClick={(e) => { e.stopPropagation(); viewStoredLog(ex.id) }}
                                    className="ml-2 text-blue-400 hover:underline"
                                  >
                                    View Stored Log
                                  </button>
                                )}
                              </div>
                            )}
                            
                            {/* Terminals/Agents in this execution */}
                            {ex.session_id && executionTerminals[ex.session_id]?.length > 0 && (
                              <div className="border-t border-gray-800 p-2 pl-6 space-y-1">
                                {executionTerminals[ex.session_id].map((term: any, idx: number) => (
                                  <div key={term.id} className="flex items-center gap-2 p-1.5 rounded hover:bg-gray-800/50">
                                    <Users size={12} className={idx === 0 ? 'text-emerald-400' : 'text-purple-400'} />
                                    <span className={idx === 0 ? 'text-emerald-400' : 'text-purple-400'}>
                                      {term.agent_profile || 'agent'}
                                    </span>
                                    <span className="text-gray-600 text-[10px]">
                                      {idx === 0 ? '(supervisor)' : '(sub-agent)'}
                                    </span>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        viewTerminalHistory(ex.session_id, term.id, term.agent_profile)
                                      }}
                                      className="ml-auto text-blue-400 hover:underline flex items-center gap-1"
                                    >
                                      <FileText size={12} /> View Log
                                    </button>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
