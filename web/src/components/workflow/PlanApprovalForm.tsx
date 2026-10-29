// PlanApprovalForm — approve a workflow plan id from the UI (the
// `cao workflow approve <plan_id>` equivalent, issue #583).
//
// The launcher already offers Approve & run for plans IT started. This covers the
// other path: an agent's workflow_run comes back approval_required and the agent
// shows the human a plan id. Pasting it here keeps approval a human act in the
// browser instead of a shell command. The id is sent verbatim (it contains a
// ':'); the server reports whether this call changed anything.

import { useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, ShieldCheck } from 'lucide-react'
import { api, PlanApproval } from '../../api'

export function PlanApprovalForm() {
  const [open, setOpen] = useState(false)
  const [planId, setPlanId] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<PlanApproval | null>(null)
  const [error, setError] = useState<string | null>(null)

  const approve = async () => {
    const id = planId.trim()
    if (!id || busy) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await api.approveWorkflowPlan(id))
    } catch (e: any) {
      setError(e?.detail || e?.message || 'Failed to approve plan')
    } finally {
      setBusy(false)
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
        Approve a plan id
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          <p className="text-xs text-gray-500">
            Paste the plan id an agent reported with approval_required. Approval covers that script with
            exactly those inputs.
          </p>
          <input
            aria-label="Plan id"
            value={planId}
            onChange={e => setPlanId(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') approve() }}
            placeholder="plan-v1:…"
            className="w-full rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm font-mono text-gray-200"
          />
          <button
            type="button"
            onClick={approve}
            disabled={busy || !planId.trim()}
            className="inline-flex items-center gap-2 rounded-lg bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <ShieldCheck size={14} aria-hidden="true" />}
            Approve
          </button>
          {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
          {result && (
            <p className="text-xs text-emerald-300" role="status">
              {result.changed ? 'Approved' : 'Already approved'}
              {result.approved_at ? ` at ${result.approved_at}` : ''}
              {result.approved_by ? ` by ${result.approved_by}` : ''}. Ask the agent to run it again.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
