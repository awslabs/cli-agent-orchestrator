import { useEffect, useState } from 'react'
import { useStore } from './store'
import { api, createActivityStream } from './api'
import { ChatBar } from './components/ChatBar'
import { AgentPanel } from './components/AgentPanel'
import { BeadsPanel } from './components/BeadsPanel'
import { ActivityFeed } from './components/ActivityFeed'
import { RalphPanel } from './components/RalphPanel'
import { ContextProposals } from './components/ContextProposals'
import { StarCraftView } from './components/starcraft'

export default function App() {
  const [view, setView] = useState<'dashboard' | 'starcraft'>('dashboard')
  const { setTasks, setAgents, setSessions, setRalph, addActivity } = useStore()

  useEffect(() => {
    // Load initial data
    api.tasks.list().then(setTasks).catch(() => {})
    api.agents.list().then(setAgents).catch(() => {})
    api.sessions.list().then(setSessions).catch(() => {})
    api.ralph.status().then(r => setRalph(r.active ? r : null)).catch(() => {})

    // WebSocket for real-time updates
    const ws = new WebSocket(`ws://${location.host}/api/ws/updates`)
    ws.onmessage = (e) => {
      const { type, data } = JSON.parse(e.data)
      addActivity({ type, timestamp: new Date().toISOString(), ...data })
      if (type.startsWith('task')) api.tasks.list().then(setTasks)
      if (type.startsWith('ralph')) api.ralph.status().then(r => setRalph(r.active ? r : null))
    }

    // Activity stream
    const activityWs = createActivityStream((data) => addActivity(data as any))

    return () => {
      ws.close()
      activityWs.close()
    }
  }, [])

  if (view === 'starcraft') {
    return (
      <div className="h-screen flex flex-col">
        <button
          onClick={() => setView('dashboard')}
          className="absolute top-2 right-2 z-50 px-3 py-1 text-xs rounded"
          style={{ background: '#1a1a2e', color: '#00ff88', border: '1px solid #333' }}
        >
          ← Dashboard
        </button>
        <StarCraftView />
      </div>
    )
  }

  return (
    <div className="min-h-screen flex flex-col bg-gray-900 text-white">
      {/* Chat Bar */}
      <ChatBar />

      {/* Main Content */}
      <div className="flex-1 p-4 overflow-auto">
        <header className="flex justify-between items-center mb-4 pb-3 border-b border-gray-700">
          <h1 className="text-xl font-bold">🎯 CAO Dashboard V2</h1>
          <div className="flex items-center gap-4">
            <button
              onClick={() => setView('starcraft')}
              className="px-3 py-1 text-sm rounded bg-green-600 hover:bg-green-500"
            >
              🎮 StarCraft Mode
            </button>
            <span className="text-sm text-green-400">● Connected</span>
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Agents & Sessions - Full width */}
          <AgentPanel />

          {/* Beads Queue */}
          <BeadsPanel />

          {/* Activity Feed */}
          <ActivityFeed />

          {/* Context Proposals */}
          <ContextProposals />

          {/* Ralph Panel */}
          <RalphPanel />
        </div>
      </div>
    </div>
  )
}
