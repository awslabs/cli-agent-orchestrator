import { useEffect, useState } from 'react'
import { useStore } from './store'
import { api, createActivityStream } from './api'
import { AgentPanel } from './components/AgentPanel'
import { BeadsPanel } from './components/BeadsPanel'
import { ActivityFeed } from './components/ActivityFeed'
import { RalphPanel } from './components/RalphPanel'
import { ContextProposals } from './components/ContextProposals'
import { FlowsPanel } from './components/FlowsPanel'
import { MessagesPanel } from './components/MessagesPanel'
import { StarCraftView } from './components/starcraft'
import { TerminalTest } from './components/TerminalTest'
import { Bot, Zap, ClipboardList, RefreshCw, Activity, Brain, Terminal, Gamepad2, Clock, ArrowLeft, Mail } from 'lucide-react'

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
function TabButton({ active, onClick, children, badge }: { active: boolean; onClick: () => void; children: React.ReactNode; badge?: number }) {
  return (
    <button
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

export default function App() {
  const [view, setView] = useState<'dashboard' | 'starcraft' | 'terminal-test'>('dashboard')
  const [activeTab, setActiveTab] = useState<'agents' | 'beads' | 'activity' | 'learn' | 'ralph' | 'flows' | 'messages'>('agents')
  const { tasks, agents, sessions, ralph, flows, messages, setTasks, setAgents, setSessions, setRalph, setFlows, setMessages, addActivity } = useStore()

  // Check URL for terminal test mode
  useEffect(() => {
    if (location.search.includes('terminal-test')) setView('terminal-test')
  }, [])

  useEffect(() => {
    api.tasks.list().then(setTasks).catch(() => {})
    api.agents.list().then(setAgents).catch(() => {})
    api.sessions.list().then(setSessions).catch(() => {})
    api.ralph.status().then(r => setRalph(r.active ? r : null)).catch(() => {})
    api.flows.list().then(setFlows).catch(() => {})

    const ws = new WebSocket(`ws://${location.host}/api/ws/updates`)
    ws.onmessage = (e) => {
      const { type, data } = JSON.parse(e.data)
      addActivity({ type, timestamp: new Date().toISOString(), ...data })
      if (type.startsWith('task')) api.tasks.list().then(setTasks)
      if (type.startsWith('ralph')) api.ralph.status().then(r => setRalph(r.active ? r : null))
    }

    const activityWs = createActivityStream((data) => addActivity(data as any))
    return () => { ws.close(); activityWs.close() }
  }, [])

  const openBeads = tasks.filter(t => t.status === 'open').length
  const wipBeads = tasks.filter(t => t.status === 'wip').length
  const activeSessions = sessions.length

  if (view === 'terminal-test') {
    return <TerminalTest />
  }

  if (view === 'starcraft') {
    return (
      <div className="h-screen flex flex-col">
        <button
          onClick={() => setView('dashboard')}
          className="absolute top-4 right-4 z-50 px-4 py-2 text-sm rounded-lg bg-gray-800/90 backdrop-blur-sm text-emerald-400 border border-gray-700 hover:bg-gray-700 transition-all flex items-center gap-2"
        >
          <ArrowLeft size={16} /> Back to Dashboard
        </button>
        <StarCraftView />
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-[#0f0f14] text-white">
      {/* Header */}
      <header className="sticky top-0 z-40 bg-[#0f0f14]/95 backdrop-blur-md border-b border-gray-800/50">
        <div className="max-w-7xl mx-auto px-6 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              {/* AWS Logo */}
              <svg className="w-10 h-10 text-[#FF9900]" viewBox="0 0 304 182" fill="currentColor">
                <path d="M86.4 66.4c0 3.7.4 6.7 1.1 8.9.8 2.2 1.8 4.6 3.2 7.2.5.8.7 1.6.7 2.3 0 1-.6 2-1.9 3l-6.3 4.2c-.9.6-1.8.9-2.6.9-1 0-2-.5-3-1.4-1.4-1.5-2.6-3.1-3.6-4.7-1-1.7-2-3.6-3.1-5.9-7.8 9.2-17.6 13.8-29.4 13.8-8.4 0-15.1-2.4-20-7.2-4.9-4.8-7.4-11.2-7.4-19.2 0-8.5 3-15.4 9.1-20.6 6.1-5.2 14.2-7.8 24.5-7.8 3.4 0 6.9.3 10.6.8 3.7.5 7.5 1.3 11.5 2.2v-7.3c0-7.6-1.6-12.9-4.7-16-3.2-3.1-8.6-4.6-16.3-4.6-3.5 0-7.1.4-10.8 1.3-3.7.9-7.3 2-10.8 3.4-1.6.7-2.8 1.1-3.5 1.3-.7.2-1.2.3-1.6.3-1.4 0-2.1-1-2.1-3.1v-4.9c0-1.6.2-2.8.7-3.5.5-.7 1.4-1.4 2.8-2.1 3.5-1.8 7.7-3.3 12.6-4.5 4.9-1.3 10.1-1.9 15.6-1.9 11.9 0 20.6 2.7 26.2 8.1 5.5 5.4 8.3 13.6 8.3 24.6v32.4zM45.8 81.6c3.3 0 6.7-.6 10.3-1.8 3.6-1.2 6.8-3.4 9.5-6.4 1.6-1.9 2.8-4 3.4-6.4.6-2.4 1-5.3 1-8.7v-4.2c-2.9-.7-6-1.3-9.2-1.7-3.2-.4-6.3-.6-9.4-.6-6.7 0-11.6 1.3-14.9 4-3.3 2.7-4.9 6.5-4.9 11.5 0 4.7 1.2 8.2 3.7 10.6 2.4 2.5 5.9 3.7 10.5 3.7zm80.3 10.8c-1.8 0-3-.3-3.8-1-.8-.6-1.5-2-2.1-3.9l-23.5-77.3c-.6-2-.9-3.3-.9-4 0-1.6.8-2.5 2.4-2.5h9.8c1.9 0 3.2.3 3.9 1 .8.6 1.4 2 2 3.9l16.8 66.2 15.6-66.2c.5-2 1.1-3.3 1.9-3.9.8-.6 2.2-1 4-1h8c1.9 0 3.2.3 4 1 .8.6 1.5 2 1.9 3.9l15.8 67 17.3-67c.6-2 1.3-3.3 2-3.9.8-.6 2.1-1 3.9-1h9.3c1.6 0 2.5.8 2.5 2.5 0 .5-.1 1-.2 1.6-.1.6-.3 1.4-.7 2.5l-24.1 77.3c-.6 2-1.3 3.3-2.1 3.9-.8.6-2.1 1-3.8 1h-8.6c-1.9 0-3.2-.3-4-1-.8-.7-1.5-2-1.9-4l-15.5-64.5-15.4 64.4c-.5 2-1.1 3.3-1.9 4-.8.7-2.2 1-4 1h-8.6zm128.5 2.7c-5.2 0-10.4-.6-15.4-1.8-5-1.2-8.9-2.5-11.5-4-1.6-.9-2.7-1.9-3.1-2.8-.4-.9-.6-1.9-.6-2.8v-5.1c0-2.1.8-3.1 2.3-3.1.6 0 1.2.1 1.8.3.6.2 1.5.6 2.5 1 3.4 1.5 7.1 2.7 11 3.5 4 .8 7.9 1.2 11.9 1.2 6.3 0 11.2-1.1 14.6-3.3 3.4-2.2 5.2-5.4 5.2-9.5 0-2.8-.9-5.1-2.7-7-1.8-1.9-5.2-3.6-10.1-5.2l-14.5-4.5c-7.3-2.3-12.7-5.7-16-10.2-3.3-4.4-5-9.3-5-14.5 0-4.2.9-7.9 2.7-11.1 1.8-3.2 4.2-6 7.2-8.2 3-2.3 6.4-4 10.4-5.2 4-1.2 8.2-1.7 12.6-1.7 2.2 0 4.5.1 6.7.4 2.3.3 4.4.7 6.5 1.1 2 .5 3.9 1 5.7 1.6 1.8.6 3.2 1.2 4.2 1.8 1.4.8 2.4 1.6 3 2.5.6.8.9 1.9.9 3.3v4.7c0 2.1-.8 3.2-2.3 3.2-.8 0-2.1-.4-3.8-1.2-5.7-2.6-12.1-3.9-19.2-3.9-5.7 0-10.2.9-13.3 2.8-3.1 1.9-4.7 4.8-4.7 8.9 0 2.8 1 5.2 3 7.1 2 1.9 5.7 3.8 11 5.5l14.2 4.5c7.2 2.3 12.4 5.5 15.5 9.6 3.1 4.1 4.6 8.8 4.6 14 0 4.3-.9 8.2-2.6 11.6-1.8 3.4-4.2 6.4-7.3 8.8-3.1 2.5-6.8 4.3-11.1 5.6-4.5 1.4-9.2 2.1-14.3 2.1z"/>
                <path d="M273.5 143.7c-32.9 24.3-80.7 37.2-121.8 37.2-57.6 0-109.5-21.3-148.7-56.7-3.1-2.8-.3-6.6 3.4-4.4 42.4 24.6 94.7 39.5 148.8 39.5 36.5 0 76.6-7.6 113.5-23.2 5.5-2.5 10.2 3.6 4.8 7.6z"/>
                <path d="M287.2 128.1c-4.2-5.4-27.8-2.6-38.5-1.3-3.2.4-3.7-2.4-.8-4.5 18.8-13.2 49.7-9.4 53.3-5 3.6 4.5-1 35.4-18.6 50.2-2.7 2.3-5.3 1.1-4.1-1.9 4-9.9 12.9-32.2 8.7-37.5z"/>
              </svg>
              <div>
                <h1 className="text-lg font-bold text-white">Messaging Agent Orchestrator</h1>
                <p className="text-xs text-gray-500">AWS SNS/SQS Support Automation</p>
              </div>
            </div>
            
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20">
                <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></div>
                <span className="text-xs text-emerald-400 font-medium">Connected</span>
              </div>
              <button
                onClick={() => setView('starcraft')}
                className="px-4 py-2 text-sm rounded-lg bg-gradient-to-r from-purple-600 to-purple-500 text-white font-medium hover:from-purple-500 hover:to-purple-400 transition-all shadow-lg shadow-purple-500/20 flex items-center gap-2"
              >
                <Gamepad2 size={16} /> StarCraft Mode
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Stats Bar */}
      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard icon={<Bot size={20} className="text-blue-400" />} label="Agents" value={agents.length} color="bg-blue-500/20" />
          <StatCard icon={<Zap size={20} className="text-emerald-400" />} label="Active Sessions" value={activeSessions} color="bg-emerald-500/20" />
          <StatCard icon={<ClipboardList size={20} className="text-amber-400" />} label="Open Beads" value={openBeads} color="bg-amber-500/20" />
          <StatCard icon={<RefreshCw size={20} className="text-purple-400" />} label="In Progress" value={wipBeads} color="bg-purple-500/20" />
        </div>
      </div>

      {/* Tab Navigation */}
      <div className="max-w-7xl mx-auto px-6">
        <div className="flex items-center gap-2 p-1 bg-gray-900/50 rounded-xl border border-gray-800/50 w-fit">
          <TabButton active={activeTab === 'agents'} onClick={() => setActiveTab('agents')} badge={sessions.length}>
            <Bot size={16} /> Agents
          </TabButton>
          <TabButton active={activeTab === 'beads'} onClick={() => setActiveTab('beads')} badge={openBeads}>
            <ClipboardList size={16} /> Beads
          </TabButton>
          <TabButton active={activeTab === 'activity'} onClick={() => setActiveTab('activity')}>
            <Activity size={16} /> Activity
          </TabButton>
          <TabButton active={activeTab === 'learn'} onClick={() => setActiveTab('learn')}>
            <Brain size={16} /> Learn
          </TabButton>
          <TabButton active={activeTab === 'ralph'} onClick={() => setActiveTab('ralph')}>
            <RefreshCw size={16} /> Ralph {ralph && <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>}
          </TabButton>
          <TabButton active={activeTab === 'flows'} onClick={() => setActiveTab('flows')} badge={flows.filter(f => f.enabled).length}>
            <Clock size={16} /> Flows
          </TabButton>
          <TabButton active={activeTab === 'messages'} onClick={() => setActiveTab('messages')} badge={messages.filter(m => m.status === 'pending').length}>
            <Mail size={16} /> Messages
          </TabButton>
        </div>
      </div>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-6 py-6">
        <div className="bg-gray-900/30 rounded-2xl border border-gray-800/50 p-6 min-h-[500px]">
          {activeTab === 'agents' && <AgentPanel />}
          {activeTab === 'beads' && <BeadsPanel />}
          {activeTab === 'activity' && <ActivityFeed />}
          {activeTab === 'learn' && <ContextProposals />}
          {activeTab === 'ralph' && <RalphPanel />}
          {activeTab === 'flows' && <FlowsPanel />}
          {activeTab === 'messages' && <MessagesPanel />}
        </div>
      </main>

      {/* Footer */}
      <footer className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between text-xs text-gray-600">
        <span>Built by @abducabd</span>
        <div className="flex items-center gap-2">
          <span>Powered by</span>
          <svg className="w-8 h-8 text-[#FF9900]" viewBox="0 0 304 182" fill="currentColor">
            <path d="M86.4 66.4c0 3.7.4 6.7 1.1 8.9.8 2.2 1.8 4.6 3.2 7.2.5.8.7 1.6.7 2.3 0 1-.6 2-1.9 3l-6.3 4.2c-.9.6-1.8.9-2.6.9-1 0-2-.5-3-1.4-1.4-1.5-2.6-3.1-3.6-4.7-1-1.7-2-3.6-3.1-5.9-7.8 9.2-17.6 13.8-29.4 13.8-8.4 0-15.1-2.4-20-7.2-4.9-4.8-7.4-11.2-7.4-19.2 0-8.5 3-15.4 9.1-20.6 6.1-5.2 14.2-7.8 24.5-7.8 3.4 0 6.9.3 10.6.8 3.7.5 7.5 1.3 11.5 2.2v-7.3c0-7.6-1.6-12.9-4.7-16-3.2-3.1-8.6-4.6-16.3-4.6-3.5 0-7.1.4-10.8 1.3-3.7.9-7.3 2-10.8 3.4-1.6.7-2.8 1.1-3.5 1.3-.7.2-1.2.3-1.6.3-1.4 0-2.1-1-2.1-3.1v-4.9c0-1.6.2-2.8.7-3.5.5-.7 1.4-1.4 2.8-2.1 3.5-1.8 7.7-3.3 12.6-4.5 4.9-1.3 10.1-1.9 15.6-1.9 11.9 0 20.6 2.7 26.2 8.1 5.5 5.4 8.3 13.6 8.3 24.6v32.4zM45.8 81.6c3.3 0 6.7-.6 10.3-1.8 3.6-1.2 6.8-3.4 9.5-6.4 1.6-1.9 2.8-4 3.4-6.4.6-2.4 1-5.3 1-8.7v-4.2c-2.9-.7-6-1.3-9.2-1.7-3.2-.4-6.3-.6-9.4-.6-6.7 0-11.6 1.3-14.9 4-3.3 2.7-4.9 6.5-4.9 11.5 0 4.7 1.2 8.2 3.7 10.6 2.4 2.5 5.9 3.7 10.5 3.7zm80.3 10.8c-1.8 0-3-.3-3.8-1-.8-.6-1.5-2-2.1-3.9l-23.5-77.3c-.6-2-.9-3.3-.9-4 0-1.6.8-2.5 2.4-2.5h9.8c1.9 0 3.2.3 3.9 1 .8.6 1.4 2 2 3.9l16.8 66.2 15.6-66.2c.5-2 1.1-3.3 1.9-3.9.8-.6 2.2-1 4-1h8c1.9 0 3.2.3 4 1 .8.6 1.5 2 1.9 3.9l15.8 67 17.3-67c.6-2 1.3-3.3 2-3.9.8-.6 2.1-1 3.9-1h9.3c1.6 0 2.5.8 2.5 2.5 0 .5-.1 1-.2 1.6-.1.6-.3 1.4-.7 2.5l-24.1 77.3c-.6 2-1.3 3.3-2.1 3.9-.8.6-2.1 1-3.8 1h-8.6c-1.9 0-3.2-.3-4-1-.8-.7-1.5-2-1.9-4l-15.5-64.5-15.4 64.4c-.5 2-1.1 3.3-1.9 4-.8.7-2.2 1-4 1h-8.6zm128.5 2.7c-5.2 0-10.4-.6-15.4-1.8-5-1.2-8.9-2.5-11.5-4-1.6-.9-2.7-1.9-3.1-2.8-.4-.9-.6-1.9-.6-2.8v-5.1c0-2.1.8-3.1 2.3-3.1.6 0 1.2.1 1.8.3.6.2 1.5.6 2.5 1 3.4 1.5 7.1 2.7 11 3.5 4 .8 7.9 1.2 11.9 1.2 6.3 0 11.2-1.1 14.6-3.3 3.4-2.2 5.2-5.4 5.2-9.5 0-2.8-.9-5.1-2.7-7-1.8-1.9-5.2-3.6-10.1-5.2l-14.5-4.5c-7.3-2.3-12.7-5.7-16-10.2-3.3-4.4-5-9.3-5-14.5 0-4.2.9-7.9 2.7-11.1 1.8-3.2 4.2-6 7.2-8.2 3-2.3 6.4-4 10.4-5.2 4-1.2 8.2-1.7 12.6-1.7 2.2 0 4.5.1 6.7.4 2.3.3 4.4.7 6.5 1.1 2 .5 3.9 1 5.7 1.6 1.8.6 3.2 1.2 4.2 1.8 1.4.8 2.4 1.6 3 2.5.6.8.9 1.9.9 3.3v4.7c0 2.1-.8 3.2-2.3 3.2-.8 0-2.1-.4-3.8-1.2-5.7-2.6-12.1-3.9-19.2-3.9-5.7 0-10.2.9-13.3 2.8-3.1 1.9-4.7 4.8-4.7 8.9 0 2.8 1 5.2 3 7.1 2 1.9 5.7 3.8 11 5.5l14.2 4.5c7.2 2.3 12.4 5.5 15.5 9.6 3.1 4.1 4.6 8.8 4.6 14 0 4.3-.9 8.2-2.6 11.6-1.8 3.4-4.2 6.4-7.3 8.8-3.1 2.5-6.8 4.3-11.1 5.6-4.5 1.4-9.2 2.1-14.3 2.1z"/>
            <path d="M273.5 143.7c-32.9 24.3-80.7 37.2-121.8 37.2-57.6 0-109.5-21.3-148.7-56.7-3.1-2.8-.3-6.6 3.4-4.4 42.4 24.6 94.7 39.5 148.8 39.5 36.5 0 76.6-7.6 113.5-23.2 5.5-2.5 10.2 3.6 4.8 7.6z"/>
            <path d="M287.2 128.1c-4.2-5.4-27.8-2.6-38.5-1.3-3.2.4-3.7-2.4-.8-4.5 18.8-13.2 49.7-9.4 53.3-5 3.6 4.5-1 35.4-18.6 50.2-2.7 2.3-5.3 1.1-4.1-1.9 4-9.9 12.9-32.2 8.7-37.5z"/>
          </svg>
        </div>
      </footer>
    </div>
  )
}
