import { useEffect, useRef, useState } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { createTerminalStream, api } from '../api'

interface Props {
  sessionId: string
  onStatusChange?: (status: string) => void
}

export function TerminalView({ sessionId, onStatusChange }: Props) {
  const termRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<Terminal | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [input, setInput] = useState('')
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!termRef.current) return

    const terminal = new Terminal({
      theme: { background: '#1a1a2e', foreground: '#eee' },
      fontSize: 13,
      fontFamily: 'monospace',
      cursorBlink: true,
      convertEol: true,
      scrollback: 5000
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(termRef.current)
    fitAddon.fit()
    terminalRef.current = terminal

    // Handle direct keyboard input to terminal
    terminal.onData((data) => {
      // Send each keystroke in raw mode (no Enter added)
      api.sessions.input(sessionId, data, true).catch(console.error)
    })

    // Load initial output
    api.sessions.output(sessionId).then(r => {
      terminal.write(r.output || '')
      onStatusChange?.(r.status)
    }).catch(() => {})

    // Connect WebSocket for live updates
    const connectWs = () => {
      wsRef.current = createTerminalStream(sessionId, (data) => {
        if (data.type === 'output') {
          terminal.write(data.data)
          onStatusChange?.(data.status)
        }
      })
      wsRef.current.onopen = () => setConnected(true)
      wsRef.current.onclose = () => {
        setConnected(false)
        // Reconnect after 2s
        setTimeout(connectWs, 2000)
      }
    }
    connectWs()

    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)

    return () => {
      wsRef.current?.close()
      terminal.dispose()
      window.removeEventListener('resize', handleResize)
    }
  }, [sessionId])

  const sendInput = () => {
    if (!input) return
    // Backend already sends Enter key after the message
    api.sessions.input(sessionId, input).catch(console.error)
    setInput('')
  }

  const focusTerminal = () => {
    terminalRef.current?.focus()
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-2 py-1 bg-gray-900 border-b border-gray-700 text-xs">
        <span className={connected ? 'text-green-400' : 'text-red-400'}>●</span>
        <span className="text-gray-400">{sessionId.slice(-8)}</span>
        <button onClick={focusTerminal} className="ml-auto px-2 py-0.5 bg-gray-700 rounded hover:bg-gray-600">
          Focus Terminal
        </button>
      </div>
      <div 
        ref={termRef} 
        className="flex-1 min-h-0 cursor-text" 
        onClick={focusTerminal}
      />
      <div className="flex gap-2 p-2 bg-gray-900 border-t border-gray-700">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              sendInput()
            }
          }}
          placeholder="Type command and press Enter..."
          className="flex-1 px-3 py-1 bg-gray-800 border border-gray-600 rounded text-sm font-mono"
        />
        <button onClick={sendInput} className="px-3 py-1 bg-blue-600 rounded text-sm">
          Send
        </button>
      </div>
    </div>
  )
}
