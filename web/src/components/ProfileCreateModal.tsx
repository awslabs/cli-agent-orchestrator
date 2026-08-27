import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, TemplateSummary, ProfileValidationMessage, ProviderInfo } from '../api'
import { ValidationFindings } from './ValidationFindings'
import { CustomSelect, SelectOption } from './CustomSelect'
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Package, X } from 'lucide-react'

/** Debounce for the template live preview, matching the search box contract. */
export const PREVIEW_DEBOUNCE_MS = 300

/**
 * Frontmatter fields shown directly in the from-scratch form. Everything else
 * in the server schema renders inside the "Advanced" expander, in schema
 * order. The list is a display-priority hint only — the set of fields and
 * their types always come from `GET /agents/profiles/schema`.
 */
const PRIMARY_FIELDS = ['name', 'description', 'provider', 'model', 'tags', 'capabilities']

/**
 * Datalist suggestions for the `role` field: the built-in tool-bundle roles
 * from constants.py ROLE_TOOL_DEFAULTS. Deliberately suggestions, not a
 * closed select — settings.json custom roles are legal, and a datalist can
 * never block a valid value if this list goes stale.
 */
const ROLE_SUGGESTIONS = ['supervisor', 'developer', 'reviewer']

type JSONSchemaProp = {
  type?: string
  enum?: string[]
  description?: string
  default?: unknown
  minimum?: number
  pattern?: string
}

/**
 * Serialize form values into YAML frontmatter. Values are emitted as JSON,
 * which YAML accepts verbatim (flow style), so no YAML library is needed and
 * object-valued fields round-trip exactly what the JSON editor validated.
 */
export function buildFrontmatter(values: Record<string, unknown>): string {
  const lines = Object.entries(values)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
  return `---\n${lines.join('\n')}\n---\n`
}

/**
 * Rewrite the frontmatter `name:` in a rendered template document so it
 * matches the storage name the user chose. The backend rejects a create where
 * the two disagree (`_validate_profile_for_write`), and templates ship a
 * fixed example name. Only the first `name:` line inside the leading
 * frontmatter block is touched; the markdown body is never modified.
 */
export function rewriteFrontmatterName(content: string, name: string): string {
  const m = content.match(/^---\r?\n([\s\S]*?)\r?\n---/)
  if (!m) return content
  // Replacement FUNCTIONS, not strings: String.replace interprets $-patterns
  // ($&, $', $1) in a replacement STRING, which corrupts a typed name
  // containing them and — worse — mangles any document whose frontmatter
  // legally contains such text (e.g. description: costs $& fees), because the
  // whole updated block is itself passed through a replace. A function
  // replacement is inserted verbatim. \r?\n keeps CRLF documents (e.g. a
  // Windows-authored profile being cloned) from silently no-opping the
  // rewrite and then failing the server's name-match check.
  const updated = m[1].replace(/^name:.*$/m, () => `name: ${JSON.stringify(name)}`)
  return content.replace(m[0], () => `---\n${updated}\n---`)
}

/** Extract the frontmatter `name:` value from a rendered document, if any. */
export function extractFrontmatterName(content: string): string | null {
  // Match the frontmatter block first and search name: within it -- an
  // unbounded [\s\S]*? scan continues into the markdown body when the
  // frontmatter has no name:, matching a body line like 'name: decoy'
  // (same bounding rewriteFrontmatterName already uses).
  const block = content.match(/^---\r?\n([\s\S]*?)\r?\n---/)
  if (!block) return null
  const m = block[1].match(/^name:\s*["']?([^"'\r\n]+)["']?\s*$/m)
  return m ? m[1].trim() : null
}

function FieldLabel({ name, required, description }: { name: string; required?: boolean; description?: string }) {
  return (
    <label className="block text-xs text-gray-400 mb-1" htmlFor={`field-${name}`}>
      <span className="font-mono text-gray-300">{name}</span>
      {required && <span className="text-red-400 ml-0.5">*</span>}
      {description && <span className="block text-gray-600 mt-0.5">{description}</span>}
    </label>
  )
}

const inputClass =
  'w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600'

/**
 * One schema-driven field. Strings with enums render as selects; booleans as
 * checkboxes; integers as number inputs; arrays of strings as comma-separated
 * inputs; object-valued fields as validated JSON editors (per #510, object
 * fields get JSON editors, not bespoke widgets).
 */
function SchemaField({
  name,
  schema,
  required,
  value,
  jsonErrors,
  hasError,
  selectOptions,
  suggestions,
  onChange,
}: {
  name: string
  schema: JSONSchemaProp
  required?: boolean
  value: unknown
  jsonErrors: Record<string, string>
  /** Server validation reported an error finding rooted at this field. */
  hasError?: boolean
  /**
   * Field-specific widget overrides on top of the schema-driven renderer.
   * selectOptions turns an open string field into a closed select (used for
   * provider, whose value space is the live provider registry); suggestions
   * attaches a datalist to a text input, keeping free entry (used for role).
   */
  selectOptions?: SelectOption[]
  suggestions?: string[]
  onChange: (name: string, value: unknown) => void
}) {
  const id = `field-${name}`
  // A red boundary marks the control the validation error points at. The
  // finding path is dotted with the frontmatter key first (e.g.
  // 'mcpServers.docs.url'), so the whole field is outlined even when the
  // error is nested inside it.
  // border-2 + !important red + ring: visibly thick and immune to losing the
  // border-color specificity contest against inputClass's border-gray-700.
  const errClass = hasError ? ' border-2 !border-red-500 ring-2 ring-red-500/30' : ''
  if (selectOptions) {
    return (
      <div>
        <FieldLabel name={name} required={required} description={schema.description} />
        <CustomSelect
          ariaLabel={name}
          value={(value as string) ?? ''}
          onChange={v => onChange(name, v || undefined)}
          options={[{ value: '', label: '(unset)' }, ...selectOptions]}
          invalid={hasError}
        />
      </div>
    )
  }
  if (schema.enum) {
    return (
      <div>
        <FieldLabel name={name} required={required} description={schema.description} />
        <CustomSelect
          ariaLabel={name}
          value={(value as string) ?? ''}
          onChange={v => onChange(name, v || undefined)}
          options={[{ value: '', label: '(unset)' }, ...schema.enum.map(opt => ({ value: opt, label: opt }))]}
          invalid={hasError}
        />
      </div>
    )
  }
  if (schema.type === 'boolean') {
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          aria-label={name}
          type="checkbox"
          checked={Boolean(value)}
          onChange={e => onChange(name, e.target.checked ? true : undefined)}
          className="rounded bg-gray-900 border-gray-700"
        />
        <FieldLabel name={name} required={required} description={schema.description} />
      </div>
    )
  }
  if (schema.type === 'integer' || schema.type === 'number') {
    return (
      <div>
        <FieldLabel name={name} required={required} description={schema.description} />
        <input
          id={id}
          aria-label={name}
          type="number"
          min={schema.minimum}
          value={(value as number | string) ?? ''}
          onChange={e => onChange(name, e.target.value === '' ? undefined : Number(e.target.value))}
          className={inputClass + errClass}
        />
      </div>
    )
  }
  if (schema.type === 'array') {
    return (
      <div>
        <FieldLabel name={name} required={required} description={`${schema.description ?? ''} (comma-separated)`.trim()} />
        <input
          id={id}
          aria-label={name}
          type="text"
          value={Array.isArray(value) ? (value as string[]).join(', ') : ''}
          onChange={e => {
            const items = e.target.value.split(',').map(s => s.trim()).filter(Boolean)
            onChange(name, items.length ? items : undefined)
          }}
          className={inputClass + errClass}
        />
      </div>
    )
  }
  if (schema.type === 'object') {
    const err = jsonErrors[name]
    return (
      <div>
        <FieldLabel name={name} required={required} description={`${schema.description ?? ''} (JSON)`.trim()} />
        <textarea
          id={id}
          aria-label={name}
          rows={4}
          spellCheck={false}
          // Controlled from the draft string: an uncontrolled defaultValue
          // remounts empty when Advanced collapses and reopens, while the
          // draft (which is what actually saves) silently persists -- the
          // user sees an empty field but the typed JSON still lands. A
          // non-string value (template default seeded as an object) is
          // pretty-printed once; edits then flow through as strings.
          value={typeof value === 'string' ? value : value ? JSON.stringify(value, null, 2) : ''}
          onChange={e => onChange(name, e.target.value)}
          className={`${inputClass} font-mono text-xs ${err || hasError ? 'border-2 !border-red-500 ring-2 ring-red-500/30' : ''}`}
          placeholder="{ }"
        />
        {err && <div className="text-xs text-red-400 mt-1" role="alert">{err}</div>}
      </div>
    )
  }
  // Default: plain string, optionally with datalist suggestions
  return (
    <div>
      <FieldLabel name={name} required={required} description={schema.description} />
      <input
        id={id}
        aria-label={name}
        type="text"
        list={suggestions ? `${id}-suggestions` : undefined}
        value={(value as string) ?? ''}
        onChange={e => onChange(name, e.target.value || undefined)}
        className={inputClass + errClass}
        placeholder={schema.pattern}
      />
      {suggestions && (
        <datalist id={`${id}-suggestions`} data-testid={`${name}-suggestions`}>
          {suggestions.map(sug => <option key={sug} value={sug} />)}
        </datalist>
      )}
    </div>
  )
}

interface ProfileCreateModalProps {
  open: boolean
  onClose: () => void
  /** Called with the created profile's name after a successful POST. */
  onCreated: (name: string, warnings: ProfileValidationMessage[]) => void
}

export function ProfileCreateModal({ open, onClose, onCreated }: ProfileCreateModalProps) {
  const [mode, setMode] = useState<'template' | 'scratch'>('template')

  // --- shared state ---
  const [profileName, setProfileName] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [findings, setFindings] = useState<ProfileValidationMessage[]>([])

  // --- template mode state ---
  const [templates, setTemplates] = useState<TemplateSummary[]>([])
  const [template, setTemplate] = useState('')
  const [templateSchema, setTemplateSchema] = useState<Record<string, any> | null>(null)
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [preview, setPreview] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const previewTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const previewSeq = useRef(0)
  // Staleness token for the template-schema fetch: a fast A->B switch with
  // out-of-order resolution must never leave A's schema under B's selection.
  const templateSeq = useRef(0)
  // Tracks whether the user typed a name; if not, follow the template default.
  const nameTouched = useRef(false)

  // Invalidate any scheduled or in-flight preview render: bumping the token
  // makes a late response fail its `seq === previewSeq.current` check and be
  // dropped. This must run at every site that clears the preview -- a token
  // bumped only when a *new* request is issued left the "reason for the
  // request disappeared" paths (template switch, deselect, modal close and
  // reopen) able to silently re-land stale content (#692 review).
  const invalidatePreview = () => {
    previewSeq.current++
    if (previewTimer.current) {
      clearTimeout(previewTimer.current)
      previewTimer.current = null
    }
    setPreviewLoading(false)
  }

  // --- scratch mode state ---
  const [profileSchema, setProfileSchema] = useState<Record<string, any> | null>(null)
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [scratchValues, setScratchValues] = useState<Record<string, unknown>>({})
  const [jsonDrafts, setJsonDrafts] = useState<Record<string, string>>({})
  const [systemPrompt, setSystemPrompt] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)

  // Reset per open so a cancelled create never leaks into the next one.
  useEffect(() => {
    if (!open) return
    invalidatePreview()
    setMode('template')
    setProfileName('')
    setSaving(false)
    setSaveError(null)
    setFindings([])
    setTemplate('')
    setTemplateSchema(null)
    setConfig({})
    setPreview(null)
    setPreviewError(null)
    setScratchValues({})
    setJsonDrafts({})
    setSystemPrompt('')
    setAdvancedOpen(false)
    nameTouched.current = false
    api.listProfileTemplates().then(setTemplates).catch(() => setTemplates([]))
    api.getProfileSchema().then(setProfileSchema).catch(() => setProfileSchema(null))
    api.listProviders().then(setProviders).catch(() => setProviders([]))
  }, [open])

  // Template selection loads that template's schema and resets its config.
  useEffect(() => {
    const seq = ++templateSeq.current
    if (!template) {
      invalidatePreview()
      setTemplateSchema(null)
      setPreview(null)
      setPreviewError(null)
      return
    }
    setTemplateSchema(null)
    api.getTemplateSchema(template)
      .then(s => {
        if (seq !== templateSeq.current) return
        setTemplateSchema(s)
        // Seed defaults so the preview renders something meaningful.
        const seeded: Record<string, unknown> = {}
        for (const [k, v] of Object.entries((s.properties ?? {}) as Record<string, JSONSchemaProp>)) {
          if (v.default !== undefined) seeded[k] = v.default
        }
        setConfig(seeded)
      })
      .catch(e => {
        if (seq !== templateSeq.current) return
        setPreviewError(e?.detail || e?.message || 'Failed to load template schema')
      })
  }, [template])

  // Debounced live preview: one render request per quiet burst of config edits.
  useEffect(() => {
    if (previewTimer.current) clearTimeout(previewTimer.current)
    if (!template || !templateSchema) {
      // No render can be issued from this state, so any in-flight one is
      // stale by definition (template switched or deselected mid-request).
      // Without the bump, its late response still satisfied the seq check
      // and silently re-armed Create with the previous template's body.
      previewSeq.current++
      setPreviewLoading(false)
      return
    }
    setPreviewLoading(true)
    previewTimer.current = setTimeout(() => {
      const seq = ++previewSeq.current
      api.previewTemplate(template, config)
        .then(p => {
          if (seq !== previewSeq.current) return
          setPreview(p.content)
          setPreviewError(null)
          if (!nameTouched.current) {
            const fmName = extractFrontmatterName(p.content)
            if (fmName) setProfileName(fmName)
          }
        })
        .catch((e: ApiError) => {
          if (seq !== previewSeq.current) return
          // A config failing template validation is a normal mid-edit state.
          setPreview(null)
          setPreviewError(e?.detail || e?.message || 'Preview failed')
        })
        .finally(() => {
          if (seq === previewSeq.current) setPreviewLoading(false)
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => { if (previewTimer.current) clearTimeout(previewTimer.current) }
  }, [template, templateSchema, config])

  // Frontmatter keys carrying error-severity findings from the last validate
  // run: first segment of the dotted finding path (e.g. 'mcpServers.docs.url'
  // -> 'mcpServers'). Drives the red boundary on the matching form control.
  const errorFields = useMemo(
    () => new Set(
      findings
        .filter(f => f.severity === 'error' && f.path)
        .map(f => (f.path as string).split('.')[0]),
    ),
    [findings],
  )

  // Template-config fields named by the preview/validate error. The backend
  // (agent_scaffold.validate_config) emits one line per error in the form
  // '  - {path}: {message}', where a missing required property reports at
  // '(root)' with the field name quoted in the message. Both forms map back
  // to a config field so the offending control gets the red boundary.
  const templateErrorFields = useMemo(() => {
    const fields = new Set<string>()
    if (!previewError) return fields
    for (const line of previewError.split('\n')) {
      const m = line.match(/^\s*-\s+([^:]+):\s*(.*)$/)
      if (!m) continue
      const loc = m[1].trim()
      if (loc === '(root)') {
        const req = m[2].match(/^'([^']+)' is a required property/)
        if (req) fields.add(req[1])
      } else {
        fields.add(loc.split('.')[0])
      }
    }
    return fields
  }, [previewError])

  // An error finding on a field inside the collapsed Advanced section would
  // paint a red boundary nobody can see; expand the section so it is visible.
  useEffect(() => {
    if (!profileSchema || errorFields.size === 0) return
    const advanced = Object.keys((profileSchema.properties ?? {}) as Record<string, unknown>)
      .filter(k => k !== 'name' && !PRIMARY_FIELDS.includes(k))
    if (advanced.some(k => errorFields.has(k))) setAdvancedOpen(true)
  }, [errorFields, profileSchema])

  // JSON drafts parse-checked on every keystroke; parse errors block create.
  const jsonErrors = useMemo(() => {
    const errs: Record<string, string> = {}
    for (const [k, draft] of Object.entries(jsonDrafts)) {
      if (draft.trim() === '') continue
      try {
        const parsed = JSON.parse(draft)
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          errs[k] = 'Must be a JSON object'
        }
      } catch (e: any) {
        errs[k] = `Invalid JSON: ${e.message}`
      }
    }
    return errs
  }, [jsonDrafts])

  if (!open) return null

  const templateProps = (templateSchema?.properties ?? {}) as Record<string, JSONSchemaProp>
  const templateRequired = new Set<string>((templateSchema?.required as string[]) ?? [])
  const scratchProps = (profileSchema?.properties ?? {}) as Record<string, JSONSchemaProp>
  const scratchRequired = new Set<string>((profileSchema?.required as string[]) ?? [])
  const primaryKeys = PRIMARY_FIELDS.filter(k => k in scratchProps && k !== 'name')
  const advancedKeys = Object.keys(scratchProps).filter(k => k !== 'name' && !PRIMARY_FIELDS.includes(k))

  // Provider select options from the live registry. Uninstalled providers
  // stay selectable — a profile may target a provider absent on this machine.
  const providerOptions: SelectOption[] = providers.map(p => ({
    value: p.name,
    label: p.name,
    sublabel: p.installed ? undefined : 'Not installed',
  }))
  const fieldOverrides = (k: string) => ({
    selectOptions: k === 'provider' && providerOptions.length > 0 ? providerOptions : undefined,
    suggestions: k === 'role' ? ROLE_SUGGESTIONS : undefined,
  })

  const handleScratchChange = (name: string, value: unknown) => {
    if (scratchProps[name]?.type === 'object') {
      setJsonDrafts(d => ({ ...d, [name]: (value as string) ?? '' }))
    } else {
      setScratchValues(v => ({ ...v, [name]: value }))
    }
  }

  const buildScratchContent = (): string => {
    const values: Record<string, unknown> = { name: profileName, ...scratchValues }
    for (const [k, draft] of Object.entries(jsonDrafts)) {
      if (draft.trim() !== '' && !jsonErrors[k]) values[k] = JSON.parse(draft)
    }
    return buildFrontmatter(values) + '\n' + (systemPrompt.trim() ? systemPrompt.trim() + '\n' : '')
  }

  const canCreate =
    profileName.trim() !== '' &&
    !saving &&
    // In template mode the POST body is the preview, so a create mid-debounce
    // would persist a stale render; wait for the in-flight preview to settle.
    (mode === 'template' ? preview !== null && !previewLoading : Object.keys(jsonErrors).length === 0)

  const handleCreate = async () => {
    setSaving(true)
    setSaveError(null)
    setFindings([])
    try {
      const name = profileName.trim()
      const content = mode === 'template'
        ? rewriteFrontmatterName(preview as string, name)
        : buildScratchContent()
      // Validate before every save (#510 stage 5). Error findings block the
      // write client-side; warnings render but do not block. The write route
      // re-runs the same validator server-side, so this is a UX gate, not the
      // security boundary.
      try {
        const check = await api.validateProfile(content)
        setFindings(check.messages ?? [])
        if (!check.valid) {
          setSaveError('Validation failed — fix the errors below before creating.')
          return
        }
      } catch (ve: any) {
        const status = (ve as ApiError)?.status
        if (typeof status === 'number' && status < 500) {
          // 400 = the document is unparseable (not even frontmatter). Block.
          setSaveError((ve as ApiError)?.detail || ve?.message || 'Validation failed')
          return
        }
        // Transport failure or server error on the pre-save check: this gate
        // is UX only -- the write route re-validates authoritatively -- so
        // fall through to the save instead of hard-blocking behind a
        // phantom 'Validation failed'.
      }
      const res = await api.createProfile(name, content)
      onCreated(res.name, res.warnings ?? [])
      onClose()
    } catch (e: any) {
      const err = e as ApiError
      if (err.status === 409) {
        setSaveError(err.detail || `A profile named '${profileName.trim()}' already exists.`)
      } else {
        setSaveError(err.detail || err.message || 'Failed to create profile')
        // Write-rejection errors carry the same {severity, message, path}
        // shape as validate findings (_profile_write_rejection), so feed them
        // through the same rendering: red field boundaries + bulleted list.
        const meta = err.detailMeta as { errors?: ProfileValidationMessage[] } | undefined
        if (meta?.errors?.length) setFindings(meta.errors)
      }
    } finally {
      setSaving(false)
    }
  }

  const tabClass = (active: boolean) =>
    `px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
      active ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-800/60'
    }`

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center" role="dialog" aria-label="Create profile">
      {/* Backdrop close is gated on !saving: Cancel is already disabled
          during a save, and an overlay click mid-flight would unmount the
          modal while the request resolves — success snackbar with no modal,
          setState on an unmounted tree. */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={saving ? undefined : onClose} />
      <div className="relative bg-gray-900 border border-gray-700/50 rounded-2xl shadow-2xl w-full max-w-3xl mx-4 max-h-[85vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 p-5 pb-3 border-b border-gray-800">
          <div className="w-9 h-9 rounded-xl bg-blue-900/50 flex items-center justify-center">
            <Package size={18} className="text-blue-400" />
          </div>
          <h3 className="text-base font-semibold text-white flex-1">Create profile</h3>
          <div className="flex gap-1" role="tablist" aria-label="Create mode">
            <button role="tab" aria-selected={mode === 'template'} className={tabClass(mode === 'template')} onClick={() => setMode('template')}>
              From template
            </button>
            <button role="tab" aria-selected={mode === 'scratch'} className={tabClass(mode === 'scratch')} onClick={() => setMode('scratch')}>
              From scratch
            </button>
          </div>
          <button onClick={onClose} aria-label="Close" className="p-1 text-gray-500 hover:text-white rounded transition-colors">
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {/* Profile name — shared by both modes; it is the storage name. */}
          <div>
            <FieldLabel name="Profile name" required description="Used as the local-store filename; must match the frontmatter name." />
            <input
              id="field-Profile name"
              aria-label="Profile name"
              type="text"
              value={profileName}
              onChange={e => { nameTouched.current = true; setProfileName(e.target.value) }}
              className={inputClass + (errorFields.has('name') ? ' border-2 !border-red-500 ring-2 ring-red-500/30' : '')}
              placeholder="my-agent"
            />
          </div>

          {mode === 'template' && (
            <>
              <div>
                <FieldLabel name="Template" required />
                <CustomSelect
                  ariaLabel="Template"
                  value={template}
                  onChange={setTemplate}
                  placeholder="Select a template…"
                  options={templates.map(t => ({ value: t.name, label: t.name, sublabel: t.description }))}
                />
              </div>

              {template && !templateSchema && !previewError && (
                <div className="flex items-center gap-2 text-sm text-gray-500" data-testid="template-schema-loading">
                  <Loader2 size={14} className="animate-spin" /> Loading template schema…
                </div>
              )}

              {templateSchema && (
                <div className="grid grid-cols-2 gap-4">
                  {Object.entries(templateProps).map(([k, s]) => (
                    <SchemaField
                      key={k}
                      name={k}
                      schema={s}
                      required={templateRequired.has(k)}
                      value={config[k]}
                      jsonErrors={{}}
                      hasError={templateErrorFields.has(k)}
                      onChange={(n, v) => setConfig(c => ({ ...c, [n]: v }))}
                    />
                  ))}
                </div>
              )}

              {(templateSchema || previewError) && (
                <div>
                  <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide mb-1.5">
                    Live preview
                    {previewLoading && <Loader2 size={12} className="animate-spin" data-testid="preview-spinner" />}
                  </div>
                  {previewError && (
                    <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-900/20 border border-amber-700/40 text-amber-300 text-xs" role="status">
                      <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                      <span className="whitespace-pre-line">{previewError}</span>
                    </div>
                  )}
                  {preview && (
                    <pre data-testid="template-preview" className="bg-gray-950 border border-gray-800 rounded-lg p-3 text-xs text-gray-300 overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap">
                      {/* Show the exact document that will be persisted: the
                          create POST applies the frontmatter name rewrite, so
                          the preview must reflect the chosen name too — not
                          the template's shipped example name. */}
                      {profileName.trim() ? rewriteFrontmatterName(preview, profileName.trim()) : preview}
                    </pre>
                  )}
                </div>
              )}
            </>
          )}

          {mode === 'scratch' && (
            <>
              {!profileSchema ? (
                <div className="flex items-center gap-2 text-sm text-gray-500" data-testid="profile-schema-loading">
                  <Loader2 size={14} className="animate-spin" /> Loading profile schema…
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    {primaryKeys.map(k => (
                      <SchemaField
                        key={k}
                        name={k}
                        schema={scratchProps[k]}
                        required={scratchRequired.has(k)}
                        value={scratchValues[k]}
                        jsonErrors={jsonErrors}
                        hasError={errorFields.has(k)}
                        {...fieldOverrides(k)}
                        onChange={handleScratchChange}
                      />
                    ))}
                  </div>

                  <div>
                    <FieldLabel name="System prompt" description="Markdown body of the profile document." />
                    <textarea
                      id="field-System prompt"
                      aria-label="System prompt"
                      rows={5}
                      value={systemPrompt}
                      onChange={e => setSystemPrompt(e.target.value)}
                      className={`${inputClass} font-mono text-xs`}
                      placeholder="# You are…"
                    />
                  </div>

                  <div className="border border-gray-800 rounded-lg">
                    <button
                      onClick={() => setAdvancedOpen(o => !o)}
                      aria-expanded={advancedOpen}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-400 hover:text-white transition-colors"
                    >
                      {advancedOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      Advanced properties ({advancedKeys.length})
                    </button>
                    {advancedOpen && (
                      <div className="grid grid-cols-2 gap-4 p-3 pt-1">
                        {advancedKeys.map(k => (
                          <SchemaField
                            key={k}
                            name={k}
                            schema={scratchProps[k]}
                            required={scratchRequired.has(k)}
                            value={scratchProps[k].type === 'object' ? (jsonDrafts[k] ?? '') : scratchValues[k]}
                            jsonErrors={jsonErrors}
                            hasError={errorFields.has(k)}
                            {...fieldOverrides(k)}
                            onChange={handleScratchChange}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </>
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
            onClick={handleCreate}
            disabled={!canCreate}
            className="px-4 py-2 text-sm font-medium text-white bg-emerald-600 hover:bg-emerald-500 rounded-lg transition-all disabled:opacity-50 flex items-center gap-2"
          >
            {saving && <Loader2 size={14} className="animate-spin" />}
            {saving ? 'Creating…' : 'Create profile'}
          </button>
        </div>
      </div>
    </div>
  )
}
