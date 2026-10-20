import { useState, useEffect, useCallback } from 'react'
import {
  api,
  ApiError,
  InstalledPlugin,
  PluginAffectedSession,
  PluginFinding,
  PluginValidationReport,
} from '../api'
import { useStore } from '../store'
import { Puzzle, Trash2, Plus, ShieldAlert, RefreshCw, ChevronDown, ChevronRight } from 'lucide-react'

/**
 * Installed Agent Plugins (Agent Plugins 1.0.0).
 *
 * Not CAO's event-plugin system — that is a separate subsystem with no web
 * surface. See docs/agent-plugins.md versus docs/plugins.md.
 *
 * The remove flow here is a **second, independent enforcement point** for the
 * warn-and-confirm behaviour, not a passive renderer of the API's report. The
 * API can only ever *report* which live sessions reference a skill the plugin
 * provides; a panel that fired DELETE immediately on click would satisfy that
 * contract while gating nothing for a web user. So the affected sessions are
 * rendered as an explicit confirmation step and the DELETE waits for it.
 */

const SEVERITY_STYLE: Record<PluginFinding['severity'], string> = {
  fatal: 'bg-red-900/50 text-red-300 border-red-800',
  skipped: 'bg-amber-900/50 text-amber-300 border-amber-800',
  warning: 'bg-yellow-900/40 text-yellow-300 border-yellow-800',
  info: 'bg-blue-900/40 text-blue-300 border-blue-800',
}

function FindingRow({ finding }: { finding: PluginFinding }) {
  return (
    <li className="flex items-start gap-2 text-xs py-1">
      <span
        className={`px-1.5 py-0.5 rounded border font-mono uppercase shrink-0 ${SEVERITY_STYLE[finding.severity]}`}
      >
        {finding.severity}
      </span>
      <span className="text-gray-500 font-mono shrink-0">{finding.spec_ref}</span>
      <span className="text-gray-500 font-mono shrink-0">{finding.code}</span>
      <span className="text-gray-300">
        {finding.message}
        {finding.path ? <span className="text-gray-500"> ({finding.path})</span> : null}
      </span>
    </li>
  )
}

function AffectedSessions({ sessions }: { sessions: PluginAffectedSession[] }) {
  return (
    <div className="rounded border border-amber-800 bg-amber-950/40 p-3 text-xs space-y-1">
      <p className="text-amber-300 font-medium">
        {sessions.length} live session{sessions.length === 1 ? '' : 's'} reference a skill this
        plugin provides.
      </p>
      <ul className="text-gray-300 space-y-0.5">
        {sessions.map(s => (
          <li key={s.terminal_id}>
            session <span className="font-mono">{s.session_name}</span> · terminal{' '}
            <span className="font-mono">{s.terminal_id}</span> · profile{' '}
            <span className="font-mono">{s.profile_name}</span> · skills:{' '}
            <span className="font-mono">{s.skill_names.join(', ')}</span>
          </li>
        ))}
      </ul>
      <p className="text-gray-400">
        Removing it now can leave an agent that is mid-task holding a stale reference to a skill
        that no longer resolves.
      </p>
    </div>
  )
}

export function PluginsPanel() {
  const { showSnackbar } = useStore()

  const [plugins, setPlugins] = useState<InstalledPlugin[]>([])
  const [warning, setWarning] = useState('')
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Install form
  const [source, setSource] = useState('')
  const [ref, setRef] = useState('')
  const [subdir, setSubdir] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<PluginValidationReport | null>(null)

  // Remove confirmation. `pending` holds the plugin awaiting an explicit
  // confirmation; DELETE is not issued until it is confirmed.
  const [pending, setPending] = useState<InstalledPlugin | null>(null)
  const [purgeData, setPurgeData] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.listPlugins()
      setPlugins(data.plugins)
      setWarning(data.untrusted_content_warning)
    } catch (e) {
      showSnackbar({ type: 'error', message: (e as ApiError).detail || 'Failed to load agent plugins' })
    } finally {
      setLoading(false)
    }
  }, [showSnackbar])

  useEffect(() => {
    load()
  }, [load])

  const toggle = (name: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      next.has(name) ? next.delete(name) : next.add(name)
      return next
    })
  }

  const body = () => ({
    source: source.trim(),
    ref: ref.trim() || undefined,
    subdir: subdir.trim() || undefined,
  })

  const handleValidate = async () => {
    if (!source.trim()) return
    setBusy(true)
    setPreview(null)
    try {
      setPreview(await api.validatePlugin(body()))
    } catch (e) {
      showSnackbar({ type: 'error', message: (e as ApiError).detail || 'Validation failed' })
    } finally {
      setBusy(false)
    }
  }

  const handleInstall = async () => {
    if (!source.trim()) return
    setBusy(true)
    try {
      const outcome = await api.installPlugin(body())
      showSnackbar({ type: 'success', message: `Installed '${outcome.record?.name ?? source}'` })
      setSource('')
      setRef('')
      setSubdir('')
      setPreview(null)
      await load()
    } catch (e) {
      const err = e as ApiError
      // A 422 carries the full validation report, so surface the first fatal
      // finding rather than a bare status.
      const detail = typeof err.detail === 'string' ? err.detail : 'Plugin is not loadable'
      showSnackbar({ type: 'error', message: detail })
      if (err.status === 422) await handleValidate()
    } finally {
      setBusy(false)
    }
  }

  const confirmRemove = async () => {
    if (!pending) return
    const name = pending.name
    setBusy(true)
    try {
      await api.uninstallPlugin(name, purgeData)
      showSnackbar({ type: 'success', message: `Removed '${name}'` })
      setPending(null)
      setPurgeData(false)
      await load()
    } catch (e) {
      showSnackbar({ type: 'error', message: (e as ApiError).detail || `Failed to remove '${name}'` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <Puzzle size={18} /> Agent Plugins
        </h2>
        <button
          onClick={load}
          className="text-sm text-gray-400 hover:text-white flex items-center gap-1.5"
          title="Reload"
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {warning && (
        <div
          role="alert"
          className="rounded-lg border border-red-800 bg-red-950/40 p-3 text-sm text-red-200 flex items-start gap-2"
        >
          <ShieldAlert size={18} className="shrink-0 mt-0.5" />
          <span>{warning}</span>
        </div>
      )}

      {/* Install from a path or a GitHub URL */}
      <div className="rounded-lg border border-gray-800 bg-gray-900/50 p-4 space-y-3">
        <h3 className="text-sm font-medium text-gray-200">Install a plugin</h3>
        <input
          value={source}
          onChange={e => setSource(e.target.value)}
          placeholder="https://github.com/owner/repo  or  /path/to/plugin"
          aria-label="Plugin source"
          className="w-full bg-gray-950 border border-gray-800 rounded px-3 py-2 text-sm text-gray-200"
        />
        <div className="flex gap-3">
          <input
            value={ref}
            onChange={e => setRef(e.target.value)}
            placeholder="branch or tag (git only)"
            aria-label="Git ref"
            className="flex-1 bg-gray-950 border border-gray-800 rounded px-3 py-2 text-sm text-gray-200"
          />
          <input
            value={subdir}
            onChange={e => setSubdir(e.target.value)}
            placeholder="subdirectory (optional)"
            aria-label="Subdirectory"
            className="flex-1 bg-gray-950 border border-gray-800 rounded px-3 py-2 text-sm text-gray-200"
          />
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleValidate}
            disabled={busy || !source.trim()}
            className="px-3 py-2 rounded text-sm bg-gray-800 text-gray-200 hover:bg-gray-700 disabled:opacity-50"
          >
            Validate
          </button>
          <button
            onClick={handleInstall}
            disabled={busy || !source.trim()}
            className="px-3 py-2 rounded text-sm bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50 flex items-center gap-1.5"
          >
            <Plus size={14} /> Install
          </button>
        </div>

        {preview && (
          <div className="rounded border border-gray-800 bg-gray-950 p-3 space-y-2">
            <p className="text-sm">
              <span className="text-gray-400">{preview.name ?? preview.root}: </span>
              <span className={preview.loadable ? 'text-emerald-400' : 'text-red-400'}>
                {preview.loadable ? 'loadable' : 'NOT loadable'}
              </span>
            </p>
            {preview.skills.length > 0 && (
              <p className="text-xs text-gray-400">
                skills: {preview.skills.map(s => s.name).join(', ')}
              </p>
            )}
            {preview.mcp_present && (
              <p className="text-xs text-gray-400">mcp.json: present</p>
            )}
            {preview.findings.length > 0 && (
              <ul>
                {preview.findings.map((f, i) => (
                  <FindingRow key={`${f.code}-${i}`} finding={f} />
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* Installed set */}
      {loading ? (
        <p className="text-gray-500 text-sm">Loading…</p>
      ) : plugins.length === 0 ? (
        <p className="text-gray-500 text-sm">No agent plugins installed</p>
      ) : (
        <ul className="space-y-2">
          {plugins.map(plugin => {
            const open = expanded.has(plugin.name)
            const unprojected = plugin.skill_names.filter(
              n => !plugin.projected_skill_names.includes(n),
            )
            return (
              <li
                key={plugin.name}
                className="rounded-lg border border-gray-800 bg-gray-900/50 p-4 space-y-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <button
                    onClick={() => toggle(plugin.name)}
                    aria-expanded={open}
                    className="flex items-start gap-2 text-left"
                  >
                    {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    <span>
                      <span className="text-white font-medium">{plugin.name}</span>
                      {plugin.version && (
                        <span className="text-gray-500 text-sm"> v{plugin.version}</span>
                      )}
                      <span className="block text-xs text-gray-400">
                        {plugin.projected_skill_names.length > 0
                          ? `skills: ${plugin.projected_skill_names.join(', ')}`
                          : 'contributes no skills'}
                      </span>
                    </span>
                  </button>
                  <button
                    onClick={() => {
                      setPending(plugin)
                      setPurgeData(false)
                    }}
                    className="text-red-400 hover:text-red-300 shrink-0"
                    title={`Remove ${plugin.name}`}
                    aria-label={`Remove ${plugin.name}`}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>

                {open && (
                  <div className="pl-6 space-y-2">
                    <p className="text-xs text-gray-500 font-mono break-all">
                      {plugin.source.kind}: {plugin.source.location}
                      {plugin.resolved_ref ? ` @ ${plugin.resolved_ref.slice(0, 12)}` : ''}
                    </p>
                    {unprojected.length > 0 && (
                      <p className="text-xs text-amber-400">
                        not projected: {unprojected.join(', ')}
                      </p>
                    )}
                    {plugin.findings.length > 0 && (
                      <ul>
                        {plugin.findings.map((f, i) => (
                          <FindingRow key={`${f.code}-${i}`} finding={f} />
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {/* Remove confirmation — the DELETE is issued only from here. */}
      {pending && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`Remove ${pending.name}`}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
        >
          <div className="w-full max-w-lg rounded-lg border border-gray-800 bg-gray-900 p-5 space-y-4">
            <h3 className="text-white font-semibold">Remove &lsquo;{pending.name}&rsquo;?</h3>

            {pending.affected_sessions.length > 0 ? (
              <AffectedSessions sessions={pending.affected_sessions} />
            ) : (
              <p className="text-sm text-gray-400">
                No live session references a skill this plugin provides.
              </p>
            )}

            <label className="flex items-center gap-2 text-sm text-gray-300">
              <input
                type="checkbox"
                checked={purgeData}
                onChange={e => setPurgeData(e.target.checked)}
              />
              Also delete its persistent data directory
            </label>

            <div className="flex justify-end gap-2">
              <button
                onClick={() => setPending(null)}
                className="px-3 py-2 rounded text-sm bg-gray-800 text-gray-200 hover:bg-gray-700"
              >
                Cancel
              </button>
              <button
                onClick={confirmRemove}
                disabled={busy}
                className="px-3 py-2 rounded text-sm bg-red-600 text-white hover:bg-red-500 disabled:opacity-50"
              >
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
