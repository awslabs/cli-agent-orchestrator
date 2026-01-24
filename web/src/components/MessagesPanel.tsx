import { useState } from 'react'
import { useStore } from '../store'
import { Mail, Filter } from 'lucide-react'

export function MessagesPanel() {
  const { messages } = useStore()
  const [statusFilter, setStatusFilter] = useState<string | null>(null)

  const filtered = statusFilter 
    ? messages.filter(m => m.status === statusFilter)
    : messages

  if (messages.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
          <Mail size={32} />
        </div>
        <p className="text-gray-400">No messages</p>
        <p className="text-xs text-gray-500 mt-1">Inter-agent messages will appear here</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Agent Messages</h2>
        <span className="text-xs text-gray-500">{filtered.length} messages</span>
      </div>

      <div className="flex items-center gap-2">
        <Filter size={14} className="text-gray-500" />
        <button
          onClick={() => setStatusFilter(null)}
          className={`px-2 py-1 text-xs rounded ${!statusFilter ? 'bg-emerald-600 text-white' : 'bg-gray-800 text-gray-400'}`}
        >
          All
        </button>
        <button
          onClick={() => setStatusFilter('pending')}
          className={`px-2 py-1 text-xs rounded ${statusFilter === 'pending' ? 'bg-amber-600 text-white' : 'bg-gray-800 text-gray-400'}`}
        >
          Pending
        </button>
        <button
          onClick={() => setStatusFilter('delivered')}
          className={`px-2 py-1 text-xs rounded ${statusFilter === 'delivered' ? 'bg-emerald-600 text-white' : 'bg-gray-800 text-gray-400'}`}
        >
          Delivered
        </button>
      </div>

      <div className="space-y-2 max-h-[500px] overflow-y-auto pr-2">
        {filtered.map((msg) => (
          <div
            key={msg.id}
            className="p-3 rounded-lg bg-gray-900/50 border border-gray-800/50 hover:border-gray-700/50 transition-all"
          >
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2 text-xs">
                <span className="text-blue-400">{msg.sender_id}</span>
                <span className="text-gray-600">→</span>
                <span className="text-purple-400">{msg.receiver_id}</span>
              </div>
              <span className={`px-2 py-0.5 text-xs rounded ${
                msg.status === 'pending' ? 'bg-amber-500/20 text-amber-400' :
                msg.status === 'delivered' ? 'bg-emerald-500/20 text-emerald-400' :
                'bg-red-500/20 text-red-400'
              }`}>
                {msg.status}
              </span>
            </div>
            <p className="text-sm text-gray-300">{msg.message}</p>
            <p className="text-xs text-gray-600 mt-1">
              {new Date(msg.created_at).toLocaleString()}
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}
