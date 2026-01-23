import { useStarCraftStore, BeadOnMap } from '../../stores/starcraftStore'

const PRIORITY_STYLES: Record<1 | 2 | 3, { bg: string; border: string; size: string; font: string }> = {
  1: { bg: '#2a0a0a', border: '#ff4444', size: 'p-3', font: 'text-sm' },
  2: { bg: '#2a2a0a', border: '#ffcc00', size: 'p-2', font: 'text-xs' },
  3: { bg: '#1a1a1a', border: '#666666', size: 'p-2', font: 'text-xs' }
}

function BeadCard({ bead, onDragStart }: { bead: BeadOnMap; onDragStart: (e: React.DragEvent, bead: BeadOnMap) => void }) {
  const style = PRIORITY_STYLES[bead.priority]
  
  return (
    <div
      draggable
      onDragStart={(e) => onDragStart(e, bead)}
      className={`${style.size} rounded cursor-grab hover:brightness-125 transition-all mb-2`}
      style={{ background: style.bg, border: `1px solid ${style.border}`, boxShadow: bead.priority === 1 ? `0 0 8px ${style.border}` : undefined }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className={`px-1 rounded ${style.font}`} style={{ background: style.border, color: '#000' }}>P{bead.priority}</span>
        <span className={`${style.font} truncate flex-1`} style={{ color: '#e0e0e0' }}>{bead.title}</span>
      </div>
      <div className="text-xs" style={{ color: '#666' }}>{bead.status}</div>
    </div>
  )
}

export function BeadQueue() {
  const { beadsInQueue, beadsOnMap, agentsOnMap, assignBead } = useStarCraftStore()
  
  const sortedBeads = [...beadsInQueue].sort((a, b) => a.priority - b.priority)
  const assignedBeads = beadsOnMap.filter(b => b.assigneeId)

  const handleDragStart = (e: React.DragEvent, bead: BeadOnMap) => {
    e.dataTransfer.setData('beadId', bead.id)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDrop = (e: React.DragEvent, agentId: string) => {
    e.preventDefault()
    const beadId = e.dataTransfer.getData('beadId')
    if (beadId) assignBead(beadId, agentId)
  }

  return (
    <div className="flex flex-col h-full" style={{ width: 260, background: '#0d0d12', borderLeft: '1px solid #1a1a2e' }}>
      {/* Header */}
      <div className="flex items-center justify-between p-3 border-b" style={{ borderColor: '#1a1a2e' }}>
        <span className="font-bold tracking-wider" style={{ color: '#00ff88' }}>📋 BEAD QUEUE</span>
        <span className="text-xs px-2 py-1 rounded" style={{ background: '#1a1a2e', color: '#666' }}>{beadsInQueue.length}</span>
      </div>

      {/* Queue */}
      <div className="flex-1 overflow-y-auto p-2">
        {sortedBeads.length === 0 ? (
          <div className="text-center py-8 text-xs" style={{ color: '#666' }}>No beads in queue</div>
        ) : (
          sortedBeads.map(bead => <BeadCard key={bead.id} bead={bead} onDragStart={handleDragStart} />)
        )}
      </div>

      {/* Assigned section */}
      {assignedBeads.length > 0 && (
        <div className="border-t p-2" style={{ borderColor: '#1a1a2e' }}>
          <div className="text-xs mb-2" style={{ color: '#666' }}>Assigned ({assignedBeads.length})</div>
          {assignedBeads.map(bead => {
            const agent = agentsOnMap.find(a => a.id === bead.assigneeId)
            return (
              <div key={bead.id} className="text-xs py-1 truncate" style={{ color: '#888' }}>
                • {bead.title} → {agent?.name || 'unknown'}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
