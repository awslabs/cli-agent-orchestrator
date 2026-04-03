import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { Bot, ChevronLeft, ChevronRight, Square, Play, Loader2, Maximize2, Minimize2 } from 'lucide-react'

const PROVIDERS = [
  { value: 'claude_code', label: 'Claude Code' },
  { value: 'kiro_cli', label: 'Kiro CLI' },
  { value: 'q_cli', label: 'Q CLI' },
  { value: 'codex', label: 'Codex' },
]

function OrchestratorTerminal({ terminalId, isFullScreen }: { terminalId: string; isFullScreen: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: isFullScreen ? 14 : 13,
      fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
        cursor: '#58a6ff',
        selectionBackground: '#264f78',
        black: '#0d1117',
        red: '#ff7b72',
        green: '#3fb950',
        yellow: '#d29922',
        blue: '#58a6ff',
        magenta: '#bc8cff',
        cyan: '#39d353',
        white: '#c9d1d9',
      },
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${location.host}/terminals/${terminalId}/ws`)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      fitAddon.fit()
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
      }
    }

    ws.onclose = () => {
      term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n')
    }

    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) navigator.clipboard.writeText(selection).catch(() => {})
    })

    term.attachCustomKeyEventHandler((e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        const selection = term.getSelection()
        if (selection) navigator.clipboard.writeText(selection).catch(() => {})
        return false
      }
      if (e.ctrlKey && e.shiftKey && e.key === 'V') {
        navigator.clipboard.readText().then(text => {
          if (text && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'input', data: text }))
          }
        }).catch(() => {})
        return false
      }
      return true
    })

    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }))
      }
    })

    let resizeTimer: ReturnType<typeof setTimeout>
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
        }
      }, 50)
    })
    resizeObserver.observe(el)

    requestAnimationFrame(() => fitAddon.fit())
    term.focus()

    return () => {
      clearTimeout(resizeTimer)
      resizeObserver.disconnect()
      ws.close()
      term.dispose()
    }
  }, [terminalId, isFullScreen])

  return (
    <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
      <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
    </div>
  )
}

export function OrchestratorSidebar() {
  const {
    orchestratorRunning, orchestratorSessionId, orchestratorExpanded,
    toggleOrchestratorSidebar, launchOrchestrator, stopOrchestrator, checkOrchestrator,
  } = useStore()

  const [provider, setProvider] = useState('claude_code')
  const [launching, setLaunching] = useState(false)
  const [terminalId, setTerminalId] = useState<string | null>(null)
  const [isFullScreen, setIsFullScreen] = useState(false)

  useEffect(() => {
    checkOrchestrator()
  }, [])

  // Resolve terminal ID when orchestrator is running
  useEffect(() => {
    if (!orchestratorRunning || !orchestratorSessionId) {
      setTerminalId(null)
      return
    }
    api.getSession(orchestratorSessionId).then(session => {
      if (session.terminals?.length > 0) {
        setTerminalId(session.terminals[0].id)
      }
    }).catch(() => {})
  }, [orchestratorRunning, orchestratorSessionId])

  const handleLaunch = async () => {
    setLaunching(true)
    await launchOrchestrator(provider)
    setLaunching(false)
  }

  // Full-screen mode
  if (isFullScreen && orchestratorRunning && terminalId) {
    return (
      <div className="fixed inset-0 z-50 flex flex-col" style={{ background: '#0d1117' }}>
        <div className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-700/50 shrink-0">
          <div className="flex items-center gap-3">
            <Bot size={16} className="text-emerald-400" />
            <span className="text-sm font-semibold text-white">AI Orchestrator</span>
            <span className="px-2 py-0.5 text-[10px] rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
              Running
            </span>
            <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{provider}</span>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={stopOrchestrator} className="p-1.5 rounded hover:bg-red-500/20 text-red-400" title="Stop">
              <Square size={14} />
            </button>
            <button onClick={() => setIsFullScreen(false)} className="p-1.5 rounded hover:bg-gray-700 text-gray-400" title="Exit full screen">
              <Minimize2 size={14} />
            </button>
          </div>
        </div>
        <OrchestratorTerminal terminalId={terminalId} isFullScreen={true} />
      </div>
    )
  }

  // Collapsed state
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

  // Expanded sidebar
  return (
    <div className="fixed right-0 top-0 bottom-0 w-[480px] z-50 flex flex-col bg-[#0d1117] border-l border-gray-800 shadow-2xl">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-gray-900/50 shrink-0">
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
          {orchestratorRunning && terminalId && (
            <button
              onClick={() => setIsFullScreen(true)}
              className="p-1.5 rounded hover:bg-gray-700 text-gray-400"
              title="Full screen"
            >
              <Maximize2 size={14} />
            </button>
          )}
          {orchestratorRunning && (
            <button onClick={stopOrchestrator} className="p-1.5 rounded hover:bg-red-500/20 text-red-400" title="Stop">
              <Square size={14} />
            </button>
          )}
          <button onClick={toggleOrchestratorSidebar} className="p-1.5 rounded hover:bg-gray-700 text-gray-400" title="Collapse">
            <ChevronRight size={14} />
          </button>
        </div>
      </div>

      {/* Content */}
      {!orchestratorRunning ? (
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
      ) : terminalId ? (
        <OrchestratorTerminal terminalId={terminalId} isFullScreen={false} />
      ) : (
        <div className="flex-1 flex items-center justify-center">
          <Loader2 size={24} className="animate-spin text-gray-500" />
        </div>
      )}
    </div>
  )
}
