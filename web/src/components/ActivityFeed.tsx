import { useStore } from '../store'
import { ClipboardList, CheckCircle, Link, Rocket, Trash2, RefreshCw, PartyPopper, MessageSquare, AlertTriangle, Activity, Users, UserPlus, ArrowRightLeft, Send } from 'lucide-react'

const EVENT_CONFIG: Record<string, { icon: React.ReactNode; color: string }> = {
  task_created: { icon: <ClipboardList size={16} />, color: 'text-blue-400' },
  task_completed: { icon: <CheckCircle size={16} />, color: 'text-emerald-400' },
  task_assigned: { icon: <Link size={16} />, color: 'text-purple-400' },
  session_created: { icon: <Rocket size={16} />, color: 'text-emerald-400' },
  session_deleted: { icon: <Trash2 size={16} />, color: 'text-red-400' },
  ralph_started: { icon: <RefreshCw size={16} />, color: 'text-amber-400' },
  ralph_completed: { icon: <PartyPopper size={16} />, color: 'text-emerald-400' },
  agent_output: { icon: <MessageSquare size={16} />, color: 'text-gray-400' },
  error: { icon: <AlertTriangle size={16} />, color: 'text-red-400' },
  // Orchestration events
  orchestration_started: { icon: <Users size={16} />, color: 'text-cyan-400' },
  worker_spawned: { icon: <UserPlus size={16} />, color: 'text-purple-400' },
  handoff_initiated: { icon: <ArrowRightLeft size={16} />, color: 'text-amber-400' },
  message_sent: { icon: <Send size={16} />, color: 'text-blue-400' }
}

function formatTime(timestamp: string) {
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  
  if (diff < 60000) return 'Just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return date.toLocaleDateString()
}

export function ActivityFeed() {
  const { activity } = useStore()
  
  const recent = (activity || []).slice(0, 50)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Activity Feed</h2>
        <span className="text-xs text-gray-500">{recent.length} events</span>
      </div>

      {recent.length === 0 ? (
        <div className="text-center py-12">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
            <Activity size={32} />
          </div>
          <p className="text-gray-400">No activity yet</p>
          <p className="text-xs text-gray-500 mt-1">Events will appear here as they happen</p>
        </div>
      ) : (
        <div className="space-y-2 max-h-[500px] overflow-y-auto pr-2">
          {recent.map((activity, i) => {
            const config = EVENT_CONFIG[activity.type] || { icon: <Activity size={16} />, color: 'text-gray-400' }
            
            return (
              <div
                key={i}
                className="group flex items-start gap-3 p-3 rounded-lg bg-gray-900/50 border border-gray-800/50 hover:border-gray-700/50 transition-all"
              >
                <div className={`w-8 h-8 rounded-lg bg-gray-800 flex items-center justify-center ${config.color}`}>
                  {config.icon}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`text-sm font-medium ${config.color}`}>
                      {activity.type.replace(/_/g, ' ')}
                    </span>
                    <span className="text-xs text-gray-600">
                      {formatTime(activity.timestamp)}
                    </span>
                  </div>
                  {activity.message && (
                    <p className="text-sm text-gray-400 mt-0.5 truncate">{activity.message}</p>
                  )}
                  {activity.session_id && (
                    <p className="text-xs text-gray-600 font-mono mt-1">{activity.session_id}</p>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
