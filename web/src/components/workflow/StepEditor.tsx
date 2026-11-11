// StepEditor: the fields of one step card in the workflow builder. The user
// finds an agent profile by search and uses it as is or clones it, picks the
// provider and the call type, fills the params, and writes the prompt.
// Retries and on_failure sit under Advanced.
//
// Params come in two parts. The params the profile declares each get a typed
// row, required ones first, under "Params from the profile". Free-form rows
// below hold every other name. Every value is fixed here and saved in the
// file.
//
// The editor is controlled. It reports changes as patches, so a late profile
// lookup merges into the newest step instead of overwriting edits made while
// it was in flight. It shows the errors it is given; validateBuilder computes
// them.

import { useEffect, useId, useState } from 'react'
import { ChevronDown, ChevronRight, Copy, Loader2, Plus, Search, X } from 'lucide-react'
import { api, type ApiError, type ProfileSearchResult, type ProviderInfo } from '../../api'
import { useStore } from '../../store'
import { useGeneration } from '../../hooks/useGeneration'
import { ProfileEditorModal } from '../ProfileEditorModal'
import {
  MAX_PARAMS,
  STEP_CALLS,
  applyDeclaredParams,
  dropDeclaredParams,
  isReservedCall,
  readDeclaredParams,
  writtenParams,
  type BuilderErrors,
  type BuilderParam,
  type BuilderStep,
  type DeclaredParam,
  type OnFailure,
  type StepCall,
  type StepPatch,
} from './workflowYaml'

export const PROFILE_SEARCH_DEBOUNCE_MS = 250
const PROFILE_SEARCH_LIMIT = 8

export interface StepEditorProps {
  step: BuilderStep
  /** 0-based position. Labels read "Step 1". */
  index: number
  /** This step's errors, keyed as validateBuilder keys them under `steps.<i>`. */
  errors?: BuilderErrors
  /** Options for the provider select, from listProviders. Empty shows a text field instead. */
  providers: ProviderInfo[]
  /** A patch, or a function of the newest step that returns one. */
  onChange: (patch: StepPatch) => void
}

type Lookup = 'idle' | 'loading' | 'from-profile' | 'no-provider' | 'error'

const LOOKUP_HINTS: Partial<Record<Lookup, string>> = {
  'from-profile': 'From the profile.',
  'no-provider': 'This profile names no provider. Pick one.',
  error: 'Could not read the profile. Pick a provider.',
}

const CALL_LABELS: Record<StepCall, string> = {
  handoff: 'handoff',
  assign: 'assign (not built yet)',
  send_message: 'send_message (not built yet)',
}

const CALL_HINTS: Record<StepCall, string> = {
  handoff: 'Starts the agent, sends the prompt and waits for the answer.',
  assign: 'Starts the agent and moves on without waiting.',
  send_message: 'Sends the prompt to an agent that an earlier step started.',
}

const fieldClass = (invalid: boolean) =>
  `w-full rounded-lg border bg-gray-900 px-2 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-hidden focus:border-emerald-600 ${
    invalid ? 'border-red-700' : 'border-gray-700'
  }`

function FieldError({ message }: { message?: string }) {
  if (!message) return null
  return <p className="mt-0.5 text-xs text-red-400">{message}</p>
}

/** The placeholder of a declared param's text field: its default, or a hint at its type. */
function declaredPlaceholder(param: DeclaredParam): string {
  if (param.default !== null) return `default: ${param.default}`
  return param.type === 'int' ? 'a whole number' : 'value'
}

/**
 * One typed row for a param the profile declares: its name, marked * when
 * required, its control and its description. An enum or a bool is a select,
 * with an empty choice only when the param is optional. An int or a string is
 * a text field, and the value stays text.
 */
function DeclaredParamRow({ param, value, error, label, descriptionId, onChange }: {
  param: DeclaredParam
  value: string
  error?: string
  label: string
  descriptionId: string
  onChange: (value: string) => void
}) {
  const common = {
    'aria-label': label,
    'aria-invalid': !!error,
    'aria-required': param.required || undefined,
    'aria-describedby': param.description ? descriptionId : undefined,
  }
  const choices = param.enum ?? (param.type === 'bool' ? ['true', 'false'] : null)
  // A value from the file that is not a choice stays visible, so its error makes sense.
  const shown = choices && value !== '' && !choices.includes(value) ? [value, ...choices] : choices
  const control = shown ? (
    <select {...common} value={value} onChange={e => onChange(e.target.value)} className={fieldClass(!!error)}>
      {!param.required && <option value="">{param.default !== null ? `not set (default: ${param.default})` : 'not set'}</option>}
      {param.required && value === '' && <option value="" disabled>Pick a value</option>}
      {shown.map(c => <option key={c} value={c}>{c}</option>)}
    </select>
  ) : (
    <input
      {...common}
      inputMode={param.type === 'int' ? 'numeric' : undefined}
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={declaredPlaceholder(param)}
      spellCheck={false}
      className={fieldClass(!!error)}
    />
  )
  return (
    <div className="flex items-start gap-1.5" data-testid="declared-param" data-name={param.name}>
      <span className="w-40 shrink-0 break-all pt-1.5 font-mono text-sm text-gray-200">
        {param.name}
        {param.required && <span aria-hidden="true" className="ml-0.5 text-red-400">*</span>}
      </span>
      <div className="min-w-0 flex-1">
        {control}
        {param.description && <p id={descriptionId} className="mt-0.5 text-[11px] text-gray-500">{param.description}</p>}
        <FieldError message={error} />
      </div>
    </div>
  )
}

export function StepEditor({ step, index, errors = {}, providers, onChange }: StepEditorProps) {
  const n = index + 1
  const showSnackbar = useStore(s => s.showSnackbar)
  const uid = useId()

  // Profile search. The generation is advanced on every query change, so a
  // response for an older query never lands under a newer one.
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<ProfileSearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const searchGen = useGeneration()
  useEffect(() => {
    const token = searchGen.begin()
    const q = query.trim()
    if (q === '') {
      setResults(null)
      setSearching(false)
      setSearchError(null)
      return
    }
    setSearching(true)
    const timer = setTimeout(() => {
      api.searchProfiles(q, PROFILE_SEARCH_LIMIT)
        .then(rows => {
          if (!token.isCurrent()) return
          setResults(rows)
          setSearchError(null)
        })
        .catch((e: ApiError) => {
          if (token.isCurrent()) setSearchError(e?.detail || e?.message || 'Search failed')
        })
        .finally(() => {
          if (token.isCurrent()) setSearching(false)
        })
    }, PROFILE_SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  // Selecting a profile sets the agent at once, then reads the profile for its
  // provider and the params it declares. Picking a profile fills each declared
  // default, in create and edit mode alike. Only the newest selection may apply
  // either. A provider the user picks while the read is in flight wins over
  // the profile's, but the params still arrive.
  const [lookup, setLookup] = useState<Lookup>('idle')
  const [readingParams, setReadingParams] = useState(false)
  const profileGen = useGeneration()
  const providerGen = useGeneration()
  useEffect(() => () => {
    searchGen.invalidate()
    profileGen.invalidate()
    providerGen.invalidate()
  }, [])
  const selectProfile = (name: string) => {
    onChange({ agent: name })
    setQuery('')
    const token = profileGen.begin()
    const providerToken = providerGen.begin()
    setLookup('loading')
    setReadingParams(true)
    api.getProfile(name)
      .then(profile => {
        if (!token.isCurrent()) return
        const params = readDeclaredParams(profile?.params)
        onChange(s => applyDeclaredParams(s, params, true))
        if (!providerToken.isCurrent()) return
        if (profile?.provider) {
          onChange({ provider: profile.provider })
          setLookup('from-profile')
        } else {
          setLookup('no-provider')
        }
      })
      .catch(() => {
        if (!token.isCurrent()) return
        onChange(dropDeclaredParams)
        if (providerToken.isCurrent()) setLookup('error')
      })
      .finally(() => {
        if (token.isCurrent()) setReadingParams(false)
      })
  }

  // Clone copies the profile under a new name (ProfileEditorModal, clone
  // mode), then selects the copy.
  const [cloneSource, setCloneSource] = useState<string | null>(null)

  const pickProvider = (provider: string) => {
    providerGen.invalidate()
    setLookup('idle')
    onChange({ provider })
  }

  const providerOptions = providers.some(p => p.name === step.provider) || step.provider === ''
    ? providers
    : [...providers, { name: step.provider, binary: '', installed: true }]

  const setParam = (j: number, patch: Partial<BuilderParam>) =>
    onChange({ params: step.params.map((p, k) => (k === j ? { ...p, ...patch } : p)) })
  const addParam = () => onChange({ params: [...step.params, { key: '', value: '' }] })
  const removeParam = (j: number) => onChange({ params: step.params.filter((_, k) => k !== j) })

  // The params the profile declares: required ones first, each group in declaration order.
  const declared = step.declared
  const declaredRows = declared.status === 'ready'
    ? [
        ...declared.params.map((param, k) => ({ param, k })).filter(r => r.param.required),
        ...declared.params.map((param, k) => ({ param, k })).filter(r => !r.param.required),
      ]
    : []
  const setDeclaredValue = (name: string, value: string) =>
    onChange(s => {
      if (s.declared.status !== 'ready') return {}
      const k = s.declared.params.findIndex(p => p.name === name)
      if (k < 0) return {}
      return { declared: { ...s.declared, values: s.declared.values.map((v, i) => (i === k ? value : v)) } }
    })
  const reading = readingParams || declared.status === 'loading'
  const showDeclared = step.agent !== '' && (reading || declared.status !== 'unknown')

  const [advancedOpen, setAdvancedOpen] = useState(step.retries !== '' || step.onFailure !== '')
  const showAdvanced = advancedOpen || !!errors.retries

  return (
    <div className="space-y-3">
      <label className="block">
        <span className="text-xs text-gray-400">Step id</span>
        <input
          aria-label={`Step ${n} id`}
          aria-invalid={!!errors.id}
          value={step.id}
          onChange={e => onChange({ id: e.target.value })}
          spellCheck={false}
          className={`mt-1 font-mono ${fieldClass(!!errors.id)}`}
        />
      </label>
      <FieldError message={errors.id} />

      <div>
        <span className="text-xs text-gray-400">Agent profile</span>
        {step.agent ? (
          <div className="mt-1 flex items-center gap-2">
            <span className="truncate rounded-sm bg-gray-800 px-2 py-0.5 font-mono text-sm text-gray-100" data-testid={`step-${n}-agent`}>
              {step.agent}
            </span>
            <button
              type="button"
              onClick={() => setCloneSource(step.agent)}
              aria-label={`Clone profile ${step.agent}`}
              className="inline-flex items-center gap-1 rounded-lg border border-gray-700 px-2 py-0.5 text-xs text-gray-300 hover:bg-gray-800"
            >
              <Copy size={12} aria-hidden="true" /> Clone
            </button>
            {lookup === 'loading' && <Loader2 size={12} className="animate-spin text-gray-500" aria-label="Reading the profile" />}
          </div>
        ) : (
          <p className="mt-1 text-xs text-gray-500">No profile picked yet. Search for one below.</p>
        )}
        <div className="relative mt-1.5">
          <Search size={13} aria-hidden="true" className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="search"
            aria-label={`Search profiles for step ${n}`}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search profiles by name, role or capability"
            className={`pl-7 ${fieldClass(!!errors.agent && !step.agent)}`}
          />
        </div>
        {searching && <p className="mt-1 text-xs text-gray-500">Searching...</p>}
        {searchError && <p className="mt-1 text-xs text-red-400" role="alert">{searchError}</p>}
        {results && !searching && (results.length === 0 ? (
          <p className="mt-1 text-xs text-gray-500">No profiles match.</p>
        ) : (
          <ul aria-label={`Profile results for step ${n}`} className="mt-1 max-h-48 overflow-y-auto rounded-lg border border-gray-700 divide-y divide-gray-800">
            {results.map(r => (
              <li key={r.name}>
                <button
                  type="button"
                  onClick={() => selectProfile(r.name)}
                  aria-label={`Use profile ${r.name}`}
                  className="block w-full px-2 py-1.5 text-left hover:bg-gray-800"
                >
                  <span className="font-mono text-sm text-gray-100">{r.name}</span>
                  <span className="ml-2 text-[11px] text-gray-500">{r.source}</span>
                  {r.description && <span className="block truncate text-xs text-gray-400">{r.description}</span>}
                </button>
              </li>
            ))}
          </ul>
        ))}
        <FieldError message={errors.agent} />
      </div>

      <div>
        <label className="block">
          <span className="text-xs text-gray-400">Provider</span>
          {providerOptions.length > 0 ? (
            <select
              aria-label={`Provider for step ${n}`}
              aria-invalid={!!errors.provider}
              value={step.provider}
              onChange={e => pickProvider(e.target.value)}
              className={`mt-1 ${fieldClass(!!errors.provider)}`}
            >
              <option value="">Pick a provider</option>
              {providerOptions.map(p => (
                <option key={p.name} value={p.name}>{p.installed ? p.name : `${p.name} (not installed)`}</option>
              ))}
            </select>
          ) : (
            <input
              aria-label={`Provider for step ${n}`}
              aria-invalid={!!errors.provider}
              value={step.provider}
              onChange={e => pickProvider(e.target.value)}
              placeholder="for example claude_code"
              spellCheck={false}
              className={`mt-1 font-mono ${fieldClass(!!errors.provider)}`}
            />
          )}
        </label>
        {LOOKUP_HINTS[lookup] && <p className="mt-0.5 text-xs text-gray-500">{LOOKUP_HINTS[lookup]}</p>}
        <FieldError message={errors.provider} />
      </div>

      <div>
        <label className="block">
          <span className="text-xs text-gray-400">Call type</span>
          <select
            aria-label={`Call type for step ${n}`}
            value={step.call}
            onChange={e => onChange({ call: e.target.value as StepCall })}
            className={`mt-1 ${fieldClass(false)}`}
          >
            {STEP_CALLS.map(c => <option key={c} value={c}>{CALL_LABELS[c]}</option>)}
          </select>
        </label>
        <p className="mt-0.5 text-xs text-gray-500">{CALL_HINTS[step.call]}</p>
        {isReservedCall(step.call) && (
          <p className="mt-0.5 text-xs text-amber-300" data-testid={`step-${n}-reserved`}>
            Not built yet. The step saves, validates and shows in the flow, but a run is refused until {step.call} is built.
          </p>
        )}
      </div>

      <fieldset>
        <legend className="text-xs text-gray-400">Params</legend>
        <p className="text-[11px] text-gray-500">
          Added to the end of the prompt. The profile's params come first, then the rows below in their order.
          Params are journaled in plaintext, so do not enter secrets.
        </p>
        {showDeclared && (
          <div
            role="group"
            aria-label={`Params from the profile for step ${n}`}
            className="mt-1.5 space-y-1.5 rounded-lg border border-gray-800 p-2"
          >
            <p className="text-xs text-gray-300">Params from the profile</p>
            {reading && <p className="text-[11px] text-gray-500">Reading the profile's params...</p>}
            {!reading && declared.status === 'error' && (
              <p className="text-[11px] text-amber-300">Couldn't load this profile, so its params can't be checked.</p>
            )}
            {!reading && declared.status === 'ready' && declared.params.length === 0 && (
              <p className="text-[11px] text-gray-500">
                This profile declares no params. Clone it above, or edit it in the Profiles tab, to add some.
              </p>
            )}
            {declared.status === 'ready' && declaredRows.map(({ param, k }) => (
              <DeclaredParamRow
                key={param.name}
                param={param}
                value={declared.values[k] ?? ''}
                error={errors[`declared.${k}`]}
                label={`Step ${n} param ${param.name}`}
                descriptionId={`${uid}-declared-${k}`}
                onChange={value => setDeclaredValue(param.name, value)}
              />
            ))}
          </div>
        )}
        <div className="mt-1.5 space-y-1.5">
          {step.params.map((p, j) => {
            const keyError = errors[`params.${j}.key`]
            const valueError = errors[`params.${j}.value`]
            return (
              <div key={j}>
                <div className="flex items-center gap-1.5">
                  <input
                    aria-label={`Step ${n} param ${j + 1} name`}
                    aria-invalid={!!keyError}
                    value={p.key}
                    onChange={e => setParam(j, { key: e.target.value })}
                    placeholder="name"
                    spellCheck={false}
                    className={`w-40 font-mono ${fieldClass(!!keyError)}`}
                  />
                  <input
                    aria-label={`Step ${n} param ${j + 1} value`}
                    aria-invalid={!!valueError}
                    value={p.value}
                    onChange={e => setParam(j, { value: e.target.value })}
                    placeholder="value"
                    spellCheck={false}
                    className={`flex-1 ${fieldClass(!!valueError)}`}
                  />
                  <button
                    type="button"
                    onClick={() => removeParam(j)}
                    aria-label={`Remove step ${n} param ${j + 1}`}
                    className="p-1 text-gray-500 hover:text-red-400"
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                </div>
                <FieldError message={keyError} />
                <FieldError message={valueError} />
              </div>
            )
          })}
          <FieldError message={errors.params} />
          <button
            type="button"
            onClick={addParam}
            disabled={writtenParams(step).length >= MAX_PARAMS}
            className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            <Plus size={13} aria-hidden="true" /> Add param
          </button>
        </div>
      </fieldset>

      <label className="block">
        <span className="text-xs text-gray-400">Prompt</span>
        <textarea
          aria-label={`Prompt for step ${n}`}
          aria-invalid={!!errors.prompt}
          rows={4}
          value={step.prompt}
          onChange={e => onChange({ prompt: e.target.value })}
          placeholder="What the agent should do. Use {{workflow.inputs.NAME}} to insert an input."
          className={`mt-1 ${fieldClass(!!errors.prompt)}`}
        />
      </label>
      <FieldError message={errors.prompt} />

      <div>
        <button
          type="button"
          onClick={() => setAdvancedOpen(o => !o)}
          aria-expanded={showAdvanced}
          className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200"
        >
          {showAdvanced ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
          Advanced
        </button>
        {showAdvanced && (
          <div className="mt-1.5 grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-gray-400">Retries</span>
              <input
                aria-label={`Retries for step ${n}`}
                aria-invalid={!!errors.retries}
                inputMode="numeric"
                value={step.retries}
                onChange={e => onChange({ retries: e.target.value })}
                placeholder="engine default"
                className={`mt-1 ${fieldClass(!!errors.retries)}`}
              />
              <FieldError message={errors.retries} />
            </label>
            <label className="block">
              <span className="text-xs text-gray-400">On failure</span>
              <select
                aria-label={`On failure for step ${n}`}
                value={step.onFailure}
                onChange={e => onChange({ onFailure: e.target.value as OnFailure })}
                className={`mt-1 ${fieldClass(false)}`}
              >
                <option value="">engine default</option>
                <option value="halt">halt</option>
                <option value="continue">continue</option>
              </select>
            </label>
          </div>
        )}
      </div>

      {cloneSource && (
        <ProfileEditorModal
          open
          mode="clone"
          name={cloneSource}
          onClose={() => setCloneSource(null)}
          onSaved={(name, warnings) => {
            showSnackbar({
              type: 'success',
              message: warnings.length
                ? `Cloned to ${name}, with ${warnings.length} warning${warnings.length > 1 ? 's' : ''}`
                : `Cloned to ${name}`,
            })
            selectProfile(name)
          }}
        />
      )}
    </div>
  )
}
