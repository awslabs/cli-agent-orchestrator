import { useEffect } from 'react'
import { useStore } from './store'
import { api, createActivityStream } from './api'
import { ChatBar } from './components/ChatBar'
import { AgentPanel } from './components/AgentPanel'
import { BeadsPanel } from './components/BeadsPanel'
import { ActivityFeed } from './components/ActivityFeed'
import { RalphPanel } from './components/RalphPanel'
import { ContextProposals } from './components/ContextProposals'

export default function App() {
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

  return (
    <div className="min-h-screen flex flex-col bg-gray-900 text-white">
      {/* Chat Bar */}
      <ChatBar />

      {/* Main Content */}
      <div className="flex-1 p-4 overflow-auto">
        <header className="flex justify-between items-center mb-4 pb-3 border-b border-gray-700">
          <h1 className="text-xl font-bold">🎯 CAO Dashboard V2</h1>
          <span className="text-sm text-green-400">● Connected</span>
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
