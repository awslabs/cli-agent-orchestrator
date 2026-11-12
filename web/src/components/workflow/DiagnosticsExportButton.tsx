// DiagnosticsExportButton — fetch the U6 diagnostic bundle for a run and
// download it as JSON (#504 / U8, FR-9). Redaction and size limits are enforced
// SERVER-SIDE; this button is a plain read + client-side file save. Surfaces
// errors to the caller via a snackbar; never swallows a failure.
//
// useDiagnosticsExport is the same export without the button, for the run
// page's More menu. The hook lives in the menu's owner, so the export keeps
// going and reports its result after the menu closes.

import { useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { api, type ApiError } from '../../api'
import { useStore } from '../../store'

interface DiagnosticsExportButtonProps {
  runId: string
}

/** Export a run's diagnostics bundle: `exporting` is true while the read is in flight. */
export function useDiagnosticsExport(runId: string): { exporting: boolean; exportDiagnostics: () => Promise<void> } {
  const [exporting, setExporting] = useState(false)
  const showSnackbar = useStore(s => s.showSnackbar)

  const exportDiagnostics = async () => {
    setExporting(true)
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
      setExporting(false)
    }
  }

  return { exporting, exportDiagnostics }
}

export function DiagnosticsExportButton({ runId }: DiagnosticsExportButtonProps) {
  const { exporting, exportDiagnostics } = useDiagnosticsExport(runId)

  return (
    <button
      type="button"
      onClick={exportDiagnostics}
      disabled={exporting}
      aria-label="Export diagnostics bundle"
      className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-200 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors focus:outline-hidden focus:ring-2 focus:ring-emerald-500 disabled:opacity-60"
    >
      {exporting ? (
        <Loader2 size={14} className="animate-spin" aria-hidden="true" />
      ) : (
        <Download size={14} aria-hidden="true" />
      )}
      Export diagnostics
    </button>
  )
}
