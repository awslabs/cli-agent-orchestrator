// WorkflowsPanel — the top-level Workflows surface (#504 / U8, FR-10), a
// sibling to Agents/Inbox/Memory. A list/detail layout: RunList (left) drives
// RunDetail (right) via the additive workflowRuns store slice. Purely
// client-side over the already-built durable-journal read APIs.
//
// New workflow opens the workflow builder. The launcher's Edit opens it on a
// YAML workflow. After a save the launcher re-reads its list and opens on the
// saved workflow, ready to run.

import { useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import type { WorkflowWriteResult } from '../api'
import { useStore } from '../store'
import { RunList } from './workflow/RunList'
import { RunDetail } from './workflow/RunDetail'
import { WorkflowLauncher } from './workflow/WorkflowLauncher'
import { WorkflowBuilder } from './workflow/WorkflowBuilder'
import { PlanApprovalForm } from './workflow/PlanApprovalForm'
import { ErrorBoundary } from './ErrorBoundary'

type BuilderTarget = { mode: 'create' } | { mode: 'edit'; name: string }

export function WorkflowsPanel() {
  const runs = useStore(s => s.workflowRuns)
  const selectedRun = useStore(s => s.selectedRun)
  const fetchWorkflowRuns = useStore(s => s.fetchWorkflowRuns)
  const selectWorkflowRun = useStore(s => s.selectWorkflowRun)
  const showSnackbar = useStore(s => s.showSnackbar)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [builder, setBuilder] = useState<BuilderTarget | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [focus, setFocus] = useState<{ name: string; seq: number } | null>(null)

  const onSaved = (name: string, result: WorkflowWriteResult) => {
    setBuilder(null)
    setReloadKey(k => k + 1)
    setFocus(f => ({ name, seq: (f?.seq ?? 0) + 1 }))
    showSnackbar(
      result?.status === 'pass_reserved'
        ? { type: 'info', message: `Saved ${name}. It uses call types that are not built yet, so a run is refused until they are built.` }
        : { type: 'success', message: `Saved workflow ${name}` },
    )
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    // fetchWorkflowRuns surfaces its own error via snackbar AND resolves to the
    // message (null on success) — it never rejects. We mirror that signal into
    // the list's inline error state for a self-contained panel, and clear it on
    // a later successful poll so a transient blip heals itself.
    const load = async () => {
      const message = await fetchWorkflowRuns()
      if (!cancelled) setError(message)
    }
    load().finally(() => {
      if (!cancelled) setLoading(false)
    })
    // Poll the list on the same 10s cadence the sessions poll uses.
    const interval = setInterval(load, 10000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [fetchWorkflowRuns])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[20rem_1fr] gap-4">
      {/* Run list */}
      <aside className="h-fit">
        <button
          type="button"
          onClick={() => setBuilder({ mode: 'create' })}
          className="mb-4 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-600"
        >
          <Plus size={15} aria-hidden="true" /> New workflow
        </button>
        <WorkflowLauncher
          reloadKey={reloadKey}
          focus={focus}
          onEdit={name => setBuilder({ mode: 'edit', name })}
        />
        <PlanApprovalForm />
        <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3">
        <h2 className="text-sm font-semibold text-gray-300 mb-2">Workflow Runs</h2>
        <RunList
          runs={runs}
          selectedRunId={selectedRun?.run_id ?? null}
          loading={loading}
          error={error}
          onSelect={selectWorkflowRun}
        />
        </div>
      </aside>

      {/* Detail */}
      <section className="min-w-0">
        {selectedRun ? (
          <ErrorBoundary>
            <RunDetail run={selectedRun} allRuns={runs} />
          </ErrorBoundary>
        ) : (
          <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-12 text-center text-sm text-gray-500">
            Select a run to inspect its timeline, playback, and diagnostics.
          </div>
        )}
      </section>

      {builder && (
        <ErrorBoundary>
          {builder.mode === 'edit' ? (
            <WorkflowBuilder mode="edit" name={builder.name} onClose={() => setBuilder(null)} onSaved={onSaved} />
          ) : (
            <WorkflowBuilder mode="create" onClose={() => setBuilder(null)} onSaved={onSaved} />
          )}
        </ErrorBoundary>
      )}
    </div>
  )
}
