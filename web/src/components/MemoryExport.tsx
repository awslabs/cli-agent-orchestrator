// MemoryExport — download one memory scope as an OKF tarball
// (GET /memory/export, the `cao memory export` equivalent).
//
// Only the shareable scopes are offered: the server refuses session/agent over
// HTTP by design. Project scope needs a scope_id; it is picked from the project
// ids the memory list already discovered (typing one is allowed too). Redact
// asks the server to scrub secrets-looking values before bundling.

import { useEffect, useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { api, MemorySummary } from '../api'
import { useStore } from '../store'

export function MemoryExport({ memories }: { memories: MemorySummary[] }) {
  const showSnackbar = useStore(s => s.showSnackbar)
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<'global' | 'project'>('global')
  const [scopeId, setScopeId] = useState('')
  const [redact, setRedact] = useState(true)
  const [busy, setBusy] = useState(false)

  const projectIds = [...new Set(memories.filter(m => m.scope === 'project' && m.scope_id).map(m => m.scope_id as string))]
  useEffect(() => {
    if (scope === 'project' && !scopeId && projectIds.length) setScopeId(projectIds[0])
  }, [scope, scopeId, projectIds.join('|')])

  const download = async () => {
    if (busy || (scope === 'project' && !scopeId.trim())) return
    setBusy(true)
    try {
      const { blob, filename } = await api.exportMemory({
        scope,
        scopeId: scope === 'project' ? scopeId.trim() : undefined,
        redact,
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      showSnackbar({ type: 'success', message: `Exported ${scope} memories` })
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e?.detail || e?.message || 'Export failed' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 border border-gray-700 hover:bg-gray-800 text-gray-200 text-sm font-medium px-3 py-2 rounded-lg transition-colors"
      >
        <Download size={14} aria-hidden="true" /> Export (OKF)
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-72 space-y-2 rounded-lg border border-gray-700 bg-gray-900 p-3 shadow-lg text-xs">
          <label className="block">
            <span className="text-gray-400">Scope</span>
            <select
              aria-label="Export scope"
              value={scope}
              onChange={e => setScope(e.target.value as 'global' | 'project')}
              className="mt-1 w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            >
              <option value="global">global</option>
              <option value="project">project</option>
            </select>
          </label>
          {scope === 'project' && (
            <label className="block">
              <span className="text-gray-400">Project scope id</span>
              <input
                aria-label="Project scope id"
                list="memory-export-project-ids"
                value={scopeId}
                onChange={e => setScopeId(e.target.value)}
                className="mt-1 w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 font-mono text-gray-200"
              />
              <datalist id="memory-export-project-ids">
                {projectIds.map(id => <option key={id} value={id} />)}
              </datalist>
            </label>
          )}
          <label className="flex items-center gap-2 text-gray-300">
            <input type="checkbox" checked={redact} onChange={e => setRedact(e.target.checked)} />
            Redact secrets
          </label>
          <p className="text-gray-500">Session and agent memories are private and are never exported here.</p>
          <button
            type="button"
            onClick={download}
            disabled={busy || (scope === 'project' && !scopeId.trim())}
            className="inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-3 py-1.5 font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {busy ? <Loader2 size={13} className="animate-spin" aria-hidden="true" /> : <Download size={13} aria-hidden="true" />}
            Download
          </button>
        </div>
      )}
    </div>
  )
}
