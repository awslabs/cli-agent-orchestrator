import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, Check } from 'lucide-react'

export interface SelectOption {
  value: string
  label: string
  disabled?: boolean
  group?: string
  sublabel?: string
}

interface CustomSelectProps {
  value: string
  onChange: (value: string) => void
  options: SelectOption[]
  placeholder?: string
  className?: string
  /** Accessible name for the trigger button (schema-driven forms label by field name). */
  ariaLabel?: string
  /** Validation-error styling: thick red boundary on the trigger. Additive; default unchanged. */
  invalid?: boolean
}

/** Menu height cap; also used to decide when to flip the menu upward. */
const MENU_MAX_H = 256

export function CustomSelect({ value, onChange, options, placeholder = 'Select...', className = '', ariaLabel, invalid = false }: CustomSelectProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  // Fixed-position coordinates for the portaled menu, derived from the trigger.
  const [pos, setPos] = useState<{ left: number; width: number; top?: number; bottom?: number }>({ left: 0, width: 0, top: 0 })

  // The menu is rendered in a portal at document.body with position:fixed so
  // it can never be clipped by an ancestor scroll container — the modal
  // bodies (Create profile, Create flow) are overflow-y-auto, and an
  // in-flow absolute menu gets cut off at the container edge. When there is
  // not enough room below the trigger, the menu flips upward.
  useLayoutEffect(() => {
    if (!open || !ref.current) return
    const rect = ref.current.getBoundingClientRect()
    const spaceBelow = window.innerHeight - rect.bottom
    const flipUp = spaceBelow < MENU_MAX_H + 8 && rect.top > spaceBelow
    setPos(flipUp
      ? { left: rect.left, width: rect.width, bottom: window.innerHeight - rect.top + 4 }
      : { left: rect.left, width: rect.width, top: rect.bottom + 4 })
  }, [open])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      // The menu lives in a portal, so it is NOT inside `ref` — check both.
      if (ref.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    // A fixed-position menu does not follow its trigger when an ancestor
    // scrolls; close instead of drifting. Scrolls inside the menu itself
    // (its own overflow list) are fine.
    const onScroll = (e: Event) => {
      if (menuRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onScroll)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [open])

  const selected = options.find(o => o.value === value)

  // Group options
  const groups: { label: string | null; items: SelectOption[] }[] = []
  const seen = new Set<string>()
  for (const opt of options) {
    const g = opt.group || null
    const key = g || '__ungrouped__'
    if (!seen.has(key)) {
      seen.add(key)
      groups.push({ label: g, items: options.filter(o => (o.group || null) === g) })
    }
  }

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={`w-full flex items-center justify-between bg-gray-900 text-sm rounded-lg px-3 py-2.5 focus:outline-none transition-colors ${
          invalid
            ? 'border-2 !border-red-500 ring-2 ring-red-500/30'
            : 'border border-gray-700 focus:border-emerald-500 hover:border-gray-600'
        }`}
      >
        <span className={selected ? 'text-gray-200' : 'text-gray-500'}>
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown size={14} className={`text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && createPortal(
        <div
          ref={menuRef}
          role="listbox"
          data-testid="custom-select-menu"
          style={{ position: 'fixed', left: pos.left, width: pos.width, top: pos.top, bottom: pos.bottom, maxHeight: MENU_MAX_H }}
          className="z-[80] bg-gray-900 border border-gray-700 rounded-lg shadow-xl shadow-black/30 overflow-y-auto"
        >
          {groups.map((group, gi) => (
            <div key={gi}>
              {group.label && (
                <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-gray-500 font-semibold bg-gray-800/50 sticky top-0">
                  {group.label}
                </div>
              )}
              {group.items.map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  disabled={opt.disabled}
                  onClick={() => {
                    if (!opt.disabled) {
                      onChange(opt.value)
                      setOpen(false)
                    }
                  }}
                  className={`w-full text-left px-3 py-2 flex items-center justify-between transition-colors ${
                    opt.disabled
                      ? 'text-gray-600 cursor-not-allowed'
                      : value === opt.value
                        ? 'bg-emerald-900/30 text-emerald-300'
                        : 'text-gray-300 hover:bg-gray-800'
                  }`}
                >
                  <div className="min-w-0">
                    <span className="text-sm block truncate">{opt.label}</span>
                    {opt.sublabel && (
                      <span className="text-[11px] text-gray-500 block truncate">{opt.sublabel}</span>
                    )}
                  </div>
                  {value === opt.value && <Check size={14} className="text-emerald-400 shrink-0 ml-2" />}
                </button>
              ))}
            </div>
          ))}
          {options.length === 0 && (
            <div className="px-3 py-4 text-sm text-gray-500 text-center">No options available</div>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}
