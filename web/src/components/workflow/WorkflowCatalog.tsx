// WorkflowCatalog — the saved-workflow list (issue #789, PR A). Sits beside
// the run list in the Workflows tab: pick a workflow to open the generated
// run form, or delete its spec (cao:admin) behind a type-to-confirm modal.

import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, WorkflowIndexRow } from '../../api'
import { ConfirmModal } from '../ConfirmModal'
import { AlertTriangle, Loader2, Trash2 } from 'lucide-react'

interface WorkflowCatalogProps {
  selectedName: string | null
  onSelect: (name: string) => void
  /** Called after a successful delete so the panel can drop a stale selection. */
  onDeleted: (name: string) => void
}

export function WorkflowCatalog({ selectedName, onSelect, onDeleted }: WorkflowCatalogProps) {
  const [rows, setRows] = useState<WorkflowIndexRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<WorkflowIndexRow | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const list = await api.listWorkflows()
      setRows(list)
      setError(null)
    } catch (e) {
      const err = e as ApiError
      setError(err.detail ?? err.message)
    }
  }, [])

  useEffect(() => {
    // Same poll cadence the run list uses, so a workflow saved from the CLI
    // appears without a manual refresh.
    let cancelled = false
    setLoading(true)
    load().finally(() => { if (!cancelled) setLoading(false) })
    const interval = setInterval(load, 10000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [load])

  async function confirmDelete() {
    if (!pendingDelete) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteWorkflow(pendingDelete.name)
      const name = pendingDelete.name
      setPendingDelete(null)
      onDeleted(name)
      await load()
    } catch (e) {
      const err = e as ApiError
      // The delete route is cao:admin only; a scope refusal must read as a
      // permission story, not a generic failure.
      setDeleteError(
        err.status === 403
          ? `Deleting a workflow requires the cao:admin scope. ${err.detail ?? ''}`
          : err.detail ?? err.message,
      )
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div>
      <h2 className="text-sm font-semibold text-gray-300 mb-2">Workflows</h2>
      {loading ? (
        <div className="text-sm text-gray-500 flex items-center gap-2 py-2">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading…
        </div>
      ) : error ? (
        <div className="text-xs text-red-400 flex items-start gap-1.5 py-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /> {error}
        </div>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-500 py-1">
          No saved workflows. Author one with the CLI or an agent, then run it from here.
        </p>
      ) : (
        <ul className="space-y-1">
          {rows.map(row => (
            <li key={row.name}>
              <div
                className={`group flex items-start gap-2 rounded-lg px-2 py-1.5 cursor-pointer border ${
                  selectedName === row.name
                    ? 'border-blue-700/60 bg-blue-900/20'
                    : 'border-transparent hover:bg-gray-800/60'
                }`}
                onClick={() => onSelect(row.name)}
                role="button"
                aria-label={`Run workflow ${row.name}`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm text-gray-200 truncate">{row.name}</span>
                    <span className="text-[10px] px-1 py-0.5 rounded border border-gray-700 bg-gray-800 text-gray-400">
                      {row.step_count == null ? 'script' : row.mode}
                    </span>
                  </div>
                  {row.description ? (
                    <p className="text-xs text-gray-500 truncate">{row.description}</p>
                  ) : null}
                  <p className="text-[10px] text-gray-600">
                    {row.step_count == null ? 'dynamic steps' : `${row.step_count} step${row.step_count === 1 ? '' : 's'}`}
                  </p>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); setDeleteError(null); setPendingDelete(row) }}
                  className="p-1 rounded text-gray-600 hover:text-red-400 hover:bg-gray-800 opacity-0 group-hover:opacity-100"
                  aria-label={`Delete workflow ${row.name}`}
                  title="Delete workflow"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {deleteError ? (
        <div className="text-xs text-amber-400 flex items-start gap-1.5 mt-2" role="alert">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /> {deleteError}
        </div>
      ) : null}

      <ConfirmModal
        open={pendingDelete != null}
        title="Delete workflow"
        message="This deletes the workflow's spec file from disk and its index row. Journaled runs are kept."
        details={pendingDelete ? [
          { label: 'Name', value: pendingDelete.name },
          { label: 'File', value: pendingDelete.source_path },
        ] : undefined}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        confirmationText={pendingDelete?.name}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}
