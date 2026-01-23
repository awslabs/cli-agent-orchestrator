import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'

interface Flow {
  name: string
  schedule: string
  agent_profile: string
  provider: string
  enabled: boolean
  next_run?: string
  last_run?: string
}

export function FlowsPanel() {
  const { agents } = useStore()
  const [flows, setFlows] = useState<Flow[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [newFlow, setNewFlow] = useState({ name: '', schedule: '0 9 * * *', agent_profile: '', prompt: '' })

  const refresh = () => api.flows.list().then(setFlows).catch(() => [])
  useEffect(() => { refresh() }, [])

  const createFlow = async () => {
    if (!newFlow.name || !newFlow.agent_profile || !newFlow.prompt) return
    try {
      await api.flows.create(newFlow)
      setNewFlow({ name: '', schedule: '0 9 * * *', agent_profile: '', prompt: '' })
      setShowCreate(false)
      refresh()
    } catch (e) {
      alert('Failed to create flow')
    }
  }

  const toggleFlow = async (name: string, enabled: boolean) => {
    if (enabled) await api.flows.disable(name)
    else await api.flows.enable(name)
    refresh()
  }

  const runFlow = async (name: string) => {
    await api.flows.run(name)
    refresh()
  }

  const deleteFlow = async (name: string) => {
    if (!confirm(`Delete flow "${name}"?`)) return
    await api.flows.delete(name)
    refresh()
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Scheduled Flows</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="px-4 py-2 text-sm rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium"
        >
          + Create Flow
        </button>
      </div>

      {/* Create Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-lg border border-gray-700" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">Create New Flow</h3>
            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-400 mb-1">Flow Name *</label>
                <input
                  value={newFlow.name}
                  onChange={e => setNewFlow({ ...newFlow, name: e.target.value.replace(/\s/g, '-').toLowerCase() })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
                  placeholder="daily-standup"
                />
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">Agent Profile *</label>
                <select
                  value={newFlow.agent_profile}
                  onChange={e => setNewFlow({ ...newFlow, agent_profile: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
                >
                  <option value="">Select agent...</option>
                  {agents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">Schedule (Cron) *</label>
                <input
                  value={newFlow.schedule}
                  onChange={e => setNewFlow({ ...newFlow, schedule: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white font-mono"
                  placeholder="0 9 * * 1-5"
                />
                <p className="text-xs text-gray-500 mt-1">Example: "0 9 * * 1-5" = 9am weekdays</p>
              </div>
              <div>
                <label className="block text-sm text-gray-400 mb-1">Prompt *</label>
                <textarea
                  value={newFlow.prompt}
                  onChange={e => setNewFlow({ ...newFlow, prompt: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white resize-none"
                  rows={4}
                  placeholder="What should the agent do?"
                />
              </div>
              <div className="flex gap-3 pt-2">
                <button onClick={() => setShowCreate(false)} className="flex-1 py-2 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-800">
                  Cancel
                </button>
                <button onClick={createFlow} className="flex-1 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white">
                  Create Flow
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Flow List */}
      <div className="space-y-3">
        {flows.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            <div className="text-4xl mb-2">📋</div>
            <p>No scheduled flows</p>
            <p className="text-sm">Create one to automate agent tasks</p>
          </div>
        ) : (
          flows.map(flow => (
            <div key={flow.name} className={`rounded-xl border p-4 ${flow.enabled ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-gray-700 bg-gray-900/50'}`}>
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`w-2 h-2 rounded-full ${flow.enabled ? 'bg-emerald-500' : 'bg-gray-500'}`} />
                    <h3 className="font-medium text-white">{flow.name}</h3>
                    <span className={`px-2 py-0.5 text-xs rounded-full ${flow.enabled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-gray-500/20 text-gray-400'}`}>
                      {flow.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </div>
                  <div className="text-sm text-gray-400 space-y-1">
                    <p>📅 <span className="font-mono">{flow.schedule}</span></p>
                    <p>🤖 {flow.agent_profile}</p>
                    {flow.next_run && <p>⏭️ Next: {new Date(flow.next_run).toLocaleString()}</p>}
                    {flow.last_run && <p>✅ Last: {new Date(flow.last_run).toLocaleString()}</p>}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => runFlow(flow.name)} className="px-3 py-1.5 text-xs rounded-lg bg-blue-500/20 text-blue-400 hover:bg-blue-500/30">
                    ▶ Run
                  </button>
                  <button onClick={() => toggleFlow(flow.name, flow.enabled)} className={`px-3 py-1.5 text-xs rounded-lg ${flow.enabled ? 'bg-amber-500/20 text-amber-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
                    {flow.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button onClick={() => deleteFlow(flow.name)} className="px-3 py-1.5 text-xs rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30">
                    🗑️
                  </button>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
