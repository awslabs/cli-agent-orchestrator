import { useState, useEffect } from 'react'
import { api } from '../api'
import { X, Folder, ArrowUp, Check, Loader2 } from 'lucide-react'

/**
 * In-app folder picker (GH #282). Lists the BACKEND's folders — so the path
 * the user picks is valid where profiles are scanned and agents run, whether
 * that's WSL, Docker, or a remote host. Directories only; no file contents.
 */
export function FolderBrowser({ title, initialPath, onSelect, onClose }: {
  title?: string
  initialPath?: string
  onSelect: (path: string) => void
  onClose: () => void
}) {
  const [path, setPath] = useState('')
  const [parent, setParent] = useState<string | null>(null)
  const [dirs, setDirs] = useState<string[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const navigate = (target?: string) => {
    setLoading(true)
    api.listDirs(target)
      .then(r => {
        setPath(r.path)
        setParent(r.parent)
        setDirs(r.dirs)
        setError('')
      })
      .catch((e: any) => setError(e.message || 'Could not open that folder'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { navigate(initialPath || undefined) }, [])

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" data-testid="folder-browser">
      <div className="bg-[#16161e] border border-gray-800 rounded-xl w-full max-w-lg p-5 flex flex-col max-h-[70vh]">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-base font-medium text-gray-100">{title || 'Choose a folder'}</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300" aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <button
            onClick={() => parent && navigate(parent)}
            disabled={!parent || loading}
            className="flex items-center gap-1 text-xs px-2 py-1.5 rounded bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-30 shrink-0"
            title="Up one folder"
            data-testid="folder-up"
          >
            <ArrowUp size={13} /> Up
          </button>
          <span className="text-xs text-gray-400 font-mono truncate" title={path}>{path}</span>
        </div>

        <div className="flex-1 overflow-auto border border-gray-800 rounded-lg bg-[#0f0f14] mb-3 min-h-[200px]">
          {loading && (
            <div className="flex items-center justify-center py-10 text-gray-500">
              <Loader2 size={18} className="animate-spin" />
            </div>
          )}
          {!loading && error && <p className="text-sm text-red-400 p-3">{error}</p>}
          {!loading && !error && dirs.length === 0 && (
            <p className="text-sm text-gray-600 p-3">No subfolders here.</p>
          )}
          {!loading && !error && dirs.map(d => (
            <button
              key={d}
              onClick={() => navigate(`${path === '/' ? '' : path}/${d}`)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm text-gray-300 hover:bg-gray-800/60"
              data-testid={`folder-entry-${d}`}
            >
              <Folder size={14} className="text-emerald-500 shrink-0" />
              <span className="truncate">{d}</span>
            </button>
          ))}
        </div>

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-2 text-sm text-gray-400 hover:text-gray-200">
            Cancel
          </button>
          <button
            onClick={() => { onSelect(path); onClose() }}
            disabled={loading || !!error}
            className="flex items-center gap-1.5 px-4 py-2 text-sm bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg disabled:opacity-40"
            data-testid="folder-select"
          >
            <Check size={14} /> Select this folder
          </button>
        </div>
      </div>
    </div>
  )
}
