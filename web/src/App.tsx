import { useEffect } from 'react'
import { useStore } from './store'
import { api } from './api'
import { TaskPanel } from './components/TaskPanel'
import { RalphPanel } from './components/RalphPanel'
import { AgentPanel } from './components/AgentPanel'
import { ActivityLog } from './components/ActivityLog'

export default function App() {
  const { setTasks, setRalph, setAgents, addActivity } = useStore()

  useEffect(() => {
    api.tasks.list().then(setTasks).catch(() => {})
    api.ralph.status().then(r => setRalph(r.active ? r : null)).catch(() => {})
    fetch('/sessions').then(r => r.json()).then(setAgents).catch(() => {})

    const ws = new WebSocket(`ws://${location.host}/api/ws/updates`)
    ws.onmessage = (e) => {
      const { type, data } = JSON.parse(e.data)
      addActivity(`${type}: ${data.title || data.id || ''}`)
      if (type.startsWith('task')) api.tasks.list().then(setTasks)
      if (type.startsWith('ralph')) api.ralph.status().then(r => setRalph(r.active ? r : null))
    }
    return () => ws.close()
  }, [])

  return (
    <div className="min-h-screen p-4">
      <header className="flex justify-between items-center mb-6 pb-4 border-b border-gray-700">
        <h1 className="text-xl font-bold">🎯 CAO Dashboard</h1>
        <span className="text-sm text-green-400">● Connected</span>
      </header>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <AgentPanel />
        <TaskPanel />
        <RalphPanel />
        <ActivityLog />
      </div>
    </div>
  )
}
