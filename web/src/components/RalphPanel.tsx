import { useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { RefreshCw, Loader2, StopCircle, Terminal, Play } from 'lucide-react'

export function RalphPanel() {
  const { ralph, setRalph } = useStore()
  const [prompt, setPrompt] = useState('')
  const [maxIter, setMaxIter] = useState(25)
  const [minIter, setMinIter] = useState(3)
  const [promise, setPromise] = useState('COMPLETE')
  const [starting, setStarting] = useState(false)

  const refresh = () => api.ralph.status().then(r => setRalph(r.active ? r : null)).catch(() => {})
  const stop = async () => {
    await api.ralph.stop()
    refresh()
  }
  const start = async () => {
    if (!prompt.trim() || starting) return
    setStarting(true)
    try {
      await api.ralph.start({ prompt, min_iterations: minIter, max_iterations: maxIter })
      refresh()
    } catch (e) { console.error('Failed to start Ralph loop:', e) }
    setStarting(false)
  }

  if (!ralph) {
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-white">Ralph Loop</h2>
        <div className="p-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5">
          <div className="mb-4">
            <label className="block text-sm text-gray-400 mb-2">PRD / Prompt</label>
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder="Paste your PRD or task description here..."
              className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm focus:border-emerald-500 focus:outline-none resize-y min-h-[150px]"
            />
          </div>
          <div className="grid grid-cols-3 gap-3 mb-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Max Iterations</label>
              <input type="number" value={maxIter} onChange={e => setMaxIter(+e.target.value)} min={1}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Min Iterations</label>
              <input type="number" value={minIter} onChange={e => setMinIter(+e.target.value)} min={1}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Completion Promise</label>
              <input type="text" value={promise} onChange={e => setPromise(e.target.value)}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm" />
            </div>
          </div>
          <button onClick={start} disabled={!prompt.trim() || starting}
            className="w-full py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium flex items-center justify-center gap-2 disabled:opacity-50">
            {starting ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {starting ? 'Starting...' : 'Start Ralph Loop'}
          </button>
        </div>
      </div>
    )
  }

  const progress = ralph.maxIterations 
    ? Math.round((ralph.iteration / ralph.maxIterations) * 100)
    : 0

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Ralph Loop</h2>
        <div className="flex items-center gap-2">
          <button onClick={refresh} className="p-2 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white">
            <RefreshCw size={16} />
          </button>
          <button
            onClick={stop}
            className="px-3 py-1.5 text-sm rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-all flex items-center gap-1"
          >
            <StopCircle size={14} /> Stop Loop
          </button>
        </div>
      </div>

      <div className="p-6 rounded-xl border border-emerald-500/30 bg-emerald-500/5">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-12 h-12 rounded-xl bg-emerald-500/20 flex items-center justify-center text-emerald-400">
            <Loader2 size={24} className="animate-spin" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium text-white">Running</span>
              <span className="px-2 py-0.5 text-xs rounded-full bg-emerald-500/20 text-emerald-400">
                Iteration {ralph.iteration}/{ralph.maxIterations}
              </span>
            </div>
            <p className="text-xs text-gray-500">
              Min: {ralph.minIterations} • Max: {ralph.maxIterations}
            </p>
          </div>
        </div>

        <div className="mb-4">
          <div className="flex justify-between text-xs text-gray-400 mb-1">
            <span>Progress</span>
            <span>{progress}%</span>
          </div>
          <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
            <div 
              className="h-full bg-gradient-to-r from-emerald-500 to-emerald-400 transition-all duration-500"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>

        {ralph.prompt && (
          <div className="p-3 rounded-lg bg-gray-900/50 border border-gray-800">
            <p className="text-xs text-gray-500 mb-1">Prompt</p>
            <p className="text-sm text-gray-300 line-clamp-3">{ralph.prompt}</p>
          </div>
        )}
      </div>
    </div>
  )
}
