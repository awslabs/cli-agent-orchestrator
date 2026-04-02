import { useState } from 'react'
import { useStarCraftStore } from '../../stores/starcraftStore'
import { api } from '../../api'

interface Props {
  onClose: () => void
}

export function NewBeadModal({ onClose }: Props) {
  const [mode, setMode] = useState<'bead' | 'epic'>('bead')
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState<1 | 2 | 3>(2)
  const [loading, setLoading] = useState(false)
  const [steps, setSteps] = useState<string[]>([''])
  const [sequential, setSequential] = useState(true)
  const { addBead } = useStarCraftStore()

  const addStep = () => setSteps([...steps, ''])
  const removeStep = (i: number) => setSteps(steps.filter((_, idx) => idx !== i))
  const updateStep = (i: number, val: string) => setSteps(steps.map((s, idx) => idx === i ? val : s))

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    setLoading(true)
    try {
      if (mode === 'epic') {
        const validSteps = steps.filter(s => s.trim())
        if (validSteps.length === 0) { setLoading(false); return }
        const result = await api.epics.create({
          title: title.trim(), steps: validSteps, priority, sequential
        })
        if (result.epic) {
          addBead({
            id: result.epic.id,
            title: result.epic.title,
            priority,
            status: 'open',
            assigneeId: null,
            position: null,
            isOrphaned: false
          })
        }
      } else {
        const task = await api.tasks.create({ title: title.trim(), priority })
        addBead({
          id: task.id,
          title: task.title,
          priority,
          status: 'open',
          assigneeId: null,
          position: null,
          isOrphaned: false
        })
      }
      onClose()
    } catch {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50" style={{ background: 'rgba(0,0,0,0.8)' }} onClick={onClose}>
      <div className="p-6 rounded-lg w-[480px] max-h-[80vh] overflow-y-auto" style={{ background: '#1a1a2e', border: '1px solid #333' }} onClick={e => e.stopPropagation()}>
        <h2 className="text-lg font-bold mb-4" style={{ color: '#00ff88' }}>
          {mode === 'epic' ? '🎯 New Epic' : '📋 New Bead'}
        </h2>

        {/* Mode toggle */}
        <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: '#0a0a0f' }}>
          <button
            type="button"
            onClick={() => setMode('bead')}
            className="flex-1 py-1.5 rounded text-sm transition-all"
            style={{ background: mode === 'bead' ? '#333' : 'transparent', color: mode === 'bead' ? '#e0e0e0' : '#666' }}
          >
            Bead
          </button>
          <button
            type="button"
            onClick={() => setMode('epic')}
            className="flex-1 py-1.5 rounded text-sm transition-all"
            style={{ background: mode === 'epic' ? '#333' : 'transparent', color: mode === 'epic' ? '#e0e0e0' : '#666' }}
          >
            Epic
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: '#888' }}>Title</label>
            <input
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              className="w-full px-3 py-2 rounded text-sm"
              style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
              placeholder={mode === 'epic' ? 'Epic title...' : 'Bead title...'}
              autoFocus
            />
          </div>

          {/* Epic-specific: step list */}
          {mode === 'epic' && (
            <div className="mb-4">
              <label className="block text-sm mb-1" style={{ color: '#888' }}>Steps</label>
              <div className="space-y-2">
                {steps.map((step, i) => (
                  <div key={i} className="flex gap-2">
                    <span className="text-xs text-gray-500 pt-2 w-5">{i + 1}.</span>
                    <input
                      type="text"
                      value={step}
                      onChange={e => updateStep(i, e.target.value)}
                      className="flex-1 px-3 py-1.5 rounded text-sm"
                      style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
                      placeholder={`Step ${i + 1}...`}
                    />
                    {steps.length > 1 && (
                      <button type="button" onClick={() => removeStep(i)}
                        className="px-2 text-red-400 hover:text-red-300 text-sm">
                        ×
                      </button>
                    )}
                  </div>
                ))}
              </div>
              <button type="button" onClick={addStep}
                className="mt-2 text-xs px-3 py-1 rounded"
                style={{ background: '#333', color: '#888' }}>
                + Add Step
              </button>

              {/* Sequential toggle */}
              <label className="flex items-center gap-2 mt-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={sequential}
                  onChange={e => setSequential(e.target.checked)}
                  className="rounded"
                />
                <span className="text-sm" style={{ color: '#888' }}>
                  Sequential (each step depends on the previous)
                </span>
              </label>
            </div>
          )}

          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: '#888' }}>Priority</label>
            <div className="flex gap-2">
              {([1, 2, 3] as const).map(p => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPriority(p)}
                  className="flex-1 py-2 rounded text-sm"
                  style={{
                    background: priority === p ? (p === 1 ? '#ff4444' : p === 2 ? '#ffcc00' : '#666') : '#0a0a0f',
                    color: priority === p ? '#000' : '#888',
                    border: `1px solid ${p === 1 ? '#ff4444' : p === 2 ? '#ffcc00' : '#666'}`
                  }}
                >
                  P{p}
                </button>
              ))}
            </div>
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded text-sm" style={{ background: '#333', color: '#888' }}>
              Cancel
            </button>
            <button type="submit" disabled={loading || !title.trim()} className="flex-1 py-2 rounded text-sm" style={{ background: '#00ff88', color: '#000' }}>
              {loading ? 'Creating...' : mode === 'epic' ? 'Create Epic' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
