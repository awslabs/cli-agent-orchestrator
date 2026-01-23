import { useState, useEffect } from 'react'
import { api } from '../api'
import { Brain, RefreshCw, Loader2, ChevronDown, ChevronRight, Check, X } from 'lucide-react'

interface Proposal {
  id: string
  bullets: string[]
  source: string
  status: string
  human_feedback: string | null
  created_at: string
}

export function ContextProposals() {
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = () => {
    setLoading(true)
    api.learn.proposals()
      .then(setProposals)
      .catch(() => setProposals([]))
      .finally(() => setLoading(false))
  }
  
  useEffect(() => { refresh() }, [])

  const approve = async (id: string) => {
    await api.learn.approve(id)
    refresh()
  }
  
  const reject = async (id: string) => {
    await api.learn.reject(id)
    refresh()
  }

  const pending = proposals.filter(p => p.status === 'pending')
  const resolved = proposals.filter(p => p.status !== 'pending')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Learning Proposals</h2>
        <button 
          onClick={refresh} 
          className="p-2 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white transition-all"
        >
          <RefreshCw size={16} />
        </button>
      </div>

      {loading ? (
        <div className="text-center py-12">
          <Loader2 size={32} className="mx-auto animate-spin text-gray-600" />
        </div>
      ) : proposals.length === 0 ? (
        <div className="text-center py-12">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
            <Brain size={32} />
          </div>
          <p className="text-gray-400">No learning proposals</p>
          <p className="text-xs text-gray-500 mt-2">
            Proposals are generated from agent sessions
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {pending.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-amber-400 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                Pending Review ({pending.length})
              </h3>
              {pending.map(p => (
                <div 
                  key={p.id} 
                  className="p-4 rounded-xl border border-amber-500/30 bg-amber-500/5"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-xs text-gray-500 font-mono">{p.source}</span>
                        <span className="text-xs text-gray-600">
                          {new Date(p.created_at).toLocaleDateString()}
                        </span>
                      </div>
                      <button
                        onClick={() => setExpanded(expanded === p.id ? null : p.id)}
                        className="text-sm text-amber-400 hover:text-amber-300 transition-colors flex items-center gap-1"
                      >
                        {expanded === p.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        {p.bullets.length} learnings
                      </button>
                      {expanded === p.id && (
                        <ul className="mt-3 space-y-1.5 pl-4 border-l-2 border-gray-800">
                          {p.bullets.map((b, i) => (
                            <li key={i} className="text-sm text-gray-300">{b}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                    <div className="flex gap-2">
                      <button
                        onClick={() => approve(p.id)}
                        className="px-3 py-1.5 rounded-lg bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 text-sm transition-all flex items-center gap-1"
                      >
                        <Check size={14} /> Approve
                      </button>
                      <button
                        onClick={() => reject(p.id)}
                        className="px-3 py-1.5 rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 text-sm transition-all flex items-center gap-1"
                      >
                        <X size={14} /> Reject
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {resolved.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-gray-500">History ({resolved.length})</h3>
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {resolved.slice(0, 10).map(p => (
                  <div 
                    key={p.id}
                    className="flex items-center gap-3 p-2 rounded-lg bg-gray-900/30"
                  >
                    <span className={p.status === 'approved' ? 'text-emerald-400' : 'text-red-400'}>
                      {p.status === 'approved' ? <Check size={14} /> : <X size={14} />}
                    </span>
                    <span className="text-sm text-gray-500 font-mono flex-1 truncate">{p.source}</span>
                    <span className="text-xs text-gray-600">
                      {new Date(p.created_at).toLocaleDateString()}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
