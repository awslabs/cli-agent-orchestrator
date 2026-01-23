import { useStarCraftStore } from '../../stores/starcraftStore'

export function Minimap() {
  const { agentsOnMap, beadsOnMap, ralphLoops, panX, panY, zoom, setPan } = useStarCraftStore()

  const width = 150, height = 100
  const scale = 0.02 // Scale factor for minimap

  const handleClick = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = (e.clientX - rect.left - width / 2) / scale
    const y = (e.clientY - rect.top - height / 2) / scale
    setPan(-x, -y)
  }

  const statusColors: Record<string, string> = {
    IDLE: '#00ff88', PROCESSING: '#00d4ff', WAITING_INPUT: '#ffcc00', ERROR: '#ff4444'
  }
  const priorityColors: Record<number, string> = { 1: '#ff4444', 2: '#ffcc00', 3: '#666' }

  return (
    <div className="absolute bottom-5 left-5 rounded border" style={{ background: 'rgba(10,10,15,0.8)', borderColor: '#333' }}>
      <svg width={width} height={height} onClick={handleClick} style={{ cursor: 'pointer' }}>
        {/* Agents as dots */}
        {agentsOnMap.map(a => (
          <circle key={a.id} cx={width/2 + a.position.x * scale} cy={height/2 + a.position.y * scale} r={4} fill={statusColors[a.status]} />
        ))}
        {/* Beads as squares */}
        {beadsOnMap.map(b => b.position && (
          <rect key={b.id} x={width/2 + b.position.x * scale - 3} y={height/2 + b.position.y * scale - 3} width={6} height={6} fill={priorityColors[b.priority]} />
        ))}
        {/* Ralph loops as circles */}
        {ralphLoops.map(r => (
          <circle key={r.id} cx={width/2 + r.position.x * scale} cy={height/2 + r.position.y * scale} r={6} fill="none" stroke="#ff00ff" strokeWidth={1} />
        ))}
        {/* Viewport rectangle */}
        <rect x={width/2 - panX * scale - 30} y={height/2 - panY * scale - 20} width={60} height={40} fill="rgba(255,255,255,0.1)" stroke="white" strokeWidth={1} />
      </svg>
    </div>
  )
}
