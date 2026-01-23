import { useState } from 'react'
import { useStarCraftStore } from '../../stores/starcraftStore'
import { api } from '../../api'

interface Props {
  onClose: () => void
}

export function NewBeadModal({ onClose }: Props) {
  const [title, setTitle] = useState('')
  const [priority, setPriority] = useState<1 | 2 | 3>(2)
  const [loading, setLoading] = useState(false)
  const { addBead } = useStarCraftStore()

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    setLoading(true)
    try {
      const task = await api.tasks.create({ title: title.trim(), priority })
      addBead({
        id: task.id,
        title: task.title,
        priority: priority,
        status: 'open',
        assigneeId: null,
        position: null,
        isOrphaned: false
      })
      onClose()
    } catch {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50" style={{ background: 'rgba(0,0,0,0.8)' }} onClick={onClose}>
      <div className="p-6 rounded-lg w-96" style={{ background: '#1a1a2e', border: '1px solid #333' }} onClick={e => e.stopPropagation()}>
        <h2 className="text-lg font-bold mb-4" style={{ color: '#00ff88' }}>📋 New Bead</h2>
        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: '#888' }}>Title</label>
            <input
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              className="w-full px-3 py-2 rounded text-sm"
              style={{ background: '#0a0a0f', border: '1px solid #333', color: '#e0e0e0' }}
              placeholder="Enter bead title..."
              autoFocus
            />
          </div>
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
              {loading ? 'Creating...' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
