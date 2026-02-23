import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Brain, RefreshCw, Loader2, Check, X, FileCode, AlertTriangle, MessageSquare } from 'lucide-react'

interface DiffProposal {
  id: string
  agent_name: string
  agent_md_path: string
  proposed_diff: string
  reason: string
  mistakes_found: string[]
  corrections_found: string[]
  status: string
  created_at: string
}

export function ContextProposals() {
  const [proposals, setProposals] = useState<DiffProposal[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)

  const refresh = () => {
    setLoading(true)
    api.learn.diffProposals()
      .then(setProposals)
      .catch(() => setProposals([]))
      .finally(() => setLoading(false))
  }
  
  useEffect(() => { refresh() }, [])

  const { showSnackbar } = useStore()

  const approve = async (id: string) => {
    await api.learn.approveDiff(id)
    showSnackbar('Proposal approved', 'success')
    refresh()
  }

  const reject = async (id: string) => {
    await api.learn.rejectDiff(id)
    showSnackbar('Proposal rejected', 'info')
    refresh()
  }

  const pending = proposals.filter(p => p.status === 'pending')
  const resolved = proposals.filter(p => p.status !== 'pending')

  const renderDiff = (diff: string) => {
    if (!diff) return <p className="text-gray-500 text-sm italic">No changes suggested</p>
    
    return (
      <pre className="text-xs font-mono bg-black/50 rounded-lg p-3 overflow-x-auto">
        {diff.split('\n').map((line, i) => {
          let className = 'text-gray-400'
          if (line.startsWith('+') && !line.startsWith('+++')) className = 'text-green-400'
          else if (line.startsWith('-') && !line.startsWith('---')) className = 'text-red-400'
          else if (line.startsWith('@@')) className = 'text-blue-400'
          else if (line.startsWith('---') || line.startsWith('+++')) className = 'text-gray-500'
          return <div key={i} className={className}>{line || ' '}</div>
        })}
      </pre>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Agent Learning Proposals</h2>
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
            Click the book icon in a terminal to analyze mistakes and suggest improvements
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {pending.length > 0 && (
            <div className="space-y-3">
              <h3 className="text-sm font-medium text-amber-400 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                Pending Review ({pending.length})
              </h3>
              {pending.map(p => (
                <div 
                  key={p.id} 
                  className="rounded-xl border border-amber-500/30 bg-amber-500/5 overflow-hidden"
                >
                  {/* Header */}
                  <div className="p-4 border-b border-gray-800">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <FileCode size={16} className="text-amber-400" />
                          <span className="font-medium text-white">{p.agent_name}</span>
                          <span className="text-xs text-gray-500 font-mono">{p.agent_md_path}</span>
                        </div>
                        <p className="text-sm text-gray-400">{p.reason}</p>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => approve(p.id)}
                          className="px-3 py-1.5 rounded-lg bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30 text-sm transition-all flex items-center gap-1"
                        >
                          <Check size={14} /> Apply
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

                  {/* Mistakes & Corrections */}
                  <div className="p-4 border-b border-gray-800 grid grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-xs font-medium text-red-400 flex items-center gap-1 mb-2">
                        <AlertTriangle size={12} /> Mistakes Found ({p.mistakes_found?.length || 0})
                      </h4>
                      <ul className="space-y-1 max-h-24 overflow-y-auto">
                        {(p.mistakes_found || []).slice(0, 5).map((m, i) => (
                          <li key={i} className="text-xs text-gray-400 truncate">{m}</li>
                        ))}
                      </ul>
                    </div>
                    <div>
                      <h4 className="text-xs font-medium text-blue-400 flex items-center gap-1 mb-2">
                        <MessageSquare size={12} /> Human Corrections ({p.corrections_found?.length || 0})
                      </h4>
                      <ul className="space-y-1 max-h-24 overflow-y-auto">
                        {(p.corrections_found || []).slice(0, 5).map((c, i) => (
                          <li key={i} className="text-xs text-gray-400 truncate">{c}</li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  {/* Diff */}
                  <div className="p-4">
                    <h4 className="text-xs font-medium text-gray-400 mb-2">Suggested Changes to agent.md</h4>
                    {renderDiff(p.proposed_diff)}
                  </div>
                </div>
              ))}
            </div>
          )}

          {resolved.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-gray-500">History ({resolved.length})</h3>
              <div className="space-y-1 max-h-96 overflow-y-auto">
                {resolved.slice(0, 20).map(p => (
                  <div key={p.id}>
                    <div 
                      className="flex items-center gap-3 p-2 rounded-lg bg-gray-900/30 cursor-pointer hover:bg-gray-900/50"
                      onClick={() => setExpanded(expanded === p.id ? null : p.id)}
                    >
                      <span className={p.status === 'approved' ? 'text-emerald-400' : 'text-red-400'}>
                        {p.status === 'approved' ? <Check size={14} /> : <X size={14} />}
                      </span>
                      <span className="text-sm text-white">{p.agent_name}</span>
                      <span className="text-xs text-gray-500 flex-1 truncate">{p.reason}</span>
                      <span className="text-xs text-gray-600">
                        {new Date(p.created_at).toLocaleDateString()}
                      </span>
                    </div>
                    {expanded === p.id && (
                      <div className="mt-2 ml-6 p-3 rounded-lg bg-gray-900/50 border border-gray-800">
                        <div className="grid grid-cols-2 gap-4 mb-3">
                          <div>
                            <h4 className="text-xs font-medium text-red-400 mb-1">Mistakes ({p.mistakes_found?.length || 0})</h4>
                            <ul className="space-y-1">
                              {(p.mistakes_found || []).map((m, i) => (
                                <li key={i} className="text-xs text-gray-400">{m}</li>
                              ))}
                            </ul>
                          </div>
                          <div>
                            <h4 className="text-xs font-medium text-blue-400 mb-1">Corrections ({p.corrections_found?.length || 0})</h4>
                            <ul className="space-y-1">
                              {(p.corrections_found || []).map((c, i) => (
                                <li key={i} className="text-xs text-gray-400">{c}</li>
                              ))}
                            </ul>
                          </div>
                        </div>
                        <h4 className="text-xs font-medium text-gray-400 mb-1">Proposed Diff</h4>
                        {renderDiff(p.proposed_diff)}
                      </div>
                    )}
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
