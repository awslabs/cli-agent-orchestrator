// The filter bars — PRESENTATIONAL ONLY.
//
// Every decision lives in lib/filters.ts (the one predicate, the shape-typed
// dimension discovery, the vocabularies). This file draws what it is handed:
// vocabulary lists, derived dimension groups, and the current FilterState. It
// is on the MODULES guard in test/annotations.test.tsx for the same reason the
// detail popover is: "if (key === ...)" is the natural way to write a filter
// row, and the guard is what stands between this file and a hard-coded facet
// dimension.
//
// EVERY CONTROL IS A REAL TARGET. The bars are operated on a 390×844 touch
// viewport, so every interactive element meets the WCAG 2.5.5 AAA 44×44 floor
// the workstate e2e measures — min-h-[44px] on pills, inputs and selects
// alike. The pill rows wrap (`flex-wrap`): at 390px the bar grows DOWN, never
// sideways.
//
// Two bars, one component set. The global bar (fleet-stable vocabulary) gates
// session visibility; the per-session bar (session-local vocabulary) narrows
// rows inside a surviving card and can never remove the card — see
// DashboardHome for the composition, here for the drawing.

import { useState } from 'react'
import { ChevronDown, X } from 'lucide-react'
import { SEMANTIC_ROLES } from '../lib/annotations'
import type {
  CallerOption,
  DimensionGroup,
  FacetDimension,
  FacetSelection,
  FilterState,
} from '../lib/filters'
import { emptyFacetSelection, isFilterActive, MAX_PILL_VALUES } from '../lib/filters'

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

/** Most agent-profile pills drawn before the "+N more" expander. At 21
 *  profiles the uncapped row was already a wall; the cap is on DRAWING, never
 *  on matching — a selected profile past the cap stays visible and applied. */
const MAX_VISIBLE_PROFILE_PILLS = 8

function toggleValue(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function patchFacet(filters: FilterState, key: string, patch: Partial<FacetSelection>): FilterState {
  const current = filters.facets[key] ?? emptyFacetSelection()
  return { ...filters, facets: { ...filters.facets, [key]: { ...current, ...patch } } }
}

/** The one pill. 44px tall, aria-pressed, emerald when on — the treatment the
 *  old agent-type row already wore. */
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
      className={`flex items-center gap-1.5 min-h-[44px] px-3 rounded-full border text-xs transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
        selected
          ? 'bg-emerald-900/40 border-emerald-500/50 text-emerald-300'
          : 'border-gray-700 text-gray-400 hover:text-gray-200'
      }`}
    >
      {children}
    </button>
  )
}

/** A dimension's caption plus its controls, wrapping as one row. */
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

/**
 * Multi-select over a value vocabulary. OR within the dimension; the selected
 * values stay on screen even past a cap, because a filter the operator cannot
 * see is a filter they cannot reason about.
 *
 * Pills at or under MAX_PILL_VALUES; a select-plus-selected-pills past it —
 * the pill-wall failure is exactly what the threshold exists to prevent, and
 * the select keeps the control a single line no matter how large the
 * vocabulary grows.
 */
function ValuePicker({
  values,
  selected,
  onToggle,
  ariaLabel,
}: {
  values: Array<{ value: string; label: string; count?: number }>
  selected: string[]
  onToggle: (value: string) => void
  ariaLabel: string
}) {
  if (values.length <= MAX_PILL_VALUES) {
    return (
      <>
        {values.map(v => (
          <Pill key={v.value} selected={selected.includes(v.value)} onClick={() => onToggle(v.value)}>
            {v.label}
            {v.count !== undefined && <span className="text-gray-400">{v.count}</span>}
          </Pill>
        ))}
      </>
    )
  }
  const labelFor = (value: string) => values.find(v => v.value === value)?.label ?? value
  return (
    <>
      <select
        aria-label={ariaLabel}
        value=""
        onChange={e => {
          if (e.target.value) onToggle(e.target.value)
        }}
        className="min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
      >
        <option value="" disabled>
          {ariaLabel}…
        </option>
        {values
          .filter(v => !selected.includes(v.value))
          .map(v => (
            <option key={v.value} value={v.value}>
              {v.label}
              {v.count !== undefined ? ` (${v.count})` : ''}
            </option>
          ))}
      </select>
      {selected.map(value => (
        <Pill key={value} selected onClick={() => onToggle(value)} label={`Remove ${labelFor(value)}`}>
          {labelFor(value)}
          <X size={12} aria-hidden />
        </Pill>
      ))}
    </>
  )
}

/** One derived facet dimension, drawn by the control type its values earned. */
function FacetControl({
  dim,
  sel,
  onPatch,
}: {
  dim: FacetDimension
  sel: FacetSelection
  onPatch: (patch: Partial<FacetSelection>) => void
}) {
  if (dim.control === 'pills' || dim.control === 'typeahead') {
    return (
      <ValuePicker
        values={dim.values.map(v => ({ value: v.value, label: v.value, count: v.rows }))}
        selected={sel.values}
        onToggle={value => onPatch({ values: toggleValue(sel.values, value) })}
        ariaLabel={dim.label}
      />
    )
  }
  if (dim.control === 'tri-state') {
    return (
      <>
        {(['any', 'true', 'false'] as const).map(option => (
          <Pill
            key={option}
            selected={sel.tri === option}
            onClick={() => onPatch({ tri: option })}
          >
            {option === 'any' ? 'Any' : option}
          </Pill>
        ))}
      </>
    )
  }
  if (dim.control === 'range') {
    return (
      <>
        <input
          type="datetime-local"
          aria-label={`${dim.label} from`}
          value={sel.from}
          onChange={e => onPatch({ from: e.target.value })}
          className="min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
        />
        <span className="text-[10px] text-gray-400">to</span>
        <input
          type="datetime-local"
          aria-label={`${dim.label} to`}
          value={sel.to}
          onChange={e => onPatch({ to: e.target.value })}
          className="min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
        />
      </>
    )
  }
  return (
    <input
      type="search"
      aria-label={`${dim.label} contains`}
      placeholder="contains…"
      value={sel.text}
      onChange={e => onPatch({ text: e.target.value })}
      className="min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
    />
  )
}

/**
 * The derived facet dimensions, under the producer's own provenance headings.
 * A key with no class stands alone under its humanised full key — the same
 * words the detail popover uses, so the two surfaces agree on what a facet is
 * called.
 */
function FacetGroups({
  groups,
  filters,
  onChange,
}: {
  groups: DimensionGroup[]
  filters: FilterState
  onChange: (next: FilterState) => void
}) {
  if (groups.length === 0) return null
  return (
    <>
      {groups.map(group => (
        <div key={group.heading ?? group.dimensions[0].key} className="space-y-1">
          {group.heading && (
            <p className="text-[10px] uppercase tracking-wide text-gray-400">{group.heading}</p>
          )}
          {group.dimensions.map(dim => (
            <FilterRow key={dim.key} label={dim.label}>
              <FacetControl
                dim={dim}
                sel={filters.facets[dim.key] ?? emptyFacetSelection()}
                onPatch={patch => onChange(patchFacet(filters, dim.key, patch))}
              />
            </FilterRow>
          ))}
        </div>
      ))}
    </>
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
export function CoverageNote({ degraded }: { degraded: boolean }) {
  if (!degraded) return null
  return (
    <p data-testid="filter-coverage-note" className="text-[10px] text-amber-300/90">
      Annotation data is partial or unverified — facet filters see only what arrived.
    </p>
  )
}

const inputClass =
  'min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none'

/**
 * The global bar: fleet-stable vocabulary. Reachability is deliberately NOT
 * here — it renders unconditionally in DashboardHome, outside the collapsible
 * region, because it predates the bar and its contract is pinned by three
 * test suites.
 */
export function GlobalFilterBar({
  filters,
  onChange,
  liveness,
  profiles,
  providers,
  sessions,
  groups,
  annotationsAvailable,
  degraded,
}: {
  filters: FilterState
  onChange: (next: FilterState) => void
  liveness: string[]
  profiles: string[]
  providers: string[]
  sessions: string[]
  groups: DimensionGroup[]
  annotationsAvailable: boolean
  degraded: boolean
}) {
  // A dimension with fewer than two options is not a filter. Vocabularies
  // arrive ungated so this is the one place the rule lives.
  const [profilesExpanded, setProfilesExpanded] = useState(false)
  const shownProfiles = profilesExpanded
    ? profiles
    : // Selected values pin themselves visible past the cap — see ValuePicker.
      [...profiles.filter(p => filters.profiles.includes(p)), ...profiles.filter(p => !filters.profiles.includes(p)).slice(0, MAX_VISIBLE_PROFILE_PILLS)]
  const hiddenProfiles = profiles.length - shownProfiles.length

  return (
    <div className="space-y-2">
      {liveness.length >= 2 && (
        <FilterRow label="Liveness">
          <ValuePicker
            values={liveness.map(v => ({ value: v, label: v }))}
            selected={filters.liveness}
            onToggle={v => onChange({ ...filters, liveness: toggleValue(filters.liveness, v) })}
            ariaLabel="Liveness"
          />
        </FilterRow>
      )}
      {profiles.length >= 2 && (
        <FilterRow label="Agent profile">
          <ValuePicker
            values={shownProfiles.map(v => ({ value: v, label: v }))}
            selected={filters.profiles}
            onToggle={v => onChange({ ...filters, profiles: toggleValue(filters.profiles, v) })}
            ariaLabel="Agent profile"
          />
          {hiddenProfiles > 0 && (
            <button
              type="button"
              onClick={() => setProfilesExpanded(true)}
              className="min-h-[44px] px-2 text-xs text-gray-400 hover:text-gray-200"
            >
              +{hiddenProfiles} more
            </button>
          )}
          {profilesExpanded && profiles.length > MAX_VISIBLE_PROFILE_PILLS && (
            <button
              type="button"
              onClick={() => setProfilesExpanded(false)}
              className="min-h-[44px] px-2 text-xs text-gray-400 hover:text-gray-200"
            >
              <ChevronDown size={12} className="inline" /> fewer
            </button>
          )}
        </FilterRow>
      )}
      {providers.length >= 2 && (
        <FilterRow label="Provider">
          <ValuePicker
            values={providers.map(v => ({ value: v, label: v }))}
            selected={filters.providers}
            onToggle={v => onChange({ ...filters, providers: toggleValue(filters.providers, v) })}
            ariaLabel="Provider"
          />
        </FilterRow>
      )}
      {sessions.length >= 2 && (
        <FilterRow label="Session">
          <ValuePicker
            values={sessions.map(v => ({ value: v, label: v }))}
            selected={filters.sessions}
            onToggle={v => onChange({ ...filters, sessions: toggleValue(filters.sessions, v) })}
            ariaLabel="Session"
          />
        </FilterRow>
      )}
      {annotationsAvailable && (
        <FilterRow label="Freshness">
          {(['any', 'fresh', 'stale'] as const).map(option => (
            <Pill
              key={option}
              selected={filters.freshness === option}
              onClick={() => onChange({ ...filters, freshness: option })}
            >
              {option === 'any' ? 'Any' : option}
            </Pill>
          ))}
        </FilterRow>
      )}
      {annotationsAvailable && (
        <FilterRow label="Chip colour">
          {SEMANTIC_ROLES.map(role => (
            <Pill
              key={role}
              selected={filters.roles.includes(role)}
              onClick={() => onChange({ ...filters, roles: toggleValue(filters.roles, role) })}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${ROLE_DOT[role]}`} />
              {role}
            </Pill>
          ))}
        </FilterRow>
      )}
      <FacetGroups groups={groups} filters={filters} onChange={onChange} />
      <FilterRow label="Filter text">
        <input
          type="search"
          aria-label="Filter text"
          placeholder="id, name, profile, facet value…"
          value={filters.text}
          onChange={e => onChange({ ...filters, text: e.target.value })}
          className={`${inputClass} flex-1 min-w-[12rem]`}
        />
      </FilterRow>
      <CoverageNote degraded={degraded} />
    </div>
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
  groups,
  shown,
  total,
  counterVisible,
  degraded,
}: {
  filters: FilterState
  onChange: (next: FilterState) => void
  onClear: () => void
  callers: CallerOption[]
  groups: DimensionGroup[]
  shown: number
  total: number
  counterVisible: boolean
  degraded: boolean
}) {
  const sessionActive = isFilterActive(filters)
  return (
    <div data-testid="session-filter-bar" className="space-y-2 rounded-lg border border-gray-700/40 bg-gray-900/40 p-2">
      {(counterVisible || sessionActive) && (
        <div className="flex items-center justify-between gap-2 flex-wrap">
          {counterVisible && (
            <span data-testid="session-filter-count" className="text-[10px] text-gray-400">
              {shown} of {total} shown
            </span>
          )}
          {sessionActive && (
            <button
              type="button"
              onClick={onClear}
              className="min-h-[44px] px-3 rounded-lg border border-gray-700 text-xs text-gray-300 hover:text-white hover:bg-gray-800 transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500"
            >
              Clear session filters
            </button>
          )}
        </div>
      )}
      {callers.length > 0 && (
        <FilterRow label="Spawned by">
          <ValuePicker
            values={callers.map(c => ({ value: c.id, label: c.label }))}
            selected={filters.callers}
            onToggle={v => onChange({ ...filters, callers: toggleValue(filters.callers, v) })}
            ariaLabel="Spawned by"
          />
        </FilterRow>
      )}
      <FacetGroups groups={groups} filters={filters} onChange={onChange} />
      <FilterRow label="Filter text">
        <input
          type="search"
          aria-label="Session filter text"
          placeholder="narrow this session…"
          value={filters.text}
          onChange={e => onChange({ ...filters, text: e.target.value })}
          className={`${inputClass} flex-1 min-w-[10rem]`}
        />
      </FilterRow>
      {groups.length > 0 && <CoverageNote degraded={degraded} />}
    </div>
  )
}
