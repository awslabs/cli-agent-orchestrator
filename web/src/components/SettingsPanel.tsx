import { useState, useEffect } from 'react'
import { api, AgentDirsSettings } from '../api'
import { useStore } from '../store'
import { FolderOpen, Save, Plus, X, RefreshCw, CheckCircle } from 'lucide-react'

export function SettingsPanel() {
  const [settings, setSettings] = useState<AgentDirsSettings | null>(null)
  const [dirs, setDirs] = useState<string[]>([])
  const [newDir, setNewDir] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [profileCount, setProfileCount] = useState<number | null>(null)
  const { showSnackbar } = useStore()

  const load = async () => {
    try {
      const s = await api.getAgentDirs()
      setSettings(s)
      // Merge all configured dirs into a single flat list, deduped
      const allDirs = [
        ...Object.values(s.agent_dirs),
        ...s.extra_dirs,
      ].filter((d, i, arr) => d && arr.indexOf(d) === i)
      setDirs(allDirs)
    } catch {
      showSnackbar({ type: 'error', message: 'Failed to load settings' })
    }
  }

  const refreshProfiles = async () => {
    try {
      const profiles = await api.listProfiles()
      setProfileCount(profiles.length)
    } catch {}
  }

  useEffect(() => {
    load()
    refreshProfiles()
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setSaved(false)
    try {
      // Send all dirs as extra_dirs — the backend will scan all of them
      const result = await api.setAgentDirs({ extra_dirs: dirs })
      setSettings(result)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      showSnackbar({ type: 'success', message: 'Settings saved' })
      refreshProfiles()
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Failed to save' })
    } finally {
      setSaving(false)
    }
  }

  const addDir = () => {
    const trimmed = newDir.trim()
    if (trimmed && !dirs.includes(trimmed)) {
      setDirs([...dirs, trimmed])
      setNewDir('')
    }
  }

  const removeDir = (idx: number) => {
    setDirs(dirs.filter((_, i) => i !== idx))
  }

  if (!settings) {
    return <div className="text-gray-500 text-sm py-8 text-center">Loading settings...</div>
  }

  return (
    <div className="space-y-6">
      {/* Agent Profile Directories */}
      <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Agent Profile Directories
          </h3>
          {profileCount !== null && (
            <span className="text-xs text-gray-500">{profileCount} profiles discovered</span>
          )}
        </div>
        <p className="text-xs text-gray-500 mb-2">
          Add directories where your agent profile <code className="text-gray-400">.md</code> files are stored.
          CAO scans all directories and makes profiles available to every provider.
        </p>
        <p className="text-xs text-emerald-400/70 mb-5">
          Install built-in profiles with: <code className="bg-gray-900 px-1.5 py-0.5 rounded text-emerald-300">cao install developer</code>
        </p>

        {dirs.length > 0 && (
          <div className="space-y-2 mb-4">
            {dirs.map((dir, i) => (
              <div key={i} className="flex items-center gap-2 bg-gray-900/50 border border-gray-700/30 rounded-lg px-3 py-2.5">
                <FolderOpen size={14} className="text-emerald-500 shrink-0" />
                <span className="text-sm text-gray-300 font-mono flex-1 truncate" title={dir}>{dir}</span>
                <button
                  onClick={() => removeDir(i)}
                  className="text-gray-500 hover:text-red-400 transition-colors shrink-0"
                  title="Remove directory"
                >
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        )}

        {dirs.length === 0 && (
          <div className="text-center py-6 mb-4 bg-gray-900/30 border border-dashed border-gray-700 rounded-lg">
            <FolderOpen size={24} className="mx-auto text-gray-600 mb-2" />
            <p className="text-gray-500 text-sm">No directories configured.</p>
            <p className="text-gray-600 text-xs mt-1">Add a directory below to start discovering agent profiles.</p>
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
            disabled={!newDir.trim()}
            className="flex items-center gap-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white text-sm px-4 py-2.5 rounded-lg transition-colors"
          >
            <Plus size={14} /> Add
          </button>
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white text-sm font-medium px-5 py-2.5 rounded-lg transition-colors"
        >
          {saved ? <CheckCircle size={16} /> : <Save size={16} />}
          {saving ? 'Saving...' : saved ? 'Saved' : 'Save Settings'}
        </button>
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
