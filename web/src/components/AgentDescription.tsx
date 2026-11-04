import { useRef, useState } from 'react'
import { Pencil } from 'lucide-react'
import { api, ApiError } from '../api'

const DESCRIPTION_MAX_LENGTH = 64

export function normalizeDescription(value: string): string | null {
  // Preserve line boundaries before removing C0/C1 controls, including tabs.
  // Unicode White_Space matches Python str.strip() after the control filter.
  return value.split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/u).join(' ')
    .replace(/[\u0000-\u001f\u007f-\u009f]/gu, '')
    .replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '') || null
}

export function AgentDescription({ id, description, onSaved, className = '' }: {
  id: string
  description: string | null
  onSaved: (description: string | null) => void
  className?: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const sending = useRef(false)
  const cancel = () => { if (!sending.current) { setEditing(false); setError('') } }
  const save = async () => {
    if (sending.current) return
    const normalized = normalizeDescription(draft)
    if (Array.from(normalized || '').length > DESCRIPTION_MAX_LENGTH) {
      setError(`Description must be at most ${DESCRIPTION_MAX_LENGTH} characters`)
      return
    }
    sending.current = true
    setPending(true)
    setError('')
    try {
      const terminal = await api.updateTerminalDescription(id, normalized)
      onSaved(terminal.description)
      setEditing(false)
    } catch (err) {
      setError((err as ApiError).detail || (err instanceof Error ? err.message : 'Failed to save description'))
    } finally {
      sending.current = false
      setPending(false)
    }
  }
  return <div className={`min-w-0 text-xs text-gray-400 ${className}`}>
    {editing ? <>
      <div className="flex flex-wrap items-center gap-2 min-w-0">
        <input aria-label="Agent description" autoFocus value={draft} disabled={pending}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') { event.preventDefault(); void save() }
            if (event.key === 'Escape') { event.preventDefault(); cancel() }
          }}
          className="min-w-0 flex-1 bg-gray-900 border border-gray-600 rounded-sm px-2 py-1 focus:outline-hidden focus:border-emerald-500" />
        <button disabled={pending} onClick={() => void save()} className="text-emerald-400 disabled:opacity-40">{pending ? 'Saving...' : 'Save'}</button>
        <button disabled={pending} onClick={cancel}>Cancel</button>
      </div>
      {error && <p role="alert" className="text-red-400 mt-1">{error}</p>}
    </> : <div className="flex items-center gap-2 min-w-0">
      {description && <span className="truncate min-w-0" title={description}>{description}</span>}
      <button aria-label="Edit agent description" title="Edit agent description"
        className="shrink-0 p-1 text-gray-500 hover:text-gray-200 focus-visible:outline-solid focus-visible:outline-emerald-500"
        onClick={() => { setDraft(description || ''); setError(''); setEditing(true) }}><Pencil size={10} /></button>
    </div>}
  </div>
}
