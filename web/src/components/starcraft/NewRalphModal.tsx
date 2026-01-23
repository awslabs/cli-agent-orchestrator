import { useState } from 'react'
import { useStarCraftStore } from '../../stores/starcraftStore'
import { api } from '../../api'

interface Props {
  onClose: () => void
}

export function NewRalphModal({ onClose }: Props) {
  const [prompt, setPrompt] = useState('')
  const [maxIterations, setMaxIterations] = useState(10)
  const [minIterations, setMinIterations] = useState(3)
  const [loading, setLoading] = useState(false)
  const { addRalphLoop, agentsOnMap } = useStarCraftStore()

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!prompt.trim()) return
    setLoading(true)
    try {
      const res = await api.ralph.create({ prompt: prompt.trim(), max_iterations: maxIterations, min_iterations: minIterations })
      addRalphLoop({
        id: res.id || `ralph-${Date.now()}`,
        prompt: prompt.trim(),
        currentIteration: 1,
        maxIterations,
        minIterations,
        status: 'running',
        beadId: res.beadId || '',
        agentQueue: agentsOnMap.slice(0, 1).map(a => a.id),
        activeAgentIndex: 0,
        qualityScore: null,
        position: { x: 300, y: 300 }
      })
      onClose()
    } catch {
      addRalphLoop({
        id: `ralph-${Date.now()}`,
        prompt: prompt.trim(),
        currentIteration: 1,
        maxIterations,
        minIterations,
        status: 'running',
        beadId: '',
        agentQueue: agentsOnMap.slice(0, 1).map(a => a.id),
        activeAgentIndex: 0,
        qualityScore: null,
        position: { x: 300, y: 300 }
      })
      onClose()
    }
  }

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50" style={{ background: 'rgba(0,0,0,0.8)' }} onClick={onClose}>
      <div className="p-6 rounded-lg w-96" style={{ background: '#1a1a2e', border: '1px solid #333' }} onClick={e => e.stopPropagation()}>
        <h2 className="text-lg font-bold mb-4" style={{ color: '#ff00ff' }}>🔄 New Ralph Loop</h2>
        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: '#888' }}>Prompt</label>
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              className="w-full px-3 py-2 rounded text-sm h-24 resize-none"
              style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
              placeholder="Enter task prompt..."
              autoFocus
            />
          </div>
          <div className="flex gap-4 mb-4">
            <div className="flex-1">
              <label className="block text-sm mb-1" style={{ color: '#888' }}>Min Iterations</label>
              <input
                type="number"
                value={minIterations}
                onChange={e => setMinIterations(Math.max(1, parseInt(e.target.value) || 1))}
                className="w-full px-3 py-2 rounded text-sm"
                style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
                min={1}
              />
            </div>
            <div className="flex-1">
              <label className="block text-sm mb-1" style={{ color: '#888' }}>Max Iterations</label>
              <input
                type="number"
                value={maxIterations}
                onChange={e => setMaxIterations(Math.max(1, parseInt(e.target.value) || 1))}
                className="w-full px-3 py-2 rounded text-sm"
                style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
                min={1}
              />
            </div>
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded text-sm" style={{ background: '#333', color: '#888' }}>
              Cancel
            </button>
            <button type="submit" disabled={loading || !prompt.trim()} className="flex-1 py-2 rounded text-sm" style={{ background: '#ff00ff', color: '#000' }}>
              {loading ? 'Starting...' : 'Start Loop'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
