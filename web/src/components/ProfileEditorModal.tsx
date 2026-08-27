import { useEffect, useState } from 'react'
import { api, ApiError, ProfileValidationMessage } from '../api'
import { rewriteFrontmatterName } from './ProfileCreateModal'
import { ValidationFindings } from './ValidationFindings'
import { AlertTriangle, FilePen, Loader2, X } from 'lucide-react'

interface ProfileEditorModalProps {
  open: boolean
  /**
   * edit: PUT back to the same local-store profile; the name is fixed.
   * clone: POST a copy under a new name — the path for built-in, provider,
   * and custom profiles, which the write routes deliberately 404
   * (they resolve only inside the local store).
   */
  mode: 'edit' | 'clone'
  /** The profile whose source is loaded. */
  name: string
  onClose: () => void
  onSaved: (name: string, warnings: ProfileValidationMessage[]) => void
}

const inputClass =
  'w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600'

/**
 * Raw document editor over `GET /agents/profiles/{name}/source`. Deliberately
 * NOT the schema form: an edit must round-trip the exact stored bytes
 * (placeholders intact, key order preserved, comments untouched), which a
 * parse-and-regenerate form cannot guarantee. Authoring-by-form is the create
 * modal's job; editing is the document's.
 */
export function ProfileEditorModal({ open, mode, name, onClose, onSaved }: ProfileEditorModalProps) {
  const [content, setContent] = useState('')
  const [cloneName, setCloneName] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [findings, setFindings] = useState<ProfileValidationMessage[]>([])

  useEffect(() => {
    if (!open) return
    setContent('')
    setCloneName(mode === 'clone' ? `${name}-copy` : '')
    setLoading(true)
    setLoadError(null)
    setSaveError(null)
    setFindings([])
    api.getProfileSource(name)
      .then(res => setContent(res.content))
      .catch((e: ApiError) => setLoadError(e?.detail || e?.message || 'Failed to load profile source'))
      .finally(() => setLoading(false))
  }, [open, mode, name])

  if (!open) return null

  const targetName = mode === 'edit' ? name : cloneName.trim()
  const canSave = !saving && !loading && !loadError && content.trim() !== '' && targetName !== ''

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    setFindings([])
    try {
      const document = mode === 'edit' ? content : rewriteFrontmatterName(content, targetName)
      // Validate before every save (#510 stage 5): errors block, warnings
      // render but allow. Validation runs on the exact document being
      // persisted — for a clone that includes the rewritten frontmatter name.
      try {
        const check = await api.validateProfile(document)
        setFindings(check.messages ?? [])
        if (!check.valid) {
          setSaveError('Validation failed — fix the errors below before saving.')
          return
        }
      } catch (ve: any) {
        setSaveError((ve as ApiError)?.detail || ve?.message || 'Validation failed')
        return
      }
      const res = mode === 'edit'
        ? await api.replaceProfile(name, document)
        : await api.createProfile(targetName, document)
      onSaved(res.name, res.warnings ?? [])
      onClose()
    } catch (e: any) {
      const err = e as ApiError
      if (err.status === 409) {
        setSaveError(err.detail || `A profile named '${targetName}' already exists.`)
      } else if (err.status === 404) {
        setSaveError(err.detail || `Profile '${name}' is not editable: only local-store profiles can be replaced.`)
      } else {
        setSaveError(err.detail || err.message || 'Failed to save profile')
        // Same {severity, message, path} shape as validate findings — render
        // through the findings panel rather than a separate flat list.
        const meta = err.detailMeta as { errors?: ProfileValidationMessage[] } | undefined
        if (meta?.errors?.length) setFindings(meta.errors)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center" role="dialog" aria-label={mode === 'edit' ? 'Edit profile' : 'Clone profile'}>
      {/* Backdrop close is gated on !saving: Cancel is already disabled
          during a save, and an overlay click mid-flight would unmount the
          modal while the request resolves — success snackbar with no modal,
          setState on an unmounted tree. */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={saving ? undefined : onClose} />
      <div className="relative bg-gray-900 border border-gray-700/50 rounded-2xl shadow-2xl w-full max-w-3xl mx-4 max-h-[85vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 p-5 pb-3 border-b border-gray-800">
          <div className="w-9 h-9 rounded-xl bg-blue-900/50 flex items-center justify-center">
            <FilePen size={18} className="text-blue-400" />
          </div>
          <h3 className="text-base font-semibold text-white flex-1">
            {mode === 'edit' ? <>Edit <span className="font-mono">{name}</span></> : <>Clone <span className="font-mono">{name}</span></>}
          </h3>
          <button onClick={onClose} aria-label="Close" className="p-1 text-gray-500 hover:text-white rounded transition-colors">
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {mode === 'clone' && (
            <div>
              <label className="block text-xs text-gray-400 mb-1" htmlFor="clone-name">
                <span className="text-gray-300">New profile name</span>
                <span className="text-red-400 ml-0.5">*</span>
                <span className="block text-gray-600 mt-0.5">
                  The copy is created in the local store; the frontmatter name is updated to match.
                </span>
              </label>
              <input
                id="clone-name"
                aria-label="New profile name"
                type="text"
                value={cloneName}
                onChange={e => setCloneName(e.target.value)}
                className={inputClass}
              />
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 text-sm text-gray-500" data-testid="source-loading">
              <Loader2 size={14} className="animate-spin" /> Loading source…
            </div>
          )}

          {loadError && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-900/20 border border-red-700/40 text-red-300 text-xs" role="alert">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span className="whitespace-pre-line">{loadError}</span>
            </div>
          )}

          {!loading && !loadError && (
            <textarea
              aria-label="Profile source"
              value={content}
              onChange={e => setContent(e.target.value)}
              rows={18}
              spellCheck={false}
              className={`${inputClass} font-mono text-xs leading-relaxed${
                findings.some(f => f.severity === 'error') ? ' border-2 !border-red-500 ring-2 ring-red-500/30' : ''
              }`}
            />
          )}

          <ValidationFindings findings={findings} />

          {saveError && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-900/20 border border-red-700/40 text-red-300 text-xs" role="alert">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <div className="whitespace-pre-line">{saveError}</div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-5 py-4 bg-gray-800/30 border-t border-gray-700/30">
          <button
            onClick={onClose}
            disabled={saving}
            className="px-4 py-2 text-sm font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={!canSave}
            className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-500 rounded-lg transition-all disabled:opacity-50 flex items-center gap-2"
          >
            {saving && <Loader2 size={14} className="animate-spin" />}
            {saving ? 'Saving…' : mode === 'edit' ? 'Save changes' : 'Create copy'}
          </button>
        </div>
      </div>
    </div>
  )
}
