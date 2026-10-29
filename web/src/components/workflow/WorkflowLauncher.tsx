// WorkflowLauncher — run a stored workflow from the UI, the `cao workflow run`
// equivalent. Picks an indexed workflow (GET /workflows), prefills its run inputs
// from the inputs the workflow declares (GET /workflows/{name}) as editable
// key/value rows, and submits async via the store's startWorkflowRun
// (POST /workflows/runs:submit), which refreshes the run list and selects the
// new run so the detail pane follows it live. Purely additive over existing
// endpoints.
//
// Approval gate (issue #583): the first run of a new plan is refused with
// approval_required + a plan id. The launcher shows that plan id and an
// "Approve & run" button (POST /workflows/plans/approve, then resubmit) — the
// human act the MCP surface deliberately withholds from agents. The approval
// binds the script AND these inputs, so editing anything clears the prompt.
//
// The draft (workflow, inputs, run id) and any pending approval persist in
// localStorage, so reloading the tab or opening the UI in a new tab still shows
// the plan id waiting for approval instead of silently dropping it.

import { useEffect, useRef, useState } from 'react'
import { Play, Plus, X, Loader2, ChevronDown, ChevronRight, ShieldCheck, ListChecks, FileCode } from 'lucide-react'
import { api, WorkflowDefRow, WorkflowSpec, WorkflowValidation } from '../../api'
import { useStore } from '../../store'

interface InputRow {
  key: string
  value: string
  /** From the workflow's declaration; a hint only, the server validates. */
  required?: boolean
  type?: string
}

const EMPTY_ROWS: InputRow[] = [{ key: '', value: '' }]

// Rows for a workflow's declared inputs (`inputs: {name: {type, required,
// default}}` on GET /workflows/{name}). Required inputs first, defaults filled.
export function rowsFromDeclaredInputs(inputs: unknown): InputRow[] {
  if (!inputs || typeof inputs !== 'object') return EMPTY_ROWS
  const rows = Object.entries(inputs as Record<string, any>).map(([key, decl]) => ({
    key,
    value: decl && decl.default !== undefined && decl.default !== null ? String(decl.default) : '',
    required: !!decl?.required,
    type: typeof decl?.type === 'string' ? decl.type : undefined,
  }))
  rows.sort((a, b) => Number(b.required) - Number(a.required))
  return rows.length ? rows : EMPTY_ROWS
}

export const LAUNCHER_DRAFT_KEY = 'cao.workflowLauncher'

interface PendingApproval {
  planId: string
  message?: string
  /** draftKey() of the selection the plan was refused for. */
  forDraft: string
}

interface LauncherDraft {
  selected: string
  runId: string
  inputs: InputRow[]
  pendingApproval: PendingApproval | null
}

const draftKey = (selected: string, inputs: InputRow[], runId: string) =>
  JSON.stringify([selected, inputs, runId])

export function loadLauncherDraft(): LauncherDraft | null {
  try {
    const raw = localStorage.getItem(LAUNCHER_DRAFT_KEY)
    if (!raw) return null
    const d = JSON.parse(raw)
    if (!d || typeof d.selected !== 'string' || typeof d.runId !== 'string' || !Array.isArray(d.inputs)) return null
    const inputs = d.inputs.filter((r: any) => r && typeof r.key === 'string' && typeof r.value === 'string')
    const p = d.pendingApproval
    const pendingApproval = p && typeof p.planId === 'string' && typeof p.forDraft === 'string' ? p : null
    return { selected: d.selected, runId: d.runId, inputs, pendingApproval }
  } catch {
    return null
  }
}

function saveLauncherDraft(d: LauncherDraft) {
  try {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify(d))
  } catch { /* storage denied or full — the draft is a convenience */ }
}

export function WorkflowLauncher() {
  const startWorkflowRun = useStore(s => s.startWorkflowRun)
  const showSnackbar = useStore(s => s.showSnackbar)

  const [draft] = useState(loadLauncherDraft)
  // Open straight away when a plan is still waiting, so a reload doesn't hide it.
  const [open, setOpen] = useState(!!draft?.pendingApproval)
  const [workflows, setWorkflows] = useState<WorkflowDefRow[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState(draft?.selected ?? '')
  const [runId, setRunId] = useState(draft?.runId ?? '')
  const [inputs, setInputs] = useState<InputRow[]>(draft?.inputs.length ? draft.inputs : EMPTY_ROWS)
  // True until the user edits a row. Only pristine rows are replaced by the
  // selected workflow's declared inputs, so a slow spec read never clobbers
  // typing, and a restored draft (not pristine) wins over the prefill.
  const pristine = useRef(!draft)
  const [submitting, setSubmitting] = useState(false)
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(draft?.pendingApproval ?? null)

  // Any change to what would run invalidates a shown plan id. Compared against
  // the selection it was refused for, so restoring a draft doesn't clear it.
  const currentDraft = draftKey(selected, inputs, runId)
  useEffect(() => {
    setPendingApproval(p => (p && p.forDraft !== currentDraft ? null : p))
  }, [currentDraft])

  useEffect(() => {
    saveLauncherDraft({ selected, runId, inputs, pendingApproval })
  }, [selected, runId, inputs, pendingApproval])

  // Validate (the `cao workflow validate` equivalent) lints the stored file.
  const [validation, setValidation] = useState<WorkflowValidation | null>(null)
  const [validating, setValidating] = useState(false)
  useEffect(() => { setValidation(null) }, [selected])
  const validate = async () => {
    const def = workflows.find(w => w.name === selected)
    if (!def || validating) return
    setValidating(true)
    try {
      setValidation(await api.validateWorkflow(def.source_path))
    } catch (e: any) {
      setValidation({ status: 'fail', errors: [e?.detail || e?.message || 'Validation request failed'] })
    } finally {
      setValidating(false)
    }
  }

  // View spec (the `cao workflow get` equivalent): source, content hash, findings.
  // Toggles; a second click hides it. Cleared when another workflow is picked.
  const [spec, setSpec] = useState<WorkflowSpec | null>(null)
  const [specError, setSpecError] = useState<string | null>(null)
  const [loadingSpec, setLoadingSpec] = useState(false)
  useEffect(() => { setSpec(null); setSpecError(null) }, [selected])
  const toggleSpec = async () => {
    if (spec || specError) { setSpec(null); setSpecError(null); return }
    if (!selected || loadingSpec) return
    setLoadingSpec(true)
    try {
      setSpec(await api.getWorkflowSpec(selected))
    } catch (e: any) {
      setSpecError(e?.detail || e?.message || 'Failed to load the workflow spec')
    } finally {
      setLoadingSpec(false)
    }
  }

  // Load the stored workflow definitions when the launcher is first opened.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    api.listWorkflows()
      .then(rows => {
        if (cancelled) return
        setWorkflows(rows)
        setLoadError(null)
        // Default the picker to the only / first workflow for a one-click run.
        if (rows.length && !selected) setSelected(rows[0].name)
      })
      .catch((e: any) => {
        if (!cancelled) setLoadError(e?.detail || e?.message || 'Failed to load workflows')
      })
    return () => { cancelled = true }
  }, [open, selected])

  // Prefill the input rows from what the selected workflow declares.
  useEffect(() => {
    if (!open || !selected || !pristine.current) return
    let cancelled = false
    api.getWorkflowSpec(selected)
      .then(spec => { if (!cancelled && pristine.current) setInputs(rowsFromDeclaredInputs(spec.inputs)) })
      .catch(() => { /* no declared inputs to offer; the rows stay as they are */ })
    return () => { cancelled = true }
  }, [open, selected])

  const pickWorkflow = (name: string) => {
    // Rows belong to the previous workflow; let the new one's declaration replace them.
    pristine.current = true
    setSelected(name)
  }

  const edited = (fn: (rows: InputRow[]) => InputRow[]) => {
    pristine.current = false
    setInputs(fn)
  }
  const setRow = (i: number, patch: Partial<InputRow>) =>
    edited(rows => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const addRow = () => edited(rows => [...rows, { key: '', value: '' }])
  const removeRow = (i: number) => edited(rows => rows.filter((_, j) => j !== i))

  // Only send rows with a non-empty key; the server validates against the
  // workflow's declared inputs and returns a 400 the store surfaces as a snackbar.
  const buildPayload = () => {
    const payload: Record<string, unknown> = {}
    for (const { key, value } of inputs) {
      const k = key.trim()
      if (k) payload[k] = value
    }
    return payload
  }

  const run = async () => {
    const outcome = await startWorkflowRun(selected, buildPayload(), runId.trim() || undefined)
    setPendingApproval(outcome && 'approvalPlanId' in outcome
      ? { planId: outcome.approvalPlanId, message: outcome.message, forDraft: currentDraft }
      : null)
  }

  const submit = async () => {
    if (!selected || submitting) return
    setSubmitting(true)
    try {
      await run()
    } finally {
      setSubmitting(false)
    }
  }

  const approveAndRun = async () => {
    if (!pendingApproval || submitting) return
    setSubmitting(true)
    try {
      await api.approveWorkflowPlan(pendingApproval.planId)
      await run()
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e?.detail || e?.message || 'Failed to approve plan' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3 mb-4">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-sm font-semibold text-gray-300"
      >
        {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
        Run a workflow
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          {loadError ? (
            <p className="text-sm text-red-400" role="alert">{loadError}</p>
          ) : workflows.length === 0 ? (
            <p className="text-sm text-gray-500">
              No stored workflows yet. Author one with the workflow agent first, then it appears here.
            </p>
          ) : (
            <>
              <label className="block">
                <span className="text-xs text-gray-400">Workflow</span>
                <select
                  value={selected}
                  onChange={e => pickWorkflow(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-900 px-2 py-1.5 text-sm text-gray-200"
                >
                  {workflows.map(w => (
                    <option key={w.name} value={w.name}>
                      {w.name}{w.description ? ` — ${w.description}` : ''}
                    </option>
                  ))}
                </select>
              </label>

              <div>
                <span className="text-xs text-gray-400">Inputs</span>
                <div className="mt-1 space-y-1.5">
                  {inputs.map((row, i) => (
                    <div key={i} className="flex items-center gap-1.5">
                      <input
                        aria-label={`Input ${i + 1} name`}
                        value={row.key}
                        onChange={e => setRow(i, { key: e.target.value })}
                        placeholder="name"
                        className="w-28 rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 font-mono"
                      />
                      <input
                        aria-label={`Input ${i + 1} value`}
                        value={row.value}
                        onChange={e => setRow(i, { value: e.target.value })}
                        placeholder={row.required ? `value (required${row.type ? `, ${row.type}` : ''})` : row.type ? `value (${row.type})` : 'value'}
                        className="flex-1 rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200"
                      />
                      <button
                        type="button"
                        onClick={() => removeRow(i)}
                        aria-label={`Remove input ${i + 1}`}
                        className="p-1 text-gray-500 hover:text-red-400"
                      >
                        <X size={14} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={addRow}
                    className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200"
                  >
                    <Plus size={13} aria-hidden="true" /> Add input
                  </button>
                </div>
              </div>

              <label className="block">
                <span className="text-xs text-gray-400">Run id (optional)</span>
                <input
                  value={runId}
                  onChange={e => setRunId(e.target.value)}
                  placeholder="auto-generated"
                  className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 font-mono"
                />
              </label>

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={submit}
                  disabled={!selected || submitting}
                  className="inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {submitting ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                  Run workflow
                </button>
                <button
                  type="button"
                  onClick={validate}
                  disabled={!selected || validating}
                  className="inline-flex items-center gap-2 rounded-lg border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {validating ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <ListChecks size={14} aria-hidden="true" />}
                  Validate
                </button>
                <button
                  type="button"
                  onClick={toggleSpec}
                  disabled={!selected || loadingSpec}
                  aria-expanded={!!spec}
                  className="inline-flex items-center gap-2 rounded-lg border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {loadingSpec ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <FileCode size={14} aria-hidden="true" />}
                  {spec ? 'Hide spec' : 'View spec'}
                </button>
              </div>

              {specError && <p className="text-xs text-red-400" role="alert">{specError}</p>}
              {spec && (
                <div className="rounded-lg border border-gray-700 p-2 text-xs" aria-label="Workflow spec">
                  {spec.path && <p className="break-all font-mono text-gray-400">{spec.path}</p>}
                  {spec.content_hash && <p className="mt-0.5 break-all font-mono text-gray-500">hash {spec.content_hash}</p>}
                  {(spec.findings ?? []).map((f, i) => (
                    <p key={i} className="mt-1 font-mono text-amber-300">line {f.line} [{f.severity}] {f.rule_id}: {f.message}</p>
                  ))}
                  <pre className="mt-2 max-h-96 overflow-auto rounded bg-gray-950 p-2 font-mono text-[11px] text-gray-300">
                    {typeof spec.source === 'string' ? spec.source : JSON.stringify(spec, null, 2)}
                  </pre>
                </div>
              )}

              {validation && (
                <div
                  role="status"
                  className={`rounded-lg border p-2 text-xs ${validation.status === 'fail' ? 'border-red-800/60 text-red-300' : 'border-emerald-800/60 text-emerald-300'}`}
                >
                  <p className="font-medium">
                    {validation.status === 'fail' ? 'Validation failed' : 'Valid'}
                    {validation.findings?.length ? ` — ${validation.findings.length} finding${validation.findings.length > 1 ? 's' : ''}` : ''}
                  </p>
                  {[...validation.errors.map(e => ({ key: `e:${e}`, text: e })),
                    ...(validation.findings ?? []).map((f, i) => ({ key: `f:${i}`, text: `line ${f.line} [${f.severity}] ${f.rule_id}: ${f.message}` })),
                  ].map(row => <p key={row.key} className="mt-1 font-mono text-gray-300">{row.text}</p>)}
                </div>
              )}

              {pendingApproval && (
                <div role="alert" className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-3 space-y-2">
                  <p className="text-sm text-amber-200">
                    This workflow plan needs your approval before it can run. Approving covers this
                    script with exactly these inputs; changing either needs a new approval.
                  </p>
                  <p className="text-xs text-gray-400">
                    Plan id: <code className="break-all font-mono text-gray-300">{pendingApproval.planId}</code>
                  </p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={approveAndRun}
                      disabled={submitting}
                      className="inline-flex items-center gap-2 rounded-lg bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {submitting ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <ShieldCheck size={14} aria-hidden="true" />}
                      Approve &amp; run
                    </button>
                    <button
                      type="button"
                      onClick={() => setPendingApproval(null)}
                      disabled={submitting}
                      className="rounded-lg px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
                    >
                      Not now
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
