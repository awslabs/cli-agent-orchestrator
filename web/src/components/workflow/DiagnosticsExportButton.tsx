// DiagnosticsExportButton — fetch the U6 diagnostic bundle for a run and
// download it as JSON (#504 / U8, FR-9). Redaction and size limits are enforced
// SERVER-SIDE; this button is a plain read + client-side file save. Surfaces
// errors to the caller via a snackbar; never swallows a failure.

import { useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { api, type ApiError } from '../../api'
import { useStore } from '../../store'

interface DiagnosticsExportButtonProps {
  runId: string
}

export function DiagnosticsExportButton({ runId }: DiagnosticsExportButtonProps) {
  const [loading, setLoading] = useState(false)
  const showSnackbar = useStore(s => s.showSnackbar)

  const handleExport = async () => {
    setLoading(true)
    try {
      const bundle = await api.getWorkflowRunDiagnostics(runId)
      // Client-side save of the server-redacted bundle. Guarded for jsdom /
      // non-browser environments where URL.createObjectURL is unavailable.
      const json = JSON.stringify(bundle, null, 2)
      if (typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function') {
        const blob = new Blob([json], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `diagnostics-${runId}.json`
        document.body.appendChild(a)
        a.click()
        a.remove()
        URL.revokeObjectURL(url)
      }
      showSnackbar({ type: 'success', message: 'Diagnostics exported' })
    } catch (e) {
      const err = e as ApiError
      showSnackbar({ type: 'error', message: err?.detail || err?.message || 'Export failed' })
    } finally {
      setLoading(false)
    }
  }

  return (
    <button
      type="button"
      onClick={handleExport}
      disabled={loading}
      aria-label="Export diagnostics bundle"
      className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-200 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:opacity-60"
    >
      {loading ? (
        <Loader2 size={14} className="animate-spin" aria-hidden="true" />
      ) : (
        <Download size={14} aria-hidden="true" />
      )}
      Export diagnostics
    </button>
  )
}
