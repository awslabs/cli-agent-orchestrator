import { useState } from 'react'
import { useStore, Task, Session } from '../store'
import { api } from '../api'

const P: Record<number, string> = { 1: 'bg-red-600', 2: 'bg-yellow-600', 3: 'bg-blue-600' }
const S: Record<string, string> = { open: '○', wip: '●', closed: '✓' }

export function BeadsPanel() {
  const { tasks, setTasks, sessions } = useStore()
  const [showAdd, setShowAdd] = useState(false)
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState(2)
  const [assignTo, setAssignTo] = useState<string | null>(null)

  const refresh = () => api.tasks.list().then(setTasks)

  const addTask = async () => {
    if (!title.trim()) return
    await api.tasks.create({ title, priority })
    setTitle('')
    setShowAdd(false)
    refresh()
  }

  const assign = async (taskId: string, sessionId: string) => {
    await api.tasks.assign(taskId, sessionId)
    refresh()
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
        <button onClick={() => setShowAdd(!showAdd)} className="px-2 text-sm bg-gray-700 rounded">
          {showAdd ? '×' : '+ Add'}
        </button>
      </div>

      {showAdd && (
        <div className="mb-3 p-2 bg-gray-700 rounded space-y-2">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Task title..."
            className="w-full px-2 py-1 bg-gray-800 rounded text-sm"
          />
          <div className="flex gap-2">
            {[1, 2, 3].map(p => (
              <button
                key={p}
                onClick={() => setPriority(p)}
                className={`px-2 py-1 rounded text-xs ${priority === p ? P[p] : 'bg-gray-600'}`}
              >
                P{p}
              </button>
            ))}
            <button onClick={addTask} className="ml-auto px-2 py-1 bg-green-600 rounded text-xs">Add</button>
          </div>
        </div>
      )}

      <div className="space-y-1 max-h-64 overflow-y-auto">
        {['open', 'wip', 'closed'].map(status => (
          grouped[status as keyof typeof grouped].map(task => (
            <div key={task.id} className="flex items-center gap-2 p-2 bg-gray-700 rounded text-sm group">
              <span className={`w-5 h-5 rounded text-xs flex items-center justify-center ${P[task.priority]}`}>
                P{task.priority}
              </span>
              <span className="text-gray-400">{S[task.status]}</span>
              <span className="flex-1 truncate">{task.title}</span>
              {task.assignee && <span className="text-xs text-gray-400">→ {task.assignee}</span>}
              
              {task.status === 'open' && sessions.length > 0 && (
                <div className="hidden group-hover:flex gap-1">
                  {sessions.slice(0, 3).map(s => (
                    <button
                      key={s.id}
                      onClick={() => assign(task.id, s.id)}
                      className="px-1 text-xs bg-blue-600 rounded"
                      title={`Assign to ${s.id}`}
                    >
                      →{s.id.slice(-4)}
                    </button>
                  ))}
                </div>
              )}
              
              {task.status === 'open' && (
                <button
                  onClick={() => api.tasks.wip(task.id).then(refresh)}
                  className="hidden group-hover:block px-1 text-xs bg-yellow-600 rounded"
                >
                  WIP
                </button>
              )}
              {task.status === 'wip' && (
                <button
                  onClick={() => api.tasks.close(task.id).then(refresh)}
                  className="hidden group-hover:block px-1 text-xs bg-green-600 rounded"
                >
                  Done
                </button>
              )}
              <button
                onClick={() => api.tasks.delete(task.id).then(refresh)}
                className="hidden group-hover:block px-1 text-xs bg-red-600 rounded"
              >
                ×
              </button>
            </div>
          ))
        ))}
        {tasks.length === 0 && <div className="text-gray-500 text-sm">No tasks</div>}
      </div>
    </div>
  )
}
