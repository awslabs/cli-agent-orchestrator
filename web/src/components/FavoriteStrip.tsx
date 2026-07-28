import { useRef, useState } from 'react'
import type { MacroRecord } from '../api'
import { renderPreview } from '../lib/macroNotation'

interface FavoriteStripProps {
  /** Server-ordered favorites (§5.4 order); grouping derives from it. */
  favorites: MacroRecord[]
  disabled: boolean
  onSend: (macro: MacroRecord) => void
  /** §4.1 guard-absent notice when the Compact built-in lacks command_controls. */
  guardNotice?: string | null
}

function safePreview(macro: MacroRecord): string {
  try {
    return renderPreview(macro.events)
  } catch {
    return '(no preview)'
  }
}

function scopeLabel(macro: MacroRecord): string {
  if (macro.scope.kind === 'global') return 'Global'
  if (macro.scope.kind === 'provider') return macro.scope.provider ?? 'provider'
  return 'This agent'
}

/**
 * The §7.2 favorite strip: horizontally scrollable, never wraps, grouping
 * labels carry scope (buttons do not repeat it), order is the server order.
 * Tooltip (desktop) / long-press (touch) shows the normalized preview
 * before send. One tap = one v3 request (D2).
 */
export function FavoriteStrip({ favorites, disabled, onSend, guardNotice }: FavoriteStripProps) {
  const [previewFor, setPreviewFor] = useState<string | null>(null)
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  if (favorites.length === 0) return null

  // Group consecutive same-scope-label entries, preserving server order.
  const groups: Array<{ label: string; macros: MacroRecord[] }> = []
  for (const macro of favorites) {
    const label = scopeLabel(macro)
    const last = groups[groups.length - 1]
    if (last && last.label === label) {
      last.macros.push(macro)
    } else {
      groups.push({ label, macros: [macro] })
    }
  }

  const startLongPress = (macroId: string) => {
    cancelLongPress()
    longPressTimer.current = setTimeout(() => setPreviewFor(macroId), 500)
  }
  const cancelLongPress = () => {
    if (longPressTimer.current !== null) {
      clearTimeout(longPressTimer.current)
      longPressTimer.current = null
    }
  }

  return (
    <div className="space-y-1" data-testid="favorite-strip">
      <div
        className="flex items-center gap-2 overflow-x-auto whitespace-nowrap py-1"
        style={{ maxHeight: 48 }}
      >
        {groups.map(group => (
          <div key={group.label} className="flex items-center gap-2 shrink-0">
            <span className="text-[10px] uppercase tracking-wider text-gray-400">
              {group.label}:
            </span>
            {group.macros.map(macro => (
              <span key={macro.id} className="relative shrink-0">
                <button
                  type="button"
                  disabled={disabled}
                  title={safePreview(macro)}
                  onClick={() => onSend(macro)}
                  onTouchStart={() => startLongPress(macro.id)}
                  onTouchEnd={() => {
                    cancelLongPress()
                    setPreviewFor(null)
                  }}
                  onTouchCancel={cancelLongPress}
                  className="min-h-[44px] min-w-[44px] rounded border border-gray-700 bg-gray-800 px-3 text-xs text-gray-200 transition-colors hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  {macro.name}
                </button>
                {previewFor === macro.id && (
                  <span className="absolute left-0 top-full z-10 mt-1 max-w-xs truncate rounded border border-gray-600 bg-gray-900 px-2 py-1 font-mono text-[10px] text-gray-300 shadow-lg">
                    {safePreview(macro)}
                  </span>
                )}
              </span>
            ))}
          </div>
        ))}
      </div>
      {guardNotice && (
        <div className="text-[10px] text-amber-300/90">{guardNotice}</div>
      )}
    </div>
  )
}
