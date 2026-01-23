import { useEffect, useState, useCallback } from 'react'
import { useStarCraftStore, getAgentIcon, getAgentColor } from '../../stores/starcraftStore'
import { api, createActivityStream } from '../../api'
import { Map } from './Map'
import { ContextMenu } from './ContextMenu'
import { BeadQueue } from './BeadQueue'
import { TerminalPanel } from './TerminalPanel'
import { Minimap } from './Minimap'
import { HotkeyHelp } from './HotkeyHelp'
import { NewBeadModal } from './NewBeadModal'
import { NewRalphModal } from './NewRalphModal'

export function StarCraftView() {
  const { setAgents, setBeads, hideContextMenu, contextMenu, zoom, setZoom, pan, terminalOpen, closeTerminal, agentsOnMap, beadsOnMap, beadsInQueue, selectItem, openTerminal, selectedId, selectedType, updateAgentStatus } = useStarCraftStore()
  const [showHelp, setShowHelp] = useState(false)
  const [showNewBead, setShowNewBead] = useState(false)
  const [showNewRalph, setShowNewRalph] = useState(false)

  // Load initial data
  useEffect(() => {
    api.sessions.list().then((sessions: any[]) => {
      const agents = sessions.map((s, i) => ({
        id: s.id,
        name: s.name || s.agent || 'agent',
        icon: getAgentIcon(s.agent || s.name || ''),
        status: s.status || 'IDLE',
        position: { x: 100 + (i % 4) * 150, y: 100 + Math.floor(i / 4) * 150 },
        assignedBeadId: null,
        color: getAgentColor(i)
      }))
      setAgents(agents)
    }).catch(() => {})

    api.tasks.list().then((tasks: any[]) => {
      const beads = tasks.map(t => ({
        id: t.id,
        title: t.title,
        priority: (t.priority || 3) as 1 | 2 | 3,
        status: (t.status === 'closed' ? 'closed' : t.status === 'wip' ? 'wip' : 'open') as 'open' | 'wip' | 'closed',
        assigneeId: t.assignee || null,
        position: null,
        isOrphaned: false
      }))
      setBeads(beads)
    }).catch(() => {})
  }, [setAgents, setBeads])

  // WebSocket for real-time activity updates
  useEffect(() => {
    const ws = createActivityStream((data: any) => {
      if (data.type === 'session_status' && data.session_id && data.status) {
        updateAgentStatus(data.session_id, data.status)
      }
    })
    return () => ws.close()
  }, [updateAgentStatus])

  // Close context menu on click outside
  useEffect(() => {
    const handler = () => hideContextMenu()
    if (contextMenu) window.addEventListener('click', handler)
    return () => window.removeEventListener('click', handler)
  }, [contextMenu, hideContextMenu])

  // Delete handler
  const handleDelete = useCallback(() => {
    if (!selectedId || !selectedType) return
    if (selectedType === 'agent') {
      api.sessions.delete(selectedId).then(() => {
        setAgents(agentsOnMap.filter(a => a.id !== selectedId))
        selectItem(null, null)
      }).catch(() => {})
    } else if (selectedType === 'bead') {
      api.tasks.delete(selectedId).then(() => {
        setBeads([...beadsOnMap.filter(b => b.id !== selectedId), ...beadsInQueue.filter(b => b.id !== selectedId)])
        selectItem(null, null)
      }).catch(() => {})
    }
  }, [selectedId, selectedType, agentsOnMap, beadsOnMap, beadsInQueue, setAgents, setBeads, selectItem])

  // Hotkeys
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      
      if (e.key === ' ') {
        e.preventDefault()
        const stuck = agentsOnMap.find(a => a.status === 'WAITING_INPUT')
        if (stuck) { selectItem(stuck.id, 'agent'); openTerminal(stuck.id) }
      } else if (e.key === 'Tab') {
        e.preventDefault()
        const idx = agentsOnMap.findIndex(a => a.id === selectedId)
        const next = agentsOnMap[(idx + (e.shiftKey ? -1 : 1) + agentsOnMap.length) % agentsOnMap.length]
        if (next) selectItem(next.id, 'agent')
      } else if (e.key >= '1' && e.key <= '9') {
        const agent = agentsOnMap[parseInt(e.key) - 1]
        if (agent) selectItem(agent.id, 'agent')
      } else if (e.key === 'Enter' && selectedId) {
        openTerminal(selectedId)
      } else if (e.key === '?' || e.key === 'F1') {
        e.preventDefault()
        setShowHelp(true)
      } else if (e.key === 'n' || e.key === 'N') {
        e.preventDefault()
        setShowNewBead(true)
      } else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault()
        setShowNewRalph(true)
      } else if (e.key === '+' || e.key === '=') {
        e.preventDefault()
        setZoom(zoom + 0.1)
      } else if (e.key === '-') {
        e.preventDefault()
        setZoom(zoom - 0.1)
      } else if (e.key === '0') {
        e.preventDefault()
        setZoom(1)
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        pan(0, 50)
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        pan(0, -50)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        pan(50, 0)
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        pan(-50, 0)
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault()
        handleDelete()
      } else if (e.key === 'Escape') {
        setShowHelp(false)
        setShowNewBead(false)
        setShowNewRalph(false)
        closeTerminal()
        selectItem(null, null)
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [agentsOnMap, selectedId, selectItem, openTerminal, closeTerminal, zoom, setZoom, pan, handleDelete])

  return (
    <div className="flex flex-col h-screen" style={{ background: '#0a0a0f', color: '#e0e0e0', fontFamily: 'JetBrains Mono, Fira Code, monospace' }}>
      <header className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: '#1a1a2e' }}>
        <h1 className="text-lg font-bold tracking-wider" style={{ color: '#00ff88' }}>🎮 CAO COMMAND</h1>
        <span className="text-xs" style={{ color: '#666' }}>StarCraft Mode</span>
      </header>

      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 relative overflow-hidden">
          <Map />
          <Minimap />
          {contextMenu && <ContextMenu />}
          {terminalOpen && <TerminalPanel />}
          <div className="absolute bottom-4 left-48 px-2 py-1 rounded text-xs" style={{ background: '#1a1a2e', color: '#666' }}>
            {Math.round(zoom * 100)}%
          </div>
        </div>
        <BeadQueue />
      </div>
      {showHelp && <HotkeyHelp onClose={() => setShowHelp(false)} />}
      {showNewBead && <NewBeadModal onClose={() => setShowNewBead(false)} />}
      {showNewRalph && <NewRalphModal onClose={() => setShowNewRalph(false)} />}
    </div>
  )
}
