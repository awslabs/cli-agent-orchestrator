import { useStarCraftStore } from '../../stores/starcraftStore'
import { api } from '../../api'

export function ContextMenu() {
  const { contextMenu, hideContextMenu, openTerminal, unassignBead, agentsOnMap, beadsOnMap, setZoom, zoom, setPan } = useStarCraftStore()
  if (!contextMenu) return null

  const items: { label: string; icon: string; action: () => void; divider?: boolean }[] = []

  if (contextMenu.type === 'agent' && contextMenu.targetId) {
    const agent = agentsOnMap.find(a => a.id === contextMenu.targetId)
    items.push(
      { label: 'Open Terminal', icon: '📺', action: () => { openTerminal(contextMenu.targetId!); hideContextMenu() } },
      { label: 'View Details', icon: '📋', action: hideContextMenu, divider: true },
      { label: 'Restart Session', icon: '🔄', action: hideContextMenu },
      { label: 'Pause Agent', icon: '⏸️', action: hideContextMenu, divider: true }
    )
    if (agent?.assignedBeadId) {
      items.push({ label: 'Unassign Bead', icon: '❌', action: () => { unassignBead(agent.assignedBeadId!); hideContextMenu() } })
    }
    items.push({ label: 'Delete Session', icon: '🗑️', action: () => { api.sessions.delete(contextMenu.targetId!); hideContextMenu() } })
  } else if (contextMenu.type === 'bead' && contextMenu.targetId) {
    const bead = beadsOnMap.find(b => b.id === contextMenu.targetId)
    items.push(
      { label: 'View Details', icon: '📋', action: hideContextMenu },
      { label: 'Edit Bead', icon: '✏️', action: hideContextMenu, divider: true },
      { label: 'Set Priority P1', icon: '🔺', action: () => { api.tasks.update(contextMenu.targetId!, { priority: 1 }); hideContextMenu() } },
      { label: 'Set Priority P2', icon: '🔸', action: () => { api.tasks.update(contextMenu.targetId!, { priority: 2 }); hideContextMenu() } },
      { label: 'Set Priority P3', icon: '🔹', action: () => { api.tasks.update(contextMenu.targetId!, { priority: 3 }); hideContextMenu() }, divider: true }
    )
    if (bead?.assigneeId) {
      items.push({ label: 'Unassign', icon: '❌', action: () => { unassignBead(contextMenu.targetId!); hideContextMenu() } })
    }
    items.push(
      { label: 'Mark Complete', icon: '✅', action: () => { api.tasks.close(contextMenu.targetId!); hideContextMenu() } },
      { label: 'Delete Bead', icon: '🗑️', action: () => { api.tasks.delete(contextMenu.targetId!); hideContextMenu() } }
    )
  } else if (contextMenu.type === 'ralph' && contextMenu.targetId) {
    items.push(
      { label: 'View Details', icon: '📋', action: hideContextMenu },
      { label: 'Stop Loop', icon: '⏹️', action: () => { api.ralph.delete(contextMenu.targetId!); hideContextMenu() } }
    )
  } else {
    items.push(
      { label: 'New Agent Here', icon: '➕', action: hideContextMenu },
      { label: 'New Bead Here', icon: '📋', action: hideContextMenu },
      { label: 'New Ralph Loop', icon: '🔄', action: hideContextMenu, divider: true },
      { label: 'Zoom In', icon: '🔍', action: () => { setZoom(zoom + 0.1); hideContextMenu() } },
      { label: 'Zoom Out', icon: '🔍', action: () => { setZoom(zoom - 0.1); hideContextMenu() } },
      { label: 'Reset View', icon: '🎯', action: () => { setZoom(1); setPan(0, 0); hideContextMenu() } }
    )
  }

  return (
    <div
      className="fixed z-50 py-1 rounded shadow-lg"
      style={{ left: contextMenu.x, top: contextMenu.y, background: '#1a1a2e', border: '1px solid #333', minWidth: 180 }}
      onClick={e => e.stopPropagation()}
    >
      {items.map((item, i) => (
        <div key={i}>
          {item.divider && i > 0 && <div className="border-t my-1" style={{ borderColor: '#333' }} />}
          <button
            className="w-full px-3 py-2 text-left text-sm hover:bg-gray-700 flex items-center gap-2"
            style={{ color: '#e0e0e0' }}
            onClick={item.action}
          >
            <span>{item.icon}</span>
            <span>{item.label}</span>
          </button>
        </div>
      ))}
    </div>
  )
}
