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
  const [term, setTerm] = useState<Terminal | null>(null)
  const [input, setInput] = useState('')
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!termRef.current) return

    const terminal = new Terminal({
      theme: { background: '#1a1a2e', foreground: '#eee' },
      fontSize: 13,
      fontFamily: 'monospace',
      cursorBlink: true,
      convertEol: true
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(termRef.current)
    fitAddon.fit()
    setTerm(terminal)

    // Load initial output
    api.sessions.output(sessionId).then(r => {
      terminal.write(r.output || '')
      onStatusChange?.(r.status)
    }).catch(() => {})

    // Connect WebSocket for live updates
    wsRef.current = createTerminalStream(sessionId, (data) => {
      if (data.type === 'output') {
        terminal.write(data.data)
        onStatusChange?.(data.status)
      }
    })

    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)

    return () => {
      wsRef.current?.close()
      terminal.dispose()
      window.removeEventListener('resize', handleResize)
    }
  }, [sessionId])

  const sendInput = () => {
    if (!input.trim()) return
    api.sessions.input(sessionId, input)
    term?.write(`\r\n> ${input}\r\n`)
    setInput('')
  }

  return (
    <div className="flex flex-col h-full">
      <div ref={termRef} className="flex-1 min-h-0" />
      <div className="flex gap-2 p-2 bg-gray-900 border-t border-gray-700">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && sendInput()}
          placeholder="Send input to agent..."
          className="flex-1 px-3 py-1 bg-gray-800 border border-gray-600 rounded text-sm"
        />
        <button onClick={sendInput} className="px-3 py-1 bg-blue-600 rounded text-sm">Send</button>
      </div>
    </div>
  )
}
