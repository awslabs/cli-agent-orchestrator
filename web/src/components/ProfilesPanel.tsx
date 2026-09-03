import { useEffect, useMemo, useRef, useState } from 'react'
import { api, AgentProfileInfo, AgentProfileDetail, ProfileSearchResult, ProfileValidationMessage } from '../api'
import { Package, Plus, Search, AlertTriangle, Copy, CopyPlus, FilePen, Tag, Trash2, Wrench, Loader2 } from 'lucide-react'
import { ProfileCreateModal } from './ProfileCreateModal'
import { ProfileEditorModal } from './ProfileEditorModal'
import { ConfirmModal } from './ConfirmModal'
import { useStore } from '../store'
import { useGeneration } from '../hooks/useGeneration'

/**
 * Debounce interval for the search box. The contract from #510 is >= 300 ms so
 * a keystroke burst produces at most one `GET /agents/profiles/search` call.
 * Exported so the test can assert the constant rather than duplicate it.
 */
export const SEARCH_DEBOUNCE_MS = 300

/**
 * A list row is either a full-catalog entry (from `GET /agents/profiles`) or a
 * search hit (from `GET /agents/profiles/search`). Both carry name,
 * description and source; only the catalog row carries `duplicated_in`, and
 * only the search row carries tags/capabilities inline.
 */
type ProfileRow = AgentProfileInfo | ProfileSearchResult

function isSearchRow(row: ProfileRow): row is ProfileSearchResult {
  return 'score' in row
}

/** Badge colour per profile source. Local is the only writable store. */
function sourceBadgeClass(source: string): string {
  if (source === 'local') return 'bg-emerald-900/60 text-emerald-300 border-emerald-700/50'
  if (source === 'built-in') return 'bg-blue-900/60 text-blue-300 border-blue-700/50'
  if (source === 'custom') return 'bg-purple-900/60 text-purple-300 border-purple-700/50'
  return 'bg-gray-800 text-gray-400 border-gray-700'
}

function SourceBadge({ source }: { source: string }) {
  return (
    <span className={`px-1.5 py-0.5 text-[10px] font-medium rounded border ${sourceBadgeClass(source)}`}>
      {source}
    </span>
  )
}

function ChipList({ label, icon, items }: { label: string; icon: React.ReactNode; items: string[] }) {
  if (!items || items.length === 0) return null
  return (
    <div>
      <div className="flex items-center gap-1.5 text-xs text-gray-500 uppercase tracking-wide mb-1.5">
        {icon}
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map(item => (
          <span key={item} className="px-2 py-0.5 text-xs rounded-full bg-gray-800 border border-gray-700 text-gray-300">
            {item}
          </span>
        ))}
      </div>
    </div>
  )
}

function DetailField({ label, value }: { label: string; value: string | undefined }) {
  if (!value) return null
  return (
    <div>
      <div className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">{label}</div>
      <div className="text-sm text-gray-200 font-mono">{value}</div>
    </div>
  )
}

/**
 * Detail pane for the selected profile. Merges the list row (source,
 * duplicated_in — catalog metadata the parsed endpoint does not return) with
 * the parsed profile from `GET /agents/profiles/{name}` (provider, model,
 * tags, capabilities).
 */
function ProfileDetail({
  row,
  duplicatedIn,
  onEdit,
  onClone,
  onDelete,
}: {
  row: ProfileRow
  /** Shadowing metadata from the CATALOG row: search results do not carry
      duplicated_in, so the panel resolves it by name for both row types
      (#692 review: the amber banner was invisible when reached via search). */
  duplicatedIn: string[] | undefined
  onEdit: (name: string) => void
  onClone: (name: string) => void
  onDelete: (name: string) => void
}) {
  const [detail, setDetail] = useState<AgentProfileDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setDetail(null)
    api.getProfile(row.name)
      .then(d => { if (!cancelled) setDetail(d) })
      .catch(e => { if (!cancelled) setError(e?.detail || e?.message || 'Failed to load profile') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [row.name])

  return (
    <div className="p-5 space-y-5" data-testid="profile-detail">
      <div>
        <div className="flex items-center gap-2.5 mb-1">
          <h3 className="text-lg font-bold text-white font-mono">{row.name}</h3>
          <SourceBadge source={row.source} />
          <div className="flex-1" />
          {/* Only the local store is writable: PUT and DELETE resolve profile
              names there alone, so a built-in/provider/custom profile gets
              "clone to customise" instead of edit/delete. */}
          {row.source === 'local' ? (
            <>
              <button
                onClick={() => onEdit(row.name)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
              >
                <FilePen size={12} /> Edit
              </button>
              <button
                onClick={() => onClone(row.name)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
              >
                <CopyPlus size={12} /> Clone
              </button>
              <button
                onClick={() => onDelete(row.name)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-red-300 bg-red-900/30 hover:bg-red-900/50 rounded-lg transition-colors"
              >
                <Trash2 size={12} /> Delete
              </button>
            </>
          ) : (
            <button
              onClick={() => onClone(row.name)}
              title="This profile is read-only; create an editable copy in the local store."
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-gray-300 bg-gray-800 hover:bg-gray-700 rounded-lg transition-colors"
            >
              <CopyPlus size={12} /> Clone to customise
            </button>
          )}
        </div>
        {row.description && <p className="text-sm text-gray-400">{row.description}</p>}
      </div>

      {duplicatedIn && duplicatedIn.length > 0 && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-900/20 border border-amber-700/40 text-amber-300 text-xs">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Also defined in: {duplicatedIn.join(', ')}. The <span className="font-mono">{row.source}</span> copy is what loads.
          </span>
        </div>
      )}

      {loading && (
        <div className="flex items-center gap-2 text-sm text-gray-500" data-testid="detail-loading">
          <Loader2 size={14} className="animate-spin" /> Loading profile…
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-red-900/20 border border-red-700/40 text-red-300 text-xs" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {detail && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <DetailField label="Provider" value={detail.provider} />
            <DetailField label="Model" value={detail.model} />
            <DetailField label="Role" value={detail.role} />
          </div>
          <ChipList label="Tags" icon={<Tag size={12} />} items={detail.tags ?? []} />
          <ChipList label="Capabilities" icon={<Wrench size={12} />} items={detail.capabilities ?? []} />
        </>
      )}
    </div>
  )
}

export function ProfilesPanel() {
  // Full catalog, fetched once on mount — no polling. Profiles change through
  // this panel's own actions or externally rarely; a manual re-mount refreshes.
  const [catalog, setCatalog] = useState<AgentProfileInfo[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [catalogError, setCatalogError] = useState<string | null>(null)

  const [query, setQuery] = useState('')
  // Search results are kept separate from the catalog: the server's ranked
  // order is the contract and must never be re-sorted client-side.
  const [results, setResults] = useState<ProfileSearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  const [selected, setSelected] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editor, setEditor] = useState<{ mode: 'edit' | 'clone'; name: string } | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Monotonic token so a slow earlier response can never clobber a newer one.
  const searchGen = useGeneration()
  // User-navigation generation: bumped by every user-initiated search
  // keystroke and row selection. The post-save continuation captures it
  // before its catalog refresh and yields if it moved -- navigation
  // performed while the refresh was pending wins over the older
  // clear-and-select (round-5 P2).
  const navGen = useGeneration()
  const { showSnackbar } = useStore()

  // Bumped after an in-place edit: remounts ProfileDetail (via its key) so
  // the parsed-profile fetch re-runs even though the selected NAME is
  // unchanged (#692 review: stale provider/model/tags after save).
  const [detailReload, setDetailReload] = useState(0)
  // Monotonic catalog-load token: without it, a slow mount request resolving
  // AFTER a post-write refresh overwrote the fresh catalog with its stale
  // snapshot, deleting the just-created row (round-4 review).
  const catalogGen = useGeneration()

  /** Refresh the catalog; resolves true only when THIS load's result was
      applied (stale/discarded and failed loads resolve false). */
  const refreshCatalog = () => {
    const token = catalogGen.begin()
    return api.listProfiles()
      .then(p => {
        if (!token.isCurrent()) return false
        setCatalog(p)
        setCatalogError(null)
        return true
      })
      .catch(e => {
        if (!token.isCurrent()) return false
        setCatalogError(e?.detail || e?.message || 'Failed to load profiles')
        return false
      })
      .finally(() => { if (token.isCurrent()) setCatalogLoading(false) })
  }

  // After a successful write, the ACTIVE search's rows are stale (they never
  // carry the new/renamed profile), so selecting the new name rendered
  // "Select a profile…" under a success snackbar (round-4 review). Clear the
  // search and select only once the refreshed catalog is the visible row set;
  // a failed refresh stays a failure instead of continuing to selection.
  const clearSearch = () => {
    searchGen.invalidate()
    setQuery('')
    setResults(null)
    setSearchError(null)
    setSearching(false)
  }

  useEffect(() => { refreshCatalog() }, [])

  const handleSaved = async (name: string, warnings: ProfileValidationMessage[]) => {
    showSnackbar(
      warnings.length > 0
        ? { type: 'info', message: `Profile '${name}' saved with ${warnings.length} warning${warnings.length !== 1 ? 's' : ''}: ${warnings[0].message}` }
        : { type: 'success', message: `Profile '${name}' saved` },
    )
    // Clear mutation-time state BEFORE the refresh (stale filtered rows
    // never carry the new/renamed profile), then select only after the
    // refresh resolves AND succeeded -- and only if the user has not
    // navigated in the meantime (round-5 P2: the modal closes without
    // awaiting this continuation, so the panel is interactive while the
    // refresh is pending).
    const nav = navGen.begin()
    clearSearch()
    if (!(await refreshCatalog())) return
    if (!nav.isCurrent()) return
    setSelected(name)
    // The detail fetch is keyed on the profile NAME, which an in-place edit
    // does not change -- without an explicit reload token the pane kept
    // showing pre-edit provider/model/tags after a successful save.
    setDetailReload(n => n + 1)
  }

  const handleDelete = async () => {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await api.deleteProfile(pendingDelete)
      showSnackbar({ type: 'success', message: `Deleted '${pendingDelete}'` })
      if (selected === pendingDelete) setSelected(null)
      // Remove from BOTH lists immediately. The visible list is
      // `results ?? catalog`, and refreshCatalog() only replaces the catalog —
      // an active search's stale results would keep showing the deleted row.
      // The follow-up refresh is still wanted: deleting a local profile that
      // shadowed a same-named one in another directory legitimately re-adds
      // the name under its new winning source.
      const name = pendingDelete
      setCatalog(c => c.filter(r => r.name !== name))
      setResults(r => (r ? r.filter(x => x.name !== name) : r))
      setPendingDelete(null)
      refreshCatalog()
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e?.detail || e?.message || 'Failed to delete profile' })
    } finally {
      setDeleting(false)
    }
  }

  const handleCreated = async (name: string, warnings: ProfileValidationMessage[]) => {
    // Surface post-save advisories (the backend allowed the write; these are
    // warning-severity findings a user should still see).
    showSnackbar(
      warnings.length > 0
        ? { type: 'info', message: `Profile '${name}' created with ${warnings.length} warning${warnings.length !== 1 ? 's' : ''}: ${warnings[0].message}` }
        : { type: 'success', message: `Profile '${name}' created` },
    )
    // Same shape as handleSaved: clear mutation-time state up front, select
    // only after a SUCCESSFUL refetch (round-4 review), and yield to any
    // navigation the user performed while the refresh was pending
    // (round-5 P2).
    const nav = navGen.begin()
    clearSearch()
    if (!(await refreshCatalog())) return
    if (!nav.isCurrent()) return
    setSelected(name)
  }

  // Debounced server search: one request per >=300ms-quiet keystroke burst.
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    const q = query.trim()
    // Advance the generation IMMEDIATELY on any query change -- not when the
    // debounced request is eventually issued (same rationale as the preview
    // generation, round-4 P1). Without the early bump, a response for prior
    // query A resolving during query B's 300ms debounce window still matched
    // the sequence and installed A's rows under B (round-5 P2).
    const token = searchGen.begin()
    if (q === '') {
      setResults(null)
      setSearching(false)
      setSearchError(null)
      return
    }
    setSearching(true)
    debounceRef.current = setTimeout(() => {
      api.searchProfiles(q)
        .then(r => {
          if (!token.isCurrent()) return
          setResults(r)
          setSearchError(null)
        })
        .catch(e => {
          if (!token.isCurrent()) return
          setSearchError(e?.detail || e?.message || 'Search failed')
        })
        .finally(() => {
          if (token.isCurrent()) setSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [query])

  // Server order preserved in both modes: catalog as listed, results as ranked.
  const rows: ProfileRow[] = results ?? catalog

  const selectedRow = useMemo(
    () => rows.find(r => r.name === selected) ?? null,
    [rows, selected],
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Package size={20} className="text-blue-400" />
          <h2 className="text-lg font-bold text-white">Profiles</h2>
          <span className="text-xs text-gray-500">
            {results !== null ? `${results.length} match${results.length !== 1 ? 'es' : ''}` : `${catalog.length} profile${catalog.length !== 1 ? 's' : ''}`}
          </span>
        </div>
        <button
          onClick={() => setCreateOpen(true)}
          className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium px-3 py-2 rounded-lg transition-colors"
        >
          <Plus size={14} /> New profile
        </button>
      </div>

      {/* Search */}
      <div className="relative max-w-md">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="text"
          role="searchbox"
          aria-label="Search profiles"
          placeholder="Search by capability, e.g. 'monitor sqs'…"
          value={query}
          onChange={e => { navGen.invalidate(); setQuery(e.target.value) }}
          className="w-full pl-9 pr-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600"
        />
        {searching && <Loader2 size={14} className="animate-spin absolute right-3 top-1/2 -translate-y-1/2 text-gray-500" data-testid="search-spinner" />}
      </div>

      {searchError && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-red-900/20 border border-red-700/40 text-red-300 text-xs" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{searchError}</span>
        </div>
      )}

      {catalogError && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-red-900/20 border border-red-700/40 text-red-300 text-xs" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{catalogError}</span>
        </div>
      )}

      {catalogLoading ? (
        <div className="flex items-center gap-2 text-sm text-gray-500 py-12 justify-center" data-testid="catalog-loading">
          <Loader2 size={16} className="animate-spin" /> Loading profiles…
        </div>
      ) : (
        <div className="grid grid-cols-[minmax(260px,1fr)_2fr] gap-4 items-start">
          {/* List pane */}
          <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden" role="listbox" aria-label="Profile list">
            {rows.length === 0 ? (
              <div className="p-6 text-sm text-gray-500 text-center">
                {results !== null ? 'No profiles match this search.' : 'No profiles found.'}
              </div>
            ) : (
              <ul className="divide-y divide-gray-800 max-h-[65vh] overflow-y-auto">
                {rows.map(row => (
                  <li key={row.name}>
                    <button
                      role="option"
                      aria-selected={selected === row.name}
                      onClick={() => { navGen.invalidate(); setSelected(row.name) }}
                      className={`w-full text-left px-4 py-3 transition-colors ${
                        selected === row.name ? 'bg-emerald-900/30' : 'hover:bg-gray-800/60'
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-gray-200 font-mono truncate">{row.name}</span>
                        <SourceBadge source={row.source} />
                        {!isSearchRow(row) && row.duplicated_in && row.duplicated_in.length > 0 && (
                          <span title={`Also defined in: ${row.duplicated_in.join(', ')}`}>
                            <Copy size={12} className="text-amber-400 shrink-0" />
                          </span>
                        )}
                      </div>
                      {row.description && (
                        <div className="text-xs text-gray-500 truncate mt-0.5">{row.description}</div>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Detail pane */}
          <div className="bg-gray-900/60 border border-gray-800 rounded-xl min-h-[200px]">
            {selectedRow ? (
              <ProfileDetail
                key={`${selectedRow.name}:${detailReload}`}
                row={selectedRow}
                duplicatedIn={catalog.find(c => c.name === selectedRow.name)?.duplicated_in}
                onEdit={name => setEditor({ mode: 'edit', name })}
                onClone={name => setEditor({ mode: 'clone', name })}
                onDelete={name => setPendingDelete(name)}
              />
            ) : (
              <div className="p-6 text-sm text-gray-500 text-center py-16">
                Select a profile to see its details.
              </div>
            )}
          </div>
        </div>
      )}

      <ProfileCreateModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={handleCreated} />
      {editor && (
        <ProfileEditorModal
          open={true}
          mode={editor.mode}
          name={editor.name}
          onClose={() => setEditor(null)}
          onSaved={handleSaved}
        />
      )}
      <ConfirmModal
        open={pendingDelete !== null}
        title="Delete profile"
        message="This removes the profile document from the local store. Running agents are unaffected; the document cannot be recovered from the UI."
        details={pendingDelete ? [{ label: 'Profile', value: pendingDelete }] : []}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        confirmationText={pendingDelete ?? undefined}
        onConfirm={handleDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}
