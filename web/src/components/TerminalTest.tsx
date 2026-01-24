import { useEffect, useRef, useState } from 'react'

const SESSION_ID = 'cao-2750634c' // Single test session

export function TerminalTest() {
  const [output, setOutput] = useState('')
  const [input, setInput] = useState('')
  const [status, setStatus] = useState('DISCONNECTED')
  const [latency, setLatency] = useState(0)
  const outputRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const lastSendTime = useRef(0)

  useEffect(() => {
    const ws = new WebSocket(`ws://${location.host}/api/v2/sessions/${SESSION_ID}/stream`)
    wsRef.current = ws
    
    ws.onopen = () => setStatus('CONNECTED')
    ws.onclose = () => setStatus('DISCONNECTED')
    ws.onerror = () => setStatus('ERROR')
    
    ws.onmessage = (e) => {
      const now = Date.now()
      if (lastSendTime.current) {
        setLatency(now - lastSendTime.current)
      }
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'output') {
          // Replace full output instead of appending
          setOutput(msg.data.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, ''))
          setStatus(msg.status || 'CONNECTED')
        }
      } catch {}
    }
    
    return () => ws.close()
  }, [])

  // Auto-scroll
  useEffect(() => {
    if (outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight
  }, [output])

  const sendInput = async () => {
    if (!input.trim()) return
    lastSendTime.current = Date.now()
    await fetch(`/api/v2/sessions/${SESSION_ID}/input?message=${encodeURIComponent(input)}`, { method: 'POST' })
    setInput('')
  }

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-white p-4">
      <div className="flex items-center gap-4 mb-2 text-sm">
        <span className={`px-2 py-1 rounded ${status === 'CONNECTED' ? 'bg-green-700' : status === 'WAITING_INPUT' ? 'bg-yellow-700' : 'bg-red-700'}`}>
          {status}
        </span>
        <span className="text-gray-400">Session: {SESSION_ID}</span>
        <span className="text-gray-400">Latency: {latency}ms</span>
      </div>
      
      <div ref={outputRef} className="flex-1 overflow-auto bg-black p-3 rounded font-mono text-sm whitespace-pre-wrap mb-2">
        {output || 'Waiting for output...'}
      </div>
      
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && sendInput()}
          placeholder="Type command and press Enter..."
          className="flex-1 bg-gray-800 px-3 py-2 rounded text-white"
        />
        <button onClick={sendInput} className="px-4 py-2 bg-blue-600 rounded hover:bg-blue-500">
          Send
        </button>
      </div>
    </div>
  )
}
