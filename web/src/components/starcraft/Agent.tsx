import { useState, useRef } from 'react'
import { useStarCraftStore, AgentOnMap } from '../../stores/starcraftStore'
import { api } from '../../api'

interface Props { agent: AgentOnMap }

export function Agent({ agent }: Props) {
  const { selectedId, selectItem, openTerminal, showContextMenu, setHovered, assignBead, moveAgent, zoom } = useStarCraftStore()
  const [isDragOver, setIsDragOver] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const dragStart = useRef<{ x: number; y: number; agentX: number; agentY: number } | null>(null)
  const isSelected = selectedId === agent.id

  const statusColors: Record<string, string> = {
    IDLE: '#00ff88',
    PROCESSING: '#00d4ff',
    WAITING_INPUT: '#ffcc00',
    ERROR: '#ff4444'
  }

  const statusIcons: Record<string, string> = {
    IDLE: '○',
    PROCESSING: '●',
    WAITING_INPUT: '❓',
    ERROR: '⚠️'
  }

  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return
    e.stopPropagation()
    dragStart.current = { x: e.clientX, y: e.clientY, agentX: agent.position.x, agentY: agent.position.y }
    setIsDragging(true)
    
    const handleMouseMove = (ev: MouseEvent) => {
      if (!dragStart.current) return
      const dx = (ev.clientX - dragStart.current.x) / zoom
      const dy = (ev.clientY - dragStart.current.y) / zoom
      moveAgent(agent.id, { x: dragStart.current.agentX + dx, y: dragStart.current.agentY + dy })
    }
    
    const handleMouseUp = () => {
      setIsDragging(false)
      if (dragStart.current) {
        api.sessions.updatePosition(agent.id, agent.position.x, agent.position.y).catch(() => {})
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
    if (!isDragging) selectItem(agent.id, 'agent')
  }

  const handleDoubleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    openTerminal(agent.id)
  }

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    showContextMenu({ x: e.clientX, y: e.clientY, type: 'agent', targetId: agent.id })
  }

  const color = statusColors[agent.status]
  const icon = statusIcons[agent.status]
  const isStuck = agent.status === 'WAITING_INPUT'
  const isError = agent.status === 'ERROR'
  const isProcessing = agent.status === 'PROCESSING'

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragOver(true)
  }

  const handleDragLeave = () => setIsDragOver(false)

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragOver(false)
    const beadId = e.dataTransfer.getData('beadId')
    if (beadId) assignBead(beadId, agent.id)
  }

  return (
    <g
      transform={`translate(${agent.position.x}, ${agent.position.y})`}
      onMouseDown={handleMouseDown}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      onContextMenu={handleContextMenu}
      onMouseEnter={() => setHovered(agent.id)}
      onMouseLeave={() => setHovered(null)}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      style={{ cursor: isDragging ? 'grabbing' : 'grab' }}
    >
      {/* Selection ring */}
      {(isSelected || isDragOver) && (
        <circle cx="24" cy="24" r="32" fill="none" stroke={isDragOver ? '#00ff88' : agent.color} strokeWidth="2" strokeDasharray={isDragOver ? undefined : '4,2'} />
      )}
      
      {/* Glow effect */}
      {(isProcessing || isStuck || isError) && (
        <circle cx="24" cy="24" r="28" fill={color} opacity="0.15">
          <animate attributeName="opacity" values="0.15;0.3;0.15" dur={isProcessing ? '1s' : '0.5s'} repeatCount="indefinite" />
        </circle>
      )}
      
      {/* Icon background */}
      <rect x="0" y="0" width="48" height="48" rx="8" fill="#1a1a2e" stroke={isSelected ? agent.color : '#333'} strokeWidth="2" />
      
      {/* Agent icon */}
      <text x="24" y="32" textAnchor="middle" fontSize="24">{agent.icon}</text>
      
      {/* Agent name */}
      <text x="24" y="68" textAnchor="middle" fill="#e0e0e0" fontSize="11" fontFamily="monospace">
        {agent.name.length > 12 ? agent.name.slice(0, 12) + '…' : agent.name}
      </text>
      
      {/* Status badge */}
      <g transform="translate(-6, 78)">
        <rect x="0" y="0" width="60" height="18" rx="4" fill={color} opacity="0.2" />
        <text x="30" y="13" textAnchor="middle" fill={color} fontSize="10" fontFamily="monospace">
          {icon} {agent.status.slice(0, 4)}
        </text>
      </g>
      
      {/* Stuck indicator */}
      {isStuck && (
        <g transform="translate(14, -30)">
          <text fontSize="20" textAnchor="middle">
            <animate attributeName="y" values="0;-5;0" dur="0.5s" repeatCount="indefinite" />
            ❓
          </text>
        </g>
      )}
      
      {/* Error flash */}
      {isError && (
        <rect x="-4" y="-4" width="56" height="56" rx="10" fill="none" stroke="#ff4444" strokeWidth="3">
          <animate attributeName="opacity" values="1;0;1" dur="0.3s" repeatCount="indefinite" />
        </rect>
      )}
    </g>
  )
}
