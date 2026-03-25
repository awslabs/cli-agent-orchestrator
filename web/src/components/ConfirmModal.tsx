import { useEffect, useRef } from 'react'
import { AlertTriangle, Loader2, X } from 'lucide-react'

interface ConfirmModalProps {
  open: boolean
  title: string
  message: string
  details?: { label: string; value: string }[]
  confirmLabel?: string
  cancelLabel?: string
  variant?: 'danger' | 'warning'
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmModal({
  open,
  title,
  message,
  details,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (open) cancelRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onCancel])

  if (!open) return null

  const colors = variant === 'danger'
    ? { icon: 'text-red-400 bg-red-900/40', btn: 'bg-red-600 hover:bg-red-500 focus:ring-red-500' }
    : { icon: 'text-yellow-400 bg-yellow-900/40', btn: 'bg-yellow-600 hover:bg-yellow-500 focus:ring-yellow-500' }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onCancel} />

      {/* Modal */}
      <div className="relative bg-gray-900 border border-gray-700/50 rounded-2xl shadow-2xl w-full max-w-md mx-4 overflow-hidden animate-in fade-in zoom-in-95">
        {/* Header */}
        <div className="flex items-start gap-4 p-6 pb-4">
          <div className={`shrink-0 w-10 h-10 rounded-xl flex items-center justify-center ${colors.icon}`}>
            <AlertTriangle size={20} />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-base font-semibold text-white">{title}</h3>
            <p className="text-sm text-gray-400 mt-1">{message}</p>
          </div>
          <button onClick={onCancel} className="shrink-0 p-1 text-gray-500 hover:text-white rounded transition-colors">
            <X size={16} />
          </button>
        </div>

        {/* Details */}
        {details && details.length > 0 && (
          <div className="mx-6 mb-4 bg-gray-800/60 border border-gray-700/40 rounded-lg p-3 space-y-1.5">
            {details.map(d => (
              <div key={d.label} className="flex items-center justify-between text-xs">
                <span className="text-gray-500">{d.label}</span>
                <span className="text-gray-300 font-mono">{d.value}</span>
              </div>
            ))}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 bg-gray-800/30 border-t border-gray-700/30">
          <button
            ref={cancelRef}
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 text-sm font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-gray-500"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`px-4 py-2 text-sm font-medium text-white rounded-lg transition-all focus:outline-none focus:ring-2 disabled:opacity-60 flex items-center gap-2 ${colors.btn}`}
          >
            {loading && <Loader2 size={14} className="animate-spin" />}
            {loading ? 'Closing...' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
