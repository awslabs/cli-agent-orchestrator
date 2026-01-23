import { useState, useEffect } from 'react'
import { useStore } from '../store'
import { api } from '../api'
import { Plus, Edit2, Check, Trash2, ChevronDown, ChevronRight, Inbox, Bot, Wrench, Search, Shield, Swords, Mail, Map, RefreshCw, Package, User, Sparkles, Loader2 } from 'lucide-react'

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
  'ralph-wiggum': <RefreshCw size={14} />,
  'amzn-builder': <Package size={14} />
}

interface Bead {
  id: string
  title: string
  description?: string
  priority?: number
  status: string
  assignee?: string
}

export function BeadsPanel() {
  const { tasks, setTasks, sessions } = useStore()
  const [showCreate, setShowCreate] = useState(false)
  const [editingBead, setEditingBead] = useState<Bead | null>(null)
  const [expandedBead, setExpandedBead] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'open' | 'wip' | 'closed'>('open')
  const [newBead, setNewBead] = useState({ title: '', description: '', priority: 2 })
  const [aiText, setAiText] = useState('')
  const [aiGenerating, setAiGenerating] = useState(false)

  const refresh = () => api.tasks.list().then(setTasks).catch(() => {})
  
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
      await api.tasks.assign(beadId, sessionId)
      refresh()
    }
  }

  const closeBead = async (id: string) => {
    await api.tasks.close(id)
    refresh()
  }

  const filtered = tasks.filter(t => filter === 'all' || t.status === filter)
    .sort((a, b) => (a.priority || 3) - (b.priority || 3))

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
      {/* AI Bead Generation */}
      <div className="p-4 rounded-xl border border-purple-500/30 bg-purple-500/5">
        <div className="flex items-center gap-2 mb-2">
          <Sparkles size={16} className="text-purple-400" />
          <span className="text-sm font-medium text-purple-300">AI Bead Generator</span>
        </div>
        <textarea
          value={aiText}
          onChange={e => setAiText(e.target.value)}
          placeholder="Paste requirements, task list, or PRD here..."
          className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm focus:border-purple-500 focus:outline-none resize-none"
          rows={3}
        />
        <button
          onClick={generateBeads}
          disabled={!aiText.trim() || aiGenerating}
          className="mt-2 px-4 py-2 text-sm rounded-lg bg-purple-600 hover:bg-purple-500 text-white font-medium transition-all flex items-center gap-2 disabled:opacity-50"
        >
          {aiGenerating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          {aiGenerating ? 'Generating...' : 'Generate Beads'}
        </button>
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

      {/* Bead List */}
      <div className="space-y-3">
        {filtered.length === 0 ? (
          <div className="text-center py-12 text-gray-500">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-800 flex items-center justify-center text-gray-600">
              <Inbox size={32} />
            </div>
            <p>No beads found</p>
          </div>
        ) : (
          filtered.map(bead => {
            const priority = PRIORITY_STYLES[(bead.priority || 3) as 1 | 2 | 3]
            const status = STATUS_STYLES[bead.status as keyof typeof STATUS_STYLES] || STATUS_STYLES.open
            const isExpanded = expandedBead === bead.id
            
            return (
              <div
                key={bead.id}
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
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    {bead.status === 'open' && sessions.length > 0 && (
                      <select
                        onChange={e => e.target.value && assignBead(bead.id, e.target.value)}
                        className="px-3 py-2 text-sm bg-gray-800 border border-gray-700 rounded-lg text-white cursor-pointer"
                        defaultValue=""
                      >
                        <option value="">Assign to...</option>
                        {sessions.map(s => {
                          const hasAssigned = tasks.some(t => t.assignee === s.id)
                          return (
                            <option key={s.id} value={s.id}>
                              {s.agent_name} {hasAssigned ? '(busy)' : '(idle)'}
                            </option>
                          )
                        })}
                      </select>
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
            )
          })
        )}
      </div>
    </div>
  )
}
