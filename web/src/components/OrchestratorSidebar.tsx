import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Bot, ChevronLeft, ChevronRight, Send, Square, Play, Loader2 } from 'lucide-react'

const PROVIDERS = [
  { value: 'claude_code', label: 'Claude Code' },
  { value: 'kiro_cli', label: 'Kiro CLI' },
  { value: 'q_cli', label: 'Q CLI' },
  { value: 'codex', label: 'Codex' },
]

export function OrchestratorSidebar() {
  const {
    orchestratorRunning, orchestratorSessionId, orchestratorExpanded,
    toggleOrchestratorSidebar, launchOrchestrator, stopOrchestrator, checkOrchestrator,
  } = useStore()

  const [provider, setProvider] = useState('claude_code')
  const [input, setInput] = useState('')
  const [output, setOutput] = useState('')
  const [sending, setSending] = useState(false)
  const [launching, setLaunching] = useState(false)
  const outputRef = useRef<HTMLPreElement>(null)
  const pollRef = useRef<ReturnType<typeof setInterval>>()

  // Check orchestrator status on mount
  useEffect(() => {
    checkOrchestrator()
  }, [])

  // Poll for output when running + expanded
  useEffect(() => {
    if (!orchestratorRunning || !orchestratorSessionId || !orchestratorExpanded) {
      if (pollRef.current) clearInterval(pollRef.current)
      return
    }

    const fetchOutput = async () => {
      try {
        const session = await api.getSession(orchestratorSessionId)
        if (session.terminals?.length > 0) {
          const termId = session.terminals[0].id
          const result = await api.getTerminalOutput(termId)
          setOutput(result.output || '')
        }
      } catch { /* session may have been deleted */ }
    }

    fetchOutput()
    pollRef.current = setInterval(fetchOutput, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [orchestratorRunning, orchestratorSessionId, orchestratorExpanded])

  // Auto-scroll output
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight
    }
  }, [output])

  const handleLaunch = async () => {
    setLaunching(true)
    await launchOrchestrator(provider)
    setLaunching(false)
  }

  const handleSend = async () => {
    if (!input.trim() || !orchestratorSessionId || sending) return
    setSending(true)
    try {
      const session = await api.getSession(orchestratorSessionId)
      if (session.terminals?.length > 0) {
        await api.sendInput(session.terminals[0].id, input.trim())
        setInput('')
      }
    } catch (e) {
      console.error('Failed to send:', e)
    }
    setSending(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Collapsed state — thin strip on right edge
  if (!orchestratorExpanded) {
    return (
      <button
        onClick={toggleOrchestratorSidebar}
        className="fixed right-0 top-1/2 -translate-y-1/2 z-50 flex flex-col items-center gap-2 px-1.5 py-4 rounded-l-lg bg-gray-800/90 border border-r-0 border-gray-700 hover:bg-gray-700/90 transition-all"
        title="Open AI Assistant"
      >
        <ChevronLeft size={14} className="text-gray-400" />
        <Bot size={18} className={orchestratorRunning ? 'text-emerald-400' : 'text-gray-500'} />
        <span className="text-[10px] text-gray-400 [writing-mode:vertical-lr] rotate-180">
          Assistant
        </span>
        {orchestratorRunning && (
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
        )}
      </button>
    )
  }

  // Expanded state
  return (
    <div className="fixed right-0 top-0 bottom-0 w-[420px] z-50 flex flex-col bg-[#0f0f14] border-l border-gray-800 shadow-2xl">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-gray-900/50">
        <div className="flex items-center gap-2">
          <Bot size={18} className={orchestratorRunning ? 'text-emerald-400' : 'text-gray-500'} />
          <span className="text-sm font-semibold text-white">AI Orchestrator</span>
          {orchestratorRunning && (
            <span className="px-2 py-0.5 text-[10px] rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
              Running
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {orchestratorRunning && (
            <button
              onClick={stopOrchestrator}
              className="p-1.5 rounded hover:bg-red-500/20 text-red-400"
              title="Stop orchestrator"
            >
              <Square size={14} />
            </button>
          )}
          <button
            onClick={toggleOrchestratorSidebar}
            className="p-1.5 rounded hover:bg-gray-700 text-gray-400"
            title="Collapse"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      </div>

      {/* Content */}
      {!orchestratorRunning ? (
        // Launch screen
        <div className="flex-1 flex flex-col items-center justify-center gap-4 px-6">
          <div className="w-16 h-16 rounded-full bg-gray-800 flex items-center justify-center">
            <Bot size={32} className="text-gray-500" />
          </div>
          <div className="text-center">
            <p className="text-sm text-gray-300 font-medium">AI Assistant</p>
            <p className="text-xs text-gray-500 mt-1">
              Launch an always-on orchestrator that can manage sessions, flows, and coordinate agents.
            </p>
          </div>
          <div className="flex flex-col gap-2 w-full max-w-xs">
            <select
              value={provider}
              onChange={e => setProvider(e.target.value)}
              className="w-full px-3 py-2 rounded-lg text-sm bg-gray-800 border border-gray-700 text-gray-300 focus:border-emerald-500 focus:outline-none"
            >
              {PROVIDERS.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
            <button
              onClick={handleLaunch}
              disabled={launching}
              className="w-full px-4 py-2.5 rounded-lg text-sm font-medium bg-gradient-to-r from-emerald-600 to-emerald-500 text-white hover:from-emerald-500 hover:to-emerald-400 disabled:opacity-50 flex items-center justify-center gap-2 shadow-lg shadow-emerald-500/20"
            >
              {launching ? (
                <><Loader2 size={16} className="animate-spin" /> Launching...</>
              ) : (
                <><Play size={16} /> Launch Orchestrator</>
              )}
            </button>
          </div>
        </div>
      ) : (
        // Running — terminal output + input
        <>
          {/* Output area */}
          <pre
            ref={outputRef}
            className="flex-1 overflow-y-auto p-3 text-xs font-mono text-gray-300 bg-[#0a0a0f] whitespace-pre-wrap break-words leading-relaxed"
          >
            {output || 'Waiting for output...'}
          </pre>

          {/* Input area */}
          <div className="border-t border-gray-800 p-3 bg-gray-900/50">
            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Chat with the orchestrator..."
                className="flex-1 px-3 py-2 rounded-lg text-sm bg-gray-800 border border-gray-700 text-gray-200 placeholder-gray-500 focus:border-emerald-500 focus:outline-none"
                disabled={sending}
              />
              <button
                onClick={handleSend}
                disabled={sending || !input.trim()}
                className="px-3 py-2 rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 disabled:hover:bg-emerald-600"
              >
                <Send size={16} />
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
