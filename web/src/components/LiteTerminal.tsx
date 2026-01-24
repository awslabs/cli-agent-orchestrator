import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Maximize2, Minimize2 } from 'lucide-react'

interface Props {
  sessionId: string
  onStatusChange?: (status: string) => void
}

const stripAnsi = (str: string) => str.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '')

export function LiteTerminal({ sessionId, onStatusChange }: Props) {
  const [output, setOutput] = useState('')
  const [fullscreen, setFullscreen] = useState(false)
  const [connected, setConnected] = useState(false)
  const outputRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    // Load initial output
    api.sessions.output(sessionId).then(r => {
      setOutput(stripAnsi(r.output || ''))
      onStatusChange?.(r.status || 'IDLE')
    }).catch(() => {})

    // WebSocket for live updates
    const connectWs = () => {
      const ws = new WebSocket(`ws://${location.host}/api/v2/sessions/${sessionId}/stream`)
      wsRef.current = ws
      ws.onopen = () => setConnected(true)
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'output' && msg.data) {
            setOutput(prev => prev + stripAnsi(msg.data))
            if (msg.status) onStatusChange?.(msg.status)
          }
        } catch {}
      }
      ws.onclose = () => { setConnected(false); setTimeout(connectWs, 1000) }
      ws.onerror = () => setConnected(false)
    }
    connectWs()
    return () => wsRef.current?.close()
  }, [sessionId])

  // Auto-scroll
  useEffect(() => {
    if (outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight
  }, [output])

  return (
    <div className={`flex flex-col ${fullscreen ? 'fixed inset-0 z-50 bg-gray-950' : 'h-80'}`}>
      <div className="flex items-center gap-2 px-2 py-1 bg-gray-900 border-b border-gray-700 text-xs shrink-0">
        <button onClick={() => setFullscreen(!fullscreen)} className="p-1 hover:bg-gray-700 rounded text-gray-400 hover:text-white">
          {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-gray-400">{sessionId.slice(-8)}</span>
        <span className="text-emerald-400 ml-auto">Lite Mode</span>
      </div>
      <div
        ref={outputRef}
        className="flex-1 overflow-auto p-2 bg-gray-950 text-gray-300 text-sm font-mono whitespace-pre-wrap break-words"
      >
        {output || 'Connecting...'}
      </div>
    </div>
  )
}
