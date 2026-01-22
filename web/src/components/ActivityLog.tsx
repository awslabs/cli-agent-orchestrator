import { useStore } from '../store'

export function ActivityLog() {
  const { activity } = useStore()
  return (
    <div className="bg-gray-800 rounded p-4">
      <h2 className="font-bold mb-3">📜 ACTIVITY</h2>
      <div className="space-y-1 max-h-32 overflow-y-auto text-xs text-gray-400">
        {activity.map((a, i) => <div key={i}>{new Date().toLocaleTimeString()} {a}</div>)}
        {activity.length === 0 && <div>No activity</div>}
      </div>
    </div>
  )
}
