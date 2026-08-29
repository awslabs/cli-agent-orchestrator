// Which node in the fleet this page is driving.
//
// Rendered only when a fleet exists (the panel is serving the app). Served by a
// single cao-server there is nothing to switch between, so the header stays
// exactly as it was.
//
// A native <select> on purpose: it is one element, it is keyboard- and
// screen-reader-accessible for free, and it needs no click-outside handling. The
// richer per-node view belongs on the Fleet tab, not in the header.
import { Server } from 'lucide-react'
import { useStore } from '../store'
import type { FleetNode } from '../api'

function optionLabel(node: FleetNode): string {
  const sessions = node.sessions?.length ?? 0
  const parts = [node.name]
  if (node.label && node.label !== node.name) parts.push(node.label)
  // An offline node stays selectable: seeing why it is empty beats hiding it.
  const state = node.online ? `${sessions} session${sessions === 1 ? '' : 's'}` : 'offline'
  return `${parts.join(' · ')} — ${state}`
}

export function NodeSwitcher() {
  const { fleetNodes, activeNode, selectNode } = useStore()

  if (fleetNodes.length === 0) return null

  const active = fleetNodes.find(n => n.name === activeNode)

  return (
    <div className="flex items-center gap-1.5" title="Node this page is driving">
      <Server
        size={14}
        className={active?.online ? 'text-emerald-400' : 'text-gray-500'}
        aria-hidden
      />
      <select
        aria-label="Active node"
        value={activeNode ?? ''}
        onChange={e => selectNode(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded-md text-xs text-gray-200 px-2 py-1 max-w-[18rem] focus:outline-none focus:ring-1 focus:ring-emerald-500"
      >
        {fleetNodes.map(node => (
          <option key={node.name} value={node.name}>
            {optionLabel(node)}
          </option>
        ))}
      </select>
    </div>
  )
}
