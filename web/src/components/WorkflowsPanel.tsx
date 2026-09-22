// WorkflowsPanel — the top-level Workflows surface (#504 / U8, FR-10), a
// sibling to Agents/Inbox/Memory. A list/detail layout: the saved-workflow
// catalog (#789, PR A) and RunList (left) drive the right pane, which shows
// either the generated run form or RunDetail via the additive workflowRuns
// store slice. Purely client-side over the already-built durable-journal read
// APIs plus the async submit spine.

import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { RunList } from './workflow/RunList'
import { RunDetail } from './workflow/RunDetail'
import { WorkflowCatalog } from './workflow/WorkflowCatalog'
import { RunWorkflowForm } from './workflow/RunWorkflowForm'
import { ErrorBoundary } from './ErrorBoundary'

export function WorkflowsPanel() {
  const runs = useStore(s => s.workflowRuns)
  const selectedRun = useStore(s => s.selectedRun)
  const fetchWorkflowRuns = useStore(s => s.fetchWorkflowRuns)
  const selectWorkflowRun = useStore(s => s.selectWorkflowRun)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // The catalog's selection opens the run form in the right pane; it is
  // mutually exclusive with a selected run (picking one clears the other), so
  // the pane always has a single owner.
  const [formWorkflow, setFormWorkflow] = useState<string | null>(null)

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
      {/* Catalog + run list */}
      <aside className="space-y-4 h-fit">
        <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3">
          <WorkflowCatalog
            selectedName={formWorkflow}
            onSelect={name => setFormWorkflow(name)}
            onDeleted={name => {
              if (formWorkflow === name) setFormWorkflow(null)
            }}
          />
        </div>
        <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3">
          <h2 className="text-sm font-semibold text-gray-300 mb-2">Workflow Runs</h2>
          <RunList
            runs={runs}
            selectedRunId={selectedRun?.run_id ?? null}
            loading={loading}
            error={error}
            onSelect={runId => {
              setFormWorkflow(null)
              void selectWorkflowRun(runId)
            }}
          />
        </div>
      </aside>

      {/* Detail: the run form when a workflow is picked, else the run detail */}
      <section className="min-w-0">
        {formWorkflow ? (
          <ErrorBoundary>
            <RunWorkflowForm
              name={formWorkflow}
              onSubmitted={runId => {
                // 202 means the run is durably journaled; land on the existing
                // RunDetail timeline and let the poll pick up the new row.
                setFormWorkflow(null)
                void selectWorkflowRun(runId)
                void fetchWorkflowRuns()
              }}
              onClose={() => setFormWorkflow(null)}
            />
          </ErrorBoundary>
        ) : selectedRun ? (
          <ErrorBoundary>
            <RunDetail run={selectedRun} allRuns={runs} />
          </ErrorBoundary>
        ) : (
          <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-12 text-center text-sm text-gray-500">
            Pick a workflow to run it, or select a run to inspect its timeline, playback, and diagnostics.
          </div>
        )}
      </section>
    </div>
  )
}
