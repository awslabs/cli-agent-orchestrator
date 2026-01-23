import { useStarCraftStore, RalphLoop as RalphLoopType } from '../../stores/starcraftStore'

interface Props { loop: RalphLoopType }

export function RalphLoop({ loop }: Props) {
  const { agentsOnMap, selectItem, selectedId, showContextMenu } = useStarCraftStore()
  const isSelected = selectedId === loop.id
  const radius = 80

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    selectItem(loop.id, 'ralph')
  }

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    showContextMenu({ x: e.clientX, y: e.clientY, type: 'ralph', targetId: loop.id })
  }

  const progress = loop.currentIteration / loop.maxIterations

  return (
    <g transform={`translate(${loop.position.x}, ${loop.position.y})`} onClick={handleClick} onContextMenu={handleContextMenu} style={{ cursor: 'pointer' }}>
      {/* Orbit circle */}
      <circle cx={0} cy={0} r={radius} fill="none" stroke="#333" strokeWidth={1} strokeDasharray="4,4" />
      
      {/* Selection ring */}
      {isSelected && <circle cx={0} cy={0} r={radius + 10} fill="none" stroke="#ff00ff" strokeWidth={2} />}
      
      {/* Central bead */}
      <rect x={-50} y={-35} width={100} height={70} rx={8} fill="#1a1a2e" stroke="#ff00ff" strokeWidth={2} />
      <text x={0} y={-10} textAnchor="middle" fill="#ff00ff" fontSize={10} fontFamily="monospace">RALPH</text>
      <text x={0} y={8} textAnchor="middle" fill="#e0e0e0" fontSize={11} fontFamily="monospace">
        {loop.prompt.length > 12 ? loop.prompt.slice(0, 12) + '…' : loop.prompt}
      </text>
      <text x={0} y={25} textAnchor="middle" fill="#888" fontSize={10} fontFamily="monospace">
        {loop.currentIteration}/{loop.maxIterations}
      </text>
      
      {/* Orbiting agents */}
      {loop.agentQueue.map((agentId, i) => {
        const angle = (i / loop.agentQueue.length) * Math.PI * 2 - Math.PI / 2
        const ax = Math.cos(angle) * radius
        const ay = Math.sin(angle) * radius
        const agent = agentsOnMap.find(a => a.id === agentId)
        const isActive = i === loop.activeAgentIndex
        
        return (
          <g key={agentId} transform={`translate(${ax}, ${ay})`}>
            <circle cx={0} cy={0} r={16} fill={isActive ? '#00ff88' : '#333'} opacity={isActive ? 1 : 0.5} />
            <text x={0} y={5} textAnchor="middle" fontSize={14}>{agent?.icon || '👤'}</text>
            {isActive && <text x={0} y={28} textAnchor="middle" fill="#00ff88" fontSize={8} fontFamily="monospace">ACTIVE</text>}
          </g>
        )
      })}
      
      {/* Progress bar */}
      <rect x={-40} y={40} width={80} height={6} rx={3} fill="#1a1a2e" />
      <rect x={-40} y={40} width={80 * progress} height={6} rx={3} fill="#ff00ff" />
      
      {/* Quality score */}
      {loop.qualityScore !== null && (
        <text x={0} y={58} textAnchor="middle" fill="#888" fontSize={9} fontFamily="monospace">
          Quality: {loop.qualityScore}/10
        </text>
      )}
    </g>
  )
}
