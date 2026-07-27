import { useEffect, useState } from 'react'
import { api, ProviderInfo, TemplateSchema, JsonSchemaProperty } from '../api'
import { CustomSelect } from './CustomSelect'

// #510 U3: the create-from-template ProfileForm. Provider and model are ALWAYS
// explicit, required, visible fields (ADR-006/AC4.4); they are never silently
// defaulted or inherited. The form holds no scaffolding/validation logic — it
// collects input; the server renders (preview), validates, and writes. Name
// containment (_validate_agent_name/_safe_join) is server-side only; the form
// never validates names itself.
//
// NOTE (#510 U4, 2026-07-27): this component was originally designed as a shared
// two-mode form (create + edit), with U4's edit/clone flow intended to reuse an
// 'edit' mode. U4 instead ships a raw frontmatter+body content editor
// (EditCloneModal), because a schema-field-only edit mode cannot edit the
// system_prompt BODY or arbitrary frontmatter and would silently drop the body
// on save. So the 'edit' mode had no production consumer and was removed
// (Stan-approved at the U4 gate). ProfileForm is now create-only; if a future
// unit needs a shared editor, extend it to handle full-document editing rather
// than reviving a schema-only 'edit' mode.

export interface ProfileFormValues {
  config: Record<string, unknown>
  provider: string
  model: string
}

interface ProfileFormProps {
  // The chosen template's schema drives the config fields.
  schema?: TemplateSchema | null
  // Controlled value + change handler (the parent owns the wizard state).
  values: ProfileFormValues
  onChange: (values: ProfileFormValues) => void
  // Per-field inline errors keyed by field name (e.g. from a failed save).
  fieldErrors?: Record<string, string>
}

function fieldLabel(name: string): string {
  return name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

function isRequired(schema: TemplateSchema | null | undefined, field: string): boolean {
  return Array.isArray(schema?.required) && schema!.required!.includes(field)
}

// Render one schema property as the appropriate widget (enum → select,
// everything else → text input). Numbers are coerced on change.
function SchemaField({
  name,
  prop,
  required,
  value,
  error,
  onChange,
}: {
  name: string
  prop: JsonSchemaProperty
  required: boolean
  value: unknown
  error?: string
  onChange: (v: unknown) => void
}) {
  const describedBy = error ? `${name}-error` : undefined
  const isNumber = prop.type === 'integer' || prop.type === 'number'
  return (
    <div>
      <label htmlFor={`field-${name}`} className="block text-xs text-gray-500 mb-1">
        {fieldLabel(name)}
        {required && <span className="text-red-400 ml-0.5">*</span>}
      </label>
      {Array.isArray(prop.enum) ? (
        <CustomSelect
          value={value === undefined || value === null ? '' : String(value)}
          onChange={v => onChange(v)}
          placeholder={`Select ${fieldLabel(name).toLowerCase()}…`}
          options={prop.enum.map(o => ({ value: String(o), label: String(o) }))}
        />
      ) : (
        <input
          id={`field-${name}`}
          type={isNumber ? 'number' : 'text'}
          value={value === undefined || value === null ? '' : String(value)}
          onChange={e => {
            const raw = e.target.value
            onChange(isNumber && raw !== '' ? Number(raw) : raw)
          }}
          aria-required={required}
          aria-invalid={!!error}
          aria-describedby={describedBy}
          placeholder={prop.description || ''}
          className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2 focus:border-emerald-500 focus:outline-none"
        />
      )}
      {prop.description && !error && (
        <p className="text-[10px] text-gray-600 mt-0.5">{prop.description}</p>
      )}
      {error && (
        <p id={`${name}-error`} role="alert" className="text-[11px] text-red-300 mt-0.5">
          {error}
        </p>
      )}
    </div>
  )
}

export function ProfileForm({ schema, values, onChange, fieldErrors = {} }: ProfileFormProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([])

  useEffect(() => {
    api.listProviders()
      .then(setProviders)
      .catch(() => setProviders([]))
  }, [])

  const setConfigField = (field: string, v: unknown) => {
    onChange({ ...values, config: { ...values.config, [field]: v } })
  }

  const schemaProps = schema?.properties || {}
  const propNames = Object.keys(schemaProps)

  // Provider options come from the server (never a hardcoded list). When the
  // list is unavailable the field is a free-text input so the user is never
  // blocked — but it stays REQUIRED either way.
  const providerOptions = providers.map(p => ({
    value: p.name,
    label: p.name.replace(/_/g, ' '),
    sublabel: !p.installed ? 'Not installed' : undefined,
  }))

  return (
    <div className="space-y-4" data-testid="profile-form">
      {/* Template-schema-driven config fields */}
      {propNames.length > 0 && (
        <div className="space-y-3">
          {propNames.map(name => (
            <SchemaField
              key={name}
              name={name}
              prop={schemaProps[name]}
              required={isRequired(schema, name)}
              value={values.config[name]}
              error={fieldErrors[name]}
              onChange={v => setConfigField(name, v)}
            />
          ))}
        </div>
      )}

      {/* Provider + model — ALWAYS explicit and required (ADR-006/AC4.4). */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1 border-t border-gray-700/40">
        <div>
          <label className="block text-xs text-gray-500 mb-1 mt-3">
            Provider<span className="text-red-400 ml-0.5">*</span>
          </label>
          {providerOptions.length > 0 ? (
            <CustomSelect
              value={values.provider}
              onChange={v => onChange({ ...values, provider: v })}
              placeholder="Select provider…"
              options={providerOptions}
              ariaLabel="Provider"
              ariaRequired
              ariaInvalid={!!fieldErrors.provider}
            />
          ) : (
            <input
              type="text"
              value={values.provider}
              onChange={e => onChange({ ...values, provider: e.target.value })}
              aria-required="true"
              aria-invalid={!!fieldErrors.provider}
              aria-describedby={fieldErrors.provider ? 'provider-error' : undefined}
              placeholder="e.g. claude_code"
              className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none"
            />
          )}
          {fieldErrors.provider && (
            <p id="provider-error" role="alert" className="text-[11px] text-red-300 mt-0.5">
              {fieldErrors.provider}
            </p>
          )}
        </div>
        <div>
          <label htmlFor="field-model" className="block text-xs text-gray-500 mb-1 mt-3">
            Model<span className="text-red-400 ml-0.5">*</span>
          </label>
          <input
            id="field-model"
            type="text"
            value={values.model}
            onChange={e => onChange({ ...values, model: e.target.value })}
            aria-required="true"
            aria-invalid={!!fieldErrors.model}
            aria-describedby={fieldErrors.model ? 'model-error' : undefined}
            placeholder="e.g. claude-sonnet-4"
            className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none"
          />
          {fieldErrors.model && (
            <p id="model-error" role="alert" className="text-[11px] text-red-300 mt-0.5">
              {fieldErrors.model}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
