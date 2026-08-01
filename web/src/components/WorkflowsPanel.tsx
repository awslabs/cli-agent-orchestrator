// WorkflowsPanel — the top-level Workflows surface (#504 / U8, FR-10), a
// sibling to Agents/Inbox/Memory. A list/detail layout: RunList (left) drives
// RunDetail (right) via the additive workflowRuns store slice. Purely
// client-side over the already-built durable-journal read APIs.

import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { RunList } from './workflow/RunList'
import { RunDetail } from './workflow/RunDetail'
import { ErrorBoundary } from './ErrorBoundary'

export function WorkflowsPanel() {
  const runs = useStore(s => s.workflowRuns)
  const selectedRun = useStore(s => s.selectedRun)
  const fetchWorkflowRuns = useStore(s => s.fetchWorkflowRuns)
  const selectWorkflowRun = useStore(s => s.selectWorkflowRun)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
      <aside className="rounded-xl border border-gray-800 bg-gray-900/40 p-3 h-fit">
        <h2 className="text-sm font-semibold text-gray-300 mb-2">Workflow Runs</h2>
        <RunList
          runs={runs}
          selectedRunId={selectedRun?.run_id ?? null}
          loading={loading}
          error={error}
          onSelect={selectWorkflowRun}
        />
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
    </div>
  )
}
