import { useState, useEffect } from 'react'
import { useStore, Task } from '../store'
import { api } from '../api'
import { X, Bot, Terminal, Plus } from 'lucide-react'

const P = { 1: 'bg-red-600', 2: 'bg-yellow-600', 3: 'bg-blue-600' } as const
const S = { open: '○', wip: '●', closed: '✓' } as const

export function TaskPanel() {
  const { tasks, setTasks, sessions, setSessions, addActivity } = useStore()
  const [agents, setAgents] = useState<{name: string; description?: string}[]>([])
  const [assignModal, setAssignModal] = useState<Task | null>(null)
  const [assigning, setAssigning] = useState(false)

  useEffect(() => {
    api.agents.list().then(setAgents).catch(() => setAgents([]))
    api.sessions.list().then(setSessions).catch(() => {})
  }, [])

  const refresh = () => {
    api.tasks.list().then(setTasks)
    api.sessions.list().then(setSessions).catch(() => {})
  }

  const add = async () => {
    const title = prompt('Task title:')
    if (title) { await api.tasks.create({ title }); refresh(); addActivity({ type: 'task_created', timestamp: new Date().toISOString(), detail: title }) }
  }

  const close = async (t: Task) => { await api.tasks.close(t.id); refresh(); addActivity({ type: 'task_closed', timestamp: new Date().toISOString(), detail: t.title }) }

  const assignToExisting = async (sessionId: string) => {
    if (!assignModal) return
    setAssigning(true)
    try {
      await api.tasks.assign(assignModal.id, sessionId)
      // Send the task to the agent
      const prompt = `Please work on this task:\n\nTitle: ${assignModal.title}\n\n${assignModal.description || 'No additional details.'}`
      await api.sessions.input(sessionId, prompt + '\n', true)
      refresh()
      addActivity({ type: 'task_assigned', timestamp: new Date().toISOString(), detail: `${assignModal.title} → ${sessionId}` })
      setAssignModal(null)
    } finally {
      setAssigning(false)
    }
  }

  const assignToNewAgent = async (agentName: string) => {
    if (!assignModal) return
    setAssigning(true)
    try {
      await api.tasks.assignAgent(assignModal.id, agentName)
      refresh()
      addActivity({ type: 'task_assigned', timestamp: new Date().toISOString(), detail: `${assignModal.title} → new ${agentName}` })
      setAssignModal(null)
    } finally {
      setAssigning(false)
    }
  }

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">📋 TASK QUEUE</h2>
        <div><button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded mr-2">↻</button><button onClick={add} className="px-2 text-sm bg-green-700 rounded">+ Add</button></div>
      </div>
      <div className="space-y-2 max-h-64 overflow-y-auto">
        {tasks.map(t => (
          <div key={t.id} className="flex items-center gap-2 p-2 bg-gray-700 rounded text-sm">
            <span className={`px-1 rounded text-xs ${P[t.priority as 1|2|3] || P[2]}`}>P{t.priority}</span>
            <span>{S[t.status as keyof typeof S] || '○'}</span>
            <span className="flex-1 truncate">{t.title}</span>
            {t.assignee && <span className="text-xs text-cyan-400 truncate max-w-24">@{t.assignee.replace('cao-','').slice(0,8)}</span>}
            {t.status === 'open' && !t.assignee && (
              <button 
                onClick={() => setAssignModal(t)}
                className="text-xs bg-purple-700 px-2 py-0.5 rounded hover:bg-purple-600"
              >
                Assign
              </button>
            )}
            {t.status === 'wip' && <button onClick={() => close(t)} className="text-xs bg-green-700 px-2 rounded">Close</button>}
          </div>
        ))}
        {tasks.length === 0 && <div className="text-gray-500 text-sm">No tasks</div>}
      </div>

      {/* Assignment Modal */}
      {assignModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => !assigning && setAssignModal(null)}>
          <div className="bg-gray-900 rounded-xl p-5 w-full max-w-md border border-gray-700" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-semibold text-white">Assign Task</h3>
              <button onClick={() => !assigning && setAssignModal(null)} className="text-gray-400 hover:text-white">
                <X size={18} />
              </button>
            </div>
            
            <div className="mb-4 p-3 bg-gray-800 rounded-lg">
              <div className="text-sm text-gray-400">Task</div>
              <div className="text-white font-medium truncate">{assignModal.title}</div>
            </div>

            {/* Existing Sessions */}
            {sessions.length > 0 && (
              <div className="mb-4">
                <div className="text-xs text-gray-400 mb-2 flex items-center gap-1">
                  <Terminal size={12} /> EXISTING SESSIONS
                </div>
                <div className="space-y-1 max-h-32 overflow-y-auto">
                  {sessions.map(s => (
                    <button
                      key={s.id}
                      onClick={() => assignToExisting(s.id)}
                      disabled={assigning}
                      className="w-full text-left p-2 bg-gray-800 hover:bg-gray-700 rounded text-sm flex items-center gap-2 disabled:opacity-50"
                    >
                      <Terminal size={14} className="text-emerald-400" />
                      <span className="flex-1 truncate">{s.agent_name || 'unknown'}</span>
                      <span className="text-xs text-gray-500">{s.id.slice(0,12)}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* New Agent */}
            <div>
              <div className="text-xs text-gray-400 mb-2 flex items-center gap-1">
                <Plus size={12} /> SPAWN NEW AGENT
              </div>
              <div className="space-y-1 max-h-40 overflow-y-auto">
                {agents.map(a => (
                  <button
                    key={a.name}
                    onClick={() => assignToNewAgent(a.name)}
                    disabled={assigning}
                    className="w-full text-left p-2 bg-gray-800 hover:bg-emerald-900/50 rounded text-sm flex items-center gap-2 disabled:opacity-50"
                  >
                    <Bot size={14} className="text-purple-400" />
                    <span className="flex-1">{a.name}</span>
                    <span className="text-xs text-gray-500">+ new</span>
                  </button>
                ))}
              </div>
            </div>

            {assigning && (
              <div className="mt-4 text-center text-sm text-amber-400">
                Spawning agent and assigning task...
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
