import { useState } from 'react'
import { useStore, Session } from '../store'
import { api } from '../api'
import { GitBranch, Square, Users } from 'lucide-react'

interface Orchestration {
  supervisor: Session
  workers: Session[]
}

function deriveOrchestrations(sessions: Session[]): Orchestration[] {
  const childIds = new Set(sessions.filter(s => s.parent_session).map(s => s.parent_session))
  const supervisors = sessions.filter(s => childIds.has(s.id))
  
  return supervisors.map(sup => ({
    supervisor: sup,
    workers: sessions.filter(s => s.parent_session === sup.id)
  }))
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    IDLE: 'bg-gray-500/20 text-gray-400',
    PROCESSING: 'bg-blue-500/20 text-blue-400',
    WAITING_INPUT: 'bg-amber-500/20 text-amber-400',
    ERROR: 'bg-red-500/20 text-red-400'
  }
  return (
    <span className={`px-2 py-0.5 text-xs rounded ${colors[status] || colors.IDLE}`}>
      {status.toLowerCase()}
    </span>
  )
}

export function OrchestrationPanel() {
  const { sessions } = useStore()
  const [selected, setSelected] = useState<string | null>(null)
  
  const orchestrations = deriveOrchestrations(sessions)
  const selectedOrch = orchestrations.find(o => o.supervisor.id === selected)

  const handleStopAll = async (orch: Orchestration) => {
    for (const worker of orch.workers) {
      await api.sessions.delete(worker.id)
    }
    await api.sessions.delete(orch.supervisor.id)
  }

  if (orchestrations.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
          <GitBranch size={32} />
        </div>
        <p className="text-gray-400">No active orchestrations</p>
        <p className="text-xs text-gray-500 mt-1">Orchestrations appear when a supervisor spawns workers</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Orchestrations</h2>
        <span className="text-xs text-gray-500">{orchestrations.length} active</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Orchestration List */}
        <div className="space-y-2">
          {orchestrations.map((orch) => (
            <div
              key={orch.supervisor.id}
              onClick={() => setSelected(orch.supervisor.id)}
              className={`p-4 rounded-lg cursor-pointer transition-all ${
                selected === orch.supervisor.id
                  ? 'bg-emerald-500/10 border border-emerald-500/30'
                  : 'bg-gray-900/50 border border-gray-800/50 hover:border-gray-700/50'
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <GitBranch size={16} className="text-emerald-400" />
                  <span className="font-medium text-white">{orch.supervisor.agent_name}</span>
                </div>
                <StatusBadge status={orch.supervisor.status} />
              </div>
              <div className="flex items-center gap-2 mt-2 text-xs text-gray-500">
                <Users size={12} />
                <span>{orch.workers.length} workers</span>
              </div>
            </div>
          ))}
        </div>

        {/* Flow Diagram */}
        {selectedOrch && (
          <div className="p-4 rounded-lg bg-gray-900/50 border border-gray-800/50">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-medium text-white">Flow Diagram</h3>
              <button
                onClick={() => handleStopAll(selectedOrch)}
                className="px-3 py-1 text-xs rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 flex items-center gap-1"
              >
                <Square size={12} /> Stop All
              </button>
            </div>
            
            {/* Tree View */}
            <div className="space-y-2">
              <div className="p-3 rounded bg-emerald-500/10 border border-emerald-500/30">
                <div className="flex items-center justify-between">
                  <span className="text-emerald-400 font-medium">{selectedOrch.supervisor.agent_name}</span>
                  <StatusBadge status={selectedOrch.supervisor.status} />
                </div>
                <span className="text-xs text-gray-500">Supervisor</span>
              </div>
              
              <div className="ml-6 border-l-2 border-gray-700 pl-4 space-y-2">
                {selectedOrch.workers.map((worker) => (
                  <div key={worker.id} className="p-3 rounded bg-gray-800/50 border border-gray-700/50">
                    <div className="flex items-center justify-between">
                      <span className="text-white">{worker.agent_name}</span>
                      <StatusBadge status={worker.status} />
                    </div>
                    <span className="text-xs text-gray-500">Worker</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
