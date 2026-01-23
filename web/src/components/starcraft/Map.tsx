import { useRef, useEffect, useCallback } from 'react'
import { useStarCraftStore } from '../../stores/starcraftStore'
import { Agent } from './Agent'
import { Bead } from './Bead'
import { AssignmentLine } from './AssignmentLine'
import { RalphLoop } from './RalphLoop'
import { Minimap } from './Minimap'

const GRID_SIZE = 20

export function Map() {
  const svgRef = useRef<SVGSVGElement>(null)
  const isDragging = useRef(false)
  const lastPos = useRef({ x: 0, y: 0 })
  
  const { zoom, panX, panY, setZoom, pan, agentsOnMap, beadsOnMap, ralphLoops, selectItem, showContextMenu, hideContextMenu } = useStarCraftStore()

  // Pan via drag
  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button === 0 && e.target === svgRef.current) {
      isDragging.current = true
      lastPos.current = { x: e.clientX, y: e.clientY }
    }
  }

  const handleMouseMove = (e: React.MouseEvent) => {
    if (isDragging.current) {
      const dx = e.clientX - lastPos.current.x
      const dy = e.clientY - lastPos.current.y
      pan(dx, dy)
      lastPos.current = { x: e.clientX, y: e.clientY }
    }
  }

  const handleMouseUp = () => { isDragging.current = false }

  // Zoom via wheel
  const handleWheel = useCallback((e: WheelEvent) => {
    e.preventDefault()
    const delta = e.deltaY > 0 ? -0.1 : 0.1
    setZoom(zoom + delta)
  }, [zoom, setZoom])

  // Keyboard controls
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      
      switch (e.key) {
        case 'ArrowUp': pan(0, 50); break
        case 'ArrowDown': pan(0, -50); break
        case 'ArrowLeft': pan(50, 0); break
        case 'ArrowRight': pan(-50, 0); break
        case '+': case '=': setZoom(zoom + 0.1); break
        case '-': setZoom(zoom - 0.1); break
        case '0': setZoom(1); break
        case 'Escape': selectItem(null, null); hideContextMenu(); break
      }
    }
    
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [zoom, pan, setZoom, selectItem, hideContextMenu])

  // Wheel listener
  useEffect(() => {
    const svg = svgRef.current
    if (svg) {
      svg.addEventListener('wheel', handleWheel, { passive: false })
      return () => svg.removeEventListener('wheel', handleWheel)
    }
  }, [handleWheel])

  // Context menu
  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    if (e.target === svgRef.current) {
      showContextMenu({ x: e.clientX, y: e.clientY, type: 'empty', targetId: null })
    }
  }

  // Click empty to deselect
  const handleClick = (e: React.MouseEvent) => {
    if (e.target === svgRef.current) {
      selectItem(null, null)
      hideContextMenu()
    }
  }

  // Grid pattern
  const gridOpacity = zoom > 0.5 ? 0.1 : 0

  return (
    <svg
      ref={svgRef}
      className="w-full h-full cursor-grab active:cursor-grabbing"
      style={{ background: '#0a0a0f' }}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
      onContextMenu={handleContextMenu}
      onClick={handleClick}
    >
      <defs>
        <pattern id="grid" width={GRID_SIZE} height={GRID_SIZE} patternUnits="userSpaceOnUse">
          <path d={`M ${GRID_SIZE} 0 L 0 0 0 ${GRID_SIZE}`} fill="none" stroke="#1a1a2e" strokeWidth="0.5" />
        </pattern>
      </defs>
      
      <g transform={`translate(${panX}, ${panY}) scale(${zoom})`}>
        {/* Grid */}
        <rect x="-5000" y="-5000" width="10000" height="10000" fill="url(#grid)" opacity={gridOpacity} />
        
        {/* Assignment lines */}
        {agentsOnMap.map(agent => {
          const bead = beadsOnMap.find(b => b.id === agent.assignedBeadId)
          return bead?.position ? (
            <AssignmentLine key={`line-${agent.id}`} agent={agent} bead={bead} />
          ) : null
        })}
        
        {/* Beads */}
        {beadsOnMap.map(bead => bead.position && <Bead key={bead.id} bead={bead} />)}
        
        {/* Ralph Loops */}
        {ralphLoops.map(loop => <RalphLoop key={loop.id} loop={loop} />)}
        
        {/* Agents */}
        {agentsOnMap.map(agent => <Agent key={agent.id} agent={agent} />)}
      </g>
      
      {/* Zoom indicator */}
      <text x="20" y="30" fill="#666" fontSize="12" fontFamily="monospace">
        {Math.round(zoom * 100)}%
      </text>
    </svg>
  )
}
