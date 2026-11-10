// ProfileParamsEditor: the "Params a workflow step must fill" section of the
// profile forms. Create profile, From scratch, shows it in the main form. Edit
// and Clone show it above the raw text.
//
// The editor is controlled: it shows the rows it is given and reports each
// change as a new row list. It checks the rows as the user types (profileParams)
// and offers params from the ${...} placeholders in a prompt text.

import { useId, useMemo, type ReactNode } from 'react'
import { Plus, X } from 'lucide-react'
import {
  MAX_PARAM_ROWS,
  PARAM_TYPES,
  newParamRow,
  paramRowErrors,
  suggestParams,
  suggestedRow,
  updateParamRow,
  type ParamRow,
  type ParamRowErrors,
  type ParamType,
} from './profileParams'

export const PARAMS_SECTION_TITLE = 'Params a workflow step must fill'

interface ProfileParamsEditorProps {
  rows: ParamRow[]
  onChange: (rows: ParamRow[]) => void
  /** The prompt text the suggestions read ${...} placeholders from. */
  suggestFrom: string
  /** The rows, Add param and the suggestions show but can't be used. */
  disabled?: boolean
  /** Hide the rows, Add param and the suggestions. */
  hideRows?: boolean
  /** A line about the section's state, under the title. */
  status?: ReactNode
  /** The server reported an error on params. */
  invalid?: boolean
}

const fieldClass = (invalid: boolean) =>
  `w-full rounded-lg border bg-gray-900 px-2 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-hidden focus:border-emerald-600 disabled:opacity-50 ${
    invalid ? 'border-red-700' : 'border-gray-700'
  }`

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null
  return <p id={id} className="mt-0.5 text-xs text-red-400">{message}</p>
}

function ParamRowFields({ row, index, errors, disabled, idBase, onChange, onRemove }: {
  row: ParamRow
  index: number
  errors: ParamRowErrors
  disabled: boolean
  idBase: string
  onChange: (patch: Partial<ParamRow>) => void
  onRemove: () => void
}) {
  const n = index + 1
  const errorId = (field: keyof ParamRowErrors) => `${idBase}-${index}-${field}`
  const described = (field: keyof ParamRowErrors) => ({
    'aria-invalid': !!errors[field],
    'aria-describedby': errors[field] ? errorId(field) : undefined,
  })
  const change = (patch: Partial<ParamRow>) => { if (!disabled) onChange(patch) }
  return (
    <div data-testid="param-row" className="space-y-1.5 rounded-lg border border-gray-800 p-2">
      <div className="grid grid-cols-[minmax(0,1fr)_7rem_auto_auto] items-end gap-2">
        <label className="block min-w-0">
          <span className="text-[11px] text-gray-500">Name</span>
          <input
            aria-label={`Param ${n} name`}
            {...described('name')}
            value={row.name}
            disabled={disabled}
            onChange={e => change({ name: e.target.value })}
            placeholder="table_name"
            spellCheck={false}
            className={`font-mono ${fieldClass(!!errors.name)}`}
          />
        </label>
        <label className="block">
          <span className="text-[11px] text-gray-500">Type</span>
          <select
            aria-label={`Param ${n} type`}
            value={row.type}
            disabled={disabled}
            onChange={e => change({ type: e.target.value as ParamType })}
            className={fieldClass(false)}
          >
            {PARAM_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-1.5 pb-2 text-xs text-gray-400">
          <input
            type="checkbox"
            aria-label={`Param ${n} required`}
            checked={row.required}
            disabled={disabled}
            onChange={e => change({ required: e.target.checked })}
            className="rounded-sm border-gray-700 bg-gray-900"
          />
          Required
        </label>
        <button
          type="button"
          aria-label={`Remove param ${n}`}
          disabled={disabled}
          onClick={() => { if (!disabled) onRemove() }}
          className="mb-1 p-1 text-gray-500 hover:text-red-400 disabled:opacity-50"
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <FieldError id={errorId('name')} message={errors.name} />

      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="block">
            <span className="text-[11px] text-gray-500">Default</span>
            {row.type === 'bool' ? (
              <select
                aria-label={`Param ${n} default`}
                {...described('default')}
                value={row.default}
                disabled={disabled || row.required}
                onChange={e => change({ default: e.target.value })}
                className={fieldClass(!!errors.default)}
              >
                <option value="">not set</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                aria-label={`Param ${n} default`}
                {...described('default')}
                value={row.default}
                disabled={disabled || row.required}
                onChange={e => change({ default: e.target.value })}
                inputMode={row.type === 'int' ? 'numeric' : undefined}
                placeholder={row.required ? 'none, the param is required' : 'not set'}
                spellCheck={false}
                className={fieldClass(!!errors.default)}
              />
            )}
          </label>
          <FieldError id={errorId('default')} message={errors.default} />
        </div>
        <div>
          <label className="block">
            <span className="text-[11px] text-gray-500">Allowed values</span>
            <input
              aria-label={`Param ${n} allowed values`}
              {...described('allowed')}
              value={row.allowed}
              disabled={disabled || row.type === 'bool'}
              onChange={e => change({ allowed: e.target.value })}
              placeholder={row.type === 'bool' ? 'true or false' : 'any, or a comma-separated list'}
              spellCheck={false}
              className={fieldClass(!!errors.allowed)}
            />
          </label>
          <FieldError id={errorId('allowed')} message={errors.allowed} />
        </div>
      </div>

      <div>
        <label className="block">
          <span className="text-[11px] text-gray-500">Description</span>
          <input
            aria-label={`Param ${n} description`}
            {...described('description')}
            value={row.description}
            disabled={disabled}
            onChange={e => change({ description: e.target.value })}
            placeholder="What the value is, for example: the table to query"
            className={fieldClass(!!errors.description)}
          />
        </label>
        <FieldError id={errorId('description')} message={errors.description} />
      </div>
    </div>
  )
}

export function ProfileParamsEditor({
  rows,
  onChange,
  suggestFrom,
  disabled = false,
  hideRows = false,
  status,
  invalid = false,
}: ProfileParamsEditorProps) {
  const uid = useId()
  const errors = useMemo(() => paramRowErrors(rows), [rows])
  const suggestions = useMemo(() => suggestParams(suggestFrom, rows), [suggestFrom, rows])
  const full = rows.length >= MAX_PARAM_ROWS

  const setRow = (i: number, patch: Partial<ParamRow>) =>
    onChange(rows.map((r, j) => (j === i ? updateParamRow(r, patch) : r)))
  const addRow = (row: ParamRow) => {
    if (!disabled && !full) onChange([...rows, row])
  }

  return (
    <section
      aria-labelledby={`${uid}-title`}
      data-testid="params-editor"
      className={`space-y-2 rounded-lg p-3 ${invalid ? 'border-2 border-red-500 ring-2 ring-red-500/30' : 'border border-gray-800'}`}
    >
      <h4 id={`${uid}-title`} className="text-xs font-medium text-gray-300">{PARAMS_SECTION_TITLE}</h4>
      {status}
      {!hideRows && (
        <>
          {rows.length === 0 && !disabled && <p className="text-xs text-gray-500">No params yet.</p>}
          {rows.map((row, i) => (
            <ParamRowFields
              key={i}
              row={row}
              index={i}
              errors={errors[i] ?? {}}
              disabled={disabled}
              idBase={uid}
              onChange={patch => setRow(i, patch)}
              onRemove={() => onChange(rows.filter((_, j) => j !== i))}
            />
          ))}
          <button
            type="button"
            onClick={() => addRow(newParamRow())}
            disabled={disabled || full}
            className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            <Plus size={13} aria-hidden="true" /> Add param
          </button>
          {suggestions.names.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5" data-testid="param-suggestions">
              <span className="text-[11px] text-gray-500">From the prompt:</span>
              {suggestions.names.map(name => (
                <button
                  key={name}
                  type="button"
                  onClick={() => addRow(suggestedRow(name))}
                  disabled={disabled || full}
                  className="rounded-full border border-gray-700 px-2 py-0.5 font-mono text-[11px] text-gray-300 hover:bg-gray-800 disabled:opacity-50"
                >
                  + {name}
                </button>
              ))}
            </div>
          )}
          {suggestions.skippedSecrets > 0 && (
            <p className="text-[11px] text-amber-300">
              {suggestions.skippedSecrets === 1
                ? 'Skipped 1 placeholder that looks like a secret.'
                : `Skipped ${suggestions.skippedSecrets} placeholders that look like secrets.`}{' '}
              Keep secrets in .env, not in params.
            </p>
          )}
        </>
      )}
      <p className="text-[11px] text-gray-500">
        A step's values reach the agent under "Parameters for this step". Tell the agent which placeholder each
        param fills, for example: <code className="font-mono">table_name</code> fills{' '}
        <code className="font-mono">{'${TABLE_NAME}'}</code>.
      </p>
    </section>
  )
}
