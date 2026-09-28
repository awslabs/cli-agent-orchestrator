// DeleteRunButton — explicit run deletion (#504 / U8, FR-11). A destructive
// action gated behind the reused ConfirmModal, invoking the U7 DELETE endpoint.
// On success it refreshes the run list, clears the selected run, and notifies.

import { useState } from 'react'
import { Trash2 } from 'lucide-react'
import { api, type ApiError } from '../../api'
import { useStore } from '../../store'
import { ConfirmModal } from '../ConfirmModal'

interface DeleteRunButtonProps {
  runId: string
  workflowName: string
}

export function DeleteRunButton({ runId, workflowName }: DeleteRunButtonProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const showSnackbar = useStore(s => s.showSnackbar)
  const fetchWorkflowRuns = useStore(s => s.fetchWorkflowRuns)
  const clearSelectedRun = useStore(s => s.clearSelectedRun)

  const handleConfirm = async () => {
    setLoading(true)
    try {
      await api.deleteWorkflowRun(runId)
      showSnackbar({ type: 'success', message: `Deleted run ${runId.slice(0, 8)}` })
      clearSelectedRun()
      await fetchWorkflowRuns()
      setOpen(false)
    } catch (e) {
      const err = e as ApiError
      showSnackbar({ type: 'error', message: err?.detail || err?.message || 'Failed to delete run' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Delete this run"
        className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-red-300 bg-red-950/40 hover:bg-red-900/50 rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-red-500"
      >
        <Trash2 size={14} aria-hidden="true" />
        Delete run
      </button>
      <ConfirmModal
        open={open}
        title="Delete run"
        message="This permanently deletes the run and all its retained diagnostic data. This cannot be undone."
        details={[
          { label: 'Run', value: runId },
          { label: 'Workflow', value: workflowName },
        ]}
        confirmLabel="Delete"
        variant="danger"
        loading={loading}
        onConfirm={handleConfirm}
        onCancel={() => setOpen(false)}
      />
    </>
  )
}
