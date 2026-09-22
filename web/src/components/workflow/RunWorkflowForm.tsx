// RunWorkflowForm — the run-with-inputs form (issue #789, PR A). One control
// per declared input, generated from `GET /workflows/{name}`'s `inputs` map —
// never a hand-written per-workflow form. Submits through the async spine
// (`POST /workflows/runs:submit`, 202) and hands the accepted run_id back to
// the panel, which lands on the existing RunDetail timeline.

import { useEffect, useMemo, useState } from 'react'
import { api, ApiError, WorkflowDetail, WorkflowInputDecl } from '../../api'
import { AlertTriangle, Loader2, Play, ShieldAlert, ShieldCheck, X } from 'lucide-react'

interface RunWorkflowFormProps {
  name: string
  onSubmitted: (runId: string) => void
  onClose: () => void
}

/** One script-lint finding from the submit route's 422 body. */
interface LintFindingRow {
  line: number
  rule_id: string
  severity: string
  message: string
}

/**
 * The submit route reuses 403 for two unrelated refusals: a missing token
 * scope (auth middleware, detail "Forbidden: requires one of [...]") and an
 * unapproved plan (approval gate, detail "Plan '<id>' has not been
 * approved..."). The gate's detail is the operator's ONLY handle on which
 * plan to approve — the run is refused before any journal row exists, so
 * there is no run_id to read the plan from.
 */
function planIdFromDetail(detail: string | undefined): string | null {
  if (!detail) return null
  const m = /[Pp]lan '([^']+)'/.exec(detail)
  return m ? m[1] : null
}

function isScopeForbidden(detail: string | undefined): boolean {
  return !!detail && detail.startsWith('Forbidden:')
}

/**
 * Map a 400 input-validation detail onto the offending field. The server's
 * `_validate_inputs` messages quote the input name (e.g. "required input
 * 'table_name' missing"), so a quoted token that matches a declared input
 * anchors the error next to that control instead of a detached banner.
 */
function fieldFromDetail(detail: string | undefined, inputNames: string[]): string | null {
  if (!detail) return null
  for (const m of detail.matchAll(/'([^']+)'/g)) {
    if (inputNames.includes(m[1])) return m[1]
  }
  return null
}

const inputTypeHint: Record<WorkflowInputDecl['type'], string> = {
  string: 'text',
  int: 'integer',
  bool: 'boolean',
  path: 'filesystem path',
}

export function RunWorkflowForm({ name, onSubmitted, onClose }: RunWorkflowFormProps) {
  const [detail, setDetail] = useState<WorkflowDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Text/number inputs hold strings until submit-time coercion; booleans are
  // real booleans from the checkbox.
  const [values, setValues] = useState<Record<string, string | boolean>>({})
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [permissionError, setPermissionError] = useState<string | null>(null)
  const [approvalPlanId, setApprovalPlanId] = useState<string | null>(null)
  const [approvalMessage, setApprovalMessage] = useState<string | null>(null)
  const [storeUnavailable, setStoreUnavailable] = useState<string | null>(null)
  const [lintFindings, setLintFindings] = useState<LintFindingRow[] | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [approving, setApproving] = useState(false)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setLoadError(null)
    setValues({})
    api.getWorkflow(name).then(
      spec => {
        if (cancelled) return
        setDetail(spec)
        // Pre-fill declared defaults. A bool with no default starts unchecked;
        // everything else starts empty and is omitted from the submit payload
        // so the server applies its own default (one source of truth).
        const initial: Record<string, string | boolean> = {}
        for (const [key, decl] of Object.entries(spec.inputs ?? {})) {
          if (decl.type === 'bool') initial[key] = decl.default === true
          else if (decl.default != null) initial[key] = String(decl.default)
          else initial[key] = ''
        }
        setValues(initial)
      },
      (err: ApiError) => {
        if (!cancelled) setLoadError(err.detail ?? err.message)
      },
    )
    return () => { cancelled = true }
  }, [name])

  const inputEntries = useMemo(
    () => Object.entries(detail?.inputs ?? {}),
    [detail],
  )

  function clearOutcome() {
    setFieldErrors({})
    setFormError(null)
    setPermissionError(null)
    setApprovalPlanId(null)
    setApprovalMessage(null)
    setStoreUnavailable(null)
    setLintFindings(null)
  }

  /** Coerce form state to the typed inputs payload, or null on a client-side error. */
  function buildPayload(): Record<string, string | number | boolean> | null {
    const payload: Record<string, string | number | boolean> = {}
    const errors: Record<string, string> = {}
    for (const [key, decl] of inputEntries) {
      const raw = values[key]
      if (decl.type === 'bool') {
        payload[key] = raw === true
        continue
      }
      const text = typeof raw === 'string' ? raw.trim() : ''
      if (text === '') {
        // An omitted key lets the server fill the declared default; a missing
        // required input is caught here so the user never burns a round trip.
        if (decl.required && decl.default == null) errors[key] = 'required'
        continue
      }
      if (decl.type === 'int') {
        const n = Number(text)
        if (!Number.isInteger(n)) {
          errors[key] = 'must be an integer'
          continue
        }
        payload[key] = n
      } else {
        payload[key] = text
      }
    }
    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors)
      return null
    }
    return payload
  }

  async function submit() {
    clearOutcome()
    const payload = buildPayload()
    if (payload == null) return
    setSubmitting(true)
    try {
      const accepted = await api.submitWorkflowRun(name, payload)
      onSubmitted(accepted.run_id)
    } catch (e) {
      const err = e as ApiError
      if (err.status === 403 && isScopeForbidden(err.detail)) {
        setPermissionError(
          'Your token cannot start runs — it lacks the cao:write scope. ' + (err.detail ?? ''),
        )
      } else if (err.status === 403) {
        setApprovalPlanId(planIdFromDetail(err.detail))
        setApprovalMessage(err.detail ?? 'This plan has not been approved.')
      } else if (err.status === 503) {
        setStoreUnavailable(
          err.detail ?? 'The approval store is unavailable. Restore database access and retry.',
        )
      } else if (err.status === 422 && Array.isArray(err.detailMeta?.findings)) {
        setLintFindings(err.detailMeta.findings as LintFindingRow[])
      } else if (err.status === 400) {
        const field = fieldFromDetail(err.detail, inputEntries.map(([k]) => k))
        if (field) setFieldErrors({ [field]: err.detail ?? 'invalid value' })
        else setFormError(err.detail ?? err.message)
      } else {
        setFormError(err.detail ?? err.message)
      }
    } finally {
      setSubmitting(false)
    }
  }

  // The human approval act, then one automatic resubmit. Approval is per
  // plan_id, so the retried run passes the gate the first refusal named.
  async function approveAndRerun() {
    if (!approvalPlanId) return
    setApproving(true)
    try {
      await api.approveWorkflowPlan(approvalPlanId)
    } catch (e) {
      const err = e as ApiError
      setApproving(false)
      if (err.status === 403) {
        setPermissionError(
          'Approving a plan requires the cao:admin scope. ' + (err.detail ?? ''),
        )
      } else {
        setApprovalMessage(err.detail ?? err.message)
      }
      return
    }
    setApproving(false)
    await submit()
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-4">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-200">Run {name}</h2>
          {detail?.description ? (
            <p className="text-xs text-gray-500 mt-0.5">{detail.description}</p>
          ) : null}
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-gray-800 text-gray-500"
          aria-label="Close run form"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {loadError ? (
        <div className="text-sm text-red-400 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" /> {loadError}
        </div>
      ) : !detail ? (
        <div className="text-sm text-gray-500 flex items-center gap-2">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading workflow…
        </div>
      ) : (
        <form
          onSubmit={e => { e.preventDefault(); void submit() }}
          className="space-y-3"
        >
          {inputEntries.length === 0 ? (
            <p className="text-xs text-gray-500">This workflow declares no inputs.</p>
          ) : (
            inputEntries.map(([key, decl]) => (
              <div key={key}>
                <label className="block text-xs text-gray-400 mb-1" htmlFor={`wf-input-${key}`}>
                  {key}
                  {decl.required && decl.default == null ? (
                    <span className="text-red-400 ml-0.5" title="required">*</span>
                  ) : null}
                  <span className="ml-1.5 text-[10px] text-gray-600">{inputTypeHint[decl.type]}</span>
                </label>
                {decl.type === 'bool' ? (
                  <input
                    id={`wf-input-${key}`}
                    type="checkbox"
                    checked={values[key] === true}
                    onChange={e => setValues(v => ({ ...v, [key]: e.target.checked }))}
                    className="h-4 w-4 rounded border-gray-700 bg-gray-800"
                  />
                ) : (
                  <input
                    id={`wf-input-${key}`}
                    type={decl.type === 'int' ? 'number' : 'text'}
                    value={typeof values[key] === 'string' ? (values[key] as string) : ''}
                    onChange={e => setValues(v => ({ ...v, [key]: e.target.value }))}
                    placeholder={decl.type === 'path' ? '/path/to/target' : undefined}
                    className="w-full rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-600"
                  />
                )}
                {fieldErrors[key] ? (
                  <p className="text-xs text-red-400 mt-1">{fieldErrors[key]}</p>
                ) : null}
              </div>
            ))
          )}

          {/* Mirrors R2 of the cao-workflow skill: inputs are journaled in
              plaintext and replayed verbatim on resume. */}
          <p className="text-[11px] text-amber-500/80 flex items-start gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            Inputs are journaled in plaintext and replayed on resume — do not enter secrets.
          </p>

          {formError ? (
            <div className="text-sm text-red-400 flex items-start gap-2" role="alert">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" /> {formError}
            </div>
          ) : null}

          {permissionError ? (
            <div className="text-sm text-amber-400 flex items-start gap-2" role="alert">
              <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" /> {permissionError}
            </div>
          ) : null}

          {storeUnavailable ? (
            <div className="text-sm text-amber-400 flex items-start gap-2" role="alert">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>
                {storeUnavailable}
                <button
                  type="button"
                  onClick={() => void submit()}
                  className="ml-2 underline text-blue-400"
                >
                  Retry
                </button>
              </span>
            </div>
          ) : null}

          {approvalMessage ? (
            <div
              className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-3 text-sm text-amber-300"
              role="alert"
            >
              <div className="flex items-start gap-2">
                <ShieldCheck className="w-4 h-4 shrink-0 mt-0.5" />
                <div>
                  <p>{approvalMessage}</p>
                  {approvalPlanId ? (
                    <>
                      <p className="mt-1 font-mono text-xs break-all">plan_id: {approvalPlanId}</p>
                      <button
                        type="button"
                        onClick={() => void approveAndRerun()}
                        disabled={approving}
                        className="mt-2 px-2.5 py-1 rounded-lg bg-amber-700/60 hover:bg-amber-700 text-amber-100 text-xs font-medium disabled:opacity-50"
                      >
                        {approving ? 'Approving…' : 'Approve plan and run again'}
                      </button>
                    </>
                  ) : (
                    // A refusal with no readable plan_id (failed manifest
                    // freeze) has nothing to approve — only the message helps.
                    <p className="mt-1 text-xs">
                      No plan identifier is available for this run, so there is nothing to approve.
                    </p>
                  )}
                </div>
              </div>
            </div>
          ) : null}

          {lintFindings ? (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 p-3" role="alert">
              <p className="text-sm text-red-300 mb-1.5">Script lint failed — the run was not started:</p>
              <ul className="space-y-1">
                {lintFindings.map((f, i) => (
                  <li key={i} className="text-xs text-red-300/90 font-mono">
                    line {f.line}: [{f.rule_id}] {f.message}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="pt-1">
            <button
              type="submit"
              disabled={submitting || permissionError != null}
              title={permissionError != null ? 'Missing the required token scope' : undefined}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-700 hover:bg-blue-600 text-white text-sm font-medium disabled:opacity-50"
            >
              {submitting
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Play className="w-4 h-4" />}
              Run
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
