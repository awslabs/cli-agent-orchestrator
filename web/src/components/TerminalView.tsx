import { useEffect, useRef, useState } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { api } from '../api'

interface Props {
  sessionId: string
  onStatusChange?: (status: string) => void
}

export function TerminalView({ sessionId, onStatusChange }: Props) {
  const termRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const lastOutputRef = useRef<string>('')
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!termRef.current) return

    const terminal = new Terminal({
      theme: { background: '#1a1a2e', foreground: '#eee' },
      fontSize: 13,
      fontFamily: 'monospace',
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000,
      allowProposedApi: true
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(termRef.current)
    fitAddon.fit()
    terminalRef.current = terminal

    // Handle keyboard input - send raw keystrokes to tmux
    terminal.onData((data) => {
      api.sessions.input(sessionId, data, true).catch(console.error)
    })

    // Handle paste from clipboard
    terminal.attachCustomKeyEventHandler((e) => {
      // Allow Ctrl+Shift+C for copy (browser default)
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        return false // Let browser handle copy
      }
      // Handle Ctrl+Shift+V or Ctrl+V for paste
      if ((e.ctrlKey && e.shiftKey && e.key === 'V') || (e.ctrlKey && e.key === 'v')) {
        if (e.type === 'keydown') {
          navigator.clipboard.readText().then(text => {
            if (text) api.sessions.input(sessionId, text, true).catch(console.error)
          }).catch(() => {})
        }
        return false
      }
      return true // Let xterm handle other keys
    })

    // Load initial output then connect WebSocket
    api.sessions.output(sessionId).then(r => {
      const output = r.output || ''
      terminal.write(output)
      lastOutputRef.current = output
      onStatusChange?.(r.status)
    }).catch(() => {})

    // WebSocket for live streaming
    const connectWs = () => {
      const ws = new WebSocket(`ws://${location.host}/api/v2/sessions/${sessionId}/stream`)
      wsRef.current = ws
      
      ws.onopen = () => setConnected(true)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'output' && msg.data) {
            terminal.write(msg.data)
            onStatusChange?.(msg.status)
          }
        } catch {}
      }
      ws.onclose = () => {
        setConnected(false)
        setTimeout(connectWs, 1000)
      }
      ws.onerror = () => setConnected(false)
    }
    connectWs()

    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)

    // Focus terminal on mount
    setTimeout(() => terminal.focus(), 100)

    return () => {
      wsRef.current?.close()
      terminal.dispose()
      window.removeEventListener('resize', handleResize)
    }
  }, [sessionId])

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-2 py-1 bg-gray-900 border-b border-gray-700 text-xs">
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-gray-400">{sessionId.slice(-8)}</span>
        <span className="text-gray-500 ml-auto">Ctrl+Shift+V to paste</span>
      </div>
      <div 
        ref={termRef} 
        className="flex-1 min-h-0" 
        onClick={() => terminalRef.current?.focus()}
      />
    </div>
  )
}
