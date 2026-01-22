import { useEffect, useState } from 'react'
import { useStore, Activity } from '../store'
import { api, createActivityStream } from '../api'

const ICONS: Record<string, string> = {
  session_started: '🚀',
  session_ended: '🛑',
  tool_call: '🔧',
  file_op: '📄',
  bead_assigned: '📋',
  default: '📌'
}

export function ActivityFeed() {
  const { activity, addActivity, sessions } = useStore()
  const [filter, setFilter] = useState<string>('')

  useEffect(() => {
    // Load initial activity
    api.activity.list().then((items: Activity[]) => {
      items.reverse().forEach(addActivity)
    }).catch(() => {})

    // Connect WebSocket for live updates
    const ws = createActivityStream((data) => addActivity(data as Activity))
    return () => ws.close()
  }, [])

  const filtered = filter 
    ? activity.filter(a => a.session_id === filter)
    : activity

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between items-center mb-3">
        <h2 className="font-bold">📜 ACTIVITY FEED</h2>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="px-2 py-1 bg-gray-700 rounded text-xs"
        >
          <option value="">All Sessions</option>
          {sessions.map(s => (
            <option key={s.id} value={s.id}>{s.id.slice(-8)}</option>
          ))}
        </select>
      </div>

      <div className="space-y-1 max-h-64 overflow-y-auto text-sm">
        {filtered.map((a, i) => (
          <div key={i} className="flex items-start gap-2 p-1 hover:bg-gray-700 rounded">
            <span>{ICONS[a.type] || ICONS.default}</span>
            <span className="text-gray-400 text-xs">
              {a.session_id?.slice(-4) || 'sys'}
            </span>
            <span className="text-gray-500 text-xs">
              {new Date(a.timestamp).toLocaleTimeString()}
            </span>
            <span className="flex-1">
              {a.type === 'tool_call' && `Tool: ${a.tool}`}
              {a.type === 'session_started' && 'Session started'}
              {a.type === 'session_ended' && 'Session ended'}
              {a.type === 'bead_assigned' && 'Task assigned'}
              {a.type === 'file_op' && (a.detail?.slice(0, 50) || 'File operation')}
              {!['tool_call', 'session_started', 'session_ended', 'bead_assigned', 'file_op'].includes(a.type) && a.type}
            </span>
          </div>
        ))}
        {filtered.length === 0 && <div className="text-gray-500">No activity</div>}
      </div>
    </div>
  )
}
