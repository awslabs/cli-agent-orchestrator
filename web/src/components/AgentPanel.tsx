import { useStore } from '../store'
import { api } from '../api'

export function AgentPanel() {
  const { agents, setAgents } = useStore()
  const refresh = () => api.agents.list().then(setAgents).catch(() => setAgents([]))
  const S = { BUSY: '● text-green-400', IDLE: '● text-blue-400', WAITING: '○ text-yellow-400', ERROR: '✗ text-red-400' } as const

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">🤖 AGENTS</h2>
        <button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded">↻</button>
      </div>
      <div className="space-y-2 max-h-48 overflow-y-auto">
        {agents.map((a: any) => (
          <div key={a.session_name || a.id} className="flex items-center gap-2 p-2 bg-gray-700 rounded text-sm">
            <span className={S[a.status as keyof typeof S] || S.IDLE}>{a.status === 'ERROR' ? '✗' : '●'}</span>
            <span className="flex-1 truncate">{a.session_name || a.name || a.id}</span>
            <span className="text-xs text-gray-400">{a.provider || 'unknown'}</span>
          </div>
        ))}
        {agents.length === 0 && <div className="text-gray-500 text-sm">No agents</div>}
      </div>
    </div>
  )
}
