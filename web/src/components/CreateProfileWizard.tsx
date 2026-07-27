import { useEffect, useState } from 'react'
import {
  api,
  TemplateInfo,
  TemplateSchema,
  PreviewProfileResult,
  ProfileWriteResult,
  ApiError,
} from '../api'
import { ProfileForm, ProfileFormValues } from './ProfileForm'
import { X, ChevronLeft, ChevronRight, FileText, Check, AlertTriangle, Loader2 } from 'lucide-react'

// #510 U3: the 4-step create-from-template wizard. Step 1 picks a template from
// the server (no hardcoded list), step 2 fills the shared ProfileForm, step 3
// shows a SERVER-rendered preview (POST /agents/profiles/preview — writes
// nothing), step 4 saves via POST /agents/profiles. All scaffolding/validation/
// write logic is server-side; this component only orchestrates the flow.
//
// F-1 (canonical project.md): provider/model are sent as top-level request
// fields and patched onto the RENDERED output by the server, never merged into
// the template `config`. This component keeps them out of `config` accordingly.

// Three reachable steps: pick a template, fill the form, then preview-and-save
// (the preview panel hosts the Save button — there is no separate save step).
type Step = 1 | 2 | 3

const STEP_LABELS = ['Pick template', 'Fill form', 'Preview & save']

const EMPTY_VALUES: ProfileFormValues = { config: {}, provider: '', model: '' }

export function CreateProfileWizard({ onClose }: { onClose: (saved?: ProfileWriteResult) => void }) {
  const [step, setStep] = useState<Step>(1)
  const [templates, setTemplates] = useState<TemplateInfo[]>([])
  const [loadingTemplates, setLoadingTemplates] = useState(true)
  const [templatesError, setTemplatesError] = useState<string | null>(null)
  const [templateName, setTemplateName] = useState<string>('')
  const [schema, setSchema] = useState<TemplateSchema | null>(null)
  const [values, setValues] = useState<ProfileFormValues>(EMPTY_VALUES)
  const [preview, setPreview] = useState<PreviewProfileResult | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const dirty = templateName !== '' || Object.keys(values.config).length > 0 || !!values.provider || !!values.model

  // Step 1: templates from the server (AC4.1 — no hardcoded list).
  useEffect(() => {
    api.listTemplates()
      .then(t => { setTemplates(t); setLoadingTemplates(false) })
      .catch((e: ApiError) => { setTemplatesError(e.detail || e.message); setLoadingTemplates(false) })
  }, [])

  const pickTemplate = async (name: string) => {
    setTemplateName(name)
    setSchema(null)
    setValues(EMPTY_VALUES)
    try {
      const s = await api.getTemplateSchema(name)
      setSchema(s)
    } catch {
      setSchema(null) // form still renders provider/model; server validates on preview
    }
    setStep(2)
  }

  // Provider + model are required before preview/save (ADR-006/AC4.4).
  const providerModelReady = !!values.provider.trim() && !!values.model.trim()

  const runPreview = async () => {
    setPreviewing(true)
    setPreviewError(null)
    setPreview(null)
    try {
      // F-1: provider/model travel as top-level fields, NOT inside config.
      const result = await api.previewProfile({
        template_name: templateName,
        config: values.config,
        provider: values.provider.trim(),
        model: values.model.trim(),
      })
      setPreview(result)
      setStep(3)
    } catch (e) {
      const err = e as ApiError
      // Invalid config (validate_config) comes back as 400 detail — surface it
      // and stay on step 2 (AC4.2).
      setPreviewError(err.detail || err.message)
    }
    setPreviewing(false)
  }

  const runSave = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      const saved = await api.createProfile({
        template_name: templateName,
        config: values.config,
        provider: values.provider.trim(),
        model: values.model.trim(),
      })
      onClose(saved)
    } catch (e) {
      const err = e as ApiError
      setSaveError(err.detail || err.message)
      setSaving(false)
    }
  }

  const handleClose = () => {
    if (dirty && !window.confirm('Discard this new profile? Your input will be lost.')) return
    onClose()
  }

  // Save is blocked while the preview reports validation errors (AC4.5).
  const saveBlocked = !preview || !preview.valid || !providerModelReady

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={handleClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Create profile from template"
        className="relative bg-gray-800 border border-gray-700 rounded-2xl shadow-2xl shadow-black/50 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto"
      >
        {/* Header + step indicator */}
        <div className="flex items-center justify-between p-5 border-b border-gray-700/50">
          <div>
            <h3 className="text-base font-semibold text-gray-200">Create Profile</h3>
            <p className="text-xs text-gray-500 mt-1">Step {step} of 3 · {STEP_LABELS[step - 1]}</p>
          </div>
          <button onClick={handleClose} aria-label="Close" className="p-1.5 text-gray-500 hover:text-gray-300 rounded-lg hover:bg-gray-700/50">
            <X size={18} />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {/* Step 1 — template picker */}
          {step === 1 && (
            <div className="space-y-2" data-testid="template-picker">
              {loadingTemplates ? (
                <p className="text-sm text-gray-500">Loading templates…</p>
              ) : templatesError ? (
                <p role="alert" className="text-sm text-red-300">Failed to load templates: {templatesError}</p>
              ) : templates.length === 0 ? (
                <p className="text-sm text-gray-500">No templates available.</p>
              ) : (
                templates.map(t => (
                  <button
                    key={t.name}
                    onClick={() => pickTemplate(t.name)}
                    className={`w-full text-left px-3 py-2.5 rounded-lg border transition-colors ${
                      templateName === t.name
                        ? 'bg-emerald-900/30 border-emerald-700/50'
                        : 'bg-gray-900/50 border-gray-700/30 hover:bg-gray-800/70'
                    }`}
                  >
                    <div className="text-sm font-medium text-gray-200">{t.name}</div>
                    {t.description && <div className="text-[11px] text-gray-500">{t.description}</div>}
                  </button>
                ))
              )}
            </div>
          )}

          {/* Step 2 — fill form (shared ProfileForm, create mode) */}
          {step === 2 && (
            <div className="space-y-3">
              <div className="text-xs text-gray-400">
                Template: <span className="font-mono text-gray-300">{templateName}</span>
              </div>
              <ProfileForm schema={schema} values={values} onChange={setValues} />
              {!providerModelReady && (
                <p className="text-[11px] text-amber-300 flex items-center gap-1">
                  <AlertTriangle size={11} /> Provider and model are required.
                </p>
              )}
              {previewError && (
                <p role="alert" className="text-xs text-red-300">Preview failed: {previewError}</p>
              )}
            </div>
          )}

          {/* Step 3 — server-rendered preview */}
          {step === 3 && (
            <div className="space-y-3" data-testid="preview-panel">
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <FileText size={13} /> Server-rendered preview
              </div>
              <pre className="bg-gray-950 border border-gray-700/50 rounded-lg p-3 text-[11px] font-mono text-gray-300 whitespace-pre-wrap max-h-72 overflow-y-auto">
                {preview?.text}
              </pre>
              {preview && !preview.valid && (
                <div role="alert" className="space-y-1">
                  <div className="text-xs font-semibold text-red-300">Validation errors — save blocked</div>
                  <ul className="pl-5 list-disc marker:text-red-500">
                    {preview.errors.map((e, i) => (
                      <li key={i} className="text-[11px] font-mono text-red-200">{e}</li>
                    ))}
                  </ul>
                </div>
              )}
              {preview && preview.warnings.length > 0 && (
                <div className="space-y-1">
                  <div className="text-xs font-semibold text-amber-300">Warnings (save allowed)</div>
                  <ul className="pl-5 list-disc marker:text-amber-500">
                    {preview.warnings.map((w, i) => (
                      <li key={i} className="text-[11px] font-mono text-amber-200">{w}</li>
                    ))}
                  </ul>
                </div>
              )}
              {preview && preview.valid && preview.warnings.length === 0 && (
                <div className="flex items-center gap-1.5 text-xs text-emerald-300">
                  <Check size={13} /> Valid — ready to save.
                </div>
              )}
              {saveError && <p role="alert" className="text-xs text-red-300">Save failed: {saveError}</p>}
            </div>
          )}
        </div>

        {/* Footer nav */}
        <div className="flex items-center justify-between gap-3 p-5 border-t border-gray-700/50">
          <button
            onClick={() => setStep(s => (s > 1 ? ((s - 1) as Step) : s))}
            disabled={step === 1}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-400 hover:text-gray-200 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft size={14} /> Back
          </button>

          {step === 2 && (
            <button
              onClick={runPreview}
              disabled={!providerModelReady || previewing}
              className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              {previewing ? <Loader2 size={14} className="animate-spin" /> : <ChevronRight size={14} />}
              {previewing ? 'Rendering…' : 'Preview'}
            </button>
          )}

          {step === 3 && (
            <button
              onClick={runSave}
              disabled={saveBlocked || saving}
              className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
              {saving ? 'Saving…' : 'Save Profile'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
