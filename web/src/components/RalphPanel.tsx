import { useStore } from '../store'
import { api } from '../api'

export function RalphPanel() {
  const { ralph, setRalph, addActivity } = useStore()
  const refresh = () => api.ralph.status().then(r => setRalph(r.active ? r : null))
  const start = async () => {
    const prompt = window.prompt('Ralph prompt:')
    if (prompt) { await api.ralph.start({ prompt }); refresh(); addActivity('Ralph loop started') }
  }
  const stop = async () => { await api.ralph.stop(); refresh(); addActivity('Ralph loop stopped') }

  const pct = ralph ? (ralph.iteration / ralph.maxIterations) * 100 : 0

  return (
    <div className="bg-gray-800 rounded p-4">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">🔄 RALPH LOOPS</h2>
        <div><button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded mr-2">↻</button><button onClick={start} className="px-2 text-sm bg-green-700 rounded">+ New</button></div>
      </div>
      {ralph ? (
        <div className="bg-gray-700 rounded p-3 text-sm">
          <div className="flex justify-between mb-2">
            <span>Iteration {ralph.iteration}/{ralph.maxIterations}</span>
            <span className={ralph.status === 'running' ? 'text-green-400' : 'text-gray-400'}>{ralph.status.toUpperCase()}</span>
          </div>
          <div className="w-full bg-gray-600 rounded h-2 mb-2"><div className="bg-blue-500 h-2 rounded" style={{ width: `${pct}%` }} /></div>
          <div className="text-gray-400 truncate mb-2">{ralph.prompt}</div>
          {ralph.previousFeedback && <div className="text-xs">Quality: {ralph.previousFeedback.qualityScore}/10 - {ralph.previousFeedback.qualitySummary}</div>}
          {ralph.status === 'running' && <button onClick={stop} className="mt-2 px-3 py-1 bg-red-700 rounded text-xs">Stop</button>}
        </div>
      ) : <div className="text-gray-500 text-sm">No active loops</div>}
    </div>
  )
}
