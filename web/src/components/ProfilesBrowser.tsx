import { useState, useEffect, useRef, useCallback } from 'react'
import {
  api,
  AgentProfileInfo,
  AgentProfileDetail,
  ProfileSearchResult,
  ValidateProfileResult,
  ApiError,
} from '../api'
import { Search, X, Package, FolderOpen, ShieldAlert, CheckCircle, AlertTriangle, XCircle, Eye, Plus, Copy, Pencil } from 'lucide-react'
import { CreateProfileWizard } from './CreateProfileWizard'
import { EditCloneModal } from './EditCloneModal'

// #510 U2 phase-1 frontend: the Profiles browser. Search + grouped list +
// unloadable treatment + detail/validate panel. Holds NO ranking/validation
// logic — every result comes from a U1 server endpoint and is rendered verbatim
// (search order is never re-sorted client-side, not even the name tie-break;
// the error/warn split is the server's). Rendered inside AgentPanel (which is
// NOT renamed). The Create wizard (U3) and Edit/Clone modal (U4) are wired in
// here as their seams landed alongside U2 in the same worktree; the U2
// deliverable is search + list + detail/validate, and those actions are U3/U4.

const SOURCE_LABELS: Record<string, string> = {
  'built-in': 'Built-in',
  local: 'Local',
  kiro: 'Kiro',
  claude_code: 'Claude Code',
  codex: 'Codex',
  installed: 'Installed',
  custom: 'Custom',
}

const SEARCH_DEBOUNCE_MS = 250

function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] || source
}

/** True only for a profile explicitly marked unloadable by the server. */
function isUnloadable(p: { loadable?: boolean }): boolean {
  return p.loadable === false
}

// ── Unloadable badge (OQ2 client-side treatment; AC1.4-list) ────────────────
function UnloadableBadge() {
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-300 border border-amber-700/40"
      title="This profile failed to load (parse or schema error), so it cannot be used. View only."
    >
      <ShieldAlert size={10} />
      unloadable
    </span>
  )
}

// ── Score indicator (relative rank, NOT a percentage; AC1.5) ────────────────
function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-300"
      title="Relative rank indicator within these results (higher = more relevant). Not an absolute percentage."
    >
      {score.toFixed(2)}
    </span>
  )
}

// ── Validate panel [FR2] ────────────────────────────────────────────────────
function ValidatePanel({ result }: { result: ValidateProfileResult }) {
  if (result.valid && result.warnings.length === 0) {
    return (
      <div className="flex items-center gap-2 text-xs text-emerald-300" role="status">
        <CheckCircle size={14} />
        Valid — no issues.
      </div>
    )
  }
  return (
    <div className="space-y-2">
      {result.errors.length > 0 && (
        <div role="alert" className="space-y-1">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-red-300">
            <XCircle size={13} />
            {result.errors.length} error{result.errors.length !== 1 ? 's' : ''} — save blocked
          </div>
          <ul className="pl-5 space-y-0.5 list-disc marker:text-red-500">
            {result.errors.map((e, i) => (
              <li key={i} className="text-[11px] font-mono text-red-200 break-words">
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.warnings.length > 0 && (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-amber-300">
            <AlertTriangle size={13} />
            {result.warnings.length} warning{result.warnings.length !== 1 ? 's' : ''} — save allowed
          </div>
          <ul className="pl-5 space-y-0.5 list-disc marker:text-amber-500">
            {result.warnings.map((w, i) => (
              <li key={i} className="text-[11px] font-mono text-amber-200 break-words">
                {w}
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.valid && (
        <p className="text-[11px] text-gray-500">
          Warnings do not block saving (matches the CLI's "exit 1 only on errors").
        </p>
      )}
    </div>
  )
}

// ── Profile detail + validate [FR2] ─────────────────────────────────────────
function ProfileDetail({ name, source, loadable, onClose, onEdit, onClone }: {
  name: string
  source: string
  loadable: boolean
  onClose: () => void
  onEdit: (name: string) => void
  onClone: (name: string) => void
}) {
  const [profile, setProfile] = useState<AgentProfileDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [validation, setValidation] = useState<ValidateProfileResult | null>(null)
  const [validating, setValidating] = useState(false)
  const [validateError, setValidateError] = useState<string | null>(null)

  const isBuiltIn = source === 'built-in'

  useEffect(() => {
    let cancelled = false
    setProfile(null)
    setLoadError(null)
    setValidation(null)
    setValidateError(null)
    api.getProfile(name)
      .then(p => { if (!cancelled) setProfile(p) })
      .catch((e: ApiError) => { if (!cancelled) setLoadError(e.detail || e.message) })
    return () => { cancelled = true }
  }, [name])

  const handleValidate = async () => {
    if (!profile) return
    setValidating(true)
    setValidateError(null)
    try {
      // Validate the parsed frontmatter metadata. system_prompt is the profile
      // body, not a frontmatter field, so it is stripped before sending.
      const { system_prompt, ...metadata } = profile
      const result = await api.validateProfile({ metadata })
      setValidation(result)
    } catch (e) {
      const err = e as ApiError
      setValidateError(err.detail || err.message)
    }
    setValidating(false)
  }

  return (
    <div className="bg-gray-900/70 border border-gray-700/50 rounded-lg p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2 min-w-0">
          {isBuiltIn ? <Package size={14} className="text-blue-400 shrink-0" /> : <FolderOpen size={14} className="text-emerald-400 shrink-0" />}
          <span className="text-sm font-medium text-gray-200 truncate">{name}</span>
          <span className="text-[10px] text-gray-500">{sourceLabel(source)}</span>
          {!loadable && <UnloadableBadge />}
        </div>
        <button
          onClick={onClose}
          className="p-1 text-gray-500 hover:text-gray-300 rounded hover:bg-gray-800/50"
          aria-label="Close profile detail"
        >
          <X size={16} />
        </button>
      </div>

      {loadError ? (
        <div role="alert" className="text-xs text-red-300">Failed to load profile: {loadError}</div>
      ) : !profile ? (
        <div className="text-xs text-gray-500">Loading profile…</div>
      ) : (
        <>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
            {profile.description && (
              <>
                <dt className="text-gray-500">Description</dt>
                <dd className="text-gray-300">{profile.description}</dd>
              </>
            )}
            {profile.role && (
              <>
                <dt className="text-gray-500">Role</dt>
                <dd className="text-gray-300">{profile.role}</dd>
              </>
            )}
            {profile.provider && (
              <>
                <dt className="text-gray-500">Provider</dt>
                <dd className="text-gray-300 font-mono">{profile.provider}</dd>
              </>
            )}
            {profile.model && (
              <>
                <dt className="text-gray-500">Model</dt>
                <dd className="text-gray-300 font-mono">{profile.model}</dd>
              </>
            )}
          </dl>

          <div className="flex items-center gap-2 pt-1 flex-wrap">
            <button
              onClick={handleValidate}
              disabled={validating}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white text-xs font-medium rounded-lg transition-colors"
            >
              <CheckCircle size={13} />
              {validating ? 'Validating…' : 'Validate'}
            </button>
            {isBuiltIn ? (
              <>
                {/* Built-ins are read-only: Clone-to-customize instead of Edit (FR6). */}
                <button
                  onClick={() => onClone(name)}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium rounded-lg transition-colors"
                  title="Create an editable local copy of this built-in. The built-in is never modified."
                >
                  <Copy size={13} />
                  Clone to customize
                </button>
                <span className="text-[11px] text-gray-500" aria-disabled="true">
                  Built-in · read-only
                </span>
              </>
            ) : loadable ? (
              // Local + loadable: Edit in place (FR5).
              <button
                onClick={() => onEdit(name)}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium rounded-lg transition-colors"
              >
                <Pencil size={13} />
                Edit
              </button>
            ) : (
              // Unloadable local profile: View-only, no Edit.
              <span className="text-[11px] text-amber-300" aria-disabled="true">
                Unloadable · view only
              </span>
            )}
          </div>

          {validateError && (
            <div role="alert" className="text-xs text-red-300">Validation failed: {validateError}</div>
          )}
          {validation && <ValidatePanel result={validation} />}
        </>
      )}
    </div>
  )
}

// ── Profile row ─────────────────────────────────────────────────────────────
function ProfileRow({ profile, score, onView, selected }: {
  profile: { name: string; description?: string; source: string; loadable?: boolean; tags?: string[] }
  score?: number
  onView: () => void
  selected: boolean
}) {
  const unloadable = isUnloadable(profile)
  return (
    <div
      data-testid={`profile-row-${profile.name}`}
      className={`flex items-center justify-between gap-3 px-3 py-2 rounded-lg border transition-colors ${
        selected
          ? 'bg-emerald-900/30 border-emerald-700/50'
          : 'bg-gray-900/50 border-gray-700/30 hover:bg-gray-800/70'
      }`}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-200 truncate">{profile.name}</span>
          {score !== undefined && <ScoreBadge score={score} />}
          {unloadable && <UnloadableBadge />}
        </div>
        {profile.description && (
          <div className="text-[11px] text-gray-500 truncate">{profile.description}</div>
        )}
      </div>
      <button
        onClick={onView}
        className="flex items-center gap-1.5 px-2.5 py-1 bg-gray-700 hover:bg-gray-600 text-white text-xs font-medium rounded-lg transition-colors shrink-0"
        title="View profile details"
      >
        <Eye size={12} />
        View
      </button>
    </div>
  )
}

export function ProfilesBrowser() {
  const [query, setQuery] = useState('')
  const [profiles, setProfiles] = useState<AgentProfileInfo[]>([])
  const [loadingList, setLoadingList] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [results, setResults] = useState<ProfileSearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [selected, setSelected] = useState<{ name: string; source: string; loadable: boolean } | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [editClone, setEditClone] = useState<{ mode: 'edit' | 'clone'; name: string } | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Load the include-all grouped list (existing endpoint, unchanged — OQ2:
  // GET /agents/profiles stays include-all; unloadable treatment is client-side).
  const loadProfiles = useCallback(() => {
    setLoadingList(true)
    api.listProfiles()
      .then(p => { setProfiles(p); setListError(null); setLoadingList(false) })
      .catch((e: ApiError) => { setListError(e.detail || e.message); setLoadingList(false) })
  }, [])

  useEffect(() => { loadProfiles() }, [loadProfiles])

  const runSearch = useCallback(async (q: string) => {
    const trimmed = q.trim()
    // Empty/whitespace: show the grouped list, do NOT call search (also guarded
    // server-side, which returns []). This is a UX short-circuit, not the source
    // of truth.
    if (!trimmed) {
      setResults(null)
      setSearchError(null)
      setSearching(false)
      return
    }
    setSearching(true)
    setSearchError(null)
    try {
      const res = await api.searchProfiles(trimmed)
      setResults(res) // rendered in SERVER ORDER — never re-sorted (coverage→BM25Plus→name)
    } catch (e) {
      const err = e as ApiError
      setSearchError(err.detail || err.message)
      setResults([])
    }
    setSearching(false)
  }, [])

  const handleQueryChange = (value: string) => {
    setQuery(value)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => runSearch(value), SEARCH_DEBOUNCE_MS)
  }

  const clearSearch = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    setQuery('')
    setResults(null)
    setSearchError(null)
    setSearching(false)
  }

  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current) }, [])

  // Grouped-by-source list (grouped mode), reusing AgentPanel's grouping idiom.
  const profilesBySource = profiles.reduce<Record<string, AgentProfileInfo[]>>((acc, p) => {
    const key = p.source || 'unknown'
    ;(acc[key] ??= []).push(p)
    return acc
  }, {})
  const sourceOrder = Object.keys(profilesBySource).sort()

  const isRanked = results !== null

  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Agent Profiles
        </h3>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500">{profiles.length} profile{profiles.length !== 1 ? 's' : ''}</span>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
          >
            <Plus size={14} />
            Create Profile
          </button>
        </div>
      </div>

      {/* Search box [FR1] */}
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="text"
          value={query}
          onChange={e => handleQueryChange(e.target.value)}
          placeholder="Search profiles by name, description, tags, capabilities…"
          aria-label="Search agent profiles"
          className="w-full bg-gray-900 border border-gray-700 text-gray-200 text-sm rounded-lg pl-9 pr-9 py-2 focus:border-emerald-500 focus:outline-none"
        />
        {query && (
          <button
            onClick={clearSearch}
            aria-label="Clear search"
            className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 text-gray-500 hover:text-gray-300 rounded"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {searchError && (
        <div role="alert" className="text-xs text-red-300">Search failed: {searchError}</div>
      )}

      {/* Selected profile detail [FR2] */}
      {selected && (
        <ProfileDetail
          name={selected.name}
          source={selected.source}
          loadable={selected.loadable}
          onClose={() => setSelected(null)}
          onEdit={(name) => setEditClone({ mode: 'edit', name })}
          onClone={(name) => setEditClone({ mode: 'clone', name })}
        />
      )}

      {/* Results (ranked, server order) or grouped list */}
      {isRanked ? (
        <div className="space-y-2">
          <div className="text-[11px] text-gray-500">
            {searching ? 'Searching…' : `${results!.length} result${results!.length !== 1 ? 's' : ''} for "${query.trim()}" (ranked)`}
          </div>
          {results!.length === 0 && !searching ? (
            <p className="text-gray-500 text-sm">No profiles matched.</p>
          ) : (
            <div className="space-y-1.5" data-testid="search-results">
              {results!.map(r => (
                <ProfileRow
                  key={`${r.source}-${r.name}`}
                  profile={r}
                  score={r.score}
                  selected={selected?.name === r.name}
                  onView={() => setSelected({ name: r.name, source: r.source, loadable: true })}
                />
              ))}
            </div>
          )}
        </div>
      ) : loadingList ? (
        <p className="text-gray-500 text-sm">Loading profiles…</p>
      ) : listError ? (
        <div role="alert" className="text-xs text-red-300">Failed to load profiles: {listError}</div>
      ) : profiles.length === 0 ? (
        <p className="text-gray-500 text-sm">No agent profiles found.</p>
      ) : (
        <div className="space-y-4" data-testid="grouped-list">
          {sourceOrder.map(source => (
            <div key={source} className="space-y-1.5">
              <div className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider">
                {sourceLabel(source)} ({profilesBySource[source].length})
              </div>
              {profilesBySource[source].map(p => (
                <ProfileRow
                  key={`${p.source}-${p.name}`}
                  profile={p}
                  selected={selected?.name === p.name}
                  onView={() => setSelected({ name: p.name, source: p.source, loadable: p.loadable !== false })}
                />
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Create-from-template wizard (#510 U3). On save, refresh the list. */}
      {showCreate && (
        <CreateProfileWizard
          onClose={(saved) => {
            setShowCreate(false)
            if (saved) loadProfiles()
          }}
        />
      )}

      {/* Edit (local) / Clone (built-in) modal (#510 U4). On save, refresh. */}
      {editClone && (
        <EditCloneModal
          mode={editClone.mode}
          sourceName={editClone.name}
          onClose={(saved) => {
            setEditClone(null)
            if (saved) {
              setSelected(null)
              loadProfiles()
            }
          }}
        />
      )}
    </div>
  )
}
