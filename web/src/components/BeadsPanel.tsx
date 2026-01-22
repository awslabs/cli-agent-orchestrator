import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'

const P: Record<number, string> = { 1: 'bg-red-600', 2: 'bg-yellow-600', 3: 'bg-blue-600' }
const S: Record<string, string> = { open: '○', wip: '●', closed: '✓' }

export function BeadsPanel() {
  const { tasks, setTasks, sessions } = useStore()
  const [showAdd, setShowAdd] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState(2)
  const [error, setError] = useState<string | null>(null)

  // Check if a bead is orphaned (wip but assignee session doesn't exist)
  const isOrphaned = (task: { status: string; assignee?: string }) => {
    if (task.status !== 'wip' || !task.assignee) return false
    return !sessions.some(s => s.id === task.assignee)
  }

  const refresh = async () => {
    try {
      const list = await api.tasks.list()
      setTasks(list)
      setError(null)
    } catch (e) {
      setError('Failed to load tasks')
    }
  }

  useEffect(() => { refresh() }, [])

  const addTask = async () => {
    if (!title.trim()) return
    try {
      await api.tasks.create({ title, description, priority })
      setTitle('')
      setDescription('')
      setShowAdd(false)
      refresh()
    } catch (e) {
      setError('Failed to create task')
    }
  }

  const deleteTask = async (id: string) => {
    if (!confirm('Delete this task?')) return
    try {
      await api.tasks.delete(id)
      refresh()
    } catch (e) {
      setError('Failed to delete task')
    }
  }

  const markWip = async (id: string) => {
    try {
      await api.tasks.wip(id)
      refresh()
    } catch (e) {
      setError('Failed to update task')
    }
  }

  const markClose = async (id: string) => {
    try {
      await api.tasks.close(id)
      refresh()
    } catch (e) {
      setError('Failed to close task')
    }
  }

  const assignTask = async (taskId: string, sessionId: string) => {
    try {
      await api.tasks.assign(taskId, sessionId)
      refresh()
    } catch (e) {
      setError('Failed to assign task')
    }
  }

  const grouped = {
    open: tasks.filter(t => t.status === 'open'),
    wip: tasks.filter(t => t.status === 'wip'),
    closed: tasks.filter(t => t.status === 'closed')
  }

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">📋 BEADS QUEUE</h2>
        <div className="flex gap-1">
          <button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded hover:bg-gray-600">↻</button>
          <button onClick={() => setShowAdd(!showAdd)} className="px-2 text-sm bg-gray-700 rounded hover:bg-gray-600">
            {showAdd ? '×' : '+ Add'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-2 p-2 bg-red-900 border border-red-600 rounded text-sm text-red-200">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-red-400">×</button>
        </div>
      )}

      {showAdd && (
        <div className="mb-3 p-3 bg-gray-700 rounded space-y-2">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Task title..."
            className="w-full px-2 py-1 bg-gray-800 rounded text-sm border border-gray-600 focus:border-blue-500 outline-none"
            autoFocus
          />
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description (optional)..."
            className="w-full px-2 py-1 bg-gray-800 rounded text-sm border border-gray-600 focus:border-blue-500 outline-none resize-none"
            rows={2}
          />
          <div className="flex gap-2 items-center">
            <span className="text-xs text-gray-400">Priority:</span>
            {[1, 2, 3].map(p => (
              <button
                key={p}
                onClick={() => setPriority(p)}
                className={`px-3 py-1 rounded text-xs font-bold ${priority === p ? P[p] : 'bg-gray-600 hover:bg-gray-500'}`}
              >
                P{p}
              </button>
            ))}
            <button 
              onClick={addTask} 
              disabled={!title.trim()}
              className="ml-auto px-3 py-1 bg-green-600 hover:bg-green-500 disabled:opacity-50 disabled:cursor-not-allowed rounded text-xs font-bold"
            >
              Create Task
            </button>
          </div>
        </div>
      )}

      <div className="space-y-1 max-h-72 overflow-y-auto">
        {tasks.length === 0 ? (
          <div className="text-gray-500 text-sm text-center py-4">No tasks - click "+ Add" to create one</div>
        ) : (
          ['open', 'wip', 'closed'].map(status => 
            grouped[status as keyof typeof grouped].length > 0 && (
              <div key={status}>
                <div className="text-xs text-gray-500 uppercase mt-2 mb-1">{status} ({grouped[status as keyof typeof grouped].length})</div>
                {grouped[status as keyof typeof grouped].map(task => {
                  const orphaned = isOrphaned(task)
                  return (
                  <div key={task.id} className={`p-2 rounded text-sm group ${orphaned ? 'bg-orange-900 border border-orange-600' : 'bg-gray-700 hover:bg-gray-650'}`}>
                    <div className="flex items-center gap-2">
                      <span className={`w-6 h-6 rounded text-xs flex items-center justify-center font-bold ${P[task.priority]}`}>
                        P{task.priority}
                      </span>
                      <span className="text-gray-400 w-4">{orphaned ? '⚠' : S[task.status]}</span>
                      <div className="flex-1 min-w-0">
                        <div className="truncate">{task.title}</div>
                      </div>
                    
                      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        {(task.status === 'open' || orphaned) && sessions.length > 0 && (
                          <select
                            onChange={(e) => e.target.value && assignTask(task.id, e.target.value)}
                            className="px-1 py-0.5 text-xs bg-blue-600 rounded cursor-pointer"
                            defaultValue=""
                          >
                            <option value="" disabled>{orphaned ? 'Reassign→' : 'Assign→'}</option>
                            {sessions.map(s => (
                              <option key={s.id} value={s.id}>{s.id.slice(-8)}</option>
                            ))}
                          </select>
                        )}
                      
                        {task.status === 'open' && (
                        <button
                          onClick={() => markWip(task.id)}
                          className="px-2 py-0.5 text-xs bg-yellow-600 hover:bg-yellow-500 rounded"
                        >
                          WIP
                        </button>
                      )}
                      {task.status === 'wip' && (
                        <button
                          onClick={() => markClose(task.id)}
                          className="px-2 py-0.5 text-xs bg-green-600 hover:bg-green-500 rounded"
                        >
                          Done
                        </button>
                      )}
                      <button
                        onClick={() => deleteTask(task.id)}
                        className="px-2 py-0.5 text-xs bg-red-600 hover:bg-red-500 rounded"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                  {task.assignee && (
                    <div className={`mt-1 ml-10 text-xs ${orphaned ? 'text-orange-400' : 'text-gray-400'}`}>
                      {orphaned ? '⚠ Agent disconnected - reassign?' : `→ 🤖 ${task.assignee.slice(-8)}`}
                    </div>
                  )}
                </div>
                )})}
              </div>
            )
          )
        )}
      </div>

      <div className="mt-2 text-xs text-gray-500">
        {tasks.filter(t => t.status === 'open').length} open · {tasks.filter(t => t.status === 'wip').length} in progress · {tasks.filter(t => t.status === 'closed').length} closed
      </div>
    </div>
  )
}
