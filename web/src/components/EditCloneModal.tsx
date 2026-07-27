import { useEffect, useState } from 'react'
import { api, AgentProfileDetail, ProfileWriteResult, ProviderInfo, ApiError } from '../api'
import { CustomSelect } from './CustomSelect'
import { X, Check, Loader2, AlertTriangle, Copy, Pencil } from 'lucide-react'

// #510 U4: edit a local profile / clone a built-in. One modal, two modes:
//  - 'edit'  → PUT /agents/profiles/{name} (in-place, local only; server
//              re-validates and enforces local-store containment).
//  - 'clone' → POST /agents/profiles/from-content with a NEW local name (the
//              packaged built-in is never mutated — AC6.2).
// The modal prefills the profile CONTENT from api.getProfile and exposes
// provider/model as explicit REQUIRED fields (ADR-006). All validation and
// containment is server-side; this component collects input and surfaces errors.

type Mode = 'edit' | 'clone'

function reconstructContent(profile: AgentProfileDetail): string {
  // Rebuild an editable frontmatter document from the parsed profile. JSON is
  // valid YAML, so a JSON frontmatter block round-trips through the server's
  // frontmatter parser (verified). system_prompt is the body, not frontmatter.
  const { system_prompt, ...meta } = profile
  const fm = JSON.stringify(meta, null, 2)
  return `---\n${fm}\n---\n\n${system_prompt ?? ''}`
}

export function EditCloneModal({ mode, sourceName, onClose }: {
  mode: Mode
  sourceName: string
  onClose: (saved?: ProfileWriteResult) => void
}) {
  const [content, setContent] = useState('')
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [cloneName, setCloneName] = useState(mode === 'clone' ? `${sourceName}-copy` : sourceName)
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    api.listProviders().then(setProviders).catch(() => setProviders([]))
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    api.getProfile(sourceName)
      .then(p => {
        if (cancelled) return
        setContent(reconstructContent(p))
        // Seed provider/model from the loaded profile, but they remain explicit
        // editable required fields — never silently inherited on save.
        if (typeof p.provider === 'string') setProvider(p.provider)
        if (typeof p.model === 'string') setModel(p.model)
        setLoading(false)
      })
      .catch((e: ApiError) => { if (!cancelled) { setLoadError(e.detail || e.message); setLoading(false) } })
    return () => { cancelled = true }
  }, [sourceName])

  const providerModelReady = !!provider.trim() && !!model.trim()
  const nameReady = mode === 'edit' || !!cloneName.trim()
  const canSave = providerModelReady && nameReady && !!content.trim() && !saving

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      let saved: ProfileWriteResult
      if (mode === 'edit') {
        // In-place edit of the local profile; server re-validates (FR5/AC5.1).
        saved = await api.updateProfile(sourceName, {
          content, provider: provider.trim(), model: model.trim(),
        })
      } else {
        // Clone → NEW local profile from content; built-in untouched (FR6/AC6.2).
        saved = await api.createProfileFromContent({
          name: cloneName.trim(), content, provider: provider.trim(), model: model.trim(),
        })
      }
      onClose(saved)
    } catch (e) {
      const err = e as ApiError
      // Surfaces server errors: validation [error]s, overwrite refusal, or the
      // containment/local-store guard.
      setSaveError(err.detail || err.message)
      setSaving(false)
    }
  }

  const providerOptions = providers.map(p => ({
    value: p.name,
    label: p.name.replace(/_/g, ' '),
    sublabel: !p.installed ? 'Not installed' : undefined,
  }))

  const title = mode === 'edit' ? `Edit ${sourceName}` : `Clone ${sourceName}`

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => onClose()} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="relative bg-gray-800 border border-gray-700 rounded-2xl shadow-2xl shadow-black/50 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto"
      >
        <div className="flex items-center justify-between p-5 border-b border-gray-700/50">
          <div className="flex items-center gap-2">
            {mode === 'edit' ? <Pencil size={16} className="text-emerald-400" /> : <Copy size={16} className="text-blue-400" />}
            <div>
              <h3 className="text-base font-semibold text-gray-200">{title}</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                {mode === 'edit'
                  ? 'Edit this local profile. Saved changes are re-validated server-side.'
                  : 'Create a new local profile from this built-in. The built-in is never modified.'}
              </p>
            </div>
          </div>
          <button onClick={() => onClose()} aria-label="Close" className="p-1.5 text-gray-500 hover:text-gray-300 rounded-lg hover:bg-gray-700/50">
            <X size={18} />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {loading ? (
            <p className="text-sm text-gray-500">Loading profile…</p>
          ) : loadError ? (
            <p role="alert" className="text-sm text-red-300">Failed to load profile: {loadError}</p>
          ) : (
            <>
              {mode === 'clone' && (
                <div>
                  <label htmlFor="clone-name" className="block text-xs text-gray-500 mb-1">
                    New profile name<span className="text-red-400 ml-0.5">*</span>
                  </label>
                  <input
                    id="clone-name"
                    type="text"
                    value={cloneName}
                    onChange={e => setCloneName(e.target.value)}
                    aria-required="true"
                    placeholder="new-local-name"
                    className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2 focus:border-emerald-500 focus:outline-none"
                  />
                </div>
              )}

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-500 mb-1">
                    Provider<span className="text-red-400 ml-0.5">*</span>
                  </label>
                  {providerOptions.length > 0 ? (
                    <CustomSelect value={provider} onChange={setProvider} placeholder="Select provider…" options={providerOptions} />
                  ) : (
                    <input
                      type="text" value={provider} onChange={e => setProvider(e.target.value)}
                      aria-required="true" placeholder="e.g. claude_code"
                      className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none"
                    />
                  )}
                </div>
                <div>
                  <label htmlFor="edit-model" className="block text-xs text-gray-500 mb-1">
                    Model<span className="text-red-400 ml-0.5">*</span>
                  </label>
                  <input
                    id="edit-model" type="text" value={model} onChange={e => setModel(e.target.value)}
                    aria-required="true" placeholder="e.g. claude-sonnet-4"
                    className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="profile-content" className="block text-xs text-gray-500 mb-1">
                  Profile content
                </label>
                <textarea
                  id="profile-content"
                  value={content}
                  onChange={e => setContent(e.target.value)}
                  spellCheck={false}
                  rows={14}
                  className="w-full bg-gray-950 border border-gray-700 text-gray-200 text-[12px] font-mono rounded-lg px-3 py-2 focus:border-emerald-500 focus:outline-none"
                />
                <p className="text-[10px] text-gray-600 mt-0.5">
                  Frontmatter + body. The server re-validates against the agent profile schema on save.
                </p>
              </div>

              {!providerModelReady && (
                <p className="text-[11px] text-amber-300 flex items-center gap-1">
                  <AlertTriangle size={11} /> Provider and model are required.
                </p>
              )}
              {saveError && <p role="alert" className="text-xs text-red-300">Save failed: {saveError}</p>}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-3 p-5 border-t border-gray-700/50">
          <button onClick={() => onClose()} className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors">
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={!canSave}
            className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
            {saving ? 'Saving…' : mode === 'edit' ? 'Save Changes' : 'Create Clone'}
          </button>
        </div>
      </div>
    </div>
  )
}
