import { useState, useEffect } from 'react'
import { api } from '../api'
import { Plus, ChevronDown, ChevronRight, CheckCircle, Circle, Clock, Loader2, Trash2, Layers, X } from 'lucide-react'

interface Bead {
  id: string; title: string; description?: string; priority?: number
  status: string; assignee?: string; parent_id?: string; type?: string
  labels?: string[]; blocked_by?: string[]
}

interface EpicData {
  epic: Bead; children: Bead[]
  progress: { total: number; completed: number; wip: number; open: number }
}

const STATUS_ICON: Record<string, React.ReactNode> = {
  open: <Circle size={14} className="text-blue-400" />,
  wip: <Clock size={14} className="text-amber-400" />,
  closed: <CheckCircle size={14} className="text-emerald-400" />,
}

const PRIORITY_COLORS: Record<number, string> = {
  1: 'border-red-500/40 bg-red-500/5',
  2: 'border-amber-500/30 bg-amber-500/5',
  3: 'border-gray-700 bg-gray-800/30',
}

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [mode, setMode] = useState<'bead' | 'epic'>('bead')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState(2)
  const [steps, setSteps] = useState([''])
  const [sequential, setSequential] = useState(true)
  const [loading, setLoading] = useState(false)

  const submit = async () => {
    if (!title.trim()) return
    setLoading(true)
    try {
      if (mode === 'epic') {
        const validSteps = steps.filter(s => s.trim()).join(',')
        if (!validSteps) return
        await api.createEpic(title.trim(), validSteps, sequential)
      } else {
        await api.createTask(title.trim(), description || undefined, priority)
      }
      onCreated()
      onClose()
    } catch (e) { console.error(e) }
    setLoading(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="w-[460px] max-h-[80vh] overflow-y-auto rounded-xl border border-gray-700 bg-[#13131a] p-6 shadow-2xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-white">{mode === 'epic' ? 'New Epic' : 'New Bead'}</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-white"><X size={18} /></button>
        </div>

        {/* Mode toggle */}
        <div className="flex gap-1 mb-4 p-1 rounded-lg bg-gray-800">
          {(['bead', 'epic'] as const).map(m => (
            <button key={m} onClick={() => setMode(m)}
              className={`flex-1 py-1.5 rounded text-sm capitalize transition-all ${mode === m ? 'bg-gray-700 text-white' : 'text-gray-500'}`}>
              {m}
            </button>
          ))}
        </div>

        <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Title..."
          className="w-full px-3 py-2 rounded-lg text-sm bg-gray-800 border border-gray-700 text-gray-200 mb-3 focus:border-emerald-500 focus:outline-none" autoFocus />

        {mode === 'bead' && (
          <textarea value={description} onChange={e => setDescription(e.target.value)} placeholder="Description (optional)..."
            className="w-full px-3 py-2 rounded-lg text-sm bg-gray-800 border border-gray-700 text-gray-200 mb-3 h-20 resize-none focus:border-emerald-500 focus:outline-none" />
        )}

        {mode === 'epic' && (
          <div className="mb-3 space-y-2">
            <label className="text-xs text-gray-500">Steps</label>
            {steps.map((s, i) => (
              <div key={i} className="flex gap-2">
                <span className="text-xs text-gray-600 pt-2 w-4">{i + 1}.</span>
                <input value={s} onChange={e => { const n = [...steps]; n[i] = e.target.value; setSteps(n) }}
                  placeholder={`Step ${i + 1}...`}
                  className="flex-1 px-3 py-1.5 rounded text-sm bg-gray-800 border border-gray-700 text-gray-200 focus:border-emerald-500 focus:outline-none" />
                {steps.length > 1 && <button onClick={() => setSteps(steps.filter((_, j) => j !== i))} className="text-red-400 text-sm">×</button>}
              </div>
            ))}
            <button onClick={() => setSteps([...steps, ''])} className="text-xs px-3 py-1 rounded bg-gray-800 text-gray-500 hover:text-gray-300">+ Add Step</button>
            <label className="flex items-center gap-2 mt-2 cursor-pointer">
              <input type="checkbox" checked={sequential} onChange={e => setSequential(e.target.checked)} className="rounded" />
              <span className="text-xs text-gray-500">Sequential (each step depends on previous)</span>
            </label>
          </div>
        )}

        {mode === 'bead' && (
          <div className="flex gap-2 mb-4">
            {[1, 2, 3].map(p => (
              <button key={p} onClick={() => setPriority(p)}
                className={`flex-1 py-1.5 rounded text-sm ${priority === p ? (p === 1 ? 'bg-red-500/20 text-red-400 border border-red-500/40' : p === 2 ? 'bg-amber-500/20 text-amber-400 border border-amber-500/40' : 'bg-gray-700 text-gray-300 border border-gray-600') : 'bg-gray-800 text-gray-500 border border-gray-700'}`}>
                P{p}
              </button>
            ))}
          </div>
        )}

        <button onClick={submit} disabled={loading || !title.trim()}
          className="w-full py-2.5 rounded-lg text-sm font-medium bg-gradient-to-r from-emerald-600 to-emerald-500 text-white hover:from-emerald-500 hover:to-emerald-400 disabled:opacity-50">
          {loading ? 'Creating...' : mode === 'epic' ? 'Create Epic' : 'Create Bead'}
        </button>
      </div>
    </div>
  )
}

function BeadCard({ bead, onRefresh }: { bead: Bead; onRefresh: () => void }) {
  const pClass = PRIORITY_COLORS[bead.priority || 3] || PRIORITY_COLORS[3]

  return (
    <div className={`rounded-lg border p-3 transition-all hover:shadow-md ${pClass}`}>
      <div className="flex items-center gap-2 mb-1">
        {STATUS_ICON[bead.status] || STATUS_ICON.open}
        <span className="text-sm text-white font-medium flex-1 truncate">{bead.title}</span>
        <span className="text-[10px] text-gray-500">P{bead.priority || 3}</span>
        {bead.assignee && <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">assigned</span>}
      </div>
      {bead.description && <p className="text-xs text-gray-500 truncate mt-1">{bead.description}</p>}
    </div>
  )
}

function EpicCard({ epic, onRefresh }: { epic: Bead; onRefresh: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [data, setData] = useState<EpicData | null>(null)
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const d = await api.getEpic(epic.id)
      setData(d)
    } catch {}
    setLoading(false)
  }

  useEffect(() => { if (expanded && !data) load() }, [expanded])

  const progress = data?.progress
  const pct = progress ? (progress.total > 0 ? Math.round(progress.completed / progress.total * 100) : 0) : 0

  return (
    <div className="rounded-xl border border-violet-500/30 bg-violet-500/5 overflow-hidden">
      <button onClick={() => setExpanded(!expanded)}
        className="w-full p-4 flex items-center gap-3 text-left hover:bg-violet-500/10 transition-all">
        {expanded ? <ChevronDown size={16} className="text-violet-400" /> : <ChevronRight size={16} className="text-violet-400" />}
        <Layers size={16} className="text-violet-400" />
        <span className="text-sm font-semibold text-white flex-1">{epic.title}</span>
        <span className="text-[10px] px-2 py-0.5 rounded-full bg-violet-500/20 text-violet-400 border border-violet-500/30">Epic</span>
        {progress && (
          <span className="text-xs text-gray-400">{progress.completed}/{progress.total}</span>
        )}
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-violet-500/20">
          {loading ? (
            <div className="py-4 text-center"><Loader2 size={16} className="animate-spin text-gray-500 mx-auto" /></div>
          ) : data ? (
            <>
              {/* Progress bar */}
              <div className="mt-3 mb-3">
                <div className="flex justify-between text-[10px] text-gray-500 mb-1">
                  <span>Progress</span>
                  <span>{progress!.completed}/{progress!.total} done ({pct}%)</span>
                </div>
                <div className="w-full bg-gray-800 rounded-full h-1.5">
                  <div className="bg-emerald-500 h-1.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
                </div>
              </div>

              {/* Children */}
              <div className="space-y-1.5">
                {data.children.map(child => (
                  <div key={child.id} className="flex items-center gap-2 py-1.5 px-2 rounded bg-gray-800/50">
                    {STATUS_ICON[child.status] || STATUS_ICON.open}
                    <span className="text-xs text-gray-300 flex-1 truncate">{child.title}</span>
                    <span className="text-[10px] text-gray-600 capitalize">{child.status === 'wip' ? 'in progress' : child.status}</span>
                  </div>
                ))}
              </div>
            </>
          ) : null}
        </div>
      )}
    </div>
  )
}

export function BeadsTab() {
  const [beads, setBeads] = useState<Bead[]>([])
  const [filter, setFilter] = useState<'all' | 'open' | 'wip' | 'closed'>('open')
  const [showCreate, setShowCreate] = useState(false)
  const [loading, setLoading] = useState(false)

  const refresh = async () => {
    setLoading(true)
    try {
      const tasks = await api.listTasks(filter === 'all' ? undefined : filter)
      setBeads(tasks)
    } catch {}
    setLoading(false)
  }

  useEffect(() => { refresh() }, [filter])

  // Separate epics (parent beads) from standalone beads
  const epics = beads.filter(b => !b.parent_id && (b.type === 'epic' || beads.some(c => c.parent_id === b.id)))
  const epicIds = new Set(epics.map(e => e.id))
  const childIds = new Set(beads.filter(b => b.parent_id).map(b => b.id))
  const standalone = beads.filter(b => !epicIds.has(b.id) && !childIds.has(b.id))

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">Beads</h2>
          <p className="text-xs text-gray-500 mt-0.5">{beads.length} tasks across {epics.length} epic{epics.length !== 1 ? 's' : ''}</p>
        </div>
        <button onClick={() => setShowCreate(true)}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-gradient-to-r from-emerald-600 to-emerald-500 text-white hover:from-emerald-500 hover:to-emerald-400 flex items-center gap-2 shadow-lg shadow-emerald-500/20">
          <Plus size={16} /> New
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-1 p-1 rounded-lg bg-gray-800/50">
        {(['all', 'open', 'wip', 'closed'] as const).map(f => (
          <button key={f} onClick={() => setFilter(f)}
            className={`flex-1 py-1.5 rounded text-xs font-medium capitalize transition-all ${filter === f ? 'bg-gray-700 text-white' : 'text-gray-500 hover:text-gray-300'}`}>
            {f === 'wip' ? 'In Progress' : f === 'closed' ? 'Done' : f}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-center py-12"><Loader2 size={24} className="animate-spin text-gray-500 mx-auto" /></div>
      ) : (
        <>
          {/* Epics */}
          {epics.length > 0 && (
            <div className="space-y-3">
              <h3 className="text-xs text-gray-500 uppercase tracking-wide">Epics</h3>
              {epics.map(epic => <EpicCard key={epic.id} epic={epic} onRefresh={refresh} />)}
            </div>
          )}

          {/* Standalone beads */}
          {standalone.length > 0 && (
            <div className="space-y-2">
              {epics.length > 0 && <h3 className="text-xs text-gray-500 uppercase tracking-wide">Tasks</h3>}
              {standalone.map(bead => <BeadCard key={bead.id} bead={bead} onRefresh={refresh} />)}
            </div>
          )}

          {beads.length === 0 && (
            <div className="text-center py-12">
              <Layers size={32} className="text-gray-700 mx-auto mb-3" />
              <p className="text-gray-500 text-sm">No beads yet</p>
              <p className="text-gray-600 text-xs mt-1">Create a bead or epic to get started</p>
            </div>
          )}
        </>
      )}

      {showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={refresh} />}
    </div>
  )
}
