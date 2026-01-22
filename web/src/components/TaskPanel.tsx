import { useStore, Task, Activity } from '../store'
import { api } from '../api'

const P = { 1: 'bg-red-600', 2: 'bg-yellow-600', 3: 'bg-blue-600' } as const
const S = { open: '○', wip: '●', closed: '✓' } as const

export function TaskPanel() {
  const { tasks, setTasks, addActivity } = useStore()
  const refresh = () => api.tasks.list().then(setTasks)
  const add = async () => {
    const title = prompt('Task title:')
    if (title) { await api.tasks.create({ title }); refresh(); addActivity({ type: 'task_created', timestamp: new Date().toISOString(), detail: title }) }
  }
  const wip = async (t: Task) => { await api.tasks.wip(t.id); refresh(); addActivity({ type: 'task_wip', timestamp: new Date().toISOString(), detail: t.title }) }
  const close = async (t: Task) => { await api.tasks.close(t.id); refresh(); addActivity({ type: 'task_closed', timestamp: new Date().toISOString(), detail: t.title }) }

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
            {t.status === 'open' && <button onClick={() => wip(t)} className="text-xs bg-yellow-700 px-2 rounded">WIP</button>}
            {t.status === 'wip' && <button onClick={() => close(t)} className="text-xs bg-green-700 px-2 rounded">Close</button>}
          </div>
        ))}
        {tasks.length === 0 && <div className="text-gray-500 text-sm">No tasks</div>}
      </div>
    </div>
  )
}
