import { useState, useRef, useEffect } from 'react'
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
}

export function CustomSelect({ value, onChange, options, placeholder = 'Select...', className = '' }: CustomSelectProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
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
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between bg-gray-900 border border-gray-700 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none transition-colors hover:border-gray-600"
      >
        <span className={selected ? 'text-gray-200' : 'text-gray-500'}>
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown size={14} className={`text-gray-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute z-50 mt-1 w-full bg-gray-900 border border-gray-700 rounded-lg shadow-xl shadow-black/30 max-h-64 overflow-y-auto">
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
        </div>
      )}
    </div>
  )
}
