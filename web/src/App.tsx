import { useEffect, useState, Suspense } from 'react'
import { useStore } from './store'
import { ErrorBoundary } from './components/ErrorBoundary'
import { DashboardHome } from './components/DashboardHome'
import { AgentPanel } from './components/AgentPanel'
import { FlowsPanel } from './components/FlowsPanel'
import { SettingsPanel } from './components/SettingsPanel'
import { Bot, Home, Clock, Settings, CheckCircle, XCircle, Info, Wifi, WifiOff } from 'lucide-react'

type TabKey = 'home' | 'agents' | 'flows' | 'settings'

const TABS: { key: TabKey; label: string; icon: React.ReactNode }[] = [
  { key: 'home', label: 'Home', icon: <Home size={16} /> },
  { key: 'agents', label: 'Agents', icon: <Bot size={16} /> },
  { key: 'flows', label: 'Flows', icon: <Clock size={16} /> },
  { key: 'settings', label: 'Settings', icon: <Settings size={16} /> },
]

function Snackbar() {
  const { snackbar, hideSnackbar } = useStore()

  useEffect(() => {
    if (snackbar) {
      const timer = setTimeout(hideSnackbar, 3000)
      return () => clearTimeout(timer)
    }
  }, [snackbar, hideSnackbar])

  if (!snackbar) return null

  const colors = {
    success: 'bg-emerald-600 border-emerald-500',
    error: 'bg-red-600 border-red-500',
    info: 'bg-blue-600 border-blue-500',
  }
  const icons = {
    success: <CheckCircle size={18} />,
    error: <XCircle size={18} />,
    info: <Info size={18} />,
  }

  return (
    <div role="alert" className={`fixed bottom-4 right-4 z-50 px-4 py-3 rounded-lg border shadow-lg flex items-center gap-2 text-white ${colors[snackbar.type]}`}>
      {icons[snackbar.type]}
      <span className="text-sm">{snackbar.message}</span>
    </div>
  )
}

export default function App() {
  const [tab, setTab] = useState<TabKey>('home')
  const { sessions, connected, fetchSessions } = useStore()

  useEffect(() => {
    fetchSessions()
    const interval = setInterval(fetchSessions, 10000)
    return () => clearInterval(interval)
  }, [])

  // Keyboard shortcuts: Alt+1-4
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.altKey && e.key >= '1' && e.key <= String(TABS.length)) {
        e.preventDefault()
        setTab(TABS[parseInt(e.key) - 1].key)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  return (
    <div className="min-h-screen bg-[#0f0f14] text-gray-200">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur-sm sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-emerald-500 to-emerald-700 flex items-center justify-center">
              <Bot size={18} className="text-white" />
            </div>
            <h1 className="text-lg font-bold text-white">CLI Agent Orchestrator</h1>
          </div>
          <div className="flex items-center gap-4">
            <span className="text-xs text-gray-500">{sessions.length} session{sessions.length !== 1 ? 's' : ''}</span>
            <div className="flex items-center gap-1.5" title={connected ? 'Connected' : 'Disconnected'}>
              {connected ? (
                <Wifi size={14} className="text-emerald-400" />
              ) : (
                <WifiOff size={14} className="text-red-400" />
              )}
              <span className={`text-xs ${connected ? 'text-emerald-400' : 'text-red-400'}`}>
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
          </div>
        </div>
      </header>

      {/* Tab Bar */}
      <div className="border-b border-gray-800">
        <div className="max-w-7xl mx-auto px-6">
          <nav className="flex gap-1 py-2" role="tablist">
            {TABS.map((t, i) => (
              <button
                key={t.key}
                role="tab"
                aria-selected={tab === t.key}
                onClick={() => setTab(t.key)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 flex items-center gap-2 ${
                  tab === t.key
                    ? 'bg-gradient-to-r from-emerald-600 to-emerald-500 text-white shadow-lg shadow-emerald-500/20'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800/50'
                }`}
                title={`Alt+${i + 1}`}
              >
                {t.icon}
                {t.label}
                {t.key === 'agents' && sessions.length > 0 && (
                  <span className={`px-1.5 py-0.5 text-xs rounded-full ${tab === t.key ? 'bg-white/20' : 'bg-gray-700'}`}>
                    {sessions.length}
                  </span>
                )}
              </button>
            ))}
          </nav>
        </div>
      </div>

      {/* Content */}
      <main className="max-w-7xl mx-auto px-6 py-6">
        <ErrorBoundary>
          <Suspense fallback={<div className="text-gray-500 text-sm py-12 text-center">Loading...</div>}>
            {tab === 'home' && <DashboardHome onNavigate={(t) => setTab(t as TabKey)} />}
            {tab === 'agents' && <AgentPanel />}
            {tab === 'flows' && <FlowsPanel />}
            {tab === 'settings' && <SettingsPanel />}
          </Suspense>
        </ErrorBoundary>
      </main>

      <Snackbar />
    </div>
  )
}
