import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Plus, Edit2, Check, Trash2, ChevronDown, ChevronRight, Inbox, Bot, Wrench, Search, Shield, Swords, Mail, Map, RefreshCw, User, Sparkles, Loader2, X, Terminal } from 'lucide-react'

const PRIORITY_STYLES = {
  1: { bg: 'bg-red-500/10', border: 'border-red-500/30', text: 'text-red-400', badge: 'bg-red-500', label: 'P1 Critical' },
  2: { bg: 'bg-amber-500/10', border: 'border-amber-500/30', text: 'text-amber-400', badge: 'bg-amber-500', label: 'P2 High' },
  3: { bg: 'bg-gray-500/10', border: 'border-gray-500/30', text: 'text-gray-400', badge: 'bg-gray-500', label: 'P3 Normal' }
}

const STATUS_STYLES = {
  open: { bg: 'bg-blue-500/10', text: 'text-blue-400', label: 'Open' },
  wip: { bg: 'bg-purple-500/10', text: 'text-purple-400', label: 'In Progress' },
  closed: { bg: 'bg-emerald-500/10', text: 'text-emerald-400', label: 'Done' }
}

const AGENT_ICONS: Record<string, React.ReactNode> = {
  'generalist': <Bot size={14} />,
  'bob-the-builder': <Wrench size={14} />,
  'log-diver': <Search size={14} />,
  'oncall-buddy': <Shield size={14} />,
  'ticket-ninja': <Swords size={14} />,
  'sns-ticket-ninja': <Mail size={14} />,
  'atlas': <Map size={14} />,
  'ralph-wiggum': <RefreshCw size={14} />
}

interface Bead {
  id: string
  title: string
  description?: string
  priority?: number
  status: string
  assignee?: string
  parent_id?: string
  blocked_by?: string[]
}

export function BeadsPanel() {
  const { tasks, setTasks, sessions, agents, setAgents } = useStore()
  const [showCreate, setShowCreate] = useState(false)
  const [editingBead, setEditingBead] = useState<Bead | null>(null)
  const [expandedBead, setExpandedBead] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'open' | 'wip' | 'closed'>('open')
  const [newBead, setNewBead] = useState({ title: '', description: '', priority: 2 })
  const [aiText, setAiText] = useState('')
  const [aiGenerating, setAiGenerating] = useState(false)
  const [assignModal, setAssignModal] = useState<Bead | null>(null)
  const [assigning, setAssigning] = useState(false)
  const [spawnProgress, setSpawnProgress] = useState<{ agent: string; bead: Bead; logs: string[] } | null>(null)
  const [assignMode, setAssignMode] = useState<'single' | 'orchestrator' | null>(null)

  const refresh = () => api.tasks.list().then(setTasks).catch(() => {})
  
  useEffect(() => {
    refresh()
    api.agents.list().then(setAgents).catch(() => {})
  }, [])
  
  const generateBeads = async () => {
    if (!aiText.trim() || aiGenerating) return
    setAiGenerating(true)
    try {
      const result = await api.tasks.decompose(aiText)
      if (result.beads?.length) {
        for (const b of result.beads) {
          await api.tasks.create({ title: b.title, description: b.description || '', priority: b.priority || 2 })
        }
        setAiText('')
        refresh()
      }
    } catch (e) { console.error('AI generation failed:', e) }
    setAiGenerating(false)
  }
  useEffect(() => { refresh() }, [])

  const createBead = async () => {
    if (!newBead.title.trim()) return
    await api.tasks.create(newBead)
    setNewBead({ title: '', description: '', priority: 2 })
    setShowCreate(false)
    refresh()
  }

  const updateBead = async () => {
    if (!editingBead) return
    await api.tasks.update(editingBead.id, {
      title: editingBead.title,
      description: editingBead.description,
      priority: editingBead.priority
    })
    setEditingBead(null)
    refresh()
  }

  const deleteBead = async (id: string) => {
    if (!confirm('Delete this bead?')) return
    await api.tasks.delete(id)
    refresh()
  }

  const assignBead = async (beadId: string, sessionId: string) => {
    if (sessionId) {
      setAssigning(true)
      try {
        await api.tasks.assign(beadId, sessionId)
        // Send the task to the agent
        const bead = tasks.find(t => t.id === beadId)
        if (bead) {
          const prompt = `Please work on this task:\n\nTitle: ${bead.title}\n\n${bead.description || 'No additional details.'}`
          await api.sessions.input(sessionId, prompt + '\n', true)
        }
        refresh()
        setAssignModal(null)
      } finally {
        setAssigning(false)
      }
    }
  }

  const assignToNewAgent = async (agentName: string) => {
    if (!assignModal) return
    const bead = assignModal
    setAssignModal(null)
    setSpawnProgress({ agent: agentName, bead, logs: [`Initializing ${agentName}...`] })
    
    try {
      await new Promise(r => setTimeout(r, 400))
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, 'Creating tmux session...'] } : null)
      await new Promise(r => setTimeout(r, 400))
      
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, 'Loading agent profile...'] } : null)
      await new Promise(r => setTimeout(r, 400))
      
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, 'Spawning kiro-cli agent...'] } : null)
      const result = await api.tasks.assignAgent(bead.id, agentName)
      
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, `Session created: ${result.session_id || 'ok'}`, 'Assigning bead to agent...'] } : null)
      await new Promise(r => setTimeout(r, 400))
      
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, `Bead assigned: ${bead.title}`, 'Sending task to agent...'] } : null)
      await new Promise(r => setTimeout(r, 500))
      
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, '✓ Done'] } : null)
      await new Promise(r => setTimeout(r, 800))
      
      refresh()
      setSpawnProgress(null)
    } catch (e) {
      setSpawnProgress(p => p ? { ...p, logs: [...p.logs, `Error: ${e}`] } : null)
      await new Promise(r => setTimeout(r, 2000))
      setSpawnProgress(null)
    }
  }

  const closeBead = async (id: string) => {
    await api.tasks.close(id)
    refresh()
  }

  // Build hierarchy: separate parents and children
  const filteredTasks = tasks.filter(t => filter === 'all' || t.status === filter)
  const parentBeads = filteredTasks.filter(t => !t.parent_id)
    .sort((a, b) => (a.priority || 3) - (b.priority || 3))
  const childrenByParent = filteredTasks.filter(t => t.parent_id)
    .reduce((acc, t) => {
      const pid = t.parent_id!
      if (!acc[pid]) acc[pid] = []
      acc[pid].push(t)
      return acc
    }, {} as Record<string, typeof tasks>)

  const counts = {
    all: tasks.length,
    open: tasks.filter(t => t.status === 'open').length,
    wip: tasks.filter(t => t.status === 'wip').length,
    closed: tasks.filter(t => t.status === 'closed').length
  }

  // Modal component for create/edit
  const BeadModal = ({ isEdit }: { isEdit: boolean }) => {
    const bead = isEdit ? editingBead : newBead
    const setBead = isEdit 
      ? (updates: any) => setEditingBead({ ...editingBead!, ...updates })
      : (updates: any) => setNewBead({ ...newBead, ...updates })
    const onSave = isEdit ? updateBead : createBead
    const onClose = () => isEdit ? setEditingBead(null) : setShowCreate(false)

    return (
      <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50" onClick={onClose}>
        <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-md border border-gray-700 shadow-2xl" onClick={e => e.stopPropagation()}>
          <h3 className="text-lg font-semibold mb-4">{isEdit ? 'Edit Bead' : 'Create New Bead'}</h3>
          <div className="space-y-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">Title</label>
              <input
                value={bead?.title || ''}
                onChange={e => setBead({ title: e.target.value })}
                className="w-full px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white focus:border-emerald-500 focus:outline-none text-base"
                placeholder="What needs to be done?"
                autoFocus
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Description</label>
              <textarea
                value={bead?.description || ''}
                onChange={e => setBead({ description: e.target.value })}
                className="w-full px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white focus:border-emerald-500 focus:outline-none resize-none text-base"
                rows={4}
                placeholder="Additional details..."
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-2">Priority</label>
              <div className="flex gap-2">
                {[1, 2, 3].map(p => {
                  const style = PRIORITY_STYLES[p as 1 | 2 | 3]
                  return (
                    <button
                      key={p}
                      onClick={() => setBead({ priority: p })}
                      className={`flex-1 py-3 rounded-lg border transition-all text-base font-medium ${
                        bead?.priority === p 
                          ? `${style.bg} ${style.border} ${style.text}` 
                          : 'border-gray-700 text-gray-500 hover:border-gray-600'
                      }`}
                    >
                      {style.label}
                    </button>
                  )
                })}
              </div>
            </div>
            <div className="flex gap-3 pt-2">
              <button 
                onClick={onClose} 
                className="flex-1 py-3 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-800 text-base font-medium"
              >
                Cancel
              </button>
              <button 
                onClick={onSave} 
                className="flex-1 py-3 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-base font-medium"
              >
                {isEdit ? 'Save Changes' : 'Create Bead'}
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Quick Add + AI Generator */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Quick Add */}
        <div className="p-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5">
          <div className="flex items-center gap-2 mb-3">
            <Plus size={16} className="text-emerald-400" />
            <span className="text-sm font-medium text-emerald-300">Quick Add Bead</span>
          </div>
          <input
            value={newBead.title}
            onChange={e => setNewBead({ ...newBead, title: e.target.value })}
            onKeyDown={e => e.key === 'Enter' && newBead.title.trim() && createBead()}
            placeholder="Type a task and press Enter..."
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm focus:border-emerald-500 focus:outline-none"
          />
          <div className="flex items-center gap-2 mt-2">
            <div className="flex gap-1">
              {[1, 2, 3].map(p => (
                <button
                  key={p}
                  onClick={() => setNewBead({ ...newBead, priority: p })}
                  className={`px-2 py-1 text-xs rounded ${
                    newBead.priority === p 
                      ? PRIORITY_STYLES[p as 1|2|3].bg + ' ' + PRIORITY_STYLES[p as 1|2|3].text
                      : 'bg-gray-800 text-gray-500 hover:text-gray-300'
                  }`}
                >
                  P{p}
                </button>
              ))}
            </div>
            <button
              onClick={() => setShowCreate(true)}
              className="ml-auto text-xs text-gray-400 hover:text-white"
            >
              + Add details
            </button>
          </div>
        </div>

        {/* AI Generator */}
        <div className="p-4 rounded-xl border border-purple-500/30 bg-purple-500/5">
          <div className="flex items-center gap-2 mb-3">
            <Sparkles size={16} className="text-purple-400" />
            <span className="text-sm font-medium text-purple-300">AI Bead Generator</span>
          </div>
          <textarea
            value={aiText}
            onChange={e => setAiText(e.target.value)}
            placeholder="Paste requirements, task list, or describe what you need..."
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm focus:border-purple-500 focus:outline-none resize-none"
            rows={2}
          />
          <button
            onClick={generateBeads}
            disabled={!aiText.trim() || aiGenerating}
            className="mt-2 px-4 py-2 text-sm rounded-lg bg-purple-600 hover:bg-purple-500 text-white font-medium transition-all flex items-center gap-2 disabled:opacity-50 w-full justify-center"
          >
            {aiGenerating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
            {aiGenerating ? 'Generating...' : 'Generate Beads from Text'}
          </button>
        </div>
      </div>

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4 flex-wrap">
          <h2 className="text-lg font-semibold text-white">Bead Queue</h2>
          <div className="flex gap-1 p-1 bg-gray-800/50 rounded-lg">
            {(['open', 'wip', 'closed', 'all'] as const).map(f => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-4 py-2 text-sm rounded-md transition-all font-medium ${
                  filter === f 
                    ? 'bg-gray-700 text-white' 
                    : 'text-gray-400 hover:text-white'
                }`}
              >
                {f.charAt(0).toUpperCase() + f.slice(1)} ({counts[f]})
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-5 py-2.5 text-sm rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-medium transition-all flex items-center gap-1.5"
        >
          <Plus size={16} /> New Bead
        </button>
      </div>

      {/* Modals */}
      {showCreate && <BeadModal isEdit={false} />}
      {editingBead && <BeadModal isEdit={true} />}

      {/* Assignment Modal */}
      {assignModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50" onClick={() => { if (!assigning) { setAssignModal(null); setAssignMode(null) } }}>
          <div className="bg-gray-900 rounded-xl p-5 w-full max-w-md border border-gray-700" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="font-semibold text-white text-lg">Assign Bead</h3>
              <button onClick={() => { if (!assigning) { setAssignModal(null); setAssignMode(null) } }} className="text-gray-400 hover:text-white">
                <X size={18} />
              </button>
            </div>
            
            <div className="mb-4 p-3 bg-gray-800 rounded-lg">
              <div className="text-sm text-gray-400">Bead</div>
              <div className="text-white font-medium">{assignModal.title}</div>
              {assignModal.description && (
                <div className="text-sm text-gray-500 mt-1 line-clamp-2">{assignModal.description}</div>
              )}
            </div>

            {/* Mode Toggle */}
            <div className="mb-4">
              <div className="text-xs text-gray-400 mb-2 uppercase tracking-wide">How should this be worked on?</div>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setAssignMode('single')}
                  className={`p-3 rounded-lg border text-left transition-all ${
                    assignMode === 'single' 
                      ? 'border-emerald-500 bg-emerald-500/10' 
                      : 'border-gray-700 hover:border-gray-600'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Bot size={16} className={assignMode === 'single' ? 'text-emerald-400' : 'text-gray-400'} />
                    <span className={`font-medium ${assignMode === 'single' ? 'text-emerald-400' : 'text-white'}`}>Single Agent</span>
                  </div>
                  <p className="text-xs text-gray-500">Assign to one agent who works independently</p>
                </button>
                <button
                  onClick={() => setAssignMode('orchestrator')}
                  className={`p-3 rounded-lg border text-left transition-all ${
                    assignMode === 'orchestrator' 
                      ? 'border-purple-500 bg-purple-500/10' 
                      : 'border-gray-700 hover:border-gray-600'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <User size={16} className={assignMode === 'orchestrator' ? 'text-purple-400' : 'text-gray-400'} />
                    <span className={`font-medium ${assignMode === 'orchestrator' ? 'text-purple-400' : 'text-white'}`}>Orchestrator</span>
                  </div>
                  <p className="text-xs text-gray-500">Supervisor coordinates multiple workers</p>
                </button>
              </div>
            </div>

            {/* Single Agent Mode Content */}
            {assignMode === 'single' && (
              <>
                {/* Existing Sessions */}
                {sessions.length > 0 && (
                  <div className="mb-4">
                    <div className="text-xs text-gray-400 mb-2 flex items-center gap-1 uppercase tracking-wide">
                      <Terminal size={12} /> Existing Sessions
                    </div>
                    <div className="space-y-1 max-h-32 overflow-y-auto">
                      {sessions.map(s => {
                        const hasAssigned = tasks.some(t => t.assignee === s.id)
                        const icon = AGENT_ICONS[s.agent_name] || <User size={14} />
                        return (
                          <button
                            key={s.id}
                            onClick={() => assignBead(assignModal.id, s.id)}
                            disabled={assigning}
                            className="w-full text-left p-3 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                          >
                            <span className="text-emerald-400">{icon}</span>
                            <span className="flex-1 truncate font-medium">{s.agent_name || 'unknown'}</span>
                            <span className={`text-xs px-2 py-0.5 rounded ${hasAssigned ? 'bg-amber-500/20 text-amber-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
                              {hasAssigned ? 'busy' : 'idle'}
                            </span>
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* Spawn New Agent */}
                <div>
                  <div className="text-xs text-gray-400 mb-2 flex items-center gap-1 uppercase tracking-wide">
                    <Plus size={12} /> Spawn New Agent
                  </div>
                  <div className="space-y-1 max-h-48 overflow-y-auto">
                    {agents.map(a => {
                      const icon = AGENT_ICONS[a.name] || <Bot size={14} />
                      return (
                        <button
                          key={a.name}
                          onClick={() => assignToNewAgent(a.name)}
                          disabled={assigning}
                          className="w-full text-left p-3 bg-gray-800 hover:bg-purple-900/30 rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                        >
                          <span className="text-purple-400">{icon}</span>
                          <span className="flex-1 font-medium">{a.name}</span>
                          <span className="text-xs text-gray-500">+ new</span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              </>
            )}

            {/* Orchestrator Mode Content */}
            {assignMode === 'orchestrator' && (
              <div>
                <div className="text-xs text-gray-400 mb-2 flex items-center gap-1 uppercase tracking-wide">
                  <User size={12} /> Select Supervisor
                </div>
                <p className="text-sm text-gray-500 mb-3">The supervisor will analyze the task and spawn worker agents as needed.</p>
                <div className="space-y-1 max-h-48 overflow-y-auto">
                  {agents.filter(a => a.name.includes('supervisor') || a.name.includes('orchestrator')).length > 0 ? (
                    agents.filter(a => a.name.includes('supervisor') || a.name.includes('orchestrator')).map(a => {
                      const icon = AGENT_ICONS[a.name] || <User size={14} />
                      return (
                        <button
                          key={a.name}
                          onClick={() => assignToNewAgent(a.name)}
                          disabled={assigning}
                          className="w-full text-left p-3 bg-gray-800 hover:bg-purple-900/30 rounded-lg text-sm flex items-center gap-2 disabled:opacity-50"
                        >
                          <span className="text-purple-400">{icon}</span>
                          <span className="flex-1 font-medium">{a.name}</span>
                          <span className="text-xs text-gray-500">supervisor</span>
                        </button>
                      )
                    })
                  ) : (
                    <div className="text-center py-4 text-gray-500 text-sm">
                      No supervisor agents available. Create an agent with "supervisor" in the name.
                    </div>
                  )}
                </div>
              </div>
            )}

            {assigning && (
              <div className="mt-4 text-center text-sm text-amber-400 flex items-center justify-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                Assigning to session...
              </div>
            )}
          </div>
        </div>
      )}

      {/* Spawn Progress Modal */}
      {spawnProgress && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-gray-900 rounded-2xl p-6 w-full max-w-md border border-gray-700 shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center text-purple-400 animate-pulse">
                {AGENT_ICONS[spawnProgress.agent] || <Bot size={20} />}
              </div>
              <div>
                <h3 className="font-semibold text-white">Spawning {spawnProgress.agent}</h3>
                <p className="text-xs text-gray-500">Assigning: {spawnProgress.bead.title}</p>
              </div>
            </div>
            <div className="bg-black/50 rounded-lg p-3 font-mono text-xs space-y-1 max-h-48 overflow-y-auto">
              {spawnProgress.logs.map((log, i) => (
                <div key={i} className={`flex items-center gap-2 ${
                  log.includes('Error') ? 'text-red-400' : 
                  log.includes('✓') ? 'text-emerald-400' : 'text-gray-400'
                }`}>
                  <Terminal size={12} className="text-gray-600" />
                  {log}
                </div>
              ))}
              {!spawnProgress.logs.some(l => l.includes('✓') || l.includes('Error')) && (
                <div className="flex items-center gap-2 text-purple-400">
                  <Loader2 size={12} className="animate-spin" />
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Bead List */}
      <div className="space-y-3">
        {filteredTasks.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
              <Inbox size={32} />
            </div>
            <p>No beads found</p>
          </div>
        ) : (
          parentBeads.map(bead => {
            const priority = PRIORITY_STYLES[(bead.priority || 3) as 1 | 2 | 3]
            const status = STATUS_STYLES[bead.status as keyof typeof STATUS_STYLES] || STATUS_STYLES.open
            const isExpanded = expandedBead === bead.id
            const children = childrenByParent[bead.id] || []
            const hasChildren = children.length > 0
            
            return (
              <div key={bead.id} data-testid="bead-card">
                <div
                  className={`group rounded-xl border transition-all hover:shadow-lg ${priority.bg} ${priority.border}`}
                >
                  {/* Main row */}
                  <div className="p-4 flex items-start gap-3">
                    {/* Priority indicator */}
                    <div className={`w-1.5 self-stretch rounded-full ${priority.badge}`}></div>
                    
                    {/* Content */}
                    <div 
                      className="flex-1 min-w-0 cursor-pointer"
                      onClick={() => setExpandedBead(isExpanded ? null : bead.id)}
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <span className={`px-2.5 py-1 text-xs rounded-full font-medium ${status.bg} ${status.text}`}>
                          {status.label}
                        </span>
                        {/* Assigned/Unassigned indicator */}
                        {bead.assignee ? (
                          <span className="px-2.5 py-1 text-xs rounded-full font-medium bg-purple-500/10 text-purple-400">
                            Assigned
                          </span>
                        ) : (
                          <span className="px-2.5 py-1 text-xs rounded-full font-medium bg-gray-500/10 text-gray-400">
                            Unassigned
                          </span>
                        )}
                        <span className={`text-xs font-medium ${priority.text}`}>P{bead.priority || 3}</span>
                        {hasChildren && (
                          <span className="px-2 py-0.5 text-xs rounded bg-cyan-500/10 text-cyan-400">
                            {children.length} sub-bead{children.length > 1 ? 's' : ''}
                          </span>
                        )}
                        {bead.blocked_by && bead.blocked_by.length > 0 && (
                          <span className="px-2 py-0.5 text-xs rounded bg-orange-500/10 text-orange-400">
                            Blocked
                          </span>
                        )}
                        {bead.description && (
                          <button className="text-xs text-gray-500 flex items-center gap-1">
                            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />} context
                          </button>
                        )}
                      </div>
                      <h3 className="font-medium text-white text-base">{bead.title}</h3>
                      {!isExpanded && bead.description && (
                        <p className="text-sm text-gray-400 mt-1 line-clamp-1">{bead.description}</p>
                      )}
                      {bead.assignee && (
                        <div className="mt-2 flex items-center gap-1.5 text-xs text-purple-400 font-medium">
                          {(() => {
                            const session = sessions.find(s => s.id === bead.assignee)
                            const icon = session ? (AGENT_ICONS[session.agent_name] || <User size={12} />) : <User size={12} />
                            return (
                              <>
                                {icon}
                                <span>{session?.agent_name || 'Unknown'}</span>
                                <span className="text-gray-500 font-mono">({bead.assignee})</span>
                              </>
                            )
                          })()}
                        </div>
                      )}
                      {bead.blocked_by && bead.blocked_by.length > 0 && (
                        <div className="mt-2 text-xs text-orange-400">
                          Blocked by: {bead.blocked_by.join(', ')}
                        </div>
                      )}
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                      {(bead.status === 'open' || (bead.status === 'wip' && !bead.assignee)) && (
                        <button
                          onClick={() => setAssignModal(bead)}
                          className="px-3 py-2 text-sm bg-purple-600 hover:bg-purple-500 rounded-lg text-white font-medium"
                        >
                          Assign
                        </button>
                      )}
                      <button
                        onClick={() => setEditingBead(bead)}
                        className="px-3 py-2 rounded-lg hover:bg-blue-500/20 text-blue-400 text-sm font-medium flex items-center gap-1"
                        title="Edit"
                      >
                        <Edit2 size={14} /> Edit
                      </button>
                      {bead.status !== 'closed' && (
                        <button
                          onClick={() => closeBead(bead.id)}
                          className="px-3 py-2 rounded-lg hover:bg-emerald-500/20 text-emerald-400 text-sm font-medium flex items-center gap-1"
                          title="Mark complete"
                        >
                          <Check size={14} /> Done
                        </button>
                      )}
                      <button
                        onClick={() => deleteBead(bead.id)}
                        className="px-3 py-2 rounded-lg hover:bg-red-500/20 text-red-400 text-sm font-medium"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>

                  {/* Expanded context */}
                  {isExpanded && bead.description && (
                    <div className="px-4 pb-4 pt-0 border-t border-gray-800/50 mt-2">
                      <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">Full Context</div>
                      <div className="text-sm text-gray-300 whitespace-pre-wrap bg-gray-900/50 rounded-lg p-3 max-h-64 overflow-y-auto">
                        {bead.description}
                      </div>
                    </div>
                  )}
                </div>

                {/* Child beads */}
                {hasChildren && (
                  <div className="ml-6 mt-2 space-y-2 border-l-2 border-gray-700 pl-4">
                    {children.map(child => {
                      const childPriority = PRIORITY_STYLES[(child.priority || 3) as 1 | 2 | 3]
                      const childStatus = STATUS_STYLES[child.status as keyof typeof STATUS_STYLES] || STATUS_STYLES.open
                      return (
                        <div
                          key={child.id}
                          data-testid="bead-card"
                          className={`group rounded-lg border p-3 ${childPriority.bg} ${childPriority.border}`}
                        >
                          <div className="flex items-center gap-2 mb-1">
                            <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${childStatus.bg} ${childStatus.text}`}>
                              {childStatus.label}
                            </span>
                            <span className={`text-xs font-medium ${childPriority.text}`}>P{child.priority || 3}</span>
                          </div>
                          <h4 className="font-medium text-white text-sm">{child.title}</h4>
                          {child.assignee && (
                            <div className="mt-1 flex items-center gap-1 text-xs text-purple-400">
                              {(() => {
                                const session = sessions.find(s => s.id === child.assignee)
                                return <span>{session?.agent_name || 'Unknown'}</span>
                              })()}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
