// PromptPresets — a small, generic prompt-preset picker for the agent message
// box. Lets a user save reusable prompts and insert one with a click instead of
// retyping or pasting. Deliberately content-agnostic: user presets live in
// localStorage, and a deployment may ship read-only ones in ui-presets.json
// (api.getUiPresets — e.g. a team's standard prompts), so no deployment-specific
// text ships in CAO. Inserting fills the message input (it does not auto-send)
// so the user can review before sending.

import { useEffect, useState } from 'react'
import { BookMarked, Plus, X } from 'lucide-react'
import { api } from '../api'

export interface PromptPreset {
  label: string
  text: string
}

export const PROMPT_PRESETS_KEY = 'cao.promptPresets'

export function loadPromptPresets(): PromptPreset[] {
  try {
    const raw = localStorage.getItem(PROMPT_PRESETS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (p): p is PromptPreset =>
        !!p && typeof p.label === 'string' && typeof p.text === 'string',
    )
  } catch {
    // Malformed JSON or a storage-access denial (private mode) — no presets.
    return []
  }
}

export function savePromptPresets(presets: PromptPreset[]): void {
  try {
    localStorage.setItem(PROMPT_PRESETS_KEY, JSON.stringify(presets))
  } catch {
    /* quota exceeded or storage denied — presets are a convenience, so swallow */
  }
}

interface PromptPresetsProps {
  /** The current message-box text, offered for "save as preset". */
  current: string
  /** Fill the message box with the chosen preset's text (does not send). */
  onInsert: (text: string) => void
}

export function PromptPresets({ current, onInsert }: PromptPresetsProps) {
  const [open, setOpen] = useState(false)
  const [presets, setPresets] = useState<PromptPreset[]>([])
  const [sitePresets, setSitePresets] = useState<PromptPreset[]>([])

  // Re-read on open so presets seeded by another tab/script show up.
  useEffect(() => {
    if (!open) return
    setPresets(loadPromptPresets())
    let cancelled = false
    api.getUiPresets().then(p => { if (!cancelled) setSitePresets(p.prompts ?? []) })
    return () => { cancelled = true }
  }, [open])

  const saveCurrent = () => {
    const text = current.trim()
    if (!text) return
    const label = window.prompt('Preset name', text.slice(0, 40))
    if (!label) return
    const next = [...loadPromptPresets().filter(p => p.label !== label), { label, text }]
    savePromptPresets(next)
    setPresets(next)
  }

  const remove = (label: string) => {
    const next = loadPromptPresets().filter(p => p.label !== label)
    savePromptPresets(next)
    setPresets(next)
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-label="Prompt presets"
        className="flex items-center gap-1 px-2 py-1.5 text-xs text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg"
      >
        <BookMarked size={13} aria-hidden="true" /> Presets
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-72 rounded-lg border border-gray-700 bg-gray-900 p-2 shadow-lg">
          <button
            type="button"
            onClick={saveCurrent}
            disabled={!current.trim()}
            className="flex w-full items-center gap-1 px-2 py-1 text-xs text-emerald-300 hover:bg-gray-800 rounded disabled:opacity-40"
          >
            <Plus size={13} aria-hidden="true" /> Save current as preset
          </button>
          <div className="mt-1 max-h-56 overflow-auto">
            {sitePresets.map(p => (
              <button
                key={`site:${p.label}`}
                type="button"
                onClick={() => { onInsert(p.text); setOpen(false) }}
                className="block w-full truncate text-left px-2 py-1 text-xs text-sky-200 hover:bg-gray-800 rounded"
                title={p.text}
              >
                {p.label}
              </button>
            ))}
            {sitePresets.length > 0 && presets.length > 0 && <hr className="my-1 border-gray-800" />}
            {presets.length === 0 ? (
              sitePresets.length > 0 ? null :
              <p className="px-2 py-2 text-xs text-gray-500">No presets yet.</p>
            ) : (
              presets.map(p => (
                <div key={p.label} className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => { onInsert(p.text); setOpen(false) }}
                    className="flex-1 truncate text-left px-2 py-1 text-xs text-gray-200 hover:bg-gray-800 rounded"
                    title={p.text}
                  >
                    {p.label}
                  </button>
                  <button
                    type="button"
                    onClick={() => remove(p.label)}
                    aria-label={`Delete preset ${p.label}`}
                    className="p-1 text-gray-600 hover:text-red-400"
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  )
}
