import { useState, useEffect } from 'react'
import { api, AgentDirsSettings } from '../api'
import { useStore } from '../store'
import { FolderOpen, Plus, X, RefreshCw } from 'lucide-react'

/** A small on/off switch (GH #280). */
function Toggle({ on, onClick, disabled, label }: {
  on: boolean
  onClick: () => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      title={on ? 'Enabled — click to skip this directory' : 'Disabled — click to scan this directory'}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
        on ? 'bg-emerald-600' : 'bg-gray-600'
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
          on ? 'translate-x-[18px]' : 'translate-x-1'
        }`}
      />
    </button>
  )
}

export function SettingsPanel() {
  const [settings, setSettings] = useState<AgentDirsSettings | null>(null)
  const [newDir, setNewDir] = useState('')
  const [busy, setBusy] = useState(false)
  const [profileCount, setProfileCount] = useState<number | null>(null)
  const [dupCount, setDupCount] = useState(0)
  const { showSnackbar } = useStore()

  const load = async () => {
    try {
      setSettings(await api.getAgentDirs())
    } catch {
      showSnackbar({ type: 'error', message: 'Failed to load settings' })
    }
  }

  const refreshProfiles = async () => {
    try {
      const profiles = await api.listProfiles()
      setProfileCount(profiles.length)
      setDupCount(profiles.filter(p => (p.duplicated_in?.length ?? 0) > 0).length)
    } catch {}
  }

  useEffect(() => {
    load()
    refreshProfiles()
  }, [])

  // Every mutation persists immediately and re-reads the effective server
  // state, so the UI never claims a save that didn't stick (GH #281).
  const apply = async (
    data: { extra_dirs?: string[]; disabled_dirs?: string[] },
    message: string,
  ) => {
    setBusy(true)
    try {
      const result = await api.setAgentDirs(data)
      setSettings(result)
      showSnackbar({ type: 'success', message })
      refreshProfiles()
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Failed to update' })
      await load()
    } finally {
      setBusy(false)
    }
  }

  if (!settings) {
    return <div className="text-gray-500 text-sm py-8 text-center">Loading settings...</div>
  }

  const disabled = new Set(settings.disabled_dirs ?? [])
  // Provider defaults are de-duped by path (claude_code & codex share one).
  const defaultDirs = Array.from(new Set(Object.values(settings.agent_dirs))).filter(Boolean)
  const extraDirs = settings.extra_dirs.filter(d => !defaultDirs.includes(d))

  const toggle = (dir: string) => {
    const next = new Set(disabled)
    if (next.has(dir)) {
      next.delete(dir)
    } else {
      next.add(dir)
    }
    apply(
      { disabled_dirs: Array.from(next) },
      next.has(dir) ? 'Directory disabled' : 'Directory enabled',
    )
  }

  const addDir = () => {
    const trimmed = newDir.trim()
    if (!trimmed || extraDirs.includes(trimmed) || defaultDirs.includes(trimmed)) return
    setNewDir('')
    apply({ extra_dirs: [...settings.extra_dirs, trimmed] }, 'Directory added')
  }

  const removeDir = (dir: string) => {
    apply(
      {
        extra_dirs: settings.extra_dirs.filter(d => d !== dir),
        disabled_dirs: (settings.disabled_dirs ?? []).filter(d => d !== dir),
      },
      'Directory removed',
    )
  }

  const row = (dir: string, isDefault: boolean) => {
    const off = disabled.has(dir)
    return (
      <div
        key={dir}
        data-testid={`dir-row-${dir}`}
        className={`flex items-center gap-2.5 bg-gray-900/50 border border-gray-700/30 rounded-lg px-3 py-2.5 ${
          off ? 'opacity-55' : ''
        }`}
      >
        <FolderOpen size={14} className={off ? 'text-gray-500 shrink-0' : 'text-emerald-500 shrink-0'} />
        <span className="text-sm text-gray-300 font-mono flex-1 truncate" title={dir}>{dir}</span>
        {isDefault && (
          <span className="text-[10px] uppercase tracking-wide text-gray-500 shrink-0">default</span>
        )}
        <Toggle on={!off} onClick={() => toggle(dir)} disabled={busy} label={`Enable ${dir}`} />
        {!isDefault && (
          <button
            onClick={() => removeDir(dir)}
            disabled={busy}
            className="text-gray-500 hover:text-red-400 transition-colors shrink-0 disabled:opacity-40"
            title="Remove directory"
            aria-label={`Remove ${dir}`}
          >
            <X size={14} />
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Agent Profile Directories
          </h3>
          {profileCount !== null && (
            <span className="text-xs text-gray-500" data-testid="profile-count">
              {profileCount} profiles discovered
            </span>
          )}
        </div>
        <p className="text-xs text-gray-500 mb-4">
          CAO scans these directories for agent profile <code className="text-gray-400">.md</code> files.
          Toggle a directory off to skip it during scanning without removing it — e.g. to park
          experimental copies, or to swap between two directories of same-named agents (say, wired to
          different providers). Built-in defaults can be disabled but not removed.
        </p>

        {defaultDirs.length > 0 && (
          <>
            <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">Built-in</div>
            <div className="space-y-2 mb-4">{defaultDirs.map(d => row(d, true))}</div>
          </>
        )}

        <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">Custom</div>
        {extraDirs.length > 0 ? (
          <div className="space-y-2 mb-4">{extraDirs.map(d => row(d, false))}</div>
        ) : (
          <div className="text-center py-5 mb-4 bg-gray-900/30 border border-dashed border-gray-700 rounded-lg">
            <p className="text-gray-500 text-sm">No custom directories.</p>
            <p className="text-gray-600 text-xs mt-1">Add one below to discover more agent profiles.</p>
          </div>
        )}

        <div className="flex gap-2">
          <input
            type="text"
            value={newDir}
            onChange={e => setNewDir(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && addDir()}
            placeholder="/path/to/agent-profiles"
            className="flex-1 bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 font-mono focus:border-emerald-500 focus:outline-none"
          />
          <button
            onClick={addDir}
            disabled={!newDir.trim() || busy}
            className="flex items-center gap-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white text-sm px-4 py-2.5 rounded-lg transition-colors"
          >
            <Plus size={14} /> Add
          </button>
        </div>

        {dupCount > 0 && (
          <p className="text-xs text-amber-400/80 mt-4" data-testid="dup-note">
            {dupCount} profile name{dupCount === 1 ? '' : 's'} defined in more than one enabled
            directory — the first-scanned one wins. Disable a directory to change which is active.
          </p>
        )}
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={() => { refreshProfiles(); showSnackbar({ type: 'info', message: 'Refreshing profiles...' }) }}
          className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white text-sm px-4 py-2.5 rounded-lg transition-colors"
        >
          <RefreshCw size={14} /> Refresh Profiles
        </button>
      </div>
    </div>
  )
}
