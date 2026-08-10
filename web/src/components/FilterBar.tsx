// The filter bars — PRESENTATIONAL ONLY, and now CHIP-SHAPED.
//
// WHY THE REDESIGN. The first version of this bar rendered every derived
// dimension as a labelled row, all at once, all equally prominent: on the
// 45-agent session that motivated it, the per-session panel was seventeen
// rows tall — taller than the fleet it filtered — and three separate faults
// hid inside it. Several rows could not discriminate anything (one value on
// all 43 rows; one value PER row; one opaque sha256). The dimensions the
// operator most wanted (the phase-like pill vocabularies) were correct in
// placement but undiscoverable behind a collapsed panel. And the lane — the
// annotation's LABEL — was not a dimension at all, only free-text-findable.
//
// THE MODEL (adapted from dnd-scheduler's dashboard-filter-bar.jsx, without
// its shadcn dependency): one bordered section, one wrapping flex row. ONLY
// ACTIVE FILTERS RENDER AS CHIPS — an unused dimension occupies zero pixels.
// A chip reads `Phase: parked, reported` (the selection, not just the name),
// opens a popover editor for that one dimension, and carries an X that
// removes it. A dashed "+ Filter" button opens a picker ranked by DERIVED
// usefulness (dimensionMerit in lib/filters.ts — value shape, never key
// names). An "Advanced" button opens a modal holding EVERYTHING, including
// the dimensions the picker demoted or omitted; the modal is the old
// seventeen-row panel, opt-in now, and it is allowed to be dense.
//
// Every decision about WHAT EXISTS lives in lib/filters.ts (the one
// predicate, the shape-typed dimension discovery, the ranking). This file
// draws what it is handed and stays on the MODULES guard in
// test/annotations.test.tsx: "if (key === ...)" is the natural way to write a
// filter row, and the guard is what stands between this file and a
// hard-coded facet dimension.
//
// EVERY CONTROL IS A REAL TARGET. The bars are operated on a 390×844 touch
// viewport, so every interactive element meets the WCAG 2.5.5 AAA 44×44
// floor the e2e measures — chips, the X on each chip (a real 44×44 button,
// revealed on hover and always visible under `hover:none`, the touch
// handling the reference implementation uses), picker items, popover
// options, modal rows. The bar is one flex-wrap row: at 390px it grows DOWN
// by wrapping chips, never sideways and never into a stacked form.

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, Plus, Search, SlidersHorizontal, X } from 'lucide-react'
import { SEMANTIC_ROLES } from '../lib/annotations'
import { FloatingCard } from './FloatingCard'
import type {
  CallerOption,
  DimensionStats,
  FacetDimension,
  FacetSelection,
  FilterState,
  PickerTier,
  TriState,
} from '../lib/filters'
import {
  dimensionMerit,
  emptyFacetSelection,
  facetSelectionActive,
  facetStats,
  groupDimensions,
  isFilterActive,
  LABEL_DIMENSION_PREFIX,
  MAX_PILL_VALUES,
} from '../lib/filters'

// The six semantic roles are the fork's own tokens (design-tokens/tokens.json)
// — the same family the chips draw from. A dot per role, no severity claim.
const ROLE_DOT: Record<string, string> = {
  success: 'bg-cao-success',
  info: 'bg-cao-info',
  accent: 'bg-cao-accent',
  warning: 'bg-cao-warning',
  danger: 'bg-cao-danger',
  neutral: 'bg-cao-neutral',
}

function toggleValue(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function patchFacet(filters: FilterState, key: string, patch: Partial<FacetSelection>): FilterState {
  const current = filters.facets[key] ?? emptyFacetSelection()
  return { ...filters, facets: { ...filters.facets, [key]: { ...current, ...patch } } }
}

// ── Dimension descriptors ─────────────────────────────────────────────────
//
// One descriptor per filterable thing the bar can draw: a Layer-1 field of
// FilterState (reachability, liveness, … — the fork's own schema, named
// outright, as it always was) or a derived FacetDimension (detail facets and
// `label:<kind>` dimensions). The descriptor carries everything the chip,
// the picker item, the popover editor and the Advanced modal row need, so
// all four surfaces can never disagree about what a dimension is called or
// what it currently selects.

/** One option in an options editor. `dot`/`activeClass` let the reachability
 *  options wear their per-status palette in both the popover and the modal. */
interface EditorOption {
  value: string
  label: string
  count?: number
  dot?: string
  activeClass?: string
}

interface DimensionBase {
  /** 'reachability' | a Layer-1 field name | a facet key | 'label:<kind>'. */
  key: string
  label: string
  /** Where this descriptor came from — the modal groups by it. */
  origin: 'builtin' | 'facet' | 'label'
  stats: DimensionStats
  tier: PickerTier | null
  /** The shape-derived one-liner the picker shows under the label. */
  note: string
  /** Whether the dimension currently constrains the fleet. */
  active: boolean
  /** The chip's text after the label — null while nothing is selected. */
  summary: string | null
  clear: () => void
}

type Dimension = DimensionBase &
  (
    | { editor: 'options'; options: EditorOption[]; selected: string[]; toggle: (value: string) => void }
    | { editor: 'choice'; choices: Array<{ value: string; label: string }>; choice: string; choose: (value: string) => void }
    | { editor: 'range'; from: string; to: string; setRange: (from: string, to: string) => void }
    | { editor: 'text'; text: string; setText: (text: string) => void }
  )

/** The reachability option row the chip bar offers. Built in DashboardHome
 *  from STATUS_META/STATUS_ACTIVE_BG — see the call site for why that build
 *  is deliberately unguarded. */
export interface StatusOption {
  value: string
  label: string
  dot: string
  activeClass: string
}

/** facetStats for a Layer-1 vocabulary: every row answers for these fields,
 *  so coverage is the whole scope by construction. */
function builtinStats(vocabularySize: number, totalRows: number): DimensionStats {
  return {
    control: vocabularySize <= MAX_PILL_VALUES ? 'pills' : 'typeahead',
    distinct: vocabularySize,
    carriers: totalRows,
    opaque: false,
  }
}

/** A plain multi-select Layer-1 dimension (liveness, profiles, providers,
 *  sessions, roles, callers). Fewer than two options is not a filter — the
 *  rule the old rows applied — so the descriptor is not built at all. */
function builtinOptionsDimension(
  base: { key: string; label: string; options: EditorOption[]; carriers?: number },
  selected: string[],
  setSelected: (next: string[]) => void,
  totalRows: number,
): Dimension | null {
  if (base.options.length < 2) return null
  const stats = builtinStats(base.options.length, totalRows)
  if (base.carriers !== undefined) stats.carriers = base.carriers
  const merit = dimensionMerit(stats, totalRows)
  return {
    key: base.key,
    label: base.label,
    origin: 'builtin',
    stats,
    tier: merit.tier,
    note: merit.note,
    active: selected.length > 0,
    summary: selected.length > 0 ? selected.join(', ') : null,
    clear: () => setSelected([]),
    editor: 'options',
    options: base.options,
    selected,
    toggle: value => setSelected(toggleValue(selected, value)),
  }
}

/** The one descriptor a collected FacetDimension becomes. The editor kind is
 *  the control its values already earned; matching is unchanged in
 *  lib/filters.ts. */
function facetDimension(
  dim: FacetDimension,
  origin: 'facet' | 'label',
  filters: FilterState,
  onChange: (next: FilterState) => void,
  totalRows: number,
): Dimension {
  const sel = filters.facets[dim.key] ?? emptyFacetSelection()
  const apply = (patch: Partial<FacetSelection>) => onChange(patchFacet(filters, dim.key, patch))
  const stats = facetStats(dim)
  const merit = dimensionMerit(stats, totalRows)
  const base: DimensionBase = {
    key: dim.key,
    label: dim.label,
    origin,
    stats,
    tier: merit.tier,
    note: merit.note,
    active: facetSelectionActive(sel),
    summary: null,
    clear: () => apply(emptyFacetSelection()),
  }
  if (dim.control === 'pills' || dim.control === 'typeahead') {
    return {
      ...base,
      editor: 'options',
      options: dim.values.map(v => ({ value: v.value, label: v.value, count: v.rows })),
      selected: sel.values,
      toggle: value => apply({ values: toggleValue(sel.values, value) }),
      summary: sel.values.length > 0 ? sel.values.join(', ') : null,
    }
  }
  if (dim.control === 'tri-state') {
    return {
      ...base,
      editor: 'choice',
      choices: [
        { value: 'any', label: 'Any' },
        { value: 'true', label: 'true' },
        { value: 'false', label: 'false' },
      ],
      choice: sel.tri,
      choose: value => apply({ tri: value as TriState }),
      summary: sel.tri !== 'any' ? sel.tri : null,
    }
  }
  if (dim.control === 'range') {
    const bounded = sel.from !== '' || sel.to !== ''
    return {
      ...base,
      editor: 'range',
      from: sel.from,
      to: sel.to,
      setRange: (from, to) => apply({ from, to }),
      summary: bounded ? `${sel.from || '…'} → ${sel.to || '…'}` : null,
    }
  }
  return {
    ...base,
    editor: 'text',
    text: sel.text,
    setText: text => apply({ text }),
    summary: sel.text.trim() !== '' ? `"${sel.text.trim()}"` : null,
  }
}

/** Picker/chip order: tier, then coverage, then the smaller vocabulary first.
 *  Array.prototype.sort is stable, so ties keep the build order — producer
 *  insertion order for derived dimensions, declaration order for Layer-1.
 *  All descriptors in one bar share the same totalRows, so raw carrier
 *  counts compare coverage directly. */
const TIER_ORDER: Record<PickerTier, number> = { primary: 0, secondary: 1, niche: 2 }

function compareDimensions(a: Dimension, b: Dimension): number {
  const ta = a.tier === null ? TIER_ORDER.niche + 1 : TIER_ORDER[a.tier]
  const tb = b.tier === null ? TIER_ORDER.niche + 1 : TIER_ORDER[b.tier]
  if (ta !== tb) return ta - tb
  if (a.stats.carriers !== b.stats.carriers) return b.stats.carriers - a.stats.carriers
  return a.stats.distinct - b.stats.distinct
}

// ── Shared atoms ──────────────────────────────────────────────────────────

const inputClass =
  'min-h-[44px] max-w-full rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none'

/** The one pill, 44px tall, aria-pressed, emerald when on — the treatment the
 *  pre-chip rows already wore, kept for the Advanced modal's dense rows. */
function Pill({
  selected,
  onClick,
  label,
  children,
}: {
  selected: boolean
  onClick: () => void
  label?: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      aria-label={label}
      onClick={onClick}
      className={`flex items-center gap-1.5 min-h-[44px] max-w-full px-3 rounded-full border text-xs transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
        selected
          ? 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300'
          : 'border-gray-700 text-gray-400 hover:text-gray-200'
      }`}
    >
      {children}
    </button>
  )
}

/** A dimension's caption plus its controls — the Advanced modal's row shape. */
function FilterRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 flex-wrap">
      <span className="w-24 shrink-0 pt-3.5 text-[10px] uppercase tracking-wide text-gray-400">
        {label}
      </span>
      <div className="flex flex-1 min-w-0 items-center gap-2 flex-wrap">{children}</div>
    </div>
  )
}

// ── Editors ───────────────────────────────────────────────────────────────
//
// Two presentations of the same four editor kinds. The POPOVER (one
// dimension, anchored to its chip) uses a vertical option list; the ADVANCED
// MODAL (every dimension, dense by design) uses the wrapping pill rows and
// select-plus-pills the pre-chip panel used. Both write through the same
// descriptor closures, so they can never disagree about the selection.

function OptionsEditor({ dim, idPrefix }: { dim: Dimension & { editor: 'options' }; idPrefix: string }) {
  const [query, setQuery] = useState('')
  const needle = query.trim().toLowerCase()
  const visible = needle === '' ? dim.options : dim.options.filter(o => o.label.toLowerCase().includes(needle))
  return (
    <div className="p-2 space-y-1">
      <p className="px-1 pb-1 text-[10px] uppercase tracking-wide text-gray-400">{dim.label}</p>
      {dim.options.length > MAX_PILL_VALUES && (
        <input
          type="search"
          aria-label={`Find ${dim.label} value`}
          placeholder="type to narrow…"
          value={query}
          onChange={e => setQuery(e.target.value)}
          className={`${inputClass} mb-1 w-full`}
        />
      )}
      {/* The options container holds EXACTLY the option buttons — the search
          input and the empty note live outside it. The status-order
          appearance suite asserts that exactness on the reachability
          container: a stray clear-all or overflow control inside it fails
          there, as it always has. */}
      <div data-testid={`${idPrefix}-editor-options`} className="max-h-64 overflow-y-auto">
        {visible.map(o => {
          const selected = dim.selected.includes(o.value)
          return (
            <button
              key={o.value}
              type="button"
              aria-pressed={selected}
              title={o.label}
              onClick={() => dim.toggle(o.value)}
              className={`flex w-full items-center gap-2 min-h-[44px] px-3 rounded-lg text-left text-xs transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
                selected ? (o.activeClass ?? 'bg-emerald-900/40 text-emerald-300') : 'text-gray-300 hover:bg-gray-800'
              }`}
            >
              {o.dot && <span aria-hidden className={`w-1.5 h-1.5 shrink-0 rounded-full ${o.dot}`} />}
              <span className="min-w-0 flex-1 truncate">{o.label}</span>
              {o.count !== undefined && <span className="shrink-0 text-[10px] text-gray-500">{o.count}</span>}
              {selected && <Check size={12} aria-hidden className="shrink-0" />}
            </button>
          )
        })}
      </div>
      {visible.length === 0 && <p className="px-1 py-2 text-[10px] text-gray-500">No values match.</p>}
    </div>
  )
}

function ChoiceEditor({ dim }: { dim: Dimension & { editor: 'choice' } }) {
  return (
    <>
      {dim.choices.map(c => (
        <Pill key={c.value} selected={dim.choice === c.value} onClick={() => dim.choose(c.value)}>
          {c.label}
        </Pill>
      ))}
    </>
  )
}

function RangeEditor({ dim }: { dim: Dimension & { editor: 'range' } }) {
  return (
    <>
      <input
        type="datetime-local"
        aria-label={`${dim.label} from`}
        value={dim.from}
        onChange={e => dim.setRange(e.target.value, dim.to)}
        className={inputClass}
      />
      <span className="text-[10px] text-gray-400">to</span>
      <input
        type="datetime-local"
        aria-label={`${dim.label} to`}
        value={dim.to}
        onChange={e => dim.setRange(dim.from, e.target.value)}
        className={inputClass}
      />
    </>
  )
}

function TextEditor({ dim }: { dim: Dimension & { editor: 'text' } }) {
  return (
    <input
      type="search"
      aria-label={`${dim.label} contains`}
      placeholder="contains…"
      value={dim.text}
      onChange={e => dim.setText(e.target.value)}
      className={`${inputClass} w-full`}
    />
  )
}

/** The popover body: the one dimension's editor, nothing else. */
function DimensionEditor({ dim, idPrefix }: { dim: Dimension; idPrefix: string }) {
  if (dim.editor === 'options') return <OptionsEditor dim={dim} idPrefix={idPrefix} />
  if (dim.editor === 'choice') {
    return (
      <div className="p-2">
        <p className="px-1 pb-1 text-[10px] uppercase tracking-wide text-gray-400">{dim.label}</p>
        <div className="flex items-center gap-2 flex-wrap">
          <ChoiceEditor dim={dim} />
        </div>
      </div>
    )
  }
  if (dim.editor === 'range') {
    return (
      <div className="p-2 space-y-1">
        <p className="px-1 pb-1 text-[10px] uppercase tracking-wide text-gray-400">{dim.label}</p>
        <div className="flex items-center gap-2 flex-wrap">
          <RangeEditor dim={dim} />
        </div>
      </div>
    )
  }
  return (
    <div className="p-2 space-y-1">
      <p className="px-1 pb-1 text-[10px] uppercase tracking-wide text-gray-400">{dim.label}</p>
      <TextEditor dim={dim} />
    </div>
  )
}

/** The modal's dense option control: pills at or under the cap, a
 *  select-plus-selected-pills past it — the pre-chip panel's own pattern. */
function ModalOptionsEditor({ dim }: { dim: Dimension & { editor: 'options' } }) {
  if (dim.options.length <= MAX_PILL_VALUES) {
    return (
      <>
        {dim.options.map(o => {
          const selected = dim.selected.includes(o.value)
          return (
            <Pill key={o.value} selected={selected} onClick={() => dim.toggle(o.value)} label={o.label}>
              {o.dot && <span aria-hidden className={`w-1.5 h-1.5 shrink-0 rounded-full ${o.dot}`} />}
              <span className="min-w-0 truncate">{o.label}</span>
              {o.count !== undefined && <span className="shrink-0 text-gray-400">{o.count}</span>}
            </Pill>
          )
        })}
      </>
    )
  }
  const labelFor = (value: string) => dim.options.find(o => o.value === value)?.label ?? value
  return (
    <>
      <select
        aria-label={dim.label}
        value=""
        onChange={e => {
          if (e.target.value) dim.toggle(e.target.value)
        }}
        className="min-h-[44px] max-w-full rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
      >
        <option value="" disabled>
          {dim.label}…
        </option>
        {dim.options
          .filter(o => !dim.selected.includes(o.value))
          .map(o => (
            <option key={o.value} value={o.value}>
              {o.label}
              {o.count !== undefined ? ` (${o.count})` : ''}
            </option>
          ))}
      </select>
      {dim.selected.map(value => (
        <Pill key={value} selected onClick={() => dim.toggle(value)} label={`Remove ${labelFor(value)}`}>
          <span className="min-w-0 truncate">{labelFor(value)}</span>
          <X size={12} aria-hidden className="shrink-0" />
        </Pill>
      ))}
    </>
  )
}

function ModalDimensionEditor({ dim }: { dim: Dimension }) {
  if (dim.editor === 'options') return <ModalOptionsEditor dim={dim} />
  if (dim.editor === 'choice') return <ChoiceEditor dim={dim} />
  if (dim.editor === 'range') return <RangeEditor dim={dim} />
  return <TextEditor dim={dim} />
}

// ── The chip ──────────────────────────────────────────────────────────────

/**
 * One active filter. The chip reads `Label: selection` — the selection is
 * the whole point — truncated with a title carrying the full text. The X is
 * a real 44×44 button (the AAA floor the e2e measures), revealed on hover
 * and always visible under `hover:none`; the main button's `pr-11` keeps the
 * text from running underneath it. A pinned-but-unselected chip (its editor
 * is open from the picker) wears a dashed outline: it filters nothing yet,
 * and the solid emerald treatment would claim otherwise.
 */
function Chip({
  dim,
  idPrefix,
  editing,
  onToggleEditor,
  onRemove,
}: {
  dim: Dimension
  idPrefix: string
  editing: boolean
  onToggleEditor: (trigger: HTMLElement) => void
  onRemove: () => void
}) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null)
  const fullText = dim.summary ? `${dim.label}: ${dim.summary}` : dim.label
  return (
    <div className="group relative shrink-0 max-w-full" data-testid={`${idPrefix}-chip`} data-dimension={dim.key}>
      <button
        ref={setAnchor}
        type="button"
        onClick={e => onToggleEditor(e.currentTarget)}
        aria-expanded={editing}
        aria-haspopup="dialog"
        title={fullText}
        className={`flex items-center min-h-[44px] max-w-full rounded-full border pl-3 pr-11 text-xs transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
          dim.active
            ? 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300'
            : 'border-dashed border-gray-600 text-gray-400'
        }`}
      >
        <span className="min-w-0 max-w-[16rem] truncate">
          {dim.summary ? (
            <>
              <span className="opacity-70">{dim.label}:</span> {dim.summary}
            </>
          ) : (
            dim.label
          )}
        </span>
      </button>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${dim.label} filter`}
        title={`Remove ${dim.label} filter`}
        className="absolute right-0 top-1/2 -translate-y-1/2 inline-flex h-[44px] w-[44px] items-center justify-center rounded-full text-gray-500 opacity-0 transition-opacity hover:text-gray-100 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-emerald-500 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
      >
        <X size={12} aria-hidden />
      </button>
      <FloatingCard
        anchor={anchor}
        open={editing}
        role="dialog"
        labelledBy={`${dim.label} filter`}
        testId={`${idPrefix}-editor`}
        className="w-[min(22rem,calc(100vw-1rem))]"
      >
        <div data-filter-popover>
          <DimensionEditor dim={dim} idPrefix={idPrefix} />
        </div>
      </FloatingCard>
    </div>
  )
}

// ── The Advanced modal ────────────────────────────────────────────────────

interface ModalSection {
  id: string
  heading: string | null
  dimensions: Dimension[]
}

/**
 * EVERYTHING the bar can filter on, in one opt-in sheet: the fork-owned
 * dimensions, then the derived facets under their provenance headings (the
 * groupDimensions grouping, unchanged), then the label dimensions. This is
 * where the seventeen-row panel went — dense is fine here, because opening
 * it is a deliberate act.
 *
 * Full-height sheet at 390px, centred dialog at sm and up — one component at
 * both viewports. Reachable by keyboard (the trigger is a plain button, and
 * the panel takes focus on open), focus is trapped while it is open, Escape
 * closes it, and focus returns to the trigger.
 */
function AdvancedFiltersModal({
  title,
  sections,
  testId,
  onClose,
}: {
  title: string
  sections: ModalSection[]
  testId: string
  onClose: () => void
}) {
  const panelRef = useRef<HTMLDivElement>(null)

  // Mount = open (the caller renders conditionally), so focusing the panel
  // once on mount is the whole "take focus" story.
  useEffect(() => {
    panelRef.current?.focus()
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // Capture-phase and stopped: a bar-level popover listener must not
        // also fire on the same keypress.
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusables = panel.querySelectorAll<HTMLElement>(
        'button, input, select, a[href], [tabindex]:not([tabindex="-1"])',
      )
      if (focusables.length === 0) {
        e.preventDefault()
        return
      }
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          last.focus()
          e.preventDefault()
        }
      } else if (active === last || !panel.contains(active)) {
        first.focus()
        e.preventDefault()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onClose])

  return createPortal(
    <div className="fixed inset-0 z-[80]" data-testid={testId}>
      <div className="absolute inset-0 bg-black/60" aria-hidden onClick={onClose} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="absolute inset-x-2 top-8 bottom-0 sm:inset-auto sm:left-1/2 sm:top-1/2 sm:w-[min(44rem,calc(100vw-2rem))] sm:max-h-[85vh] sm:-translate-x-1/2 sm:-translate-y-1/2 flex flex-col overflow-hidden rounded-t-xl sm:rounded-xl border border-gray-700 bg-gray-900 shadow-2xl focus:outline-none"
      >
        <div className="flex items-center justify-between gap-2 px-3 py-1 border-b border-gray-700 bg-gray-900">
          <p className="text-xs font-semibold text-white">{title}</p>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close advanced filters"
            className="min-h-[44px] min-w-[44px] grid place-items-center rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <X size={14} aria-hidden />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4 bg-gray-900">
          {sections.map(section => (
            <section key={section.id} className="space-y-2">
              {section.heading && (
                <p className="text-[10px] uppercase tracking-wide text-gray-400">{section.heading}</p>
              )}
              {section.dimensions.map(dim => (
                <FilterRow key={dim.key} label={dim.label}>
                  <ModalDimensionEditor dim={dim} />
                </FilterRow>
              ))}
            </section>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  )
}

// ── The bar ───────────────────────────────────────────────────────────────

/**
 * The one chip bar, instantiated by both scopes. Chips render in picker
 * order (ranked by derived usefulness); the picker offers what is not yet
 * active; the Advanced modal holds everything. The search input is the free
 * text dimension and stays inline — it is the highest-frequency control the
 * bar has.
 */
function ChipBar({
  idPrefix,
  testId,
  filters,
  onChange,
  onClear,
  clearLabel,
  specs,
  sections,
  searchLabel,
  searchPlaceholder,
  counter,
  degraded,
  showCoverage,
}: {
  idPrefix: string
  testId: string
  filters: FilterState
  onChange: (next: FilterState) => void
  onClear: () => void
  clearLabel: string
  specs: Dimension[]
  sections: ModalSection[]
  searchLabel: string
  searchPlaceholder: string
  counter?: { shown: number; total: number; visible: boolean }
  degraded: boolean
  showCoverage: boolean
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerAnchor, setPickerAnchor] = useState<HTMLButtonElement | null>(null)
  const [editorKey, setEditorKey] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const barRef = useRef<HTMLElement | null>(null)
  const advancedButtonRef = useRef<HTMLButtonElement | null>(null)
  /** The control that opened the current popover — focus returns to it. */
  const lastTriggerRef = useRef<HTMLElement | null>(null)

  const sorted = [...specs].sort(compareDimensions)
  const pickerDims = sorted.filter(d => d.tier !== null && !d.active)
  const chipDims = sorted.filter(d => d.active || d.key === editorKey)
  const filterActive = isFilterActive(filters)

  // A dimension can vanish between polls (the producer stopped emitting it).
  // An editor pinned to a vanished dimension would float, anchored to
  // nothing, forever — close it instead.
  useEffect(() => {
    if (editorKey && !specs.some(d => d.key === editorKey)) setEditorKey(null)
  }, [editorKey, specs])

  // Escape and outside-click close the popovers. `data-filter-popover` marks
  // the portalled card bodies: a click inside one is not "outside" even
  // though the portal escapes the bar's DOM subtree.
  useEffect(() => {
    if (!pickerOpen && !editorKey) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setPickerOpen(false)
      setEditorKey(null)
      lastTriggerRef.current?.focus()
    }
    const onDown = (e: MouseEvent) => {
      const t = e.target
      if (!(t instanceof Element)) return
      if (barRef.current?.contains(t)) return
      if (t.closest('[data-filter-popover]')) return
      setPickerOpen(false)
      setEditorKey(null)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown)
    }
  }, [pickerOpen, editorKey])

  // Move focus into a freshly opened editor, so the keyboard path is
  // chip → Enter → the editor's first control, with no portal detour.
  useEffect(() => {
    if (!editorKey) return
    const card = document.querySelector<HTMLElement>(`[data-testid="${idPrefix}-editor"]`)
    card?.querySelector<HTMLElement>('input, button, select')?.focus()
  }, [editorKey, idPrefix])

  const openEditor = (key: string, trigger: HTMLElement) => {
    lastTriggerRef.current = trigger
    setPickerOpen(false)
    setEditorKey(current => (current === key ? null : key))
  }

  const removeDimension = (dim: Dimension) => {
    if (editorKey === dim.key) setEditorKey(null)
    dim.clear()
  }

  return (
    <section ref={barRef} data-testid={testId} className="rounded-lg border border-gray-700/40 bg-gray-900/40 px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative flex-1 min-w-[6rem] max-w-full sm:max-w-[22rem]">
          <Search
            size={12}
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-500"
          />
          <input
            type="search"
            aria-label={searchLabel}
            placeholder={searchPlaceholder}
            value={filters.text}
            onChange={e => onChange({ ...filters, text: e.target.value })}
            className="w-full min-h-[44px] rounded-full border border-gray-700 bg-gray-950 pl-8 pr-3 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
          />
        </label>
        {chipDims.map(dim => (
          <Chip
            key={dim.key}
            dim={dim}
            idPrefix={idPrefix}
            editing={editorKey === dim.key}
            onToggleEditor={trigger => openEditor(dim.key, trigger)}
            onRemove={() => removeDimension(dim)}
          />
        ))}
        <button
          ref={setPickerAnchor}
          type="button"
          data-testid={`${idPrefix}-picker-button`}
          disabled={specs.length === 0}
          aria-expanded={pickerOpen}
          aria-haspopup="dialog"
          onClick={e => {
            lastTriggerRef.current = e.currentTarget
            setEditorKey(null)
            setPickerOpen(o => !o)
          }}
          className="flex items-center gap-1.5 min-h-[44px] px-3 rounded-full border border-dashed border-gray-600 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Plus size={12} aria-hidden />
          Filter
        </button>
        {specs.length > 0 && (
          <button
            ref={advancedButtonRef}
            type="button"
            data-testid={`${idPrefix}-advanced-button`}
            aria-label="Advanced filters"
            aria-haspopup="dialog"
            aria-expanded={advancedOpen}
            onClick={() => {
              setPickerOpen(false)
              setEditorKey(null)
              setAdvancedOpen(true)
            }}
            className="flex items-center justify-center gap-1.5 min-h-[44px] min-w-[44px] px-3 rounded-full border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <SlidersHorizontal size={12} aria-hidden />
            {/* Icon-only below sm: the session bar lives inside a card and
                measures 308px at 390 — search + Filter + a labelled Advanced
                wrapped it to two rows, and the one-row default is a hard
                requirement. The aria-label keeps the name. */}
            <span className="hidden sm:inline">Advanced</span>
          </button>
        )}
        {(counter?.visible || filterActive) && (
          <div className="ml-auto flex items-center gap-2 flex-wrap">
            {counter?.visible && (
              <span data-testid="session-filter-count" className="text-[10px] text-gray-400">
                {counter.shown} of {counter.total} shown
              </span>
            )}
            {filterActive && (
              <button
                type="button"
                onClick={() => {
                  // Clearing everything while an editor pins its chip would
                  // leave an empty chip floating over a cleared bar.
                  setPickerOpen(false)
                  setEditorKey(null)
                  onClear()
                }}
                className="min-h-[44px] px-3 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
              >
                {clearLabel}
              </button>
            )}
          </div>
        )}
      </div>
      {showCoverage && <CoverageNote degraded={degraded} />}
      <FloatingCard
        anchor={pickerAnchor}
        open={pickerOpen}
        role="dialog"
        labelledBy="Add filter"
        testId={`${idPrefix}-picker`}
        className="max-h-[calc(100vh-1rem)] w-[min(20rem,calc(100vw-1rem))] !overflow-y-auto overscroll-contain"
      >
        <div data-filter-popover className="p-2">
          <p className="px-1 pb-1 text-[10px] uppercase tracking-wide text-gray-400">Add filter</p>
          {pickerDims.length === 0 && (
            <p className="px-1 py-2 text-xs text-gray-500">
              {specs.length === 0 ? 'No filter dimensions on these rows.' : 'All filters are active.'}
            </p>
          )}
          {(['primary', 'secondary', 'niche'] as const).map(tier => {
            const tierDims = pickerDims.filter(d => d.tier === tier)
            if (tierDims.length === 0) return null
            return (
              <div key={tier}>
                {tier === 'niche' && (
                  // Below the fold: the identity-shaped and opaque dimensions.
                  // Still offered — the ranking demotes, it never deletes.
                  <p className="px-1 pt-2 pb-1 text-[10px] uppercase tracking-wide text-gray-500">
                    Less useful as filters
                  </p>
                )}
                {tierDims.map(dim => (
                  <button
                    key={dim.key}
                    type="button"
                    data-dimension={dim.key}
                    onClick={e => openEditor(dim.key, e.currentTarget)}
                    className="flex w-full flex-col items-start gap-0.5 min-h-[44px] rounded-lg px-2 py-2 text-left transition-colors hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  >
                    <span className="text-xs font-semibold text-gray-200">{dim.label}</span>
                    <span className="text-[10px] text-gray-500">{dim.note}</span>
                  </button>
                ))}
              </div>
            )
          })}
        </div>
      </FloatingCard>
      {advancedOpen && (
        <AdvancedFiltersModal
          title={clearLabel === 'Clear all' ? 'Advanced fleet filters' : 'Advanced session filters'}
          sections={sections}
          testId={`${idPrefix}-advanced`}
          onClose={() => {
            setAdvancedOpen(false)
            advancedButtonRef.current?.focus()
          }}
        />
      )}
    </section>
  )
}

/**
 * The honest degraded state. "0 matches" is indistinguishable from "the
 * producer is not running" unless the bar says which one it is — the envelope
 * already reports the coverage, so the bar repeats it next to the controls
 * that depend on it.
 *
 * Rendered ONLY on degraded coverage, never on an empty one. A fleet with no
 * conductor (or a producer with nothing to say) gets the quiet dashboard it
 * always had: no facet dimensions are offered, and their absence is not an
 * error to name. The byte-identical-DOM test pins the empty-payload and
 * no-route cases rendering alike.
 */
function CoverageNote({ degraded }: { degraded: boolean }) {
  if (!degraded) return null
  return (
    <p data-testid="filter-coverage-note" className="pt-1 text-[10px] text-amber-300/90">
      Annotation data is partial or unverified — facet filters see only what arrived.
    </p>
  )
}

/** The modal sections for one bar: fork-owned dimensions first (unheaded,
 *  the way the pre-chip panel led with them), then the derived facets under
 *  their provenance classes, then the label dimensions as their own family.
 *  Specs are matched back to their dimension by key — grouping is the
 *  producer's, labels are the fork's `label:` family's. */
function buildSections(specs: Dimension[], facetDims: FacetDimension[]): ModalSection[] {
  const byKey = new Map(specs.map(d => [d.key, d]))
  const sections: ModalSection[] = []
  const builtin = specs.filter(d => d.origin === 'builtin')
  if (builtin.length > 0) sections.push({ id: 'builtin', heading: null, dimensions: builtin })
  for (const group of groupDimensions(facetDims)) {
    const grouped = group.dimensions
      .map(d => byKey.get(d.key))
      .filter((d): d is Dimension => !!d)
    if (grouped.length === 0) continue
    sections.push({
      id: group.heading ?? `plain-${group.dimensions[0].key}`,
      heading: group.heading,
      dimensions: grouped,
    })
  }
  const labels = specs.filter(d => d.origin === 'label')
  if (labels.length > 0) {
    sections.push({ id: 'labels', heading: 'Annotation labels', dimensions: labels })
  }
  return sections
}

/**
 * The global bar: fleet-stable vocabulary, one chip row. Reachability is a
 * chip here like everything else — its editor's options container is what
 * the status-order appearance suite now pins, with exactly the STATUS_ORDER
 * entries and nothing else.
 */
export function GlobalFilterBar({
  filters,
  onChange,
  onClear,
  statusOptions,
  liveness,
  profiles,
  providers,
  sessions,
  dimensions,
  totalRows,
  annotationsAvailable,
  degraded,
}: {
  filters: FilterState
  onChange: (next: FilterState) => void
  onClear: () => void
  statusOptions: StatusOption[]
  liveness: string[]
  profiles: string[]
  providers: string[]
  sessions: string[]
  dimensions: FacetDimension[]
  totalRows: number
  annotationsAvailable: boolean
  degraded: boolean
}) {
  const specs: Dimension[] = []

  // Reachability is ALWAYS offered — a fixed fork vocabulary, not a measured
  // one — and its editor options carry the per-status palette.
  {
    const stats = builtinStats(statusOptions.length, totalRows)
    const merit = dimensionMerit(stats, totalRows)
    const selected = statusOptions.filter(o => filters.reachability.includes(o.value))
    specs.push({
      key: 'reachability',
      label: 'Reachability',
      origin: 'builtin',
      stats,
      tier: merit.tier,
      note: merit.note,
      active: filters.reachability.length > 0,
      summary: selected.length > 0 ? selected.map(o => o.label).join(', ') : null,
      clear: () => onChange({ ...filters, reachability: [] }),
      editor: 'options',
      options: statusOptions,
      selected: filters.reachability,
      toggle: value => onChange({ ...filters, reachability: toggleValue(filters.reachability, value) }),
    })
  }

  const plain = (
    key: string,
    label: string,
    vocabulary: string[],
    selected: string[],
    setSelected: (next: string[]) => void,
    options?: EditorOption[],
  ) => {
    const dim = builtinOptionsDimension(
      { key, label, options: options ?? vocabulary.map(v => ({ value: v, label: v })) },
      selected,
      setSelected,
      totalRows,
    )
    if (dim) specs.push(dim)
  }

  plain('liveness', 'Liveness', liveness, filters.liveness, next => onChange({ ...filters, liveness: next }))
  plain('profiles', 'Agent profile', profiles, filters.profiles, next => onChange({ ...filters, profiles: next }))
  plain('providers', 'Provider', providers, filters.providers, next => onChange({ ...filters, providers: next }))
  plain('sessions', 'Session', sessions, filters.sessions, next => onChange({ ...filters, sessions: next }))
  if (annotationsAvailable) {
    const stats = { control: 'tri-state' as const, distinct: 2, carriers: totalRows, opaque: false }
    const merit = dimensionMerit(stats, totalRows)
    specs.push({
      key: 'freshness',
      label: 'Freshness',
      origin: 'builtin',
      stats,
      tier: merit.tier,
      note: merit.note,
      active: filters.freshness !== 'any',
      summary: filters.freshness !== 'any' ? filters.freshness : null,
      clear: () => onChange({ ...filters, freshness: 'any' }),
      editor: 'choice',
      choices: [
        { value: 'any', label: 'Any' },
        { value: 'fresh', label: 'fresh' },
        { value: 'stale', label: 'stale' },
      ],
      choice: filters.freshness,
      choose: value => onChange({ ...filters, freshness: value as FilterState['freshness'] }),
    })
    plain(
      'roles',
      'Chip colour',
      SEMANTIC_ROLES as unknown as string[],
      filters.roles,
      next => onChange({ ...filters, roles: next }),
      SEMANTIC_ROLES.map(role => ({ value: role, label: role, dot: ROLE_DOT[role] })),
    )
  }

  const facetDims = dimensions.filter(d => !d.key.startsWith(LABEL_DIMENSION_PREFIX))
  const labelDims = dimensions.filter(d => d.key.startsWith(LABEL_DIMENSION_PREFIX))
  for (const dim of facetDims) specs.push(facetDimension(dim, 'facet', filters, onChange, totalRows))
  for (const dim of labelDims) specs.push(facetDimension(dim, 'label', filters, onChange, totalRows))

  const sections = buildSections(specs, facetDims)

  return (
    <ChipBar
      idPrefix="global"
      testId="filter-bar"
      filters={filters}
      onChange={onChange}
      onClear={onClear}
      clearLabel="Clear all"
      specs={specs}
      sections={sections}
      searchLabel="Filter text"
      searchPlaceholder="id, name, profile, facet value…"
      degraded={degraded}
      showCoverage
    />
  )
}

/**
 * The per-session bar: session-local vocabulary. It narrows rows INSIDE a
 * surviving card and can never remove the card — when it matches nothing, the
 * card stays, says 0 of N, and offers the one-click clear. The counter counts
 * rows visible after BOTH bars have run; it is a third thing beside the
 * status summary (all terminals) and the session-visibility gate, not a
 * restatement of either.
 */
export function SessionFilterBar({
  filters,
  onChange,
  onClear,
  callers,
  callerRows,
  dimensions,
  totalRows,
  shown,
  total,
  counterVisible,
  degraded,
  idPrefix,
}: {
  filters: FilterState
  onChange: (next: FilterState) => void
  onClear: () => void
  callers: CallerOption[]
  /** Rows in this session carrying a caller_id — the spawned-by coverage. */
  callerRows: number
  dimensions: FacetDimension[]
  totalRows: number
  shown: number
  total: number
  counterVisible: boolean
  degraded: boolean
  idPrefix: string
}) {
  const specs: Dimension[] = []

  // Spawned-by keeps its pre-chip rule (offered whenever any caller exists):
  // selecting the single caller of a session still DISCRIMINATES — it keeps
  // only that subtree — so the one-value case is a presence question, not
  // the no-op the picker's full-coverage rule exists to omit.
  if (callers.length > 0) {
    const options = callers.map(c => ({ value: c.id, label: c.label }))
    const stats = builtinStats(options.length, totalRows)
    stats.carriers = callerRows
    const merit = dimensionMerit(stats, totalRows)
    const selected = callers.filter(c => filters.callers.includes(c.id))
    specs.push({
      key: 'callers',
      label: 'Spawned by',
      origin: 'builtin',
      stats,
      tier: merit.tier,
      note: merit.note,
      active: filters.callers.length > 0,
      summary: selected.length > 0 ? selected.map(c => c.label).join(', ') : null,
      clear: () => onChange({ ...filters, callers: [] }),
      editor: 'options',
      options,
      selected: filters.callers,
      toggle: value => onChange({ ...filters, callers: toggleValue(filters.callers, value) }),
    })
  }

  const facetDims = dimensions.filter(d => !d.key.startsWith(LABEL_DIMENSION_PREFIX))
  const labelDims = dimensions.filter(d => d.key.startsWith(LABEL_DIMENSION_PREFIX))
  for (const dim of facetDims) specs.push(facetDimension(dim, 'facet', filters, onChange, totalRows))
  for (const dim of labelDims) specs.push(facetDimension(dim, 'label', filters, onChange, totalRows))

  const sections = buildSections(specs, facetDims)

  return (
    <ChipBar
      idPrefix={idPrefix}
      testId="session-filter-bar"
      filters={filters}
      onChange={onChange}
      onClear={onClear}
      clearLabel="Clear session filters"
      specs={specs}
      sections={sections}
      searchLabel="Session filter text"
      searchPlaceholder="narrow this session…"
      counter={{ shown, total, visible: counterVisible }}
      degraded={degraded}
      showCoverage={dimensions.length > 0}
    />
  )
}
