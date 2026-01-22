import { useState, useEffect } from 'react'
import { useStore, Agent } from '../store'
import { api } from '../api'
import { TerminalView } from './TerminalView'

const STATUS_STYLE: Record<string, string> = {
  IDLE: 'text-blue-400',
  PROCESSING: 'text-yellow-400 animate-pulse',
  WAITING_INPUT: 'text-orange-400 animate-bounce',
  ERROR: 'text-red-400'
}

export function AgentPanel() {
  const { agents, setAgents, sessions, setSessions, activeSession, setActiveSession, autoModeSessions, toggleAutoMode, tasks } = useStore()
  const [showCreate, setShowCreate] = useState(false)
  const [newAgent, setNewAgent] = useState({ name: '', description: '' })
  const [spawning, setSpawning] = useState<string | null>(null)
  const [sessionStatuses, setSessionStatuses] = useState<Record<string, string>>({})
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null)

  const refresh = async () => {
    const [agentList, sessionList] = await Promise.all([
      api.agents.list().catch(() => []),
      api.sessions.list().catch(() => [])
    ])
    setAgents(agentList)
    setSessions(sessionList)
  }

  useEffect(() => { refresh() }, [])

  const spawnSession = async (agentName: string) => {
    if (spawning) return // Prevent double-click
    setSpawning(agentName)
    try {
      const session = await api.sessions.create({ agent_name: agentName })
      await refresh()
      setActiveSession(session.id)
    } catch (e) {
      console.error('Failed to spawn session:', e)
      alert(`Failed to spawn ${agentName}: ${e instanceof Error ? e.message : 'Unknown error'}`)
    } finally {
      setSpawning(null)
    }
  }

  const createAgent = async () => {
    if (!newAgent.name.trim()) return
    await api.agents.create(newAgent)
    setNewAgent({ name: '', description: '' })
    setShowCreate(false)
    refresh()
  }

  const deleteAgent = async (name: string) => {
    if (!confirm(`Delete agent "${name}"?`)) return
    await api.agents.delete(name)
    refresh()
  }

  const deleteSession = async (id: string) => {
    await api.sessions.delete(id)
    if (activeSession === id) setActiveSession(null)
    refresh()
  }

  const triggerLearning = async (id: string) => {
    try {
      const proposal = await api.learn.trigger(id)
      alert(`Learning proposal created!\n\n${proposal.changes}`)
    } catch (e) {
      alert('Failed to trigger learning')
    }
  }

  const handleStatusChange = (sessionId: string, status: string) => {
    setSessionStatuses(prev => ({ ...prev, [sessionId]: status }))
  }

  const viewAgentInfo = async (agent: Agent) => {
    try {
      const details = await api.agents.get(agent.name)
      setSelectedAgent(details)
    } catch {
      setSelectedAgent(agent)
    }
  }

  const waitingInputSessions = sessions.filter(s => 
    sessionStatuses[s.id] === 'WAITING_INPUT' || s.status === 'WAITING_INPUT'
  )

  return (
    <div className="bg-gray-800 rounded p-4 col-span-2">
      {waitingInputSessions.length > 0 && (
        <div className="mb-3 p-2 bg-orange-900 border border-orange-600 rounded flex items-center gap-2">
          <span className="animate-bounce">⚠️</span>
          <span>Agent needs input: {waitingInputSessions.map(s => s.id.slice(-8)).join(', ')}</span>
        </div>
      )}

      <div className="flex justify-between mb-3">
        <h2 className="font-bold">🤖 AGENTS & SESSIONS</h2>
        <div className="flex gap-2">
          <button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded">↻</button>
          <button onClick={() => setShowCreate(!showCreate)} className="px-2 text-sm bg-gray-700 rounded">
            {showCreate ? '×' : '+ Agent'}
          </button>
        </div>
      </div>

      {showCreate && (
        <div className="mb-3 p-2 bg-gray-700 rounded space-y-2">
          <input
            value={newAgent.name}
            onChange={(e) => setNewAgent({ ...newAgent, name: e.target.value })}
            placeholder="Agent name..."
            className="w-full px-2 py-1 bg-gray-800 rounded text-sm"
          />
          <input
            value={newAgent.description}
            onChange={(e) => setNewAgent({ ...newAgent, description: e.target.value })}
            placeholder="Description..."
            className="w-full px-2 py-1 bg-gray-800 rounded text-sm"
          />
          <button onClick={createAgent} className="px-2 py-1 bg-green-600 rounded text-xs">Create</button>
        </div>
      )}

      {/* Agent Info Modal */}
      {selectedAgent && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setSelectedAgent(null)}>
          <div className="bg-gray-800 rounded-lg p-6 max-w-lg w-full mx-4 max-h-[80vh] overflow-auto" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between mb-4">
              <h3 className="text-lg font-bold">{selectedAgent.name}</h3>
              <button onClick={() => setSelectedAgent(null)} className="text-gray-400 hover:text-white">×</button>
            </div>
            <div className="space-y-3 text-sm">
              <div><span className="text-gray-400">Description:</span> {selectedAgent.description || 'N/A'}</div>
              <div><span className="text-gray-400">Path:</span> <code className="text-xs bg-gray-700 px-1 rounded">{selectedAgent.path}</code></div>
              {selectedAgent.steering && (
                <div>
                  <span className="text-gray-400">Steering:</span>
                  <pre className="mt-1 p-2 bg-gray-900 rounded text-xs overflow-auto max-h-48">{selectedAgent.steering}</pre>
                </div>
              )}
              {selectedAgent.config && (
                <div>
                  <span className="text-gray-400">Config:</span>
                  <pre className="mt-1 p-2 bg-gray-900 rounded text-xs overflow-auto max-h-48">{JSON.stringify(selectedAgent.config, null, 2)}</pre>
                </div>
              )}
            </div>
            <div className="mt-4 flex gap-2">
              <button onClick={() => spawnSession(selectedAgent.name)} className="px-3 py-1 bg-green-600 rounded text-sm">Spawn Session</button>
              <button onClick={() => setSelectedAgent(null)} className="px-3 py-1 bg-gray-600 rounded text-sm">Close</button>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        {/* Available Agents */}
        <div>
          <h3 className="text-sm text-gray-400 mb-2">Available Agents</h3>
          <div className="space-y-1 max-h-48 overflow-y-auto">
            {agents.map(agent => (
              <div key={agent.name} className="flex items-center gap-2 p-2 bg-gray-700 rounded text-sm group">
                <span className="text-blue-400">○</span>
                <span className="font-mono font-bold truncate">{agent.name}</span>
                <div className="ml-auto flex gap-1">
                  <button
                    onClick={() => viewAgentInfo(agent)}
                    className="px-2 py-1 bg-gray-600 hover:bg-gray-500 rounded text-xs"
                  >
                    Info
                  </button>
                  <button
                    onClick={() => spawnSession(agent.name)}
                    disabled={spawning === agent.name}
                    className="px-2 py-1 bg-green-600 rounded text-xs disabled:opacity-50"
                  >
                    {spawning === agent.name ? '...' : 'Spawn'}
                  </button>
                  <button
                    onClick={() => deleteAgent(agent.name)}
                    className="hidden group-hover:block px-1 text-xs bg-red-600 rounded"
                  >
                    ×
                  </button>
                </div>
              </div>
            ))}
            {agents.length === 0 && <div className="text-gray-500 text-sm">No agents found</div>}
          </div>
        </div>

        {/* Active Sessions */}
        <div>
          <h3 className="text-sm text-gray-400 mb-2">Active Sessions</h3>
          <div className="space-y-1 max-h-48 overflow-y-auto">
            {sessions.map(session => {
              const status = sessionStatuses[session.id] || session.status
              const assignedBead = tasks.find(t => t.assignee === session.id && t.status === 'wip')
              return (
                <div
                  key={session.id}
                  className={`p-2 rounded text-sm cursor-pointer ${
                    activeSession === session.id ? 'bg-blue-900' : 'bg-gray-700 hover:bg-gray-600'
                  }`}
                  onClick={() => setActiveSession(activeSession === session.id ? null : session.id)}
                >
                  <div className="flex items-center gap-2">
                    <span className={STATUS_STYLE[status] || STATUS_STYLE.IDLE}>●</span>
                    <span className="font-mono">{session.id.slice(-8)}</span>
                    <span className="text-xs text-gray-400 truncate">{session.terminals?.[0]?.agent_profile}</span>
                    <span className="text-xs text-gray-500">{status}</span>
                    <button
                      onClick={(e) => { e.stopPropagation(); toggleAutoMode(session.id) }}
                      className={`ml-auto px-1 text-xs rounded ${autoModeSessions.has(session.id) ? 'bg-green-600' : 'bg-gray-600'}`}
                    >
                      Auto
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); triggerLearning(session.id) }}
                      className="px-1 text-xs bg-purple-600 hover:bg-purple-500 rounded"
                      title="Extract learnings from this session"
                    >
                      Learn
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); deleteSession(session.id) }}
                      className="px-1 text-xs bg-red-600 rounded"
                    >
                      ×
                    </button>
                  </div>
                  {assignedBead && (
                    <div className="mt-1 ml-4 text-xs text-yellow-400">
                      📋 Working on: "{assignedBead.title}" (P{assignedBead.priority})
                    </div>
                  )}
                </div>
              )
            })}
            {sessions.length === 0 && <div className="text-gray-500 text-sm">No active sessions</div>}
          </div>
        </div>
      </div>

      {/* Terminal View */}
      {activeSession && (
        <div className="mt-4 h-80 border border-gray-600 rounded overflow-hidden">
          <TerminalView 
            sessionId={activeSession} 
            onStatusChange={(status) => handleStatusChange(activeSession, status)}
          />
        </div>
      )}

      {/* Loading bar for spawning */}
      {spawning && (
        <div className="mt-2">
          <div className="text-xs text-gray-400 mb-1">Spawning {spawning}...</div>
          <div className="h-2 bg-gray-700 rounded overflow-hidden">
            <div className="h-full bg-blue-500 animate-[loading_1s_ease-in-out_infinite]" style={{ width: '60%' }} />
          </div>
        </div>
      )}
    </div>
  )
}
