import { useState, useEffect } from 'react'
import { useStore, Agent, Session } from '../store'
import { api } from '../api'
import { TerminalView } from './TerminalView'

const STATUS_STYLE: Record<string, string> = {
  IDLE: 'text-blue-400',
  PROCESSING: 'text-yellow-400 animate-pulse',
  WAITING_INPUT: 'text-orange-400 animate-bounce',
  ERROR: 'text-red-400'
}

export function AgentPanel() {
  const { agents, setAgents, sessions, setSessions, activeSession, setActiveSession, autoModeSessions, toggleAutoMode } = useStore()
  const [showCreate, setShowCreate] = useState(false)
  const [newAgent, setNewAgent] = useState({ name: '', description: '' })
  const [spawning, setSpawning] = useState<string | null>(null)
  const [sessionStatuses, setSessionStatuses] = useState<Record<string, string>>({})

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
    setSpawning(agentName)
    try {
      const session = await api.sessions.create({ agent_name: agentName })
      await refresh()
      setActiveSession(session.id)
    } catch (e) {
      console.error(e)
    }
    setSpawning(null)
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

  const handleStatusChange = (sessionId: string, status: string) => {
    setSessionStatuses(prev => ({ ...prev, [sessionId]: status }))
  }

  // Check for WAITING_INPUT alerts
  const waitingInputSessions = sessions.filter(s => 
    sessionStatuses[s.id] === 'WAITING_INPUT' || s.status === 'WAITING_INPUT'
  )

  return (
    <div className="bg-gray-800 rounded p-4 col-span-2">
      {/* Alert banner for WAITING_INPUT */}
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

      <div className="grid grid-cols-2 gap-4">
        {/* Available Agents */}
        <div>
          <h3 className="text-sm text-gray-400 mb-2">Available Agents</h3>
          <div className="space-y-1">
            {agents.map(agent => (
              <div key={agent.name} className="flex items-center gap-2 p-2 bg-gray-700 rounded text-sm group">
                <span className="text-blue-400">○</span>
                <span className="font-mono font-bold">{agent.name}</span>
                <span className="text-xs text-gray-400 truncate flex-1">{agent.description?.slice(0, 30)}</span>
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
            ))}
            {agents.length === 0 && <div className="text-gray-500 text-sm">No agents found in ~/.kiro/agents/</div>}
          </div>
        </div>

        {/* Active Sessions */}
        <div>
          <h3 className="text-sm text-gray-400 mb-2">Active Sessions</h3>
          <div className="space-y-1">
            {sessions.map(session => {
              const status = sessionStatuses[session.id] || session.status
              return (
                <div
                  key={session.id}
                  className={`flex items-center gap-2 p-2 rounded text-sm cursor-pointer ${
                    activeSession === session.id ? 'bg-blue-900' : 'bg-gray-700 hover:bg-gray-600'
                  }`}
                  onClick={() => setActiveSession(activeSession === session.id ? null : session.id)}
                >
                  <span className={STATUS_STYLE[status] || STATUS_STYLE.IDLE}>●</span>
                  <span className="font-mono">{session.id.slice(-8)}</span>
                  <span className="text-xs text-gray-400">{session.terminals?.[0]?.agent_profile}</span>
                  <span className="text-xs text-gray-500">{status}</span>
                  <button
                    onClick={(e) => { e.stopPropagation(); toggleAutoMode(session.id) }}
                    className={`ml-auto px-1 text-xs rounded ${autoModeSessions.has(session.id) ? 'bg-green-600' : 'bg-gray-600'}`}
                    title="Toggle auto-mode"
                  >
                    Auto
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); deleteSession(session.id) }}
                    className="px-1 text-xs bg-red-600 rounded"
                  >
                    ×
                  </button>
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
        <div className="mt-2 h-1 bg-gray-700 rounded overflow-hidden">
          <div className="h-full bg-blue-500 animate-pulse" style={{ width: '60%' }} />
        </div>
      )}
    </div>
  )
}
