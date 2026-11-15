// WorkflowBuilder: create or edit a YAML workflow step by step, in a modal.
//
// The form holds the name (fixed when editing), the description, the declared
// inputs and an ordered list of step cards (StepEditor). A live flow preview
// and the YAML sit beside it. The YAML follows the form until it is edited by
// hand. From then on the form is paused, and Validate and Save use the YAML as
// written, until the edits are discarded: the builder does not read YAML text
// back into the form. Validate checks the YAML on the server without saving.
// Save validates first and saves only YAML that passes. It creates the
// workflow, or replaces it using the content hash read when the editor opened,
// so a concurrent edit is never overwritten.
//
// Edit loads the spec as the server parsed it and regenerates the whole file
// (workflowYaml), so comments are not kept. A workflow the builder cannot
// represent without losing data opens read-only and names the fields that
// block editing. Edit also reads each distinct step profile once, for the
// params it declares. Loading never pre-fills a default, because the file is
// the truth. Picking a profile afterwards does, as it does in create mode.
//
// A note above the form says what a YAML workflow cannot do and points to
// Python workflows for those cases. In Edit mode, when the workflow has runs, a
// second note says that changes apply to new runs only.
//
// New workflow can clone a stored YAML workflow ("Clone from"). The clone
// fills the form with that workflow's description, inputs and steps, under a
// free name such as nightly-check-copy, and Save creates a new workflow; the
// source is never changed. Python workflows are not listed, because the
// builder writes YAML only. Replacing a form that is not blank asks first.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Info, ListChecks, Loader2, Plus, Save, Trash2, Workflow, X } from 'lucide-react'
import { api, type ApiError, type ProviderInfo, type WorkflowDefRow, type WorkflowValidation, type WorkflowWriteResult } from '../../api'
import { useStore } from '../../store'
import { useGeneration, type GenerationToken } from '../../hooks/useGeneration'
import { ValidationFindings } from '../ValidationFindings'
import { ConfirmModal } from '../ConfirmModal'
import { StepEditor } from './StepEditor'
import { WorkflowFlow } from './WorkflowFlow'
import { isYamlWorkflow } from './WorkflowLauncher'
import {
  INPUT_TYPES,
  NAME_RE,
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

/** Where a clone for "Clone from" stands. */
type CopyState =
  | { status: 'idle' }
  | { status: 'loading'; source: string }
  | { status: 'blocked'; source: string; blocking: string[] }
  | { status: 'error'; source: string; message: string }

/**
 * A free name for a copy of `source`: source-copy, then source-copy-2 and so
 * on, cut to the 64 characters a workflow name may have.
 */
export function copyName(source: string, taken: Iterable<string>): string {
  const used = new Set(taken)
  for (let n = 1; ; n++) {
    const suffix = n === 1 ? '-copy' : `-copy-${n}`
    const name = `${source.slice(0, 64 - suffix.length)}${suffix}`
    if (!used.has(name)) return name
  }
}

/** Steps loaded from a file wait for their profile's declared params. */
function awaitingProfiles(steps: BuilderStep[]): BuilderStep[] {
  return steps.map(step => (step.agent !== '' ? { ...step, declared: { status: 'loading' as const } } : step))
}

/**
 * The workflow name that YAML text declares in a top-level `name:` line, or
 * null. It picks the name a create request sends when the YAML was edited by
 * hand, since the server refuses a name that differs from the YAML's. The
 * server parses and checks the YAML in full; this reads one line only.
 */
export function yamlTopLevelName(source: string): string | null {
  const m = source.match(/^(?:"name"|'name'|name)[ \t]*:[ \t]*(?:"([^"\n]*)"|'([^'\n]*)'|([^\s"'#][^\s#]*))[ \t]*(?:#.*)?$/m)
  const value = m ? (m[1] ?? m[2] ?? m[3]) : null
  return value !== null && value !== undefined && NAME_RE.test(value) ? value : null
}

/** The line under "Clone from": what the clone is doing, or what it copies. */
function CopyStatus({ copy, copiedFrom }: { copy: CopyState; copiedFrom: string }) {
  if (copy.status === 'loading') {
    return (
      <p className="mt-1 flex items-center gap-1.5 text-[11px] text-gray-500">
        <Loader2 size={12} className="animate-spin" aria-hidden="true" /> Cloning {copy.source}...
      </p>
    )
  }
  if (copy.status === 'blocked') {
    return (
      <div role="alert" className="mt-1.5 rounded-lg border border-amber-700/50 bg-amber-950/30 p-2 text-xs text-amber-200">
        <p>{copy.source} uses fields the builder cannot edit, so it cannot be cloned here:</p>
        <ul className="mt-1 list-disc pl-5 font-mono">
          {copy.blocking.map(b => <li key={b}>{b}</li>)}
        </ul>
      </div>
    )
  }
  if (copy.status === 'error') {
    return <p role="alert" className="mt-1 text-xs text-red-400">Could not clone {copy.source}: {copy.message}</p>
  }
  if (copiedFrom) {
    return (
      <p className="mt-0.5 text-[11px] text-gray-500">
        Cloned from {copiedFrom}. Saving creates a new workflow and leaves {copiedFrom} unchanged.
      </p>
    )
  }
  return (
    <p className="mt-0.5 text-[11px] text-gray-500">
      Pick a workflow to copy its description, inputs and steps. Only YAML workflows are listed.
    </p>
  )
}

const fieldBase = (invalid: boolean, width: string) =>
  `${width} rounded-lg border bg-gray-900 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-hidden focus:border-emerald-600 ${
    invalid ? 'border-red-700' : 'border-gray-700'
  }`

/**
 * A text field's classes. `width` replaces the full width for a field that
 * shares a row; passing both a width and w-full lets w-full win.
 */
const fieldClass = (invalid: boolean, width = 'w-full') => `px-2 ${fieldBase(invalid, width)}`

/** A select's classes: the right padding leaves room for the chevron index.css draws. */
const selectClass = (invalid: boolean, width = 'w-full') => `pl-2 pr-8 ${fieldBase(invalid, width)}`

function FieldError({ message }: { message?: string }) {
  if (!message) return null
  return <p className="mt-0.5 text-xs text-red-400">{message}</p>
}

const WORKFLOW_DOCS_URL = 'https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/workflows.md'

/** The most runs one read of `GET /workflows/runs` returns: the server's `limit` cap. */
export const RUN_LIST_MAX_LIMIT = 500

export const FUTURE_RUNS_NOTE =
  'Changes apply to new runs only. Runs that already started, including runs you resume, keep the version they started with.'

/**
 * Shown when editing a workflow that has runs. It is accurate for resumed runs
 * too: resume reads the spec snapshot the run started with, not the file.
 */
function FutureRunsNote() {
  return (
    <div
      role="note"
      aria-label="Changes apply to new runs only"
      data-testid="builder-future-runs-note"
      className="mb-4 flex gap-2 rounded-xl border border-sky-800/60 bg-sky-950/30 p-3 text-xs text-sky-200"
    >
      <Info size={14} className="mt-0.5 shrink-0 text-sky-300" aria-hidden="true" />
      <p>{FUTURE_RUNS_NOTE}</p>
    </div>
  )
}

/**
 * What a YAML workflow cannot do, so nobody builds one expecting it to. The engine
 * reserves branching, loops and parallel modes, a step's answer reaches a later step
 * only through output_schema, which the builder has no field for, and a run that uses
 * assign or send_message is refused.
 */
function YamlLimitsNote() {
  return (
    <div
      role="note"
      aria-label="What YAML workflows cannot do"
      className="mb-4 flex gap-2 rounded-xl border border-gray-800 bg-gray-950/40 p-3 text-xs text-gray-300"
    >
      <Info size={14} className="mt-0.5 shrink-0 text-gray-400" aria-hidden="true" />
      <div className="min-w-0 space-y-1">
        <p>A YAML workflow runs a fixed list of steps, one at a time, in order. Workflows made here cannot:</p>
        <ul className="list-disc space-y-0.5 pl-5">
          <li>branch on a condition, loop, or run steps in parallel</li>
          <li>pass one step's answer to a later step</li>
          <li>run assign or send_message steps, which are not built yet</li>
        </ul>
        <p>
          For these, write a Python workflow. See the{' '}
          <a
            href={WORKFLOW_DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-emerald-400 underline hover:text-emerald-300"
          >
            workflow docs
          </a>
          .
        </p>
      </div>
    </div>
  )
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

  // Whether the workflow being edited has runs, for the future-runs note. The
  // list is newest first and has no name filter, so read the most it returns.
  // Best effort: a failed read shows no note.
  const [hasRuns, setHasRuns] = useState(false)
  useEffect(() => {
    setHasRuns(false)
    if (!editing) return
    let cancelled = false
    api.listWorkflowRuns(RUN_LIST_MAX_LIMIT)
      .then(rows => {
        if (!cancelled) setHasRuns(Array.isArray(rows) && rows.some(r => r.workflow_name === editName))
      })
      .catch(() => { /* no note */ })
    return () => { cancelled = true }
  }, [editing, editName])

  const loadGen = useGeneration()
  // A profile read from the edit load lands only on steps that still use that
  // profile and still wait for it, so it never undoes a pick made meanwhile.
  const settleDeclared = (agent: string, patch: (step: BuilderStep) => Partial<BuilderStep>) =>
    setCards(cs => cs.map(c => (
      c.step.agent === agent && c.step.declared.status === 'loading' ? { ...c, step: applyStepPatch(c.step, patch) } : c
    )))
  // Read each distinct step profile once, for the params it declares. Loading
  // never pre-fills a default: the file is the truth.
  const readStepProfiles = (steps: BuilderStep[], token: GenerationToken) => {
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
  }
  const load = useCallback(() => {
    if (!editing) return
    const token = loadGen.begin()
    setLoadState('loading')
    setLoadError(null)
    setSaveError(null)
    setValidation(null)
    setAttempted(false)
    setYamlDraft(null)
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
        const steps = awaitingProfiles(result.workflow.steps)
        // The route name wins: the server refuses a spec whose name differs from it.
        setName(editName)
        setDescription(result.workflow.description)
        setInputs(result.workflow.inputs)
        setCards(steps.map(step => ({ key: nextKey.current++, step })))
        setExpectedHash(typeof spec.content_hash === 'string' && spec.content_hash !== '' ? spec.content_hash : null)
        setLoadState('ready')
        readStepProfiles(steps, token)
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
  // Hand edits to the YAML panel: null while the YAML follows the form. Once
  // the YAML is edited by hand, Validate and Save use it as written and the
  // form is paused, because the builder does not read YAML text back into it.
  const [yamlDraft, setYamlDraft] = useState<string | null>(null)
  const yamlEdited = yamlDraft !== null
  const shownYaml = yamlDraft ?? yaml
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const errors = useMemo(() => validateBuilder(wf), [wf])
  const errorCount = Object.keys(errors).length
  const shown = attempted ? errors : {}

  // A validation result describes the YAML it checked. Drop it once that changes.
  const yamlRef = useRef(shownYaml)
  yamlRef.current = shownYaml
  useEffect(() => { setValidation(null) }, [shownYaml])

  // Start from: create mode can begin from a copy of a stored YAML workflow.
  // Best effort: when the list cannot be read, the choice is not shown.
  const [stored, setStored] = useState<WorkflowDefRow[]>([])
  useEffect(() => {
    if (editing) return
    let cancelled = false
    api.listWorkflows()
      .then(rows => { if (!cancelled && Array.isArray(rows)) setStored(rows) })
      .catch(() => { /* no Start from choice */ })
    return () => { cancelled = true }
  }, [editing])
  const copySources = useMemo(
    () => stored.filter(isYamlWorkflow).map(w => w.name).sort((a, b) => a.localeCompare(b)),
    [stored],
  )
  const [copiedFrom, setCopiedFrom] = useState('')
  const [copy, setCopy] = useState<CopyState>({ status: 'idle' })
  // The choice waiting for "Replace": a source name, or '' for a blank workflow.
  const [pendingStart, setPendingStart] = useState<string | null>(null)
  const blankYaml = useMemo(() => toYaml({ name: '', description: '', inputs: [], steps: [newStep('step-1')] }), [])

  const applyStart = (source: string) => {
    setPendingStart(null)
    setSaveError(null)
    setValidation(null)
    setAttempted(false)
    setYamlDraft(null)
    if (source === '') {
      loadGen.invalidate()
      setCopy({ status: 'idle' })
      setCopiedFrom('')
      setName('')
      setDescription('')
      setInputs([])
      setCards([{ key: nextKey.current++, step: newStep('step-1') }])
      return
    }
    const token = loadGen.begin()
    setCopy({ status: 'loading', source })
    api.getWorkflowSpec(source)
      .then(spec => {
        if (!token.isCurrent()) return
        const result = fromSpec(spec)
        if (!result.ok) {
          // The form stays as it was: nothing of it is lost.
          setCopy({ status: 'blocked', source, blocking: result.blocking })
          return
        }
        const steps = awaitingProfiles(result.workflow.steps)
        setName(copyName(source, stored.map(w => w.name)))
        setDescription(result.workflow.description)
        setInputs(result.workflow.inputs)
        setCards(steps.map(step => ({ key: nextKey.current++, step })))
        setCopiedFrom(source)
        setCopy({ status: 'idle' })
        readStepProfiles(steps, token)
      })
      .catch((e: ApiError) => {
        if (!token.isCurrent()) return
        setCopy({ status: 'error', source, message: e?.detail || e?.message || 'Failed to load the workflow' })
      })
  }
  // A blank form is replaced at once. Anything else asks first.
  const startFrom = (source: string) => {
    if (yaml !== blankYaml) {
      setPendingStart(source)
      return
    }
    applyStart(source)
  }

  const validate = async () => {
    setAttempted(true)
    setValidation(null)
    // The form's own checks apply only while the form writes the YAML.
    if ((!yamlEdited && errorCount > 0) || validating) return
    const sent = shownYaml
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

  // Save validates the YAML on the server first, and saves only YAML that passes.
  const save = async () => {
    if (saveGuard.current) return
    setAttempted(true)
    setSaveError(null)
    if (!yamlEdited && errorCount > 0) return
    if (editing && !expectedHash) {
      setSaveError({ message: 'The server sent no content hash for this workflow, so it cannot be replaced safely. Reload it.', stale: true })
      return
    }
    const sent = shownYaml
    // Hand-edited YAML names itself. The server checks that name in full.
    const saveName = editing ? editName : (yamlEdited ? yamlTopLevelName(sent) ?? name : name)
    saveGuard.current = true
    setSaving(true)
    try {
      let check: WorkflowValidation
      try {
        check = await api.validateWorkflowSource(sent, 'yaml')
      } catch (e: any) {
        setValidation(null)
        setSaveError({ message: `Not saved: the workflow could not be validated. ${e?.detail || e?.message || 'The request failed.'}` })
        return
      }
      if (yamlRef.current === sent) setValidation(check)
      if (check.status === 'fail') {
        setSaveError({ message: 'Not saved: fix the validation errors first.' })
        return
      }
      const result = editing
        ? await api.updateWorkflow(editName, sent, expectedHash as string, 'yaml')
        : await api.createWorkflow(saveName, sent, 'yaml')
      onSaved(result?.name || saveName, result)
    } catch (e) {
      setSaveError(describeSaveError(e as ApiError, editing ? 'edit' : 'create', saveName))
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

          {ready && <YamlLimitsNote />}
          {ready && editing && hasRuns && <FutureRunsNote />}
          {ready && (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
              <div className="min-w-0 space-y-4">
                {yamlEdited && (
                  <div
                    role="note"
                    aria-label="The form is paused"
                    data-testid="builder-form-paused"
                    className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-amber-700/50 bg-amber-950/30 p-3 text-xs text-amber-200"
                  >
                    <p className="min-w-0 flex-1">
                      You edited the YAML by hand, so the form is paused. Validate and Save use the YAML as written.
                    </p>
                    <button
                      type="button"
                      onClick={() => setConfirmDiscard(true)}
                      className="shrink-0 rounded-lg border border-amber-700/60 px-2 py-1 text-amber-100 hover:bg-amber-900/40"
                    >
                      Discard YAML edits
                    </button>
                  </div>
                )}
                <fieldset disabled={yamlEdited} aria-label="Workflow form" className="min-w-0 space-y-4 disabled:opacity-50">
                <section className="space-y-2">
                  {!editing && copySources.length > 0 && (
                    <div data-testid="builder-clone-from">
                      <label className="block">
                        <span className="text-xs text-gray-400">Clone from</span>
                        <select
                          aria-label="Clone from"
                          value={copy.status === 'loading' ? copy.source : copiedFrom}
                          onChange={e => startFrom(e.target.value)}
                          disabled={saving}
                          className={`mt-1 ${selectClass(false)}`}
                        >
                          <option value="">None</option>
                          {copySources.map(source => <option key={source} value={source}>{source}</option>)}
                        </select>
                      </label>
                      <CopyStatus copy={copy} copiedFrom={copiedFrom} />
                    </div>
                  )}
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
                            className={`font-mono ${fieldClass(!!nameError, 'w-36')}`}
                          />
                          <select
                            aria-label={`Workflow input ${i + 1} type`}
                            value={input.type}
                            onChange={e => setInputType(i, e.target.value as InputType)}
                            className={selectClass(false, 'w-24')}
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
                              className={selectClass(!!defaultError, 'w-32')}
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
                              className={fieldClass(!!defaultError, 'min-w-0 flex-1')}
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
                </fieldset>
              </div>

              <aside className="min-w-0 space-y-3">
                <section className="rounded-xl border border-gray-800 p-3">
                  <h4 className="mb-2 text-sm font-semibold text-gray-300">Flow</h4>
                  {/* The flow is drawn from the form, so it pauses with the form. */}
                  {yamlEdited && (
                    <p className="mb-2 text-[11px] text-amber-200" data-testid="flow-paused">
                      Paused with the form, so it does not show your YAML edits.
                    </p>
                  )}
                  <div className={yamlEdited ? 'opacity-50' : undefined}>
                    <WorkflowFlow
                      label="Flow preview"
                      steps={wf.steps.map(s => ({ id: s.id, call: s.call, agent: s.agent, provider: s.provider }))}
                    />
                  </div>
                </section>
                <section className="rounded-xl border border-gray-800 p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <h4 className="text-sm font-semibold text-gray-300">YAML</h4>
                    {yamlEdited && (
                      <span className="rounded-sm bg-amber-900/40 px-1.5 py-0.5 text-[10px] font-medium text-amber-200" data-testid="yaml-edited">
                        Edited by hand
                      </span>
                    )}
                  </div>
                  <p className="mb-2 text-[11px] text-gray-500">
                    {yamlEdited
                      ? 'Saved as written, comments included. Save validates it first.'
                      : 'Edit the YAML here, or use the form. Save validates it first. When the form writes the YAML, comments in the file are not kept.'}
                  </p>
                  <textarea
                    aria-label="Workflow YAML"
                    value={shownYaml}
                    // Typing the form's own YAML back exactly puts the form back in charge.
                    onChange={e => setYamlDraft(e.target.value === yaml ? null : e.target.value)}
                    disabled={saving}
                    spellCheck={false}
                    autoCapitalize="off"
                    autoCorrect="off"
                    wrap="off"
                    rows={22}
                    className="block h-96 w-full resize-y rounded-sm border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] leading-relaxed text-gray-300 focus:border-emerald-600 focus:outline-hidden"
                  />
                  {yamlEdited && (
                    <button
                      type="button"
                      onClick={() => setConfirmDiscard(true)}
                      className="mt-2 rounded-lg border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:bg-gray-800"
                    >
                      Discard YAML edits
                    </button>
                  )}
                </section>
              </aside>
            </div>
          )}
        </div>

        {ready && ((attempted && errorCount > 0 && !yamlEdited) || validation || saveError) && (
          <div className="space-y-2 border-t border-gray-800 px-4 py-3 text-xs">
            {attempted && errorCount > 0 && !yamlEdited && (
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
      <ConfirmModal
        open={pendingStart !== null}
        title={pendingStart ? `Clone ${pendingStart}?` : 'Clear the form?'}
        message={
          pendingStart
            ? `The form fills with the description, inputs and steps of ${pendingStart}. This replaces everything in the form.`
            : 'This clears everything in the form.'
        }
        confirmLabel="Replace"
        cancelLabel="Keep editing"
        variant="warning"
        onConfirm={() => { if (pendingStart !== null) applyStart(pendingStart) }}
        onCancel={() => setPendingStart(null)}
      />
      <ConfirmModal
        open={confirmDiscard}
        title="Discard your YAML edits?"
        message="The YAML goes back to what the form shows, and you can use the form again."
        confirmLabel="Discard edits"
        cancelLabel="Keep editing"
        variant="warning"
        onConfirm={() => { setYamlDraft(null); setConfirmDiscard(false) }}
        onCancel={() => setConfirmDiscard(false)}
      />
    </div>
  )
}
