import { useStore } from '../store'
import { Bot, Activity, ArrowRight } from 'lucide-react'

interface DashboardHomeProps {
  onNavigate: (tab: string) => void
}

export function DashboardHome({ onNavigate }: DashboardHomeProps) {
  const { agents, sessions, activity } = useStore()

  // Last 5 activity items
  const recentActivity = activity.slice(0, 5)

  return (
    <div className="space-y-6">
      {/* Quick Actions */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wide mb-3">Quick Actions</h3>
        <div className="grid grid-cols-2 gap-3">
          <button
            onClick={() => onNavigate('agents')}
            className="p-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5 hover:bg-emerald-500/10 transition-all text-left group"
          >
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-emerald-500/20 flex items-center justify-center text-emerald-400">
                <Bot size={20} />
              </div>
              <div className="flex-1">
                <div className="font-medium text-white">Spawn Agent</div>
                <div className="text-xs text-gray-500">Start a new agent session</div>
              </div>
              <ArrowRight size={16} className="text-gray-600 group-hover:text-emerald-400 transition-colors" />
            </div>
          </button>
          <button
            onClick={() => onNavigate('activity')}
            className="p-4 rounded-xl border border-blue-500/30 bg-blue-500/5 hover:bg-blue-500/10 transition-all text-left group"
          >
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center text-blue-400">
                <Activity size={20} />
              </div>
              <div className="flex-1">
                <div className="font-medium text-white">View Activity</div>
                <div className="text-xs text-gray-500">See recent agent activity</div>
              </div>
              <ArrowRight size={16} className="text-gray-600 group-hover:text-blue-400 transition-colors" />
            </div>
          </button>
        </div>
      </div>

      {/* System Summary */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wide mb-3">System Summary</h3>
        <div className="grid grid-cols-2 gap-3">
          <div className="p-3 rounded-xl bg-gray-800/50 border border-gray-700/50 text-center">
            <div className="text-2xl font-bold text-blue-400">{agents.length}</div>
            <div className="text-xs text-gray-500">Agents</div>
          </div>
          <div className="p-3 rounded-xl bg-gray-800/50 border border-gray-700/50 text-center">
            <div className="text-2xl font-bold text-emerald-400">{sessions.length}</div>
            <div className="text-xs text-gray-500">Sessions</div>
          </div>
        </div>
      </div>

      {/* Recent Activity */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wide mb-3">Recent Activity</h3>
        {recentActivity.length === 0 ? (
          <div className="p-6 rounded-xl bg-gray-800/30 border border-gray-700/50 text-center text-gray-500 text-sm">
            No recent activity
          </div>
        ) : (
          <div className="space-y-2">
            {recentActivity.map((item, i) => (
              <div
                key={i}
                className="p-3 rounded-xl bg-gray-800/30 border border-gray-700/50"
              >
                <div className="flex items-center gap-2">
                  <Activity size={14} className="text-gray-500 flex-shrink-0" />
                  <span className="text-xs text-emerald-400 font-mono">{item.type}</span>
                  <span className="text-xs text-gray-500 ml-auto flex-shrink-0">
                    {new Date(item.timestamp).toLocaleTimeString()}
                  </span>
                </div>
                {(item.message || item.detail) && (
                  <p className="text-xs text-gray-400 mt-1 truncate ml-6">
                    {item.message || item.detail}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
