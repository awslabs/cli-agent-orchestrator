// RunArtifacts — "View report" for a finished run. Lists the run inputs that name
// a report file (GET /workflows/runs/{id}/artifacts) and shows one on demand
// (GET .../artifacts/{input}). Replaces `cat "$REPORT_PATH"` in a shell. The
// server decides what is readable (a .md directly in its temp dir); this only
// renders the text, as plain preformatted text — never as HTML.

import { useEffect, useState } from 'react'
import { FileText, Loader2, X } from 'lucide-react'
import { api, WorkflowArtifact, WorkflowArtifactRef } from '../../api'

interface RunArtifactsProps {
  runId: string
  /** Only look once the run is terminal: a running run is still writing. */
  terminal: boolean
}

export function RunArtifacts({ runId, terminal }: RunArtifactsProps) {
  const [refs, setRefs] = useState<WorkflowArtifactRef[]>([])
  const [open, setOpen] = useState<WorkflowArtifact | null>(null)
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setRefs([])
    setOpen(null)
    setError(null)
    if (!terminal) return
    let cancelled = false
    api.listWorkflowArtifacts(runId)
      .then(r => { if (!cancelled) setRefs(r.artifacts) })
      .catch(() => { /* no artifacts is the common case; nothing to show */ })
    return () => { cancelled = true }
  }, [runId, terminal])

  if (refs.length === 0) return null

  const view = async (input: string) => {
    setLoading(input)
    setError(null)
    try {
      setOpen(await api.getWorkflowArtifact(runId, input))
    } catch (e: any) {
      setError(e?.detail || e?.message || 'Could not read the report')
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="rounded-lg border border-gray-700/50 bg-gray-900/40 p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {refs.map(r => (
          <button
            key={r.input}
            type="button"
            disabled={!r.available || loading !== null}
            onClick={() => view(r.input)}
            title={r.available ? r.path : `${r.path} (not available)`}
            className="inline-flex items-center gap-1.5 rounded-lg border border-sky-800/60 px-2.5 py-1 text-xs text-sky-200 hover:bg-sky-900/30 disabled:opacity-40"
          >
            {loading === r.input
              ? <Loader2 size={13} className="animate-spin" aria-hidden="true" />
              : <FileText size={13} aria-hidden="true" />}
            View {r.input}
          </button>
        ))}
      </div>
      {error && <p className="text-xs text-red-400" role="alert">{error}</p>}
      {open && (
        <div className="rounded-lg border border-gray-800 bg-gray-950">
          <div className="flex items-center justify-between border-b border-gray-800 px-3 py-1.5">
            <span className="truncate font-mono text-xs text-gray-400">{open.path}</span>
            <button
              type="button"
              onClick={() => setOpen(null)}
              aria-label="Close report"
              className="p-1 text-gray-500 hover:text-gray-200"
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words p-3 text-xs text-gray-200">
            {open.content}
          </pre>
        </div>
      )}
    </div>
  )
}
