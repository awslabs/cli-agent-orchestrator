import { useState, useEffect } from 'react'
import { api } from '../api'

interface Proposal {
  id: string
  session_id: string
  agent_name: string
  changes: string
  reason: string
  status: string
  created_at: string
  learnings?: { tools_used: string[]; errors: string[]; patterns: string[]; files_modified: string[] }
}

export function ContextProposals() {
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)

  const refresh = () => api.learn.proposals().then(setProposals).catch(() => [])
  useEffect(() => { refresh() }, [])

  const approve = async (id: string) => { await api.learn.approve(id); refresh() }
  const reject = async (id: string) => { await api.learn.reject(id); refresh() }

  const pending = proposals.filter(p => p.status === 'pending')
  const resolved = proposals.filter(p => p.status !== 'pending')

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">🧠 CONTEXT PROPOSALS</h2>
        <button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded">↻</button>
      </div>

      {pending.length > 0 && (
        <div className="mb-3">
          <h3 className="text-sm text-yellow-400 mb-2">Pending ({pending.length})</h3>
          {pending.map(p => (
            <div key={p.id} className="bg-gray-700 rounded p-2 mb-2 text-sm">
              <div className="flex justify-between items-start">
                <div>
                  <span className="font-mono text-blue-400">{p.agent_name}</span>
                  <span className="text-gray-400 text-xs ml-2">{p.session_id.slice(-8)}</span>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => approve(p.id)} className="px-2 py-1 bg-green-600 rounded text-xs">✓</button>
                  <button onClick={() => reject(p.id)} className="px-2 py-1 bg-red-600 rounded text-xs">✗</button>
                </div>
              </div>
              <div className="text-gray-300 mt-1 text-xs">{p.reason}</div>
              <button 
                onClick={() => setExpanded(expanded === p.id ? null : p.id)}
                className="text-xs text-blue-400 mt-1"
              >
                {expanded === p.id ? '▼ Hide' : '▶ Show'} changes
              </button>
              {expanded === p.id && (
                <pre className="mt-2 p-2 bg-gray-800 rounded text-xs overflow-x-auto">{p.changes}</pre>
              )}
            </div>
          ))}
        </div>
      )}

      {resolved.length > 0 && (
        <div>
          <h3 className="text-sm text-gray-400 mb-2">History ({resolved.length})</h3>
          <div className="max-h-32 overflow-y-auto space-y-1">
            {resolved.slice(0, 5).map(p => (
              <div key={p.id} className="flex items-center gap-2 text-xs text-gray-500">
                <span className={p.status === 'approved' ? 'text-green-400' : 'text-red-400'}>
                  {p.status === 'approved' ? '✓' : '✗'}
                </span>
                <span>{p.agent_name}</span>
                <span>{new Date(p.created_at).toLocaleDateString()}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {proposals.length === 0 && <div className="text-gray-500 text-sm">No proposals</div>}
    </div>
  )
}
