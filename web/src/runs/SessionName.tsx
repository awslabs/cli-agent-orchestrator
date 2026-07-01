import { useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'
import { Pencil, Check, X } from 'lucide-react'

/**
 * A session's display name with inline rename. The friendly label is stored
 * server-side as a pure display alias (never the real tmux session name), so
 * renaming disturbs nothing that references the session. Shows the label when
 * set, the raw `cao-…` name otherwise; the pencil reveals on hover.
 */
export function SessionName({ name, label, className }: {
  name: string
  label?: string | null
  className?: string
}) {
  const { fetchSessions, showSnackbar } = useStore()
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(label || '')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    try {
      await api.setSessionLabel(name, value.trim())
      await fetchSessions()
      setEditing(false)
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Could not rename session' })
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <span className="inline-flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <input
          autoFocus
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') save()
            if (e.key === 'Escape') setEditing(false)
          }}
          placeholder={name}
          className="bg-[#0f0f14] border border-gray-600 rounded px-2 py-0.5 text-sm text-gray-200 outline-none focus:border-blue-500 w-48"
          data-testid={`rename-input-${name}`}
        />
        <button onClick={save} disabled={saving} className="text-emerald-400 hover:text-emerald-300 shrink-0" title="Save name">
          <Check size={14} />
        </button>
        <button onClick={() => setEditing(false)} className="text-gray-500 hover:text-gray-300 shrink-0" title="Cancel">
          <X size={14} />
        </button>
      </span>
    )
  }

  return (
    <span className={`inline-flex items-center gap-1.5 group min-w-0 ${className || ''}`}>
      <span className="truncate" title={label ? `${label}  (${name})` : name}>{label || name}</span>
      <button
        onClick={e => { e.stopPropagation(); setValue(label || ''); setEditing(true) }}
        className="text-gray-600 hover:text-gray-300 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
        title="Rename session"
        data-testid={`rename-${name}`}
      >
        <Pencil size={12} />
      </button>
    </span>
  )
}
