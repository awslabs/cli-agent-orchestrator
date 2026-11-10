// WorkflowBuilder: create or edit a YAML workflow step by step, in a modal.
//
// The form holds the name (fixed when editing), the description, the declared
// inputs and an ordered list of step cards (StepEditor). A live flow preview
// and a read-only YAML preview sit beside it. Validate checks the generated
// YAML on the server without saving. Save creates the workflow, or replaces it
// using the content hash read when the editor opened, so a concurrent edit is
// never overwritten.
//
// Edit loads the spec as the server parsed it and regenerates the whole file
// (workflowYaml), so comments are not kept. A workflow the builder cannot
// represent without losing data opens read-only and names the fields that
// block editing. Edit also reads each distinct step profile once, for the
// params it declares. Loading never pre-fills a default, because the file is
// the truth. Picking a profile afterwards does, as it does in create mode.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, ListChecks, Loader2, Plus, Save, Trash2, Workflow, X } from 'lucide-react'
import { api, type ApiError, type ProviderInfo, type WorkflowValidation, type WorkflowWriteResult } from '../../api'
import { useStore } from '../../store'
import { useGeneration } from '../../hooks/useGeneration'
import { ValidationFindings } from '../ValidationFindings'
import { StepEditor } from './StepEditor'
import { WorkflowFlow } from './WorkflowFlow'
import {
  INPUT_TYPES,
  applyDeclaredParams,
  applyStepPatch,
  dropDeclaredParams,
  errorsUnder,
  fromSpec,
  newInput,
  newStep,
  nextStepId,
  readDeclaredParams,
  toYaml,
  validateBuilder,
  type BuilderInput,
  type BuilderStep,
  type BuilderWorkflow,
  type InputType,
  type StepPatch,
} from './workflowYaml'

export type WorkflowBuilderProps = {
  onClose: () => void
  /** Called after a save succeeds, with the saved name and the server's result. */
  onSaved: (name: string, result: WorkflowWriteResult) => void
} & ({ mode: 'create'; name?: undefined } | { mode: 'edit'; name: string })

export const STALE_MESSAGE = 'This workflow changed since you opened it. Reload it.'

export interface SaveError {
  message: string
  details?: string[]
  /** The hash was stale: reloading is the way forward. */
  stale?: boolean
}

/** The inline message for a failed save. */
export function describeSaveError(err: ApiError | undefined, mode: 'create' | 'edit', name: string): SaveError {
  const detail = err?.detail
  const listed = (err?.detailMeta as { errors?: unknown } | undefined)?.errors
  const details = Array.isArray(listed) ? listed.filter((d): d is string => typeof d === 'string') : []
  switch (err?.status) {
    case 400:
      return { message: detail || 'The server refused this workflow.', details }
    case 404:
      return { message: detail || `No workflow named '${name}' exists any more.` }
    case 409:
      if (mode === 'create') return { message: `A workflow named '${name}' already exists. Choose another name.` }
      // An update 409 is a stale hash only when the server says the file changed
      // on disk (StaleSpecError's wording). Every other conflict is shown as the
      // server wrote it: a Python workflow, a cross-tier collision, or both
      // <name>.yaml and <name>.yml on disk. Matching the stale wording, and not
      // the other cases, keeps a name such as python-smoke from changing the result.
      if (!detail || /has changed on disk since it was read/i.test(detail)) return { message: STALE_MESSAGE, stale: true }
      return { message: detail }
    default:
      return { message: detail || err?.message || 'Failed to save the workflow.' }
  }
}

interface StepCard {
  key: number
  step: BuilderStep
}

type LoadState = 'loading' | 'ready' | 'blocked' | 'error'

const fieldClass = (invalid: boolean) =>
  `w-full rounded-lg border bg-gray-900 px-2 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-hidden focus:border-emerald-600 ${
    invalid ? 'border-red-700' : 'border-gray-700'
  }`

function FieldError({ message }: { message?: string }) {
  if (!message) return null
  return <p className="mt-0.5 text-xs text-red-400">{message}</p>
}

export function WorkflowBuilder(props: WorkflowBuilderProps) {
  const { onClose, onSaved } = props
  const editing = props.mode === 'edit'
  const editName = props.mode === 'edit' ? props.name : ''

  const [loadState, setLoadState] = useState<LoadState>(editing ? 'loading' : 'ready')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [blocking, setBlocking] = useState<string[]>([])
  const [specPath, setSpecPath] = useState('')
  const [expectedHash, setExpectedHash] = useState<string | null>(null)

  const [name, setName] = useState(editName)
  const [description, setDescription] = useState('')
  const [inputs, setInputs] = useState<BuilderInput[]>([])
  const nextKey = useRef(1)
  const [cards, setCards] = useState<StepCard[]>(() => [{ key: 0, step: newStep('step-1') }])

  const [providers, setProviders] = useState<ProviderInfo[]>([])
  // Field errors show after the first Validate or Save, then follow the edits.
  const [attempted, setAttempted] = useState(false)
  const [validation, setValidation] = useState<WorkflowValidation | null>(null)
  const [validating, setValidating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<SaveError | null>(null)
  const saveGuard = useRef(false)

  // A save owns navigation while it runs: a tab change would unmount the
  // builder and drop both the draft and the outcome (see ProfileEditorModal).
  const acquireNavLock = useStore(s => s.acquireNavLock)
  const releaseNavLock = useStore(s => s.releaseNavLock)
  useEffect(() => {
    if (!saving) return
    acquireNavLock()
    return () => releaseNavLock()
  }, [saving])

  useEffect(() => {
    let cancelled = false
    api.listProviders()
      .then(rows => { if (!cancelled) setProviders(rows) })
      .catch(() => { /* the step editors fall back to a text field */ })
    return () => { cancelled = true }
  }, [])

  const loadGen = useGeneration()
  // A profile read from the edit load lands only on steps that still use that
  // profile and still wait for it, so it never undoes a pick made meanwhile.
  const settleDeclared = (agent: string, patch: (step: BuilderStep) => Partial<BuilderStep>) =>
    setCards(cs => cs.map(c => (
      c.step.agent === agent && c.step.declared.status === 'loading' ? { ...c, step: applyStepPatch(c.step, patch) } : c
    )))
  const load = useCallback(() => {
    if (!editing) return
    const token = loadGen.begin()
    setLoadState('loading')
    setLoadError(null)
    setSaveError(null)
    setValidation(null)
    setAttempted(false)
    api.getWorkflowSpec(editName)
      .then(spec => {
        if (!token.isCurrent()) return
        setSpecPath(typeof spec.path === 'string' ? spec.path : '')
        const result = fromSpec(spec)
        if (!result.ok) {
          setBlocking(result.blocking)
          setLoadState('blocked')
          return
        }
        const steps = result.workflow.steps.map(step =>
          step.agent !== '' ? { ...step, declared: { status: 'loading' as const } } : step)
        // The route name wins: the server refuses a spec whose name differs from it.
        setName(editName)
        setDescription(result.workflow.description)
        setInputs(result.workflow.inputs)
        setCards(steps.map(step => ({ key: nextKey.current++, step })))
        setExpectedHash(typeof spec.content_hash === 'string' && spec.content_hash !== '' ? spec.content_hash : null)
        setLoadState('ready')
        // Read each distinct step profile once, for the params it declares.
        // Loading never pre-fills a default: the file is the truth.
        for (const agent of new Set(steps.map(s => s.agent).filter(a => a !== ''))) {
          api.getProfile(agent)
            .then(profile => {
              const params = readDeclaredParams(profile?.params)
              if (token.isCurrent()) settleDeclared(agent, s => applyDeclaredParams(s, params, false))
            })
            .catch(() => {
              if (token.isCurrent()) settleDeclared(agent, dropDeclaredParams)
            })
        }
      })
      .catch((e: ApiError) => {
        if (!token.isCurrent()) return
        setLoadError(e?.detail || e?.message || 'Failed to load the workflow')
        setLoadState('error')
      })
  }, [editing, editName])
  useEffect(() => {
    load()
    return () => loadGen.invalidate()
  }, [load])

  const wf: BuilderWorkflow = useMemo(
    () => ({ name, description, inputs, steps: cards.map(c => c.step) }),
    [name, description, inputs, cards],
  )
  const yaml = useMemo(() => toYaml(wf), [wf])
  const errors = useMemo(() => validateBuilder(wf), [wf])
  const errorCount = Object.keys(errors).length
  const shown = attempted ? errors : {}

  // A validation result describes the YAML it checked. Drop it once that changes.
  const yamlRef = useRef(yaml)
  yamlRef.current = yaml
  useEffect(() => { setValidation(null) }, [yaml])

  const validate = async () => {
    setAttempted(true)
    setValidation(null)
    if (errorCount > 0 || validating) return
    const sent = yaml
    setValidating(true)
    try {
      const result = await api.validateWorkflowSource(sent, 'yaml')
      if (yamlRef.current === sent) setValidation(result)
    } catch (e: any) {
      if (yamlRef.current === sent) {
        setValidation({ status: 'fail', errors: [e?.detail || e?.message || 'Validation request failed'] })
      }
    } finally {
      setValidating(false)
    }
  }

  const save = async () => {
    if (saveGuard.current) return
    setAttempted(true)
    setSaveError(null)
    if (errorCount > 0) return
    if (editing && !expectedHash) {
      setSaveError({ message: 'The server sent no content hash for this workflow, so it cannot be replaced safely. Reload it.', stale: true })
      return
    }
    saveGuard.current = true
    setSaving(true)
    try {
      const result = editing
        ? await api.updateWorkflow(editName, yaml, expectedHash as string, 'yaml')
        : await api.createWorkflow(name, yaml, 'yaml')
      onSaved(result?.name || name, result)
    } catch (e) {
      setSaveError(describeSaveError(e as ApiError, editing ? 'edit' : 'create', editing ? editName : name))
    } finally {
      saveGuard.current = false
      setSaving(false)
    }
  }

  // Inputs.
  const setInput = (i: number, patch: Partial<BuilderInput>) =>
    setInputs(rows => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const setInputType = (i: number, type: InputType) =>
    setInputs(rows => rows.map((r, j) => {
      if (j !== i) return r
      // A bool default is a choice of true or false; anything else is dropped.
      const keep = type !== 'bool' || r.default === 'true' || r.default === 'false'
      return { ...r, type, default: keep ? r.default : null }
    }))
  const addInput = () => setInputs(rows => [...rows, newInput()])
  const removeInput = (i: number) => setInputs(rows => rows.filter((_, j) => j !== i))

  // Steps.
  const updateStep = (key: number, patch: StepPatch) =>
    setCards(cs => cs.map(c => (c.key === key ? { ...c, step: applyStepPatch(c.step, patch) } : c)))
  const addStep = () => {
    const key = nextKey.current++
    setCards(cs => [...cs, { key, step: newStep(nextStepId(cs.map(c => c.step))) }])
  }
  const removeStep = (key: number) => setCards(cs => cs.filter(c => c.key !== key))
  const moveStep = (index: number, delta: -1 | 1) =>
    setCards(cs => {
      const j = index + delta
      if (j < 0 || j >= cs.length) return cs
      const next = [...cs]
      ;[next[index], next[j]] = [next[j], next[index]]
      return next
    })

  const title = editing ? `Edit workflow ${editName}` : 'New workflow'
  const ready = loadState === 'ready'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-modal="true" aria-label={title}>
      {/* No close on backdrop click: one stray click would drop the whole draft. */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-xs" />
      <div className="relative mx-4 flex max-h-[92vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-gray-700/50 bg-gray-900 shadow-2xl">
        <div className="flex items-center gap-3 border-b border-gray-800 p-4">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-900/40">
            <Workflow size={18} className="text-emerald-400" aria-hidden="true" />
          </div>
          <h3 className="flex-1 text-base font-semibold text-white">
            {editing ? <>Edit workflow <span className="font-mono">{editName}</span></> : 'New workflow'}
          </h3>
          <button
            type="button"
            onClick={saving ? undefined : onClose}
            disabled={saving}
            aria-label="Close"
            className="rounded-sm p-1 text-gray-500 transition-colors hover:text-white disabled:opacity-50"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loadState === 'loading' && (
            <p className="flex items-center gap-2 text-sm text-gray-500"><Loader2 size={14} className="animate-spin" aria-hidden="true" /> Loading the workflow...</p>
          )}
          {loadState === 'error' && (
            <div role="alert" className="space-y-2 rounded-lg border border-red-700/40 bg-red-900/20 p-3 text-sm text-red-300">
              <p>{loadError}</p>
              <button type="button" onClick={load} className="rounded-lg border border-red-700/60 px-2 py-1 text-xs hover:bg-red-900/40">Try again</button>
            </div>
          )}
          {loadState === 'blocked' && (
            <div role="alert" className="space-y-2 rounded-lg border border-amber-700/50 bg-amber-950/30 p-3 text-sm text-amber-200">
              <p>
                The builder cannot edit this workflow without losing data. Edit the file directly
                {specPath ? <>: <code className="break-all font-mono text-amber-100">{specPath}</code></> : '.'}
              </p>
              <p className="text-xs text-amber-300">Fields that block editing:</p>
              <ul className="list-disc pl-5 font-mono text-xs">
                {blocking.map(b => <li key={b}>{b}</li>)}
              </ul>
            </div>
          )}

          {ready && (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
              <div className="min-w-0 space-y-4">
                <section className="space-y-2">
                  {editing ? (
                    <div>
                      <span className="text-xs text-gray-400">Name</span>
                      <p className="font-mono text-sm text-gray-100" data-testid="builder-fixed-name">{editName}</p>
                      <p className="text-[11px] text-gray-500">The name is fixed when editing.</p>
                    </div>
                  ) : (
                    <label className="block">
                      <span className="text-xs text-gray-400">Name</span>
                      <input
                        aria-label="Workflow name"
                        aria-invalid={!!shown.name}
                        value={name}
                        onChange={e => setName(e.target.value)}
                        placeholder="for example nightly-smoke-check"
                        spellCheck={false}
                        className={`mt-1 font-mono ${fieldClass(!!shown.name)}`}
                      />
                      <FieldError message={shown.name} />
                    </label>
                  )}
                  <label className="block">
                    <span className="text-xs text-gray-400">Description</span>
                    <input
                      aria-label="Workflow description"
                      value={description}
                      onChange={e => setDescription(e.target.value)}
                      placeholder="What this workflow checks or does"
                      className={`mt-1 ${fieldClass(false)}`}
                    />
                  </label>
                </section>

                <section className="space-y-2 rounded-xl border border-gray-800 p-3">
                  <h4 className="text-sm font-semibold text-gray-300">Inputs</h4>
                  <p className="text-[11px] text-gray-500">
                    Values a run asks for. Use them in a prompt as {'{{workflow.inputs.NAME}}'}. Inputs and params are
                    journaled in plaintext with every run, so do not enter secrets. Pass a name instead, such as an
                    environment variable, and resolve the secret inside the step.
                  </p>
                  {inputs.map((input, i) => {
                    const nameError = shown[`inputs.${i}.name`]
                    const defaultError = shown[`inputs.${i}.default`]
                    return (
                      <div key={i}>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <input
                            aria-label={`Workflow input ${i + 1} name`}
                            aria-invalid={!!nameError}
                            value={input.name}
                            onChange={e => setInput(i, { name: e.target.value })}
                            placeholder="name"
                            spellCheck={false}
                            className={`w-36 font-mono ${fieldClass(!!nameError)}`}
                          />
                          <select
                            aria-label={`Workflow input ${i + 1} type`}
                            value={input.type}
                            onChange={e => setInputType(i, e.target.value as InputType)}
                            className={`w-24 ${fieldClass(false)}`}
                          >
                            {INPUT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                          </select>
                          <label className="inline-flex items-center gap-1 text-xs text-gray-300">
                            <input
                              type="checkbox"
                              aria-label={`Workflow input ${i + 1} required`}
                              checked={input.required}
                              onChange={e => setInput(i, { required: e.target.checked })}
                            />
                            required
                          </label>
                          {input.type === 'bool' ? (
                            <select
                              aria-label={`Workflow input ${i + 1} default`}
                              aria-invalid={!!defaultError}
                              value={input.default ?? ''}
                              onChange={e => setInput(i, { default: e.target.value === '' ? null : e.target.value })}
                              className={`w-32 ${fieldClass(!!defaultError)}`}
                            >
                              <option value="">no default</option>
                              <option value="true">true</option>
                              <option value="false">false</option>
                            </select>
                          ) : (
                            <input
                              aria-label={`Workflow input ${i + 1} default`}
                              aria-invalid={!!defaultError}
                              inputMode={input.type === 'int' ? 'numeric' : undefined}
                              value={input.default ?? ''}
                              onChange={e => setInput(i, { default: e.target.value === '' ? null : e.target.value })}
                              placeholder={input.type === 'int' ? 'default, a whole number' : 'default (optional)'}
                              className={`min-w-0 flex-1 ${fieldClass(!!defaultError)}`}
                            />
                          )}
                          <button
                            type="button"
                            onClick={() => removeInput(i)}
                            aria-label={`Remove workflow input ${i + 1}`}
                            className="p-1 text-gray-500 hover:text-red-400"
                          >
                            <X size={14} aria-hidden="true" />
                          </button>
                        </div>
                        <FieldError message={nameError} />
                        <FieldError message={defaultError} />
                      </div>
                    )
                  })}
                  <button
                    type="button"
                    onClick={addInput}
                    className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200"
                  >
                    <Plus size={13} aria-hidden="true" /> Add input
                  </button>
                </section>

                <section className="space-y-2">
                  <h4 className="text-sm font-semibold text-gray-300">Steps</h4>
                  <p className="text-[11px] text-gray-500">The orchestrator runs the steps in this order.</p>
                  <ol className="space-y-3">
                    {cards.map((card, i) => (
                      <li key={card.key} className="rounded-xl border border-gray-800 bg-gray-950/40 p-3" data-testid={`step-card-${i + 1}`}>
                        <div className="mb-2 flex items-center gap-2">
                          <span className="text-sm font-semibold text-gray-200">Step {i + 1}</span>
                          <span className="truncate font-mono text-xs text-gray-500">{card.step.id}</span>
                          <div className="ml-auto flex items-center gap-1">
                            <button
                              type="button"
                              onClick={() => moveStep(i, -1)}
                              disabled={i === 0}
                              aria-label={`Move step ${i + 1} up`}
                              className="rounded-sm p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 disabled:opacity-30"
                            >
                              <ArrowUp size={14} aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              onClick={() => moveStep(i, 1)}
                              disabled={i === cards.length - 1}
                              aria-label={`Move step ${i + 1} down`}
                              className="rounded-sm p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-200 disabled:opacity-30"
                            >
                              <ArrowDown size={14} aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              onClick={() => removeStep(card.key)}
                              aria-label={`Remove step ${i + 1}`}
                              className="rounded-sm p-1 text-gray-400 hover:bg-gray-800 hover:text-red-400"
                            >
                              <Trash2 size={14} aria-hidden="true" />
                            </button>
                          </div>
                        </div>
                        <StepEditor
                          step={card.step}
                          index={i}
                          errors={errorsUnder(shown, `steps.${i}`)}
                          providers={providers}
                          onChange={patch => updateStep(card.key, patch)}
                        />
                      </li>
                    ))}
                  </ol>
                  <FieldError message={shown.steps} />
                  <button
                    type="button"
                    onClick={addStep}
                    className="inline-flex items-center gap-1 rounded-lg border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800"
                  >
                    <Plus size={13} aria-hidden="true" /> Add step
                  </button>
                </section>
              </div>

              <aside className="min-w-0 space-y-3">
                <section className="rounded-xl border border-gray-800 p-3">
                  <h4 className="mb-2 text-sm font-semibold text-gray-300">Flow</h4>
                  <WorkflowFlow
                    label="Flow preview"
                    steps={wf.steps.map(s => ({ id: s.id, call: s.call, agent: s.agent, provider: s.provider }))}
                  />
                </section>
                <section className="rounded-xl border border-gray-800 p-3">
                  <h4 className="text-sm font-semibold text-gray-300">YAML</h4>
                  <p className="mb-2 text-[11px] text-gray-500">
                    Saving rewrites the file from this form. Comments in the file are not kept.
                  </p>
                  <pre aria-label="YAML preview" className="max-h-96 overflow-auto rounded-sm bg-gray-950 p-2 font-mono text-[11px] text-gray-300">
                    {yaml}
                  </pre>
                </section>
              </aside>
            </div>
          )}
        </div>

        {ready && ((attempted && errorCount > 0) || validation || saveError) && (
          <div className="space-y-2 border-t border-gray-800 px-4 py-3 text-xs">
            {attempted && errorCount > 0 && (
              <p role="alert" className="text-red-300">
                Fix the {errorCount === 1 ? 'problem' : `${errorCount} problems`} marked in the form first.
              </p>
            )}
            {validation && (
              <div role="status" className={validation.status === 'fail' ? 'text-red-300' : validation.status === 'pass_reserved' ? 'text-amber-200' : 'text-emerald-300'}>
                <p className="font-medium">
                  {validation.status === 'fail'
                    ? 'Validation failed.'
                    : validation.status === 'pass_reserved'
                      ? 'Valid. It uses constructs that are not built yet, so it saves but a run is refused until they are built.'
                      : 'Valid.'}
                </p>
                <div className="mt-1">
                  <ValidationFindings
                    findings={[
                      ...validation.errors.map(message => ({ severity: 'error' as const, message })),
                      ...(validation.reserved_notes ?? []).map(message => ({ severity: 'warning' as const, message })),
                    ]}
                  />
                </div>
              </div>
            )}
            {saveError && (
              <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-700/40 bg-red-900/20 p-2 text-red-300">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                <div className="min-w-0 space-y-1">
                  <p className="whitespace-pre-line">{saveError.message}</p>
                  {saveError.details?.map(d => <p key={d} className="font-mono">{d}</p>)}
                  {saveError.stale && editing && (
                    <button
                      type="button"
                      onClick={load}
                      className="rounded-lg border border-red-700/60 px-2 py-0.5 hover:bg-red-900/40"
                    >
                      Reload, discarding my changes
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        <div className="flex items-center justify-end gap-3 border-t border-gray-700/30 bg-gray-800/30 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="rounded-lg bg-gray-800 px-4 py-2 text-sm font-medium text-gray-300 transition-colors hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={validate}
            disabled={!ready || validating || saving}
            className="inline-flex items-center gap-2 rounded-lg border border-gray-700 px-4 py-2 text-sm text-gray-200 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {validating ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <ListChecks size={14} aria-hidden="true" />}
            Validate
          </button>
          <button
            type="button"
            onClick={save}
            disabled={!ready || saving}
            className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-all hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <Save size={14} aria-hidden="true" />}
            {saving ? 'Saving...' : editing ? 'Save changes' : 'Create workflow'}
          </button>
        </div>
      </div>
    </div>
  )
}
