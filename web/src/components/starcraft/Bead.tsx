import { useState, useRef } from 'react'
import { useStarCraftStore, BeadOnMap } from '../../stores/starcraftStore'
import { api } from '../../api'

interface Props { bead: BeadOnMap }

const SIZES = { 1: { w: 120, h: 80, font: 14 }, 2: { w: 100, h: 65, font: 13 }, 3: { w: 80, h: 50, font: 12 } }
const COLORS = { 1: { bg: '#2a0a0a', border: '#ff4444' }, 2: { bg: '#2a2a0a', border: '#ffcc00' }, 3: { bg: '#1a1a1a', border: '#666666' } }

export function Bead({ bead }: Props) {
  const { selectedId, selectItem, showContextMenu, setHovered, moveBead, zoom } = useStarCraftStore()
  const [isDragging, setIsDragging] = useState(false)
  const dragStart = useRef<{ x: number; y: number; beadX: number; beadY: number } | null>(null)
  const isSelected = selectedId === bead.id
  
  if (!bead.position) return null
  
  const size = SIZES[bead.priority]
  const color = COLORS[bead.priority]
  const isWip = bead.status === 'wip'
  const isClosed = bead.status === 'closed'

  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return
    e.stopPropagation()
    dragStart.current = { x: e.clientX, y: e.clientY, beadX: bead.position!.x, beadY: bead.position!.y }
    setIsDragging(true)
    
    const handleMouseMove = (ev: MouseEvent) => {
      if (!dragStart.current) return
      const dx = (ev.clientX - dragStart.current.x) / zoom
      const dy = (ev.clientY - dragStart.current.y) / zoom
      moveBead(bead.id, { x: dragStart.current.beadX + dx, y: dragStart.current.beadY + dy })
    }
    
    const handleMouseUp = () => {
      setIsDragging(false)
      if (dragStart.current) {
        api.tasks.updatePosition(bead.id, bead.position!.x, bead.position!.y).catch(() => {})
      }
      dragStart.current = null
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
    
    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
  }

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!isDragging) selectItem(bead.id, 'bead')
  }

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    showContextMenu({ x: e.clientX, y: e.clientY, type: 'bead', targetId: bead.id })
  }

  return (
    <g
      transform={`translate(${bead.position.x}, ${bead.position.y})`}
      onMouseDown={handleMouseDown}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
      onMouseEnter={() => setHovered(bead.id)}
      onMouseLeave={() => setHovered(null)}
      style={{ cursor: isDragging ? 'grabbing' : 'grab' }}
      opacity={isClosed ? 0.5 : 1}
    >
      {/* Glow for P1/P2 */}
      {bead.priority < 3 && (
        <rect x="-4" y="-4" width={size.w + 8} height={size.h + 8} rx="8" fill={color.border} opacity="0.1" />
      )}
      
      {/* Main rect */}
      <rect
        x="0" y="0" width={size.w} height={size.h} rx="6"
        fill={color.bg} stroke={isSelected ? '#00ff88' : color.border} strokeWidth={isSelected ? 2 : 1}
      >
        {isWip && <animate attributeName="stroke-opacity" values="1;0.5;1" dur="1s" repeatCount="indefinite" />}
      </rect>
      
      {/* Priority badge */}
      <text x="8" y="16" fill={color.border} fontSize="10" fontFamily="monospace" fontWeight="bold">P{bead.priority}</text>
      
      {/* Title */}
      <text x={size.w / 2} y={size.h / 2 + 4} textAnchor="middle" fill="#e0e0e0" fontSize={size.font} fontFamily="monospace">
        {bead.title.length > 14 ? bead.title.slice(0, 14) + '…' : bead.title}
      </text>
      
      {/* Closed checkmark */}
      {isClosed && <text x={size.w - 16} y="16" fontSize="12">✅</text>}
      
      {/* Orphaned warning */}
      {bead.isOrphaned && <text x={size.w - 16} y="16" fontSize="12">⚠️</text>}
    </g>
  )
}
