import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  api, ApiError, TrackerProject, TrackerIssue, TrackerIssuePage, TrackerVocabulary, TrackerScope,
} from '../api'
import { useStore } from '../store'
import { ConfirmModal } from './ConfirmModal'
import {
  FolderGit2, Plus, Search, Trash2, X, Loader2, Archive, ChevronRight, MessageSquare,
  History, Link2, Save, FileDown, CircleDot, CheckCircle2, Lightbulb,
} from 'lucide-react'

/**
 * Projects + issue tracker.
 *
 * A project here is a declared grouping — one name over many directories,
 * sessions and git remotes — so the header leads with its SCOPES rather than
 * with a single path. Getting a scope wrong is how issues end up filed in the
 * wrong log, and the only way to notice is to see the whole set at once.
 *
 * Every enumeration (statuses, severities, scope kinds, link kinds) is fetched
 * from /tracker/vocabulary rather than hard-coded, so a dropdown can never
 * offer a value the server will reject.
 */

const SEVERITY_CLASS: Record<string, string> = {
  P0: 'bg-red-500/15 text-red-300 border-red-500/30',
  P1: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  P2: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  P3: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  P4: 'bg-slate-500/15 text-slate-300 border-slate-500/30',
  unset: 'bg-gray-700/40 text-gray-400 border-gray-600/40',
}

const STATUS_CLASS: Record<string, string> = {
  open: 'bg-emerald-500/15 text-emerald-300',
  triage: 'bg-violet-500/15 text-violet-300',
  'in-progress': 'bg-blue-500/15 text-blue-300',
  blocked: 'bg-red-500/15 text-red-300',
  resolved: 'bg-teal-500/15 text-teal-300',
  closed: 'bg-gray-600/30 text-gray-400',
  wontfix: 'bg-gray-600/30 text-gray-400',
  duplicate: 'bg-gray-600/30 text-gray-400',
}

const KIND_CLASS: Record<string, string> = {
  issue: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  feature: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
}

const PAGE_SIZE = 50

const FEATURE_BODY_STARTER = `## Problem / opportunity

## Desired outcome

## Acceptance criteria

## Constraints / alternatives
`

function errorText(err: unknown): string {
  const api = err as ApiError
  return api?.detail || api?.message || String(err)
}

function shortDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 10)
}

function Pill({ text, className }: { text: string; className: string }) {
  return <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium border border-transparent ${className}`}>{text}</span>
}

// ---------------------------------------------------------------------------
// Kind presentation descriptor — the single place that decides how the two
// kinds differ in copy and which fields are shown. Every component reads it
// rather than branching on string literals inline, so the 600-line JSX is not
// duplicated for the second kind.

type ItemKind = 'issue' | 'feature'
type TabKind = 'issue' | 'feature' | 'all'

interface KindPresentation {
  kind: ItemKind
  createButtonLabel: string
  modalTitle: (projectName: string) => string
  createActionLabel: string
  severityLabel: string
  reporterLabel: string
  assigneeLabel: string
  resolutionLabel: string
  bodyLabel: string
  bodyStarter: string
  showFailingCommandInCreate: boolean
  closingOptions: Array<{ uiLabel: string; status: string; needsDuplicateOf?: boolean }>
}

const KIND_PRESENTATION: Record<ItemKind, KindPresentation> = {
  issue: {
    kind: 'issue',
    createButtonLabel: 'Log issue',
    modalTitle: (name: string) => `Log an issue against ${name}`,
    createActionLabel: 'File issue',
    severityLabel: 'Severity',
    reporterLabel: 'Reporter',
    assigneeLabel: 'Assignee',
    resolutionLabel: 'Resolution',
    bodyLabel: 'What happened',
    bodyStarter: '',
    showFailingCommandInCreate: true,
    closingOptions: [],
  },
  feature: {
    kind: 'feature',
    createButtonLabel: 'Request feature',
    modalTitle: (name: string) => `Request a feature for ${name}`,
    createActionLabel: 'Request feature',
    severityLabel: 'Priority',
    reporterLabel: 'Requester',
    assigneeLabel: 'Owner',
    resolutionLabel: 'Outcome',
    bodyLabel: 'Proposal',
    bodyStarter: FEATURE_BODY_STARTER,
    showFailingCommandInCreate: false,
    closingOptions: [
      { uiLabel: 'Shipped', status: 'closed' },
      { uiLabel: 'Declined', status: 'wontfix' },
      { uiLabel: 'Withdrawn', status: 'wontfix' },
      { uiLabel: 'Duplicate', status: 'duplicate', needsDuplicateOf: true },
    ],
  },
}

function presentationFor(kind: string | undefined): KindPresentation {
  return KIND_PRESENTATION[(kind as ItemKind) === 'feature' ? 'feature' : 'issue']
}

// ---------------------------------------------------------------------------
// Per-tab filter state — search/open-only/filters/pagination are independent
// per tab as D6 requires. Changing tab does not leak the previous tab's query.

interface TabFilters {
  query: string
  statusFilter: string[]
  severityFilter: string[]
  openOnly: boolean
  offset: number
}

function defaultTabFilters(): TabFilters {
  return { query: '', statusFilter: [], severityFilter: [], openOnly: true, offset: 0 }
}

// ---------------------------------------------------------------------------

export function ProjectsPanel() {
  const { showSnackbar } = useStore()

  const [vocab, setVocab] = useState<TrackerVocabulary | null>(null)
  const [projects, setProjects] = useState<TrackerProject[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [project, setProject] = useState<TrackerProject | null>(null)
  const [loading, setLoading] = useState(true)

  const [page, setPage] = useState<TrackerIssuePage | null>(null)
  const [issuesLoading, setIssuesLoading] = useState(false)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  const [kind, setKind] = useState<TabKind>('issue')
  const [filtersByKind, setFiltersByKind] = useState<Record<TabKind, TabFilters>>({
    issue: defaultTabFilters(),
    feature: defaultTabFilters(),
    all: defaultTabFilters(),
  })

  const currentFilters = filtersByKind[kind]

  const updateCurrentFilters = useCallback((patch: Partial<TabFilters>) => {
    setFiltersByKind(prev => ({ ...prev, [kind]: { ...prev[kind], ...patch } }))
  }, [kind])

  // URL state: ?project=cao-system&kind=feature&key=cond-0342
  const urlSyncRef = useRef(false)

  // Initialize from URL on first load
  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search)
      const urlProject = params.get('project')
      const urlKind = params.get('kind') as TabKind | null
      const urlKey = params.get('key')
      if (urlKind && (['issue', 'feature', 'all'] as TabKind[]).includes(urlKind)) {
        setKind(urlKind)
      }
      if (urlKey) setSelectedKey(urlKey)
      // Defer project override until after projects load; store intent
      if (urlProject) {
        ;(window as unknown as { __caoInitialProject?: string }).__caoInitialProject = urlProject
      }
    } catch { /* non-browser test env */ }
  }, [])

  const [showNewProject, setShowNewProject] = useState(false)
  const [showNewIssue, setShowNewIssue] = useState(false)
  const [showScopes, setShowScopes] = useState(false)
  const [pendingArchive, setPendingArchive] = useState<TrackerProject | null>(null)

  const loadProjects = useCallback(async (preferId?: string) => {
    try {
      const rows = await api.listTrackerProjects(true)
      setProjects(rows)
      // Honor URL project if present
      const urlInitial = (window as unknown as { __caoInitialProject?: string }).__caoInitialProject
      const desired = preferId ?? urlInitial ?? null
      if (desired && rows.some(r => r.id === desired)) {
        setActiveId(desired)
        delete (window as unknown as { __caoInitialProject?: string }).__caoInitialProject
      } else {
        setActiveId(current => preferId ?? current ?? (rows.length ? rows[0].id : null))
      }
    } catch (err) {
      showSnackbar({ type: 'error', message: `Could not load projects: ${errorText(err)}` })
    } finally {
      setLoading(false)
    }
  }, [showSnackbar])

  useEffect(() => {
    api.getTrackerVocabulary().then(setVocab).catch(() => {})
    loadProjects()
  }, [loadProjects])

  useEffect(() => {
    if (!activeId) { setProject(null); return }
    api.getTrackerProject(activeId).then(setProject).catch(() => setProject(null))
  }, [activeId])

  const loadIssues = useCallback(async () => {
    if (!activeId) { setPage(null); return }
    setIssuesLoading(true)
    const f = filtersByKind[kind]
    try {
      let result
      if (kind === 'feature') {
        result = await api.listTrackerFeatures({
          projectId: activeId,
          q: f.query.trim() || undefined,
          status: f.statusFilter.length ? f.statusFilter : undefined,
          severity: f.severityFilter.length ? f.severityFilter : undefined,
          openOnly: f.openOnly,
          limit: PAGE_SIZE,
          offset: f.offset,
          order: 'severity',
        })
      } else if (kind === 'all') {
        result = await api.listTrackerIssues({
          projectId: activeId,
          q: f.query.trim() || undefined,
          status: f.statusFilter.length ? f.statusFilter : undefined,
          severity: f.severityFilter.length ? f.severityFilter : undefined,
          openOnly: f.openOnly,
          limit: PAGE_SIZE,
          offset: f.offset,
          order: 'severity',
          kind: 'all',
        })
      } else {
        result = await api.listTrackerIssues({
          projectId: activeId,
          q: f.query.trim() || undefined,
          status: f.statusFilter.length ? f.statusFilter : undefined,
          severity: f.severityFilter.length ? f.severityFilter : undefined,
          openOnly: f.openOnly,
          limit: PAGE_SIZE,
          offset: f.offset,
          order: 'severity',
        })
      }
      setPage(result)
    } catch (err) {
      showSnackbar({ type: 'error', message: `Could not load issues: ${errorText(err)}` })
      setPage(null)
    } finally {
      setIssuesLoading(false)
    }
  }, [activeId, filtersByKind, kind, showSnackbar])

  useEffect(() => { loadIssues() }, [loadIssues])

  // Sync URL on project/kind/key changes — pushState so Back/Forward traverses history
  const lastPushedUrlRef = useRef<string | null>(null)
  useEffect(() => {
    if (!urlSyncRef.current) {
      // Skip first render until projects have loaded, to avoid clobbering incoming URL
      urlSyncRef.current = true
      try {
        lastPushedUrlRef.current = window.location.pathname + window.location.search
      } catch { /* test env */ }
      return
    }
    try {
      const params = new URLSearchParams(window.location.search)
      if (activeId) params.set('project', activeId)
      else params.delete('project')
      params.set('kind', kind)
      if (selectedKey) params.set('key', selectedKey)
      else params.delete('key')
      const newSearch = params.toString()
      const newUrl = `${window.location.pathname}${newSearch ? '?' + newSearch : ''}`
      if (newUrl === lastPushedUrlRef.current) return
      window.history.pushState(null, '', newUrl)
      lastPushedUrlRef.current = newUrl
    } catch { /* test env */ }
  }, [activeId, kind, selectedKey])

  // Cleanup stale key on unmount so next test starts collapsed
  useEffect(() => {
    return () => {
      try {
        const params = new URLSearchParams(window.location.search)
        params.delete('key')
        const newSearch = params.toString()
        window.history.replaceState(null, '', `${window.location.pathname}${newSearch ? '?' + newSearch : ''}`)
        delete (window as unknown as { __caoInitialProject?: string }).__caoInitialProject
      } catch { /* test env */ }
    }
  }, [])

  // Handle back/forward
  useEffect(() => {
    const handler = () => {
      try {
        const params = new URLSearchParams(window.location.search)
        const urlProject = params.get('project')
        const urlKind = params.get('kind') as TabKind | null
        const urlKey = params.get('key')
        if (urlProject) setActiveId(urlProject)
        if (urlKind && (['issue', 'feature', 'all'] as TabKind[]).includes(urlKind)) setKind(urlKind)
        setSelectedKey(urlKey)
      } catch { /* */ }
    }
    window.addEventListener('popstate', handler)
    return () => window.removeEventListener('popstate', handler)
  }, [])

  // Changing project resets all tab offsets/selection? Spec says changing project or tab resets offset and stale selection.
  // Tab offset is per-tab so tab change keeps other tab's offset; but selection always resets.
  // Project change should reset all offsets.
  const handleSelectProject = useCallback((id: string) => {
    setActiveId(id)
    setSelectedKey(null)
    setFiltersByKind(prev => ({
      issue: { ...prev.issue, offset: 0 },
      feature: { ...prev.feature, offset: 0 },
      all: { ...prev.all, offset: 0 },
    }))
  }, [])

  const handleSelectKind = useCallback((k: TabKind) => {
    setKind(k)
    setSelectedKey(null)
    // reset offset for that tab? Keep per-tab offset but ensure current page starts at 0 if we want; spec says changing tab resets offset
    setFiltersByKind(prev => ({ ...prev, [k]: { ...prev[k], offset: 0 } }))
  }, [])

  const refreshAfterIssueChange = useCallback(async () => {
    await loadIssues()
    if (activeId) api.getTrackerProject(activeId).then(setProject).catch(() => {})
    api.listTrackerProjects(true).then(setProjects).catch(() => {})
  }, [loadIssues, activeId])

  const toggle = useCallback((list: string[], value: string, set: (next: string[]) => void) =>
    set(list.includes(value) ? list.filter(v => v !== value) : [...list, value]), [])

  const updateStatusFilter = useCallback((value: string) => {
    const list = currentFilters.statusFilter
    const next = list.includes(value) ? list.filter(v => v !== value) : [...list, value]
    updateCurrentFilters({ statusFilter: next, offset: 0 })
  }, [currentFilters.statusFilter, updateCurrentFilters])

  const updateSeverityFilter = useCallback((value: string) => {
    const list = currentFilters.severityFilter
    const next = list.includes(value) ? list.filter(v => v !== value) : [...list, value]
    updateCurrentFilters({ severityFilter: next, offset: 0 })
  }, [currentFilters.severityFilter, updateCurrentFilters])

  if (loading) {
    return <div className="text-gray-500 text-sm py-12 text-center">Loading projects…</div>
  }

  const headerPresentation = presentationFor(kind === 'all' ? 'issue' : kind)

  return (
    <div className="flex gap-6 items-start">
      {/* Project rail */}
      <aside className="w-60 shrink-0 space-y-2">
        <button
          onClick={() => setShowNewProject(true)}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition-colors"
        >
          <Plus size={15} /> New project
        </button>
        {projects.length === 0 && (
          <p className="text-xs text-gray-500 px-1 py-3">
            No projects yet. A project groups any number of directories, tmux sessions and git
            remotes under one issue log.
          </p>
        )}
        {projects.map(p => {
          const byKind = p.counts?.by_kind
          const hasKindCounts = !!byKind
          return (
            <button
              key={p.id}
              onClick={() => handleSelectProject(p.id)}
              className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${
                activeId === p.id
                  ? 'bg-gray-800 border-emerald-600/50'
                  : 'bg-gray-900/50 border-gray-800 hover:border-gray-700'
              }`}
            >
              <div className="flex items-center gap-2">
                <FolderGit2 size={14} className={activeId === p.id ? 'text-emerald-400' : 'text-gray-500'} />
                <span className="text-sm text-gray-200 truncate">{p.name}</span>
                {p.status === 'archived' && <Archive size={12} className="text-gray-600 shrink-0" />}
              </div>
              <div className="text-[11px] text-gray-500 mt-0.5 pl-6">
                {hasKindCounts ? (
                  <>
                    <span title="Issues open">I {byKind!.issue?.open ?? 0}</span>
                    <span className="mx-1">·</span>
                    <span title="Features open">F {byKind!.feature?.open ?? 0}</span>
                    <span className="mx-1 text-gray-600">/</span>
                    <span className="text-gray-600">{p.counts?.open ?? 0} open</span>
                    <span className="mx-1 text-gray-600">/</span>
                    <span className="text-gray-600">{p.counts?.total ?? 0}</span>
                  </>
                ) : (
                  <>{p.counts?.open ?? 0} open / {p.counts?.total ?? 0}</>
                )}
              </div>
            </button>
          )
        })}
      </aside>

      {/* Project detail */}
      <section className="flex-1 min-w-0">
        {!project && (
          <div className="text-gray-500 text-sm py-12 text-center">
            Select a project, or create one to start an issue log.
          </div>
        )}

        {project && (
          <>
            <div className="flex gap-2 mb-3">
              {(['issue','feature','all'] as const).map(k => {
                const label = k === 'issue' ? `Issues ${project.counts?.by_kind?.issue?.open ?? project.counts?.open ?? 0}` : k === 'feature' ? `Feature requests ${project.counts?.by_kind?.feature?.open ?? 0}` : `All ${project.counts?.all_open ?? ((project.counts?.by_kind?.issue?.open ?? 0)+(project.counts?.by_kind?.feature?.open ?? 0))}`
                const active = kind === k
                return <button key={k} onClick={() => handleSelectKind(k)} className={`px-3 py-1.5 rounded text-sm ${active ? 'bg-emerald-600 text-white' : 'bg-gray-800 text-gray-300 hover:bg-gray-700'}`}>{label}</button>
              })}
            </div>
            <ProjectHeader
              project={project}
              kind={kind}
              onToggleScopes={() => setShowScopes(v => !v)}
              scopesOpen={showScopes}
              onArchive={() => setPendingArchive(project)}
              onChanged={async () => { await loadProjects(project.id); const fresh = await api.getTrackerProject(project.id); setProject(fresh) }}
            />

            {showScopes && vocab && (
              <ScopeEditor
                project={project}
                kinds={vocab.scope_kinds}
                onChanged={async () => setProject(await api.getTrackerProject(project.id))}
              />
            )}

            {/* Filters */}
            <div className="mt-4 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[220px]">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                  <input
                    value={currentFilters.query}
                    onChange={e => updateCurrentFilters({ query: e.target.value, offset: 0 })}
                    placeholder={kind === 'feature' ? 'Search features' : kind === 'all' ? 'Search issues and features' : 'Search title, body, key or failing command'}
                    aria-label={kind === 'feature' ? 'Search features' : kind === 'all' ? 'Search all' : 'Search issues'}
                    className="w-full pl-9 pr-3 py-2 rounded-lg bg-gray-900 border border-gray-800 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600/50"
                  />
                </div>
                <label className="flex items-center gap-2 text-xs text-gray-400 px-2">
                  <input type="checkbox" checked={currentFilters.openOnly} onChange={e => updateCurrentFilters({ openOnly: e.target.checked, offset: 0 })} className="accent-emerald-600" />
                  Open only
                </label>
                <button
                  onClick={() => setShowNewIssue(true)}
                  className="flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition-colors"
                >
                  <Plus size={15} /> {kind === 'feature' ? 'Request feature' : 'Log issue'}
                </button>
              </div>

              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[11px] font-medium text-gray-500 mr-1">Type:</span>
                {(['all','issue','feature'] as const).map(k => {
                  const label = k === 'all' ? 'Both' : k === 'issue' ? `Bugs` : `Features`
                  const count = k === 'all'
                    ? (project.counts?.all_open ?? ((project.counts?.by_kind?.issue?.open ?? 0)+(project.counts?.by_kind?.feature?.open ?? 0)))
                    : k === 'issue'
                      ? (project.counts?.by_kind?.issue?.open ?? project.counts?.open ?? 0)
                      : (project.counts?.by_kind?.feature?.open ?? 0)
                  const active = kind === k
                  return (
                    <button
                      key={k}
                      onClick={() => handleSelectKind(k)}
                      aria-pressed={active}
                      aria-label={`Show ${label}`}
                      className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded text-[11px] border transition-colors ${
                        active
                          ? 'bg-emerald-600 border-emerald-500 text-white'
                          : 'border-gray-800 text-gray-500 hover:text-gray-300 hover:border-gray-700'
                      }`}
                    >
                      {k === 'issue' && <CircleDot size={11} />}
                      {k === 'feature' && <Lightbulb size={11} />}
                      {label} <span className={`ml-1 text-[10px] ${active ? 'text-emerald-200' : 'text-gray-600'}`}>{count}</span>
                    </button>
                  )
                })}
                <span className="w-px h-4 bg-gray-800 mx-2" />
                {(vocab?.severities ?? []).map(s => (
                  <button
                    key={s}
                    onClick={() => updateSeverityFilter(s)}

                    className={`px-2 py-0.5 rounded text-[11px] border transition-colors ${
                      currentFilters.severityFilter.includes(s)
                        ? SEVERITY_CLASS[s] ?? SEVERITY_CLASS.unset
                        : 'border-gray-800 text-gray-500 hover:text-gray-300'
                    }`}
                  >
                    {s}
                  </button>
                ))}
                <span className="w-px bg-gray-800 mx-1" />
                {(vocab?.statuses ?? []).map(s => (
                  <button
                    key={s}
                    onClick={() => updateStatusFilter(s)}
                    className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                      currentFilters.statusFilter.includes(s)
                        ? STATUS_CLASS[s] ?? 'bg-gray-700 text-gray-200'
                        : 'text-gray-500 hover:text-gray-300'
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>

            {/* Issue list */}
            <div className="mt-4 rounded-lg border border-gray-800 overflow-hidden">
              {issuesLoading && (
                <div className="px-4 py-8 text-center text-sm text-gray-500 flex items-center justify-center gap-2">
                  <Loader2 size={14} className="animate-spin" /> Loading {kind === 'feature' ? 'features' : kind === 'all' ? 'items' : 'issues'}…
                </div>
              )}
              {!issuesLoading && page && page.issues.length === 0 && (
                <div className="px-4 py-8 text-center text-sm text-gray-500">No {kind === 'feature' ? 'features' : kind === 'all' ? 'items' : 'issues'} match these filters.</div>
              )}
              {!issuesLoading && page?.issues.map(issue => (
                <div key={issue.key} className="border-b border-gray-800/70 last:border-b-0">
                  <button
                    onClick={() => setSelectedKey(selectedKey === issue.key ? null : issue.key)}
                    aria-expanded={selectedKey === issue.key}
                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-gray-900/60 transition-colors"
                  >
                    <ChevronRight
                      size={14}
                      className={`text-gray-600 shrink-0 transition-transform ${selectedKey === issue.key ? 'rotate-90' : ''}`}
                    />
                    <code className="text-xs text-gray-500 w-24 shrink-0">{issue.key}</code>
                    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium border ${KIND_CLASS[issue.kind] ?? (issue.kind === 'feature' ? 'bg-violet-500/15 text-violet-300 border-violet-500/30' : 'bg-sky-500/15 text-sky-300 border-sky-500/30')}`}>
                      {issue.kind === 'feature' ? <Lightbulb size={10} /> : <CircleDot size={10} />}
                      {issue.kind === 'feature' ? 'feature' : 'bug'}
                    </span>
                    <Pill text={issue.severity} className={SEVERITY_CLASS[issue.severity] ?? SEVERITY_CLASS.unset} />
                    <Pill text={issue.status} className={STATUS_CLASS[issue.status] ?? 'bg-gray-700/40 text-gray-300'} />
                    <span className="text-sm text-gray-200 truncate flex-1">{issue.title}</span>
                    {issue.component && <span className="text-[11px] text-gray-500 shrink-0">{issue.component}</span>}
                    <span className="text-[11px] text-gray-600 shrink-0 w-20 text-right">{shortDate(issue.created_at)}</span>
                  </button>
                  {selectedKey === issue.key && vocab && (
                    <ItemDetail
                      issueKey={issue.key}
                      initialKind={issue.kind}
                      vocab={vocab}
                      onChanged={refreshAfterIssueChange}
                      onDeleted={() => { setSelectedKey(null); refreshAfterIssueChange() }}
                    />
                  )}
                </div>
              ))}
            </div>

            {/* Deep-link fallback: render selected issue even if closed/off-page or on another page */}
            {selectedKey && vocab && page && !page.issues.some(i => i.key === selectedKey) && (
              <div className="mt-4 rounded-lg border border-gray-800 overflow-hidden">
                <div className="px-3 py-1.5 text-[11px] text-gray-500 bg-gray-900/40 border-b border-gray-800">Deep link: {selectedKey} (not in current page/filters)</div>
                <ItemDetail
                  issueKey={selectedKey}
                  initialKind={kind === 'all' ? undefined : kind}
                  vocab={vocab}
                  onChanged={refreshAfterIssueChange}
                  onDeleted={() => { setSelectedKey(null); refreshAfterIssueChange() }}
                />
              </div>
            )}

            {page && page.total > PAGE_SIZE && (
              <div className="flex items-center justify-between mt-3 text-xs text-gray-500">
                <span>
                  {page.offset + 1}–{Math.min(page.offset + page.issues.length, page.total)} of {page.total}
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={currentFilters.offset === 0}
                    onClick={() => updateCurrentFilters({ offset: Math.max(0, currentFilters.offset - PAGE_SIZE) })}
                    className="px-2 py-1 rounded border border-gray-800 disabled:opacity-30 hover:border-gray-700"
                  >
                    Previous
                  </button>
                  <button
                    disabled={currentFilters.offset + PAGE_SIZE >= page.total}
                    onClick={() => updateCurrentFilters({ offset: currentFilters.offset + PAGE_SIZE })}
                    className="px-2 py-1 rounded border border-gray-800 disabled:opacity-30 hover:border-gray-700"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {showNewProject && vocab && (
        <NewProjectModal
          kinds={vocab.scope_kinds}
          onClose={() => setShowNewProject(false)}
          onCreated={async id => { setShowNewProject(false); await loadProjects(id); setSelectedKey(null) }}
        />
      )}

      {showNewIssue && project && vocab && (
        <NewItemModal
          project={project}
          vocab={vocab}
          kind={kind === 'feature' ? 'feature' : 'issue'}
          onClose={() => setShowNewIssue(false)}
          onCreated={async key => { setShowNewIssue(false); setSelectedKey(key); await refreshAfterIssueChange() }}
        />
      )}

      <ConfirmModal
        open={pendingArchive !== null}
        title="Archive project"
        message="Archiving hides the project from the default list. Its issues are kept and stay searchable."
        details={pendingArchive ? [{ label: 'Project', value: pendingArchive.name }] : undefined}
        confirmLabel="Archive"
        variant="warning"
        onCancel={() => setPendingArchive(null)}
        onConfirm={async () => {
          const target = pendingArchive!
          setPendingArchive(null)
          try {
            await api.updateTrackerProject(target.id, { status: 'archived' })
            await loadProjects(target.id)
            setProject(await api.getTrackerProject(target.id))
            showSnackbar({ type: 'success', message: `${target.name} archived` })
          } catch (err) {
            showSnackbar({ type: 'error', message: errorText(err) })
          }
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------

function ProjectHeader({
  project, kind, onToggleScopes, scopesOpen, onArchive, onChanged,
}: {
  project: TrackerProject
  kind: TabKind
  onToggleScopes: () => void
  scopesOpen: boolean
  onArchive: () => void
  onChanged: () => Promise<void>
}) {
  const { showSnackbar } = useStore()
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(project.name)
  const [description, setDescription] = useState(project.description)

  useEffect(() => { setName(project.name); setDescription(project.description) }, [project.id, project.name, project.description])

  const save = async () => {
    try {
      await api.updateTrackerProject(project.id, { name, description })
      setEditing(false)
      await onChanged()
      showSnackbar({ type: 'success', message: 'Project updated' })
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  const scopeCount = project.scopes?.length ?? 0
  const byKind = project.counts?.by_kind

  return (
    <header className="rounded-lg border border-gray-800 bg-gray-900/40 p-4">
      {editing ? (
        <div className="space-y-2">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            aria-label="Project name"
            className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-700 text-sm text-gray-100"
          />
          <textarea
            value={description}
            onChange={e => setDescription(e.target.value)}
            aria-label="Project description"
            rows={2}
            className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-700 text-sm text-gray-300"
          />
          <div className="flex gap-2">
            <button onClick={save} className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs">Save</button>
            <button onClick={() => setEditing(false)} className="px-3 py-1.5 rounded border border-gray-700 text-xs text-gray-400">Cancel</button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h2 className="text-lg font-semibold text-white flex items-center gap-2">
                {project.name}
                {project.status === 'archived' && (
                  <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-300">archived</span>
                )}
              </h2>
              <p className="text-xs text-gray-500 mt-0.5">
                <code>{project.id}</code> · keys <code>{project.issue_prefix}-NNNN</code> ·{' '}
                {byKind ? (
                  <>I {byKind.issue?.open ?? 0}/{byKind.issue?.total ?? 0} · F {byKind.feature?.open ?? 0}/{byKind.feature?.total ?? 0} · {project.counts?.open ?? 0} open of {project.counts?.total ?? 0}</>
                ) : (
                  <>{project.counts?.open ?? 0} open of {project.counts?.total ?? 0}</>
                )}
              </p>
              {project.description && <p className="text-sm text-gray-400 mt-2">{project.description}</p>}
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <a
                href={`/tracker/projects/${encodeURIComponent(project.id)}/${kind === 'feature' ? 'features/export' : kind === 'all' ? 'export?kind=all' : 'export'}`}
                target="_blank"
                rel="noreferrer"
                title={kind === 'feature' ? "Render the feature log as markdown" : kind === 'all' ? "Render all items as markdown" : "Render the issue log as markdown"}
                className="p-2 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200"
              >
                <FileDown size={15} />
              </a>
              <button onClick={() => setEditing(true)} className="px-2 py-1 rounded text-xs text-gray-400 hover:text-gray-200 hover:bg-gray-800">
                Edit
              </button>
              {project.status !== 'archived' && (
                <button onClick={onArchive} title="Archive project" className="p-2 rounded hover:bg-gray-800 text-gray-500 hover:text-gray-300">
                  <Archive size={15} />
                </button>
              )}
            </div>
          </div>
          <button
            onClick={onToggleScopes}
            className="mt-3 text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1.5"
          >
            <ChevronRight size={12} className={`transition-transform ${scopesOpen ? 'rotate-90' : ''}`} />
            {scopeCount} scope{scopeCount === 1 ? '' : 's'} — the paths, sessions and remotes that file here
          </button>
        </>
      )}
    </header>
  )
}

// ---------------------------------------------------------------------------

function ScopeEditor({
  project, kinds, onChanged,
}: { project: TrackerProject; kinds: string[]; onChanged: () => Promise<void> }) {
  const { showSnackbar } = useStore()
  const [kind, setKind] = useState(kinds[0] ?? 'path')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  const add = async () => {
    if (!value.trim()) return
    setBusy(true)
    try {
      const row = await api.addTrackerScope(project.id, { kind, value: value.trim() })
      setValue('')
      await onChanged()
      showSnackbar({
        type: row.created ? 'success' : 'info',
        message: row.created ? `Added ${row.kind} scope` : 'That scope was already registered here',
      })
    } catch (err) {
      // A 409 names the project that already owns the value — the most useful
      // thing the operator can be told, so it is surfaced verbatim.
      showSnackbar({ type: 'error', message: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  const remove = async (scope: TrackerScope) => {
    try {
      await api.removeTrackerScope(project.id, scope.id)
      await onChanged()
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  return (
    <div className="mt-2 rounded-lg border border-gray-800 bg-gray-900/20 p-3 space-y-2">
      {(project.scopes ?? []).map(scope => (
        <div key={scope.id} className="flex items-center gap-2 text-xs">
          <span className="w-20 shrink-0 text-gray-500">{scope.kind}</span>
          <code className="flex-1 truncate text-gray-300">{scope.value}</code>
          <button onClick={() => remove(scope)} title="Remove scope" className="p-1 rounded text-gray-600 hover:text-red-400">
            <X size={13} />
          </button>
        </div>
      ))}
      <div className="flex items-center gap-2 pt-1">
        <select
          value={kind}
          onChange={e => setKind(e.target.value)}
          aria-label="Scope kind"
          className="px-2 py-1.5 rounded bg-gray-900 border border-gray-700 text-xs text-gray-300"
        >
          {kinds.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
        <input
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }}
          placeholder={kind === 'path' ? '/Users/you/Projects/repo' : kind === 'session' ? 'cao-p1-closure' : 'github.com/owner/repo'}
          aria-label="Scope value"
          className="flex-1 px-2 py-1.5 rounded bg-gray-900 border border-gray-700 text-xs text-gray-200 placeholder-gray-600"
        />
        <button
          onClick={add}
          disabled={busy || !value.trim()}
          className="px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-40"
        >
          Add
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Item detail — factored around presentation descriptor so the two kinds share
// one implementation and copy is not duplicated.

function ItemDetail({
  issueKey, initialKind, vocab, onChanged, onDeleted,
}: {
  issueKey: string
  initialKind?: string
  vocab: TrackerVocabulary
  onChanged: () => Promise<void>
  onDeleted: () => void
}) {
  const { showSnackbar } = useStore()
  const [issue, setIssue] = useState<TrackerIssue | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [labelText, setLabelText] = useState('')
  const [comment, setComment] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [saving, setSaving] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(false)
  const [duplicateOf, setDuplicateOf] = useState('')
  const [closingChoice, setClosingChoice] = useState('')
  const [linkTo, setLinkTo] = useState('')
  const [linkKind, setLinkKind] = useState('relates')

  const presentation = useMemo(() => presentationFor(issue?.kind ?? initialKind), [issue?.kind, initialKind])
  const isFeature = presentation.kind === 'feature'

  const load = useCallback(async () => {
    try {
      // Use key-universal fetch; typed wrappers would 404 on cross-kind
      const row = await api.getTrackerIssue(issueKey)
      setIssue(row)
      setDraft({
        title: row.title,
        body: row.body,
        component: row.component ?? '',
        assignee: row.assignee ?? '',
        reporter: row.reporter ?? '',
        failing_command: row.failing_command ?? '',
        evidence: row.evidence ?? '',
        resolution: row.resolution ?? '',
      })
      setLabelText(row.labels.join(', '))
      setDuplicateOf(row.duplicate_of ?? '')
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }, [issueKey, showSnackbar])

  useEffect(() => { load() }, [load])

  const dirty = useMemo(() => {
    if (!issue) return {}
    const changes: Record<string, unknown> = {}
    for (const [field, value] of Object.entries(draft)) {
      const current = (issue as unknown as Record<string, unknown>)[field]
      if ((current ?? '') !== value) changes[field] = value
    }
    const labels = labelText.split(',').map(s => s.trim()).filter(Boolean)
    if (labels.join('\u0000') !== issue.labels.join('\u0000')) changes.labels = labels
    return changes
  }, [issue, draft, labelText])

  const hasChanges = Object.keys(dirty).length > 0

  const patch = async (extra?: Record<string, unknown>) => {
    setSaving(true)
    try {
      const body: Record<string, unknown> = { ...dirty, ...extra, actor: 'dashboard' }
      if (isFeature) {
        // Feature-specific validation: duplicate requires canonical key
        if (extra?.status === 'duplicate' && !duplicateOf.trim()) {
          showSnackbar({ type: 'error', message: 'Duplicate requires a canonical key' })
          setSaving(false)
          return
        }
        if (extra?.status === 'duplicate') body.duplicate_of = duplicateOf.trim()
      }
      const updater = isFeature ? api.updateTrackerFeature : api.updateTrackerIssue
      await updater(issueKey, body)
      await load()
      await onChanged()
      showSnackbar({ type: 'success', message: `${issueKey} updated` })
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    } finally {
      setSaving(false)
    }
  }

  const postComment = async () => {
    if (!comment.trim()) return
    try {
      const poster = isFeature ? api.addTrackerFeatureComment : api.addTrackerComment
      await poster(issueKey, { body: comment, author: 'dashboard' })
      setComment('')
      await load()
      await onChanged()
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  const addLink = async () => {
    if (!linkTo.trim()) return
    try {
      const poster = isFeature ? api.addTrackerFeatureLink : api.addTrackerLink
      await poster(issueKey, { to_key: linkTo.trim(), kind: linkKind })
      setLinkTo('')
      await load()
      await onChanged()
      showSnackbar({ type: 'success', message: `Linked ${issueKey} → ${linkTo.trim()}` })
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  const removeLink = async (linkId: number) => {
    try {
      if (isFeature) {
        await api.removeTrackerFeatureLink(issueKey, linkId)
      } else {
        await api.removeTrackerLink(issueKey, linkId)
      }
      await load()
      await onChanged()
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  const handleDelete = async () => {
    setPendingDelete(false)
    try {
      const deleter = isFeature ? api.deleteTrackerFeature : api.deleteTrackerIssue
      await deleter(issueKey)
      showSnackbar({ type: 'success', message: `${issueKey} deleted` })
      onDeleted()
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    }
  }

  if (!issue) {
    return <div className="px-10 py-4 text-xs text-gray-600">Loading {issueKey}…</div>
  }

  const statusesForKind = presentation.kind === 'feature' && vocab.statuses_by_kind?.feature
    ? vocab.statuses_by_kind.feature
    : presentation.kind === 'issue' && vocab.statuses_by_kind?.issue
      ? vocab.statuses_by_kind.issue
      : vocab.statuses
  const terminalStatuses = vocab.terminal_statuses_by_kind?.[presentation.kind] ?? vocab.terminal_statuses
  const isTerminal = terminalStatuses.includes(issue.status)

  // Fields to render: for feature, hide failing_command when null/empty
  const editableFields: Array<{ field: keyof TrackerIssue; label: string; mono?: boolean; hideWhenEmpty?: boolean }> = [
    { field: 'component' as keyof TrackerIssue, label: 'Component' },
    { field: 'assignee' as keyof TrackerIssue, label: presentation.assigneeLabel },
    { field: 'reporter' as keyof TrackerIssue, label: presentation.reporterLabel },
    { field: 'failing_command' as keyof TrackerIssue, label: 'Failing command', mono: true, hideWhenEmpty: isFeature },
    { field: 'evidence' as keyof TrackerIssue, label: 'Evidence', mono: true },
    { field: 'resolution' as keyof TrackerIssue, label: presentation.resolutionLabel },
  ].filter(f => !(f.hideWhenEmpty && !draft[f.field as string] && !issue[f.field]))

  return (
    <div className="px-10 py-4 bg-gray-950/50 border-t border-gray-800/70 space-y-4">
      <input
        value={draft.title ?? ''}
        onChange={e => setDraft({ ...draft, title: e.target.value })}
        aria-label={isFeature ? 'Feature title' : 'Issue title'}
        className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-800 text-sm text-gray-100 focus:outline-none focus:border-emerald-600/50"
      />

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={issue.kind}
          onChange={e => patch({ kind: e.target.value })}
          aria-label="Type"
          title="Change type — bug or feature request"
          className={`px-2 py-1.5 rounded border text-xs font-medium inline-flex items-center gap-1 ${issue.kind === 'feature' ? 'bg-violet-500/15 text-violet-300 border-violet-500/30' : 'bg-sky-500/15 text-sky-300 border-sky-500/30'}`}
        >
          <option value="issue">Bug</option>
          <option value="feature">Feature</option>
        </select>
        <span className={`hidden sm:inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium border ${KIND_CLASS[issue.kind] ?? (issue.kind === 'feature' ? 'bg-violet-500/15 text-violet-300 border-violet-500/30' : 'bg-sky-500/15 text-sky-300 border-sky-500/30')}`}>
          {isFeature ? <Lightbulb size={10} /> : <CircleDot size={10} />}
          {isFeature ? 'feature' : 'bug'}
        </span>
        <select
          value={issue.status}
          onChange={e => patch({ status: e.target.value })}
          aria-label="Status"
          className="px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200"
        >
          {statusesForKind.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select
          value={issue.severity}
          onChange={e => patch({ severity: e.target.value })}
          aria-label={presentation.severityLabel}
          className="px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200"
        >
          {vocab.severities.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <span className="text-[11px] text-gray-600 flex items-center gap-1.5">
          {isTerminal ? <CheckCircle2 size={12} /> : <CircleDot size={12} />}
          filed {shortDate(issue.created_at)}
          {issue.closed_at && ` · closed ${shortDate(issue.closed_at)}`}
          {issue.origin === 'migration' && ' · migrated from the markdown ledger'}
        </span>
        <span className="flex-1" />
        <button
          onClick={() => setShowHistory(v => !v)}
          title="Audit trail"
          className="p-1.5 rounded text-gray-500 hover:text-gray-300 hover:bg-gray-800"
        >
          <History size={14} />
        </button>
        <button
          onClick={() => setPendingDelete(true)}
          title={isFeature ? 'Delete feature' : 'Delete issue'}
          className="p-1.5 rounded text-gray-600 hover:text-red-400 hover:bg-gray-800"
        >
          <Trash2 size={14} />
        </button>
      </div>

      {/* Feature closing choices */}
      {isFeature && !isTerminal && presentation.closingOptions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 p-2 rounded bg-gray-900/60 border border-gray-800">
          <span className="text-[11px] text-gray-500">Close as:</span>
          {presentation.closingOptions.map(opt => (
            <button
              key={opt.uiLabel}
              onClick={() => {
                if (opt.needsDuplicateOf) {
                  setClosingChoice(opt.status)
                } else {
                  patch({ status: opt.status })
                }
              }}
              className="px-2 py-1 rounded text-[11px] bg-gray-800 hover:bg-gray-700 text-gray-300"
            >
              {opt.uiLabel}
            </button>
          ))}
          {closingChoice === 'duplicate' && (
            <span className="flex items-center gap-2 ml-2">
              <input
                value={duplicateOf}
                onChange={e => setDuplicateOf(e.target.value)}
                placeholder="canonical key (e.g. cond-0039)"
                aria-label="Canonical key"
                className="px-2 py-1 rounded bg-gray-950 border border-gray-700 text-[11px] text-gray-200 w-40"
              />
              <button
                onClick={() => { patch({ status: 'duplicate' }); setClosingChoice('') }}
                disabled={!duplicateOf.trim()}
                className="px-2 py-1 rounded bg-violet-600 hover:bg-violet-500 text-white text-[11px] disabled:opacity-40"
              >
                Confirm
              </button>
              <button onClick={() => setClosingChoice('')} className="text-gray-500 hover:text-gray-300">
                <X size={12} />
              </button>
            </span>
          )}
        </div>
      )}

      <textarea
        value={draft.body ?? ''}
        onChange={e => setDraft({ ...draft, body: e.target.value })}
        rows={6}
        aria-label={isFeature ? 'Feature body' : 'Issue body'}
        className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-800 text-sm text-gray-300 font-mono leading-relaxed focus:outline-none focus:border-emerald-600/50"
      />

      <div className="grid grid-cols-2 gap-3">
        {editableFields.map(({ field, label, mono }) => (
          <label key={field as string} className="block">
            <span className="text-[11px] uppercase tracking-wide text-gray-600">{label}</span>
            <input
              value={draft[field as string] ?? ''}
              onChange={e => setDraft({ ...draft, [field as string]: e.target.value })}
              aria-label={label}
              className={`mt-1 w-full px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200 ${mono ? 'font-mono' : ''}`}
            />
          </label>
        ))}
        <label className="block col-span-2">
          <span className="text-[11px] uppercase tracking-wide text-gray-600">Labels (comma separated)</span>
          <input
            value={labelText}
            onChange={e => setLabelText(e.target.value)}
            aria-label="Labels"
            className="mt-1 w-full px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200"
          />
        </label>
        {isFeature && issue.duplicate_of && (
          <label className="block col-span-2">
            <span className="text-[11px] uppercase tracking-wide text-gray-600">Duplicate of</span>
            <input
              value={duplicateOf}
              onChange={e => setDuplicateOf(e.target.value)}
              aria-label="Duplicate of"
              className="mt-1 w-full px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200 font-mono"
            />
          </label>
        )}
      </div>

      {hasChanges && (
        <div className="flex items-center gap-2">
          <button
            onClick={() => patch()}
            disabled={saving}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs disabled:opacity-50"
          >
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            Save {Object.keys(dirty).length} change{Object.keys(dirty).length === 1 ? '' : 's'}
          </button>
          <button onClick={load} className="px-3 py-1.5 rounded border border-gray-800 text-xs text-gray-400">
            Discard
          </button>
        </div>
      )}

      <div className="space-y-2">
        {issue.links && issue.links.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {issue.links.map(link => (
              <span key={link.id} className="inline-flex items-center gap-1.5 text-[11px] text-gray-400 px-2 py-1 rounded bg-gray-900 border border-gray-800">
                <Link2 size={11} />
                {link.from_key === issue.key ? `${link.kind} ${link.to_key}` : `${link.from_key} ${link.kind} this`}
                <button onClick={() => removeLink(link.id)} aria-label={`Remove link ${link.id}`} className="ml-1 text-gray-500 hover:text-red-400">
                  <X size={10} />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex gap-2">
          <input
            value={linkTo}
            onChange={e => setLinkTo(e.target.value)}
            placeholder="Link to key (e.g. cond-0042)"
            aria-label="Link target key"
            className="flex-1 px-3 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200 placeholder-gray-600"
          />
          <select value={linkKind} onChange={e => setLinkKind(e.target.value)} aria-label="Link kind" className="px-2 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200">
            {vocab.link_kinds.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
          <button onClick={addLink} disabled={!linkTo.trim()} className="px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-40">Link</button>
        </div>
      </div>

      <div className="space-y-2">
        {(issue.comments ?? []).map(c => (
          <div key={c.id} className="rounded bg-gray-900/60 border border-gray-800 px-3 py-2">
            <div className="text-[11px] text-gray-600">{c.author ?? 'unknown'} · {shortDate(c.created_at)}</div>
            <div className="text-xs text-gray-300 whitespace-pre-wrap mt-1">{c.body}</div>
          </div>
        ))}
        <div className="flex gap-2">
          <input
            value={comment}
            onChange={e => setComment(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) postComment() }}
            placeholder="Add a comment"
            aria-label="Add a comment"
            className="flex-1 px-3 py-1.5 rounded bg-gray-900 border border-gray-800 text-xs text-gray-200 placeholder-gray-600"
          />
          <button
            onClick={postComment}
            disabled={!comment.trim()}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 disabled:opacity-40"
          >
            <MessageSquare size={13} /> Comment
          </button>
        </div>
      </div>

      {showHistory && (
        <ol className="space-y-1 border-t border-gray-800 pt-3">
          {(issue.events ?? []).map(event => (
            <li key={event.id} className="text-[11px] text-gray-500 font-mono">
              {shortDate(event.created_at)} · {event.actor ?? 'unknown'} ·{' '}
              {event.kind === 'field'
                ? `${event.field}: ${event.old_value ?? '∅'} → ${event.new_value ?? '∅'}`
                : `${event.kind}${event.new_value ? `: ${event.new_value.slice(0, 60)}` : ''}`}
            </li>
          ))}
        </ol>
      )}

      <ConfirmModal
        open={pendingDelete}
        title={`Delete ${issue.key}`}
        message={`This removes the ${isFeature ? 'feature request' : 'issue'} and every comment, link and audit event attached to it. The key is never reissued.`}
        details={[{ label: 'Title', value: issue.title }]}
        confirmLabel="Delete"
        onCancel={() => setPendingDelete(false)}
        onConfirm={handleDelete}
      />
    </div>
  )
}

// Backward compat alias — keep old name for any external import
const IssueDetail = ItemDetail

// ---------------------------------------------------------------------------

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-label={title}
        onClick={e => e.stopPropagation()}
        className="w-full max-w-lg rounded-xl border border-gray-800 bg-gray-900 shadow-2xl"
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-white">{title}</h3>
          <button onClick={onClose} aria-label="Close" className="p-1 rounded text-gray-500 hover:text-gray-200">
            <X size={16} />
          </button>
        </div>
        <div className="p-5 space-y-3 max-h-[70vh] overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}

function NewProjectModal({
  kinds, onClose, onCreated,
}: { kinds: string[]; onClose: () => void; onCreated: (id: string) => void }) {
  const { showSnackbar } = useStore()
  const [name, setName] = useState('')
  const [id, setId] = useState('')
  const [prefix, setPrefix] = useState('')
  const [description, setDescription] = useState('')
  const [scopes, setScopes] = useState<Array<{ kind: string; value: string }>>([])
  const [kind, setKind] = useState(kinds[0] ?? 'path')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  const create = async () => {
    setBusy(true)
    try {
      const created = await api.createTrackerProject({
        name,
        id: id.trim() || undefined,
        issue_prefix: prefix.trim() || undefined,
        description,
        scopes,
      })
      showSnackbar({ type: 'success', message: `Created ${created.id}` })
      onCreated(created.id)
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="New project" onClose={onClose}>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">Name</span>
        <input value={name} onChange={e => setName(e.target.value)} aria-label="Name"
          placeholder="CAO System"
          className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-100" />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">Id (optional)</span>
          <input value={id} onChange={e => setId(e.target.value)} aria-label="Id" placeholder="derived from the name"
            className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-100 font-mono" />
        </label>
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">Key prefix (optional)</span>
          <input value={prefix} onChange={e => setPrefix(e.target.value)} aria-label="Key prefix" placeholder="cond"
            className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-100 font-mono" />
        </label>
      </div>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">Description</span>
        <textarea value={description} onChange={e => setDescription(e.target.value)} rows={2} aria-label="Description"
          className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-300" />
      </label>

      <div className="space-y-1.5">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">Scopes</span>
        {scopes.map((s, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            <span className="w-20 text-gray-500">{s.kind}</span>
            <code className="flex-1 truncate text-gray-300">{s.value}</code>
            <button onClick={() => setScopes(scopes.filter((_, j) => j !== i))} aria-label="Remove"
              className="p-1 text-gray-600 hover:text-red-400"><X size={12} /></button>
          </div>
        ))}
        <div className="flex gap-2">
          <select value={kind} onChange={e => setKind(e.target.value)} aria-label="Scope kind"
            className="px-2 py-1.5 rounded bg-gray-950 border border-gray-800 text-xs text-gray-300">
            {kinds.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
          <input value={value} onChange={e => setValue(e.target.value)} aria-label="Scope value"
            onKeyDown={e => { if (e.key === 'Enter' && value.trim()) { setScopes([...scopes, { kind, value: value.trim() }]); setValue('') } }}
            placeholder="/Users/you/Projects/repo"
            className="flex-1 px-2 py-1.5 rounded bg-gray-950 border border-gray-800 text-xs text-gray-200 placeholder-gray-600" />
          <button
            onClick={() => { if (value.trim()) { setScopes([...scopes, { kind, value: value.trim() }]); setValue('') } }}
            className="px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200">Add</button>
        </div>
      </div>

      <button onClick={create} disabled={busy || !name.trim()}
        className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50">
        {busy && <Loader2 size={14} className="animate-spin" />} Create project
      </button>
    </Modal>
  )
}

// Generic create modal factored around presentation descriptor — one component
// handles both kinds so fixes to validation, body-file handling or audit actors
// do not diverge.

function NewItemModal({
  project, vocab, kind, onClose, onCreated,
}: {
  project: TrackerProject
  vocab: TrackerVocabulary
  kind: ItemKind
  onClose: () => void
  onCreated: (key: string) => void
}) {
  const presentation = KIND_PRESENTATION[kind]
  const { showSnackbar } = useStore()
  const [title, setTitle] = useState('')
  const [body, setBody] = useState(presentation.bodyStarter)
  const [severity, setSeverity] = useState('unset')
  const [status, setStatus] = useState('open')
  const [component, setComponent] = useState('')
  const [requester, setRequester] = useState('')
  const [owner, setOwner] = useState('')
  const [labels, setLabels] = useState('')
  const [evidence, setEvidence] = useState('')
  const [failingCommand, setFailingCommand] = useState('')
  const [busy, setBusy] = useState(false)

  // Reset body when kind switches (modal is remounted via key, but guard anyway)
  useEffect(() => {
    setBody(presentation.bodyStarter)
  }, [presentation.bodyStarter])

  const create = async () => {
    setBusy(true)
    try {
      const base: Record<string, unknown> = {
        project_id: project.id,
        title,
        body,
        severity,
        status: status || undefined,
        component: component.trim() || undefined,
        evidence: evidence.trim() || undefined,
        labels: labels.split(',').map(s => s.trim()).filter(Boolean),
      }
      if (kind === 'feature') {
        base.reporter = requester.trim() || undefined
        base.assignee = owner.trim() || undefined
        // feature creation must not send failing_command
      } else {
        base.failing_command = failingCommand.trim() || undefined
        base.evidence = evidence.trim() || undefined
        base.reporter = 'dashboard'
      }
      const creator = kind === 'feature' ? api.createTrackerFeature : api.createTrackerIssue
      const created = await creator(base)
      showSnackbar({ type: 'success', message: `Filed ${created.key}` })
      onCreated(created.key)
    } catch (err) {
      showSnackbar({ type: 'error', message: errorText(err) })
    } finally {
      setBusy(false)
    }
  }

  const statusOptions = kind === 'feature' && vocab.statuses_by_kind?.feature
    ? vocab.statuses_by_kind.feature
    : kind === 'issue' && vocab.statuses_by_kind?.issue
      ? vocab.statuses_by_kind.issue
      : vocab.statuses

  return (
    <Modal title={presentation.modalTitle(project.name)} onClose={onClose}>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">Title</span>
        <input value={title} onChange={e => setTitle(e.target.value)} aria-label="Title"
          className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-100" />
      </label>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">{presentation.bodyLabel}</span>
        <textarea value={body} onChange={e => setBody(e.target.value)} rows={6} aria-label="Body"
          className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-300 font-mono" />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">{presentation.severityLabel}</span>
          <select value={severity} onChange={e => setSeverity(e.target.value)} aria-label={presentation.severityLabel}
            className="mt-1 w-full px-2 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-200">
            {vocab.severities.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">Status</span>
          <select value={status} onChange={e => setStatus(e.target.value)} aria-label="Status"
            className="mt-1 w-full px-2 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-200">
            {statusOptions.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">Component</span>
          <input value={component} onChange={e => setComponent(e.target.value)} aria-label="Component"
            className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-200" />
        </label>
        <label className="block">
          <span className="text-[11px] uppercase tracking-wide text-gray-500">Evidence path</span>
          <input value={evidence} onChange={e => setEvidence(e.target.value)} aria-label="Evidence"
            className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-xs text-gray-200 font-mono" />
        </label>
      </div>
      {kind === 'feature' ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-[11px] uppercase tracking-wide text-gray-500">Requester</span>
              <input value={requester} onChange={e => setRequester(e.target.value)} aria-label="Requester"
                className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-200" />
            </label>
            <label className="block">
              <span className="text-[11px] uppercase tracking-wide text-gray-500">Owner</span>
              <input value={owner} onChange={e => setOwner(e.target.value)} aria-label="Owner"
                className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-sm text-gray-200" />
            </label>
          </div>
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-gray-500">Labels (comma separated)</span>
            <input value={labels} onChange={e => setLabels(e.target.value)} aria-label="Labels"
              className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-xs text-gray-200" />
          </label>
        </>
      ) : (
        <>
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-gray-500">Failing command</span>
            <input value={failingCommand} onChange={e => setFailingCommand(e.target.value)} aria-label="Failing command"
              className="mt-1 w-full px-3 py-2 rounded bg-gray-950 border border-gray-800 text-xs text-gray-200 font-mono" />
          </label>
        </>
      )}
      <button onClick={create} disabled={busy || !title.trim()}
        className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50">
        {busy && <Loader2 size={14} className="animate-spin" />} {presentation.createActionLabel}
      </button>
    </Modal>
  )
}

// Keep old name as alias for backward compat (tests don't import it, but keep for safety)
const NewIssueModal = NewItemModal
