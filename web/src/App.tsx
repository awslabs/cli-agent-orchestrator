import React, { useEffect, useState, Suspense } from 'react'
import { useStore } from './store'
import { api, createActivityStream } from './api'
import { ErrorBoundary } from './components/ErrorBoundary'
import { TerminalTest } from './components/TerminalTest'
import { Bot, Zap, Activity, Clock, CheckCircle, XCircle, Info, Home } from 'lucide-react'

// Lazy-loaded panels
const DashboardHome = React.lazy(() => import('./components/DashboardHome').then(m => ({ default: m.DashboardHome })))
const AgentPanel = React.lazy(() => import('./components/AgentPanel').then(m => ({ default: m.AgentPanel })))
const ActivityFeed = React.lazy(() => import('./components/ActivityFeed').then(m => ({ default: m.ActivityFeed })))
const FlowsPanel = React.lazy(() => import('./components/FlowsPanel').then(m => ({ default: m.FlowsPanel })))

type TabKey = 'home' | 'agents' | 'activity' | 'flows'

const TABS: { key: TabKey; label: string; icon: React.ReactNode }[] = [
  { key: 'home', label: 'Home', icon: <Home size={16} /> },
  { key: 'agents', label: 'Agents', icon: <Bot size={16} /> },
  { key: 'activity', label: 'Activity', icon: <Activity size={16} /> },
  { key: 'flows', label: 'Flows', icon: <Clock size={16} /> },
]

// Snackbar component
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
    info: 'bg-blue-600 border-blue-500'
  }
  const icons = {
    success: <CheckCircle size={18} />,
    error: <XCircle size={18} />,
    info: <Info size={18} />
  }

  return (
    <div role="alert" className={`fixed bottom-4 right-4 z-50 px-4 py-3 rounded-lg border shadow-lg flex items-center gap-2 text-white ${colors[snackbar.type]}`}>
      {icons[snackbar.type]}
      <span className="text-sm">{snackbar.message}</span>
    </div>
  )
}

// Modern stat card component
function StatCard({ icon, label, value, color }: { icon: React.ReactNode; label: string; value: number | string; color: string }) {
  return (
    <div className="bg-gradient-to-br from-gray-800/80 to-gray-900/80 backdrop-blur-sm rounded-xl p-4 border border-gray-700/50 hover:border-gray-600/50 transition-all duration-300 hover:shadow-lg hover:shadow-black/20">
      <div className="flex items-center gap-3">
        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${color}`}>
          {icon}
        </div>
        <div>
          <div className="text-2xl font-bold text-white">{value}</div>
          <div className="text-xs text-gray-400 uppercase tracking-wide">{label}</div>
        </div>
      </div>
    </div>
  )
}

// Tab button component
function TabButton({ active, onClick, children, badge, ariaSelected }: { active: boolean; onClick: () => void; children: React.ReactNode; badge?: number; ariaSelected: boolean }) {
  return (
    <button
      role="tab"
      aria-selected={ariaSelected}
      onClick={onClick}
      className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 flex items-center gap-2 ${
        active
          ? 'bg-gradient-to-r from-emerald-600 to-emerald-500 text-white shadow-lg shadow-emerald-500/20'
          : 'text-gray-400 hover:text-white hover:bg-gray-800/50'
      }`}
    >
      {children}
      {badge !== undefined && badge > 0 && (
        <span className={`px-1.5 py-0.5 text-xs rounded-full ${active ? 'bg-white/20' : 'bg-gray-700'}`}>
          {badge}
        </span>
      )}
    </button>
  )
}

// Loading fallback
function PanelLoader() {
  return (
    <div className="flex items-center justify-center py-16">
      <div className="animate-pulse text-gray-500 text-sm">Loading...</div>
    </div>
  )
}

export default function App() {
  const [view, setView] = useState<'dashboard' | 'terminal-test'>('dashboard')
  const [activeTab, setActiveTab] = useState<TabKey>('home')
  const [wsConnected, setWsConnected] = useState(false)
  const { agents, sessions, flows, setAgents, setSessions, setFlows, addActivity, setActiveSession } = useStore()

  // Check URL for terminal test mode
  useEffect(() => {
    if (location.search.includes('terminal-test')) setView('terminal-test')
  }, [])

  // Keyboard shortcuts: Alt+1 through Alt+7
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!e.altKey) return
      const tabs: TabKey[] = ['home', 'agents', 'activity', 'flows']
      const idx = parseInt(e.key) - 1
      if (idx >= 0 && idx < tabs.length) {
        e.preventDefault()
        setActiveTab(tabs[idx])
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // Initial data load + WebSocket + polling fallback
  useEffect(() => {
    api.agents.list().then(setAgents).catch(() => {})
    api.sessions.list().then(setSessions).catch(() => {})
    api.flows.list().then(setFlows).catch(() => {})

    const ws = new WebSocket(`ws://${location.host}/api/ws/updates`)
    ws.onopen = () => setWsConnected(true)
    ws.onclose = () => setWsConnected(false)
    ws.onerror = () => setWsConnected(false)
    ws.onmessage = (e) => {
      const { type, data } = JSON.parse(e.data)
      addActivity({ type, timestamp: new Date().toISOString(), ...data })
      if (type.startsWith('session')) api.sessions.list().then(setSessions)
    }

    const activityWs = createActivityStream((data) => addActivity(data as any))

    // 30s polling fallback
    const pollInterval = setInterval(() => {
      api.sessions.list().then(setSessions).catch(() => {})
      api.flows.list().then(setFlows).catch(() => {})
    }, 30000)

    return () => {
      ws.close()
      activityWs.close()
      clearInterval(pollInterval)
    }
  }, [])

  const activeSessions = sessions.length

  const getBadge = (key: TabKey): number | undefined => {
    if (key === 'agents') return sessions.length
    if (key === 'flows') return flows.filter(f => f.enabled).length
    return undefined
  }

  if (view === 'terminal-test') {
    return <TerminalTest />
  }

  return (
    <div className="min-h-screen bg-[#0f0f14] text-white">
      {/* Header */}
      <header className="sticky top-0 z-40 bg-[#0f0f14]/95 backdrop-blur-md border-b border-gray-800/50">
        <div className="max-w-7xl mx-auto px-6 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className="w-10 h-10 rounded-lg bg-emerald-500/20 flex items-center justify-center text-emerald-400">
                <Bot size={24} />
              </div>
              <div>
                <h1 className="text-lg font-bold text-white">Agent Orchestrator</h1>
                <p className="text-xs text-gray-500">CLI Agent Orchestrator</p>
              </div>
            </div>

            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-full ${wsConnected ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-red-500/10 border border-red-500/20'}`}>
              <div className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'}`}></div>
              <span className={`text-xs font-medium ${wsConnected ? 'text-emerald-400' : 'text-red-400'}`}>{wsConnected ? 'Connected' : 'Disconnected'}</span>
            </div>
          </div>
        </div>
      </header>

      {/* Stats Bar */}
      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="grid grid-cols-2 gap-4">
          <StatCard icon={<Bot size={20} className="text-blue-400" />} label="Agents" value={agents.length} color="bg-blue-500/20" />
          <StatCard icon={<Zap size={20} className="text-emerald-400" />} label="Active Sessions" value={activeSessions} color="bg-emerald-500/20" />
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="max-w-7xl mx-auto px-6">
        <div role="tablist" className="flex items-center gap-2 p-1 bg-gray-900/50 rounded-xl border border-gray-800/50 w-fit">
          {TABS.map((tab) => (
            <TabButton
              key={tab.key}
              active={activeTab === tab.key}
              ariaSelected={activeTab === tab.key}
              onClick={() => setActiveTab(tab.key)}
              badge={getBadge(tab.key)}
            >
              {tab.icon} {tab.label}
            </TabButton>
          ))}
        </div>
      </div>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-6 py-6">
        <div role="tabpanel" className="bg-gray-900/30 rounded-2xl border border-gray-800/50 p-6 min-h-[500px]">
          <ErrorBoundary>
            <Suspense fallback={<PanelLoader />}>
              {activeTab === 'home' && <DashboardHome onNavigate={setActiveTab} />}
              {activeTab === 'agents' && <AgentPanel />}
              {activeTab === 'activity' && <ActivityFeed />}
              {activeTab === 'flows' && <FlowsPanel onNavigateToSession={(sessionId: string) => {
                setActiveSession(sessionId)
                setActiveTab('agents')
              }} />}
            </Suspense>
          </ErrorBoundary>
        </div>
      </main>

      {/* Footer */}
      <footer className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between text-xs text-gray-600">
        <span>Built by @abducabd</span>
      </footer>

      <Snackbar />
    </div>
  )
}
