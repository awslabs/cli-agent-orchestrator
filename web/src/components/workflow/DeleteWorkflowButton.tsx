// DeleteWorkflowButton: delete a stored workflow (DELETE /workflows/{name}).
// The confirm button stays disabled until the exact workflow name is typed.

import { useState } from 'react'
import { Trash2 } from 'lucide-react'
import { api, type ApiError } from '../../api'
import { useStore } from '../../store'
import { ConfirmModal } from '../ConfirmModal'

interface DeleteWorkflowButtonProps {
  name: string
  /** Called after the delete succeeds, so the caller can refresh its list. */
  onDeleted?: (name: string) => void
}

export function DeleteWorkflowButton({ name, onDeleted }: DeleteWorkflowButtonProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const showSnackbar = useStore(s => s.showSnackbar)

  const handleConfirm = async () => {
    if (loading) return
    setLoading(true)
    try {
      await api.deleteWorkflow(name)
      showSnackbar({ type: 'success', message: `Deleted workflow ${name}` })
      setOpen(false)
      onDeleted?.(name)
    } catch (e) {
      const err = e as ApiError
      showSnackbar({ type: 'error', message: err?.detail || err?.message || 'Failed to delete the workflow' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={`Delete workflow ${name}`}
        className="inline-flex items-center gap-1 rounded-lg border border-red-900/60 px-2 py-1 text-xs text-red-300 hover:bg-red-950/40"
      >
        <Trash2 size={13} aria-hidden="true" /> Delete
      </button>
      <ConfirmModal
        open={open}
        title="Delete workflow"
        message="This deletes the workflow file and removes it from the list. This cannot be undone."
        details={[{ label: 'Workflow', value: name }]}
        confirmLabel="Delete"
        variant="danger"
        loading={loading}
        confirmationText={name}
        onConfirm={handleConfirm}
        onCancel={() => setOpen(false)}
      />
    </>
  )
}
