import { useEffect, useRef, useState } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { api } from '../api'
import { Maximize2, Minimize2 } from 'lucide-react'

interface Props {
  sessionId: string
  onStatusChange?: (status: string, contextChars?: number) => void
}

export function TerminalView({ sessionId, onStatusChange }: Props) {
  const termRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<Terminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const totalCharsRef = useRef<number>(0)
  const inputBufferRef = useRef<string>('')
  const [connected, setConnected] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)

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
    fitAddonRef.current = fitAddon
    terminal.loadAddon(fitAddon)
    terminal.open(termRef.current)
    fitAddon.fit()
    terminalRef.current = terminal

    // Buffer input, show locally, send all on Enter
    terminal.onData((data) => {
      if (data === '\r') {
        // Enter: send buffered text + Enter key to tmux
        const toSend = inputBufferRef.current + '\r'
        console.log('Sending to tmux:', JSON.stringify(toSend), 'sessionId:', sessionId)
        inputBufferRef.current = ''
        terminal.write('\r\n')
        api.sessions.input(sessionId, toSend, true)
          .then(() => console.log('Send success'))
          .catch((e) => console.error('Send failed:', e))
      } else if (data === '\x7f' || data === '\b') {
        // Backspace: remove from buffer, erase from display
        if (inputBufferRef.current.length > 0) {
          inputBufferRef.current = inputBufferRef.current.slice(0, -1)
          terminal.write('\b \b')
        }
      } else if (data === '\x03') {
        // Ctrl+C: send immediately to interrupt
        inputBufferRef.current = ''
        api.sessions.input(sessionId, '\x03', true).catch(console.error)
      } else if (data >= ' ' || data === '\t') {
        // Printable: buffer and echo locally
        inputBufferRef.current += data
        terminal.write(data)
      }
    })

    // Handle paste
    terminal.attachCustomKeyEventHandler((e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'C') return false
      if ((e.ctrlKey && e.shiftKey && e.key === 'V') || (e.ctrlKey && e.key === 'v')) {
        if (e.type === 'keydown') {
          navigator.clipboard.readText().then(text => {
            if (text) api.sessions.input(sessionId, text, true).catch(console.error)
          }).catch(() => {})
        }
        return false
      }
      return true
    })

    // Load initial output
    api.sessions.output(sessionId).then(r => {
      const output = r.output || ''
      terminal.write(output)
      totalCharsRef.current = output.length
      onStatusChange?.(r.status || 'IDLE', totalCharsRef.current)
    }).catch(() => onStatusChange?.('IDLE', 0))

    // WebSocket for live streaming output only
    const connectWs = () => {
      const ws = new WebSocket(`ws://${location.host}/api/v2/sessions/${sessionId}/stream`)
      wsRef.current = ws
      
      ws.onopen = () => setConnected(true)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'output' && msg.data) {
            terminal.write(msg.data)
            totalCharsRef.current += msg.data.length
            if (msg.status) onStatusChange?.(msg.status, totalCharsRef.current)
          }
        } catch {}
      }
      ws.onclose = () => { setConnected(false); setTimeout(connectWs, 1000) }
      ws.onerror = () => { setConnected(false); onStatusChange?.('ERROR', totalCharsRef.current) }
    }
    connectWs()

    const handleResize = () => fitAddon.fit()
    window.addEventListener('resize', handleResize)
    setTimeout(() => terminal.focus(), 100)

    return () => {
      wsRef.current?.close()
      terminal.dispose()
      window.removeEventListener('resize', handleResize)
    }
  }, [sessionId])

  useEffect(() => {
    setTimeout(() => fitAddonRef.current?.fit(), 50)
  }, [fullscreen])

  return (
    <div className={`flex flex-col ${fullscreen ? 'fixed inset-0 z-50 bg-gray-950' : 'h-full'}`}>
      <div className="flex items-center gap-2 px-2 py-1 bg-gray-900 border-b border-gray-700 text-xs">
        <button
          onClick={() => setFullscreen(!fullscreen)}
          className="p-1 hover:bg-gray-700 rounded text-gray-400 hover:text-white"
        >
          {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-gray-400">{sessionId.slice(-8)}</span>
        <span className="text-gray-500 ml-auto">Type directly • Ctrl+Shift+V to paste</span>
      </div>
      <div 
        ref={termRef} 
        className="flex-1 min-h-0" 
        onClick={() => terminalRef.current?.focus()}
      />
    </div>
  )
}
