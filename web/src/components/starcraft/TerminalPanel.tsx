import { useEffect, useRef, useState } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { useStarCraftStore } from '../../stores/starcraftStore'
import 'xterm/css/xterm.css'

export function TerminalPanel() {
  const { terminalAgentId, agentsOnMap, beadsOnMap, closeTerminal } = useStarCraftStore()
  const termRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<Terminal | null>(null)
  const [input, setInput] = useState('')

  const agent = agentsOnMap.find(a => a.id === terminalAgentId)
  const bead = agent?.assignedBeadId ? beadsOnMap.find(b => b.id === agent.assignedBeadId) : null

  useEffect(() => {
    if (!termRef.current || xtermRef.current) return

    const term = new Terminal({
      theme: { background: '#0a0a0f', foreground: '#e0e0e0', cursor: '#00ff88', cursorAccent: '#0a0a0f' },
      fontFamily: 'JetBrains Mono, Fira Code, monospace',
      fontSize: 13,
      cursorBlink: true,
      scrollback: 5000
    })
    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(termRef.current)
    fitAddon.fit()
    xtermRef.current = term

    // Demo output
    term.writeln('\x1b[32m$ Agent session started\x1b[0m')
    term.writeln(`\x1b[36m> Connected to ${agent?.name || 'agent'}\x1b[0m`)
    term.writeln('')

    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      term.dispose()
      xtermRef.current = null
    }
  }, [agent?.name])

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeTerminal()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [closeTerminal])

  const handleSend = () => {
    if (!input.trim() || !xtermRef.current) return
    xtermRef.current.writeln(`\x1b[33m> ${input}\x1b[0m`)
    setInput('')
  }

  if (!agent) return null

  const statusColors: Record<string, string> = {
    IDLE: '#00ff88', PROCESSING: '#00d4ff', WAITING_INPUT: '#ffcc00', ERROR: '#ff4444'
  }

  return (
    <div className="absolute right-0 top-0 h-full flex flex-col" style={{ width: 450, background: '#0a0a0f', borderLeft: '1px solid #1a1a2e' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b" style={{ borderColor: '#1a1a2e' }}>
        <div className="flex items-center gap-2">
          <span className="text-xl">{agent.icon}</span>
          <span className="font-bold" style={{ color: '#e0e0e0' }}>{agent.name}</span>
        </div>
        <button onClick={closeTerminal} className="text-gray-500 hover:text-white text-xl">&times;</button>
      </div>

      {/* Status bar */}
      <div className="px-4 py-2 text-xs border-b" style={{ borderColor: '#1a1a2e', color: '#888' }}>
        <div>Status: <span style={{ color: statusColors[agent.status] }}>● {agent.status}</span></div>
        {bead && <div>Bead: "{bead.title}" (P{bead.priority})</div>}
      </div>

      {/* Terminal */}
      <div ref={termRef} className="flex-1 p-2" />

      {/* Input */}
      <div className="flex border-t" style={{ borderColor: '#1a1a2e' }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSend()}
          placeholder="Type command..."
          className="flex-1 px-4 py-2 bg-transparent outline-none"
          style={{ color: '#e0e0e0', fontFamily: 'monospace' }}
        />
        <button onClick={handleSend} className="px-4 py-2" style={{ color: '#00ff88' }}>Send</button>
      </div>
    </div>
  )
}
