import { useState, useRef, useEffect, useLayoutEffect, useId } from 'react'
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
  searchable?: boolean
  searchPlaceholder?: string
  noResultsMessage?: string
}

/** Menu height cap; also used to decide when to flip the menu upward. */
const MENU_MAX_H = 256

export function CustomSelect({ value, onChange, options, placeholder = 'Select...', className = '', ariaLabel, invalid = false, searchable = false, searchPlaceholder = 'Search options...', noResultsMessage = 'No options match' }: CustomSelectProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState<string | null>(null)
  const composing = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listId = useId()
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
    // (its own overflow list) are fine. The instanceof guard matters for the
    // shared resize path: a resize event's target is `window`, which is not
    // a Node -- contains(window) throws a TypeError and the menu never closed.
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && menuRef.current?.contains(e.target)) return
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

  useEffect(() => {
    if (open && searchable) inputRef.current?.focus()
    if (!open) { setQuery(''); setActive(null) }
  }, [open, searchable])

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

  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const visibleGroups = groups.map(group => ({
    ...group,
    items: group.items.filter(opt => terms.every(term => `${opt.label} ${opt.sublabel || ''}`.toLowerCase().includes(term))),
  })).filter(group => group.items.length > 0)
  const visible = visibleGroups.flatMap(group => group.items)
  const enabled = visible.filter(opt => !opt.disabled)
  const activeOption = enabled.find(opt => opt.value === active)
  const optionId = (opt: SelectOption) => `${listId}-option-${options.indexOf(opt)}`
  const select = (opt: SelectOption) => {
    if (opt.disabled) return
    onChange(opt.value)
    setOpen(false)
    triggerRef.current?.focus()
  }
  const handleSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (composing.current || e.nativeEvent.isComposing) return
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const index = enabled.findIndex(opt => opt.value === activeOption?.value)
      // Start at the confirmed value only in an unfiltered list; subsequent
      // arrows follow rendered group order and stop at the enabled extremes.
      const selectedIndex = terms.length === 0 ? enabled.findIndex(opt => opt.value === value) : -1
      const next = index < 0 ? (selectedIndex >= 0 ? selectedIndex : e.key === 'ArrowDown' ? 0 : enabled.length - 1)
        : Math.max(0, Math.min(enabled.length - 1, index + (e.key === 'ArrowDown' ? 1 : -1)))
      const option = enabled[next]
      setActive(option?.value ?? null)
      if (option) document.getElementById(optionId(option))?.scrollIntoView?.({ block: 'nearest' })
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeOption) select(activeOption)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
    } else if (e.key === 'Tab') {
      const focusable = Array.from(document.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
        .filter(element => !menuRef.current?.contains(element) && element.tabIndex >= 0 && !element.closest('[hidden], [inert]'))
      const index = focusable.indexOf(triggerRef.current!)
      const target = focusable[index + (e.shiftKey ? -1 : 1)]
      if (target) { e.preventDefault(); setOpen(false); target.focus() }
      else setOpen(false)
    }
  }

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={`w-full flex items-center justify-between bg-gray-900 text-sm rounded-lg px-3 py-2.5 focus:outline-hidden transition-colors ${
          invalid
            ? 'border-2 border-red-500! ring-2 ring-red-500/30'
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
          // The outer container has no role. Normal mode retains plain buttons;
          // searchable mode uses an input combobox with listbox/option semantics.
          onMouseDown={searchable ? e => {
            const target = e.target as Element
            // Preserve mouse editing and future internal controls. Option buttons
            // use virtual focus, so noninteractive surfaces keep focus in the input.
            if (!target.closest('input, textarea, select, a[href], [contenteditable]:not([contenteditable="false"]), [tabindex]:not([tabindex="-1"]), button:not([role="option"])')) e.preventDefault()
          } : undefined}
          data-testid="custom-select-menu"
          style={{ position: 'fixed', left: pos.left, width: pos.width, top: pos.top, bottom: pos.bottom, maxHeight: MENU_MAX_H }}
          className="z-80 bg-gray-900 border border-gray-700 rounded-lg shadow-xl shadow-black/30 overflow-y-auto"
        >
          {searchable && (
            <input
              ref={inputRef}
              role="combobox"
              aria-label={ariaLabel}
              aria-expanded={open}
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={activeOption ? optionId(activeOption) : undefined}
              value={query}
              onChange={e => { setQuery(e.target.value); setActive(null) }}
              onKeyDown={handleSearchKey}
              onCompositionStart={() => { composing.current = true }}
              onCompositionEnd={() => { composing.current = false }}
              onBlur={e => {
                const target = e.relatedTarget as Node | null
                if (!ref.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false)
              }}
              placeholder={searchPlaceholder}
              className="w-full bg-gray-900 text-gray-200 text-sm px-3 py-2 border-b border-gray-700 focus:outline-hidden"
            />
          )}
          <div role={searchable ? 'listbox' : undefined} aria-label={searchable ? ariaLabel : undefined} id={searchable ? listId : undefined}>
          {(searchable ? visibleGroups : groups).map((group, gi) => (
            <div key={gi} role={searchable && group.label ? "group" : undefined} aria-label={searchable && group.label ? group.label : undefined}>
              {group.label && (
                <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-gray-500 font-semibold bg-gray-800/50 sticky top-0">
                  {group.label}
                </div>
              )}
              {group.items.map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  id={searchable ? optionId(opt) : undefined}
                  role={searchable ? 'option' : undefined}
                  aria-label={searchable ? [opt.label, opt.sublabel].filter(Boolean).join(', ') : undefined}
                  aria-selected={searchable ? value === opt.value : undefined}
                  aria-disabled={searchable && opt.disabled ? true : undefined}
                  tabIndex={searchable ? -1 : undefined}
                  disabled={opt.disabled}
                  onClick={() => {
                    if (searchable) select(opt)
                    else if (!opt.disabled) { onChange(opt.value); setOpen(false) }
                  }}
                  className={`w-full text-left px-3 py-2 flex items-center justify-between transition-colors ${
                    opt.disabled
                      ? 'text-gray-600 cursor-not-allowed'
                      : value === opt.value
                        ? 'bg-emerald-900/30 text-emerald-300'
                        : searchable && activeOption?.value === opt.value
                          ? 'bg-gray-800 text-gray-300'
                          : 'text-gray-300 hover:bg-gray-800'
                  } ${searchable && activeOption?.value === opt.value ? 'ring-2 ring-inset ring-emerald-400' : ''}`}
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
          </div>
          {searchable && visible.length === 0 && options.length > 0 && (
            <div role="status" className="px-3 py-4 text-sm text-gray-500 text-center">{noResultsMessage}</div>
          )}
          {options.length === 0 && (
            <div className="px-3 py-4 text-sm text-gray-500 text-center">No options available</div>
          )}
        </div>,
        document.body,
      )}
    </div>
  )
}
