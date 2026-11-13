// WorkflowsPanel — the top-level Workflows surface (#504 / U8, FR-10), a
// sibling to Agents/Inbox/Memory. Purely client-side over the already-built
// durable-journal read APIs. It has two views, and the store's run selection
// picks between them (the app has no URL routing):
//
// - Home, with no run selected: New workflow, the "Run a workflow" launcher
//   and "Approve a plan id" on the left, and the runs list as the page's main
//   content. A row click selects its run. Launching a run selects it too.
// - The run page (RunDetail), with a run selected. Its "Workflows" back button
//   clears the selection. While a re-select of the same run is loading (after
//   Cancel or Resume), the page keeps showing the run it had, so it does not
//   flash.
//
// New workflow opens the workflow builder. The launcher's Edit opens it on a
// YAML workflow. After a save the launcher re-reads its list and opens on the
// saved workflow, ready to run.

import { useEffect, useRef, useState } from 'react'
import { AlertCircle, Loader2, Plus } from 'lucide-react'
import type { RunInspection, RunSummaryRow, WorkflowWriteResult } from '../api'
import { useStore } from '../store'
import { RunList } from './workflow/RunList'
import { RunDetail } from './workflow/RunDetail'
import { BackToWorkflows } from './workflow/RunBar'
import { WorkflowLauncher } from './workflow/WorkflowLauncher'
import { WorkflowBuilder } from './workflow/WorkflowBuilder'
import { PlanApprovalForm } from './workflow/PlanApprovalForm'
import { useRunProgress } from './workflow/useRunProgress'
import { shortRunId } from './workflow/runPage'
import { useFillHeight } from './workflow/runPageParts'
import { ErrorBoundary } from './ErrorBoundary'

type BuilderTarget = { mode: 'create' } | { mode: 'edit'; name: string }

export function WorkflowsPanel() {
  const runs = useStore(s => s.workflowRuns)
  const selectedRun = useStore(s => s.selectedRun)
  const selectedRunId = useStore(s => s.selectedRunId)
  const selectedRunError = useStore(s => s.selectedRunError)
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
    // Poll the list on the same 10s cadence the sessions poll uses. The run
    // page reads its run's freshest state from this list too.
    const interval = setInterval(load, 10000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [fetchWorkflowRuns])

  // The run last shown, kept while the same run is selected again.
  const lastRun = useRef<RunInspection | null>(null)
  useEffect(() => {
    if (selectedRun) lastRun.current = selectedRun
  }, [selectedRun])
  const shownRun =
    selectedRun ?? (selectedRunId && lastRun.current?.run_id === selectedRunId && !selectedRunError ? lastRun.current : null)

  const back = () => void selectWorkflowRun(null)

  const builderModal = builder && (
    <ErrorBoundary>
      {builder.mode === 'edit' ? (
        <WorkflowBuilder mode="edit" name={builder.name} onClose={() => setBuilder(null)} onSaved={onSaved} />
      ) : (
        <WorkflowBuilder mode="create" onClose={() => setBuilder(null)} onSaved={onSaved} />
      )}
    </ErrorBoundary>
  )

  if (selectedRunId) {
    return (
      <>
        {shownRun ? (
          <ErrorBoundary>
            <RunDetail run={shownRun} allRuns={runs} />
          </ErrorBoundary>
        ) : (
          <RunPagePending
            runId={selectedRunId}
            row={runs.find(r => r.run_id === selectedRunId) ?? null}
            error={selectedRunError}
            onBack={back}
            onRetry={() => void selectWorkflowRun(selectedRunId)}
          />
        )}
        {builderModal}
      </>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
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
      </aside>

      <RunsSection runs={runs} loading={loading} error={error} onSelect={id => void selectWorkflowRun(id)} />

      {builderModal}
    </div>
  )
}

function RunsSection({
  runs,
  loading,
  error,
  onSelect,
}: {
  runs: RunSummaryRow[]
  loading: boolean
  error: string | null
  onSelect: (runId: string) => void
}) {
  const progress = useRunProgress(runs)
  return (
    <section aria-labelledby="workflow-runs-heading" className="min-w-0 rounded-xl border border-gray-800 bg-gray-900/40">
      <header className="flex items-center gap-2 border-b border-gray-800 px-4 py-3">
        <h2 id="workflow-runs-heading" className="text-sm font-semibold text-gray-200">Workflow runs</h2>
        {!loading && !error && runs.length > 0 && (
          <span className="rounded-full bg-gray-800 px-2 py-0.5 text-[11px] text-gray-400">{runs.length}</span>
        )}
        <span className="ml-auto text-xs text-gray-500">Newest first. Select a run to open it.</span>
      </header>
      <RunList runs={runs} loading={loading} error={error} onSelect={onSelect} progress={progress} />
    </section>
  )
}

/** The run page while its run loads, or when it could not be loaded. */
function RunPagePending({
  runId,
  row,
  error,
  onBack,
  onRetry,
}: {
  runId: string
  row: RunSummaryRow | null
  error: string | null
  onBack: () => void
  onRetry: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const height = useFillHeight(ref)
  return (
    <div
      ref={ref}
      style={height != null ? { height } : undefined}
      className="flex min-h-[30rem] flex-col overflow-hidden rounded-xl border border-gray-800 bg-[#0a0d12]"
      data-testid="run-page-pending"
    >
      <div className="flex shrink-0 items-center gap-3 border-b border-gray-800 bg-[#0b1017] px-5 py-3">
        <BackToWorkflows onBack={onBack} />
        <h2 className="truncate text-[17px] font-bold text-white">{row?.workflow_name ?? shortRunId(runId)}</h2>
      </div>
      <div className="flex flex-1 items-center justify-center p-6">
        {error ? (
          <div role="alert" className="flex flex-col items-center gap-3 text-sm text-red-300">
            <span className="inline-flex items-center gap-2">
              <AlertCircle size={16} aria-hidden="true" /> Could not load run {runId}: {error}
            </span>
            <button
              type="button"
              onClick={onRetry}
              className="rounded-lg border border-gray-700 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-800"
            >
              Try again
            </button>
          </div>
        ) : (
          <div role="status" className="inline-flex items-center gap-2 text-sm text-gray-400">
            <Loader2 size={16} className="motion-safe:animate-spin" aria-hidden="true" /> Loading run…
          </div>
        )}
      </div>
    </div>
  )
}
