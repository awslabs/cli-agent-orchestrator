// RunResultPanel — a finished run's retained result (GET /workflows/runs/{id}/result,
// the `cao workflow result` equivalent).
//
// Fetched only once the run is terminal: before that the result is still moving
// and the step table already shows live state. The journal-built result has no
// run-level output, so what a run emitted is read from its steps' outputs; any
// string field that names a report (a `report*` key, or a value ending in .md) is
// lifted out and shown first, because that is the deliverable a user looks for.

import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, FileText } from 'lucide-react'
import { api, WorkflowRunResult } from '../../api'

/** Report-like string fields across all step outputs, de-duplicated, in step order. */
export function findReportPaths(result: WorkflowRunResult): string[] {
  const found: string[] = []
  const visit = (value: unknown, key: string) => {
    if (typeof value === 'string') {
      if ((/report/i.test(key) || /\.md$/i.test(value)) && /^\/|\.md$/i.test(value) && !found.includes(value)) {
        found.push(value)
      }
    } else if (value && typeof value === 'object') {
      for (const [k, v] of Object.entries(value as Record<string, unknown>)) visit(v, k)
    }
  }
  for (const step of result.steps ?? []) visit(step.output, '')
  return found
}

export function RunResultPanel({ runId, terminal, state }: { runId: string; terminal: boolean; state: string }) {
  const [result, setResult] = useState<WorkflowRunResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showJson, setShowJson] = useState(false)

  useEffect(() => {
    setResult(null)
    setError(null)
    if (!terminal) return
    let cancelled = false
    api.getWorkflowRunResult(runId)
      .then(r => { if (!cancelled) setResult(r) })
      .catch((e: any) => { if (!cancelled) setError(e?.detail || e?.message || 'Failed to load the run result') })
    return () => { cancelled = true }
    // `state` re-reads the result after a resume finishes the run again.
  }, [runId, terminal, state])

  if (!terminal) return null

  const reports = result ? findReportPaths(result) : []
  const outputs = result ? (result.steps ?? []).filter(s => s.output != null) : []

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3" aria-label="Run result">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">Result</h3>
      {error ? (
        <p className="text-xs text-red-400" role="alert">{error}</p>
      ) : !result ? (
        <p className="text-xs text-gray-500">Loading result…</p>
      ) : (
        <div className="space-y-2 text-xs">
          <p className="text-gray-300">
            Final state: <span className="font-mono text-white">{result.state}</span>
            {result.kind ? <span className="text-gray-500"> ({result.kind})</span> : null}
            {result.finished_at ? <span className="text-gray-500"> · finished {result.finished_at}</span> : null}
          </p>
          {reports.map(path => (
            <p key={path} className="flex items-center gap-1.5 text-emerald-300">
              <FileText size={13} aria-hidden="true" /> Report: <code className="break-all font-mono">{path}</code>
            </p>
          ))}
          {(result.warnings ?? []).map(w => <p key={w} className="text-amber-300">{w}</p>)}
          {outputs.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setShowJson(v => !v)}
                aria-expanded={showJson}
                className="inline-flex items-center gap-1 text-gray-400 hover:text-gray-200"
              >
                {showJson ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
                Step outputs ({outputs.length})
              </button>
              {showJson && (
                <pre className="mt-1 max-h-80 overflow-auto rounded bg-gray-950 p-2 font-mono text-[11px] text-gray-300">
                  {JSON.stringify(Object.fromEntries(outputs.map(s => [s.id, s.output])), null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
