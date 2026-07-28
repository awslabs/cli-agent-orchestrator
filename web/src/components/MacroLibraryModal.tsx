/**
 * The §7.4 operator macro library modal (Lane B).
 *
 * Desktop (≥1024px): two-pane modal ≤920px — search/scope/favorites filters
 * and the macro list on the left (each row with a direct Send), the editor on
 * the right. Mobile: a full-height sheet (100dvh) with a list view and an
 * editor view and back navigation between them. One DOM tree serves both; the
 * `mobileView` state only toggles which pane is visible below the lg
 * breakpoint.
 *
 * Honesty rules honored here:
 * - The modal never writes to the terminal itself: sending happens only
 *   through the `onSend` callback (one tap = one v3 request; the parent owns
 *   identity binding), and persistence happens only through the api.* macro
 *   routes. No localStorage/sessionStorage anywhere.
 * - Built-ins are immutable: badge, Send, and Duplicate only — no edit/delete
 *   affordances. Duplicating a built-in is the only way to "edit" one.
 * - The recorder refuses unrepresentable input with a message, never
 *   approximates (§3.3), and gates chords on the per-terminal advertised set
 *   with zero POSTs (D9).
 * - Escape closes the modal unless the capture surface is actively recording;
 *   while recording, Escape is recorded input (the capture surface
 *   stopPropagations every key, so it never reaches the dialog handler).
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowLeft, Copy, Plus, Star, X } from 'lucide-react'
import {
  api,
  type ApiError,
  type MacroErrorsBody,
  type MacroRecord,
  type MacroScope,
  type MacroWriteBody,
} from '../api'
import {
  renderNotation,
  renderPreview,
  tryParseNotation,
  type NotationErrorInfo,
} from '../lib/macroNotation'
import {
  applyKeyToRecording,
  previewToken,
  sequenceTextBytes,
  MAX_SEQUENCE_EVENTS,
  MAX_SEQUENCE_TEXT_BYTES,
  type SequenceEvent,
} from '../lib/sequenceRecorder'

export interface MacroLibraryModalProps {
  /** The terminal's provider, e.g. 'kimi_cli'. */
  provider?: string
  /** The terminal's agent profile. */
  agentProfile?: string | null
  /** The visible set, server-ordered (§5.4). The modal never re-sorts. */
  macros: MacroRecord[]
  /** §5.2 quarantine notice, when the server reports one. */
  quarantine?: { count: number | null; path: string }
  /** §3.5: provider_controls advertised; when false, built-ins hide with a stated reason. */
  builtinsVisible: boolean
  /** §4.1: command_controls advertised; gates the Compact guard notice. */
  commandGuardAvailable: boolean
  /** Per-terminal advertised chord set for the recorder's capture gating. */
  advertisedChords: ReadonlySet<string>
  /** A send is in flight; disables Send/Send Test buttons. */
  busy: boolean
  /** Parent closes the modal and restores focus to the invoking control. */
  onClose: () => void
  /** One tap = one v3 request; the parent owns identity binding. */
  onSend: (macro: MacroRecord) => void
  /** Called after any successful create/update/delete/duplicate so the parent refetches. */
  onChanged: () => void
}

type ScopeKind = MacroScope['kind']
type ScopeFilter = 'all' | ScopeKind

interface EditorError {
  offset: number | null
  message: string
}

const FOCUSABLE_SELECTOR = 'a[href], button, input, select, textarea, [tabindex]'

function errorMessage(error: unknown): string {
  const apiError = error as ApiError
  return apiError.detail ?? apiError.message ?? 'request failed'
}

/** The §5.3/§5.4 error list from an ApiError body, or the top-level detail. */
function extractErrors(error: unknown): EditorError[] {
  const apiError = error as ApiError
  const body = apiError.body as MacroErrorsBody | undefined
  if (body && Array.isArray(body.errors)) return body.errors
  return [{ offset: null, message: errorMessage(error) }]
}

function safePreview(events: SequenceEvent[]): string {
  try {
    return renderPreview(events)
  } catch {
    return '(no preview)'
  }
}

/**
 * Visibility check that works in browsers and degrades to "visible" where
 * layout is unavailable (jsdom has no checkVisibility/layout).
 */
function isVisible(el: HTMLElement): boolean {
  if (typeof el.checkVisibility === 'function') {
    try {
      return el.checkVisibility()
    } catch {
      /* fall through */
    }
  }
  return true
}

export function MacroLibraryModal(props: MacroLibraryModalProps): JSX.Element {
  const {
    provider,
    agentProfile,
    macros,
    quarantine,
    builtinsVisible,
    commandGuardAvailable,
    advertisedChords,
    busy,
    onClose,
    onSend,
    onChanged,
  } = props

  const dialogRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const captureRef = useRef<HTMLDivElement>(null)

  // List state
  const [search, setSearch] = useState('')
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all')
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  const [mobileView, setMobileView] = useState<'list' | 'editor'>('list')
  const [listError, setListError] = useState('')

  // Editor state
  const [selected, setSelected] = useState<MacroRecord | null>(null)
  const [draftOpen, setDraftOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [scopeKind, setScopeKind] = useState<ScopeKind>('global')
  const [favorite, setFavorite] = useState(false)
  const [events, setEvents] = useState<SequenceEvent[]>([])
  const [notationText, setNotationText] = useState('')
  const [notationDerivable, setNotationDerivable] = useState(true)
  const [notationErrors, setNotationErrors] = useState<NotationErrorInfo[]>([])
  const [unmappableNotice, setUnmappableNotice] = useState('')
  const [recording, setRecording] = useState(false)
  const [recordNotice, setRecordNotice] = useState('')
  const [saveErrors, setSaveErrors] = useState<EditorError[]>([])
  const [saving, setSaving] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  // Focus the search input (the first control) on open.
  useEffect(() => {
    searchRef.current?.focus()
  }, [])

  // The capture surface holds focus for the duration of a recording.
  useEffect(() => {
    if (recording) captureRef.current?.focus()
  }, [recording])

  // §7.4: Escape closes the modal unless the capture surface is actively
  // recording. This listens at document level so the close works even when
  // focus has drifted out of the dialog (e.g. after the mobile list/editor
  // view swap unmounts the focused control); while recording, the capture
  // surface stopPropagations every key, so Escape never reaches here.
  useEffect(() => {
    if (recording) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [recording, onClose])

  const isBuiltin = selected?.origin === 'builtin'
  const editorOpen = draftOpen || selected !== null

  // ── List filtering (server order is preserved; filters only narrow) ─────

  const visibleMacros = useMemo(() => {
    const query = search.trim().toLowerCase()
    return macros.filter(macro => {
      if (!builtinsVisible && macro.origin === 'builtin') return false
      if (favoritesOnly && !macro.favorite) return false
      if (scopeFilter !== 'all' && macro.scope.kind !== scopeFilter) return false
      if (query && !macro.name.toLowerCase().includes(query)) return false
      return true
    })
  }, [macros, builtinsVisible, favoritesOnly, scopeFilter, search])

  const compactVisible = visibleMacros.some(
    macro => macro.origin === 'builtin' && macro.builtin_kind === 'compact',
  )

  // ── Editor helpers ───────────────────────────────────────────────────────

  const resetNotices = () => {
    setRecordNotice('')
    setSaveErrors([])
    setNotationErrors([])
    setUnmappableNotice('')
    setConfirmingDelete(false)
    setRecording(false)
  }

  const openMacro = (macro: MacroRecord) => {
    resetNotices()
    setDraftOpen(false)
    setSelected(macro)
    setName(macro.name)
    setDescription(macro.description ?? '')
    setScopeKind(macro.scope.kind)
    setFavorite(macro.favorite)
    const macroEvents = macro.events as SequenceEvent[]
    setEvents(macroEvents)
    try {
      setNotationText(renderNotation(macroEvents))
      setNotationDerivable(true)
    } catch {
      setNotationText('')
      setNotationDerivable(false)
      setUnmappableNotice(
        'These events cannot be written as notation; the events remain the source of truth.',
      )
    }
    setMobileView('editor')
  }

  const startDraft = () => {
    resetNotices()
    setSelected(null)
    setDraftOpen(true)
    setName('')
    setDescription('')
    setScopeKind('global')
    setFavorite(false)
    setEvents([])
    setNotationText('')
    setNotationDerivable(true)
    setMobileView('editor')
  }

  const clearEditor = () => {
    resetNotices()
    setSelected(null)
    setDraftOpen(false)
    setName('')
    setDescription('')
    setScopeKind('global')
    setFavorite(false)
    setEvents([])
    setNotationText('')
    setNotationDerivable(true)
  }

  /** Recorder → events → notation sync (events stay the source of truth). */
  const commitEvents = (next: SequenceEvent[]) => {
    setEvents(next)
    setSaveErrors([])
    try {
      setNotationText(renderNotation(next))
      setNotationDerivable(true)
      setNotationErrors([])
      setUnmappableNotice('')
    } catch {
      setNotationDerivable(false)
      setUnmappableNotice(
        'These events cannot be written as notation; the recorded events remain the source of truth and are saved as events.',
      )
    }
  }

  /** Notation → events sync; a failed parse keeps the previous valid events. */
  const onNotationChange = (text: string) => {
    setNotationText(text)
    setSaveErrors([])
    const parsed = tryParseNotation(text)
    if (parsed.ok) {
      setEvents(parsed.events)
      setNotationErrors([])
      setNotationDerivable(true)
      setUnmappableNotice('')
    } else {
      setNotationErrors(parsed.errors)
    }
  }

  const buildScope = (): MacroScope => {
    if (scopeKind === 'provider') return { kind: 'provider', ...(provider ? { provider } : {}) }
    if (scopeKind === 'profile') {
      return { kind: 'profile', ...(agentProfile ? { profile: agentProfile } : {}) }
    }
    return { kind: 'global' }
  }

  // ── Recorder ─────────────────────────────────────────────────────────────

  const startRecording = () => {
    commitEvents([])
    setRecordNotice('')
    setRecording(true)
  }

  const stopRecording = () => {
    setRecording(false)
    setRecordNotice('')
  }

  const onCaptureKeyDown = (event: React.KeyboardEvent) => {
    if (!recording) return
    // While recording, every key is the payload: none of them may reach the
    // dialog (Escape would close it), the page, or the browser's bindings.
    event.preventDefault()
    event.stopPropagation()
    const result = applyKeyToRecording(
      events,
      {
        key: event.key,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        altKey: event.altKey,
        shiftKey: event.shiftKey,
      },
      advertisedChords,
    )
    if (result.refused) {
      setRecordNotice(result.refused)
      return
    }
    setRecordNotice('')
    commitEvents(result.events)
  }

  // ── Dialog keyboard handling: focus trap + Escape ───────────────────────

  const onDialogKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      // An Escape that reaches here is a close request: the capture surface
      // stopPropagations every key while it is recording.
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const root = dialogRef.current
    if (!root) return
    const focusables = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
      el => !el.hasAttribute('disabled') && el.tabIndex >= 0 && isVisible(el),
    )
    if (focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    const active = document.activeElement as HTMLElement | null
    if (event.shiftKey) {
      if (!active || !root.contains(active) || active === first) {
        event.preventDefault()
        last.focus()
      }
    } else if (!active || !root.contains(active) || active === last) {
      event.preventDefault()
      first.focus()
    }
  }

  // ── Actions ──────────────────────────────────────────────────────────────

  const canSave =
    !saving &&
    !isBuiltin &&
    name.trim().length > 0 &&
    events.length > 0 &&
    notationErrors.length === 0

  const runSave = async () => {
    const parsed = tryParseNotation(notationText)
    const body: MacroWriteBody = {
      name: name.trim(),
      scope: buildScope(),
      favorite,
    }
    if (description.trim()) body.description = description.trim()
    // The round-trip is the point: send notation when the text parses cleanly
    // (the server re-parses authoritatively); fall back to the raw events only
    // when the recorder produced events with no notation form.
    if (notationDerivable && parsed.ok) body.notation = notationText
    else body.events = events
    setSaving(true)
    setSaveErrors([])
    try {
      const saved =
        selected && !draftOpen
          ? await api.updateMacro(selected.id, body)
          : await api.createMacro(body)
      onChanged()
      openMacro(saved)
    } catch (error) {
      setSaveErrors(extractErrors(error))
    } finally {
      setSaving(false)
    }
  }

  const runSendTest = () => {
    // A temporary MacroRecord-shaped object; the parent binds identity and
    // sends one ordinary v3 request. Nothing here touches the store.
    const temp: MacroRecord = {
      id: selected?.id ?? 'editor-draft',
      name: name.trim() || 'Untitled macro',
      description: description.trim() || null,
      scope: buildScope(),
      events,
      favorite,
      origin: 'user',
      mutable: true,
      created_at: null,
      updated_at: null,
    }
    onSend(temp)
  }

  const runDuplicate = async (macro: MacroRecord) => {
    setActionBusy(true)
    setListError('')
    setSaveErrors([])
    try {
      const copy = await api.duplicateMacro(macro.id)
      onChanged()
      // Select the copy: duplicating a built-in is the only way to edit one.
      openMacro(copy)
    } catch (error) {
      setListError(errorMessage(error))
    } finally {
      setActionBusy(false)
    }
  }

  const runDelete = async () => {
    if (!selected) return
    setActionBusy(true)
    setSaveErrors([])
    try {
      await api.deleteMacro(selected.id)
      onChanged()
      clearEditor()
      setMobileView('list')
    } catch (error) {
      setSaveErrors(extractErrors(error))
    } finally {
      setActionBusy(false)
      setConfirmingDelete(false)
    }
  }

  // ── Render ───────────────────────────────────────────────────────────────

  const previewText = safePreview(events)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop (covered by the full-height sheet on mobile) */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Macro library"
        onKeyDown={onDialogKeyDown}
        className="relative flex h-[100dvh] w-full flex-col overflow-hidden border border-gray-700/50 bg-gray-900 pb-[env(safe-area-inset-bottom)] shadow-2xl lg:h-auto lg:max-h-[85vh] lg:max-w-[920px] lg:rounded-2xl"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-700/50 px-4 py-3">
          <div className="flex items-center gap-2">
            {mobileView === 'editor' && (
              <button
                type="button"
                aria-label="Back to macro list"
                onClick={() => setMobileView('list')}
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-400 hover:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500 lg:hidden"
              >
                <ArrowLeft size={16} />
              </button>
            )}
            <h2 className="text-sm font-semibold text-white">Macro library</h2>
          </div>
          <button
            type="button"
            aria-label="Close macro library"
            onClick={onClose}
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-500 hover:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          {/* ── Left pane: filters + list ─────────────────────────────── */}
          <div
            data-testid="macro-list-pane"
            className={`${
              mobileView === 'list' ? 'flex' : 'hidden'
            } w-full flex-col lg:flex lg:w-[340px] lg:shrink-0 lg:border-r lg:border-gray-700/50`}
          >
            <div className="space-y-2 border-b border-gray-700/30 p-3">
              <input
                ref={searchRef}
                type="search"
                aria-label="Search macros"
                placeholder="Search macros…"
                value={search}
                onChange={event => setSearch(event.target.value)}
                className="w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none"
              />
              <div className="flex items-center gap-2">
                <select
                  aria-label="Scope filter"
                  value={scopeFilter}
                  onChange={event => setScopeFilter(event.target.value as ScopeFilter)}
                  className="min-h-[44px] flex-1 rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
                >
                  <option value="all">All</option>
                  <option value="global">Global</option>
                  <option value="provider" disabled={!provider}>
                    Provider
                  </option>
                  <option value="profile" disabled={!agentProfile}>
                    This agent
                  </option>
                </select>
                <button
                  type="button"
                  role="switch"
                  aria-checked={favoritesOnly}
                  aria-label="Favorites only"
                  onClick={() => setFavoritesOnly(value => !value)}
                  className={`flex min-h-[44px] items-center gap-1.5 rounded border px-2.5 text-xs focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
                    favoritesOnly
                      ? 'border-amber-600/60 bg-amber-900/20 text-amber-200'
                      : 'border-gray-700 bg-gray-900 text-gray-300'
                  }`}
                >
                  <Star
                    size={12}
                    aria-hidden
                    className={favoritesOnly ? 'fill-amber-400 text-amber-400' : 'text-gray-500'}
                  />
                  Favorites
                </button>
              </div>
            </div>

            {quarantine && (
              <div
                role="alert"
                className="mx-3 mt-2 flex items-start gap-2 rounded border border-amber-700/50 bg-amber-900/20 px-2 py-1.5 text-[11px] text-amber-200"
              >
                <AlertTriangle size={14} aria-hidden className="mt-0.5 shrink-0" />
                <span>
                  The server quarantined{' '}
                  {quarantine.count === null
                    ? 'some records'
                    : `${quarantine.count} macro record${quarantine.count === 1 ? '' : 's'}`}{' '}
                  at <span className="font-mono">{quarantine.path}</span>; the library loads
                  without them until the operator deletes that file.
                </span>
              </div>
            )}

            {!builtinsVisible && (
              <div className="mx-3 mt-2 rounded border border-gray-700/50 bg-gray-800/40 px-2 py-1.5 text-[11px] text-gray-300">
                provider-native built-ins unavailable — this server did not advertise provider
                controls for {provider ?? 'this provider'}
              </div>
            )}

            {!commandGuardAvailable && compactVisible && (
              <div className="mx-3 mt-2 rounded border border-amber-700/40 bg-amber-900/10 px-2 py-1.5 text-[11px] text-amber-300/90">
                prefill-concatenation guard unavailable on this server
              </div>
            )}

            {listError && (
              <div role="alert" className="mx-3 mt-2 text-[11px] text-red-300">
                {listError}
              </div>
            )}

            {visibleMacros.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 px-4 py-8 text-center">
                <p className="text-xs text-gray-400">
                  {macros.length === 0
                    ? 'No macros yet.'
                    : 'No macros match the current filters.'}
                </p>
                <button
                  type="button"
                  onClick={startDraft}
                  className="flex min-h-[44px] items-center gap-1 rounded bg-emerald-700 px-3 py-1.5 text-xs text-white hover:bg-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  <Plus size={14} aria-hidden />
                  New Macro
                </button>
              </div>
            ) : (
              <ul aria-label="Macros" className="min-h-0 flex-1 overflow-y-auto">
                {visibleMacros.map(macro => (
                  <li
                    key={macro.id}
                    data-testid="macro-row"
                    className="flex items-stretch gap-1 border-b border-gray-800/60 px-2 py-1.5"
                  >
                    <button
                      type="button"
                      onClick={() => openMacro(macro)}
                      aria-current={selected?.id === macro.id || undefined}
                      className="min-w-0 flex-1 rounded px-2 py-1 text-left hover:bg-gray-800/70 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      <span className="flex items-center gap-1.5">
                        {macro.favorite && (
                          <Star
                            size={12}
                            aria-hidden
                            className="shrink-0 fill-amber-400 text-amber-400"
                          />
                        )}
                        <span className="truncate text-sm text-gray-100">{macro.name}</span>
                        {macro.origin === 'builtin' && (
                          <span className="shrink-0 rounded border border-indigo-600/50 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-indigo-300">
                            built-in
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block truncate font-mono text-[10px] text-gray-400">
                        {safePreview(macro.events as SequenceEvent[])}
                      </span>
                    </button>
                    {macro.origin === 'builtin' && (
                      <button
                        type="button"
                        aria-label={`Duplicate ${macro.name}`}
                        disabled={actionBusy}
                        onClick={() => void runDuplicate(macro)}
                        className="flex min-h-[44px] min-w-[44px] items-center justify-center self-center rounded bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      >
                        <Copy size={14} aria-hidden />
                      </button>
                    )}
                    <button
                      type="button"
                      aria-label={`Send ${macro.name}`}
                      disabled={busy}
                      onClick={() => onSend(macro)}
                      className="min-h-[44px] min-w-[44px] self-center rounded bg-emerald-700 px-3 text-xs text-white hover:bg-emerald-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      Send
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <div className="border-t border-gray-700/30 p-3">
              <button
                type="button"
                onClick={startDraft}
                className="flex min-h-[44px] w-full items-center justify-center gap-1 rounded bg-emerald-700 px-3 py-1.5 text-xs text-white hover:bg-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-500"
              >
                <Plus size={14} aria-hidden />
                New Macro
              </button>
            </div>
          </div>

          {/* ── Right pane: editor ────────────────────────────────────── */}
          <div
            data-testid="macro-editor-pane"
            className={`${
              mobileView === 'editor' ? 'flex' : 'hidden'
            } min-w-0 flex-1 flex-col lg:flex`}
          >
            {editorOpen ? (
              <>
                <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
                  {isBuiltin && (
                    <div className="rounded border border-indigo-700/40 bg-indigo-900/20 px-2 py-1.5 text-[11px] text-indigo-300">
                      Built-in macro — it cannot be edited or deleted. Duplicate it to edit a
                      copy.
                    </div>
                  )}

                  <label className="block text-xs text-gray-400">
                    Name
                    <input
                      value={name}
                      onChange={event => setName(event.target.value)}
                      disabled={isBuiltin}
                      placeholder="Macro name"
                      className="mt-1 w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-50"
                    />
                  </label>

                  <label className="block text-xs text-gray-400">
                    Description
                    <input
                      value={description}
                      onChange={event => setDescription(event.target.value)}
                      disabled={isBuiltin}
                      placeholder="Optional description"
                      className="mt-1 w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-50"
                    />
                  </label>

                  <div className="flex flex-wrap items-center gap-2">
                    <label className="block text-xs text-gray-400">
                      Scope
                      <select
                        value={scopeKind}
                        onChange={event => setScopeKind(event.target.value as ScopeKind)}
                        disabled={isBuiltin}
                        className="mt-1 block min-h-[44px] rounded border border-gray-700 bg-gray-950 px-2 py-1.5 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none disabled:opacity-50"
                      >
                        <option value="global">Global</option>
                        <option value="provider" disabled={!provider}>
                          Provider: {provider ?? 'unavailable'}
                        </option>
                        <option value="profile" disabled={!agentProfile}>
                          This agent: {agentProfile ?? 'unavailable'}
                        </option>
                      </select>
                    </label>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={favorite}
                      aria-label="Favorite"
                      disabled={isBuiltin}
                      onClick={() => setFavorite(value => !value)}
                      className={`flex min-h-[44px] items-center gap-1.5 self-end rounded border px-3 text-xs focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:opacity-50 ${
                        favorite
                          ? 'border-amber-600/60 bg-amber-900/20 text-amber-200'
                          : 'border-gray-700 bg-gray-900 text-gray-300'
                      }`}
                    >
                      <Star
                        size={12}
                        aria-hidden
                        className={favorite ? 'fill-amber-400 text-amber-400' : 'text-gray-500'}
                      />
                      Favorite
                    </button>
                  </div>

                  {/* Recorder (§6.2 capture, §3.3 refusals) */}
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400">Recorder</span>
                      <button
                        type="button"
                        disabled={isBuiltin}
                        onClick={recording ? stopRecording : startRecording}
                        className={`min-h-[44px] rounded px-3 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-emerald-500 disabled:opacity-40 ${
                          recording
                            ? 'bg-amber-700 text-white hover:bg-amber-600'
                            : 'bg-gray-800 text-gray-200 hover:bg-gray-700'
                        }`}
                      >
                        {recording ? 'Stop' : 'Record'}
                      </button>
                    </div>
                    <div
                      ref={captureRef}
                      data-testid="macro-capture-surface"
                      tabIndex={isBuiltin ? -1 : 0}
                      aria-label="Key capture surface"
                      onKeyDown={onCaptureKeyDown}
                      className={`min-h-[44px] rounded border px-2 py-1.5 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
                        recording
                          ? 'border-amber-600/60 bg-gray-950 text-amber-200'
                          : 'border-gray-700 bg-gray-950 text-gray-300'
                      }`}
                    >
                      {events.length === 0 ? (
                        <span className="text-gray-500">
                          {recording
                            ? 'Recording — press keys now'
                            : 'No events yet — record keys or write notation below'}
                        </span>
                      ) : (
                        <span className="flex flex-wrap gap-1">
                          {events.map((event, index) => (
                            <span key={index} className="rounded bg-gray-800 px-1.5 py-0.5">
                              {previewToken(event)}
                            </span>
                          ))}
                        </span>
                      )}
                      {recording && (
                        <span className="mt-1 block font-sans text-[10px] text-amber-300/80">
                          Recording — {events.length}/{MAX_SEQUENCE_EVENTS} events,{' '}
                          {sequenceTextBytes(events)}/{MAX_SEQUENCE_TEXT_BYTES} B. Every key is
                          captured, including Escape.
                        </span>
                      )}
                    </div>
                    {recordNotice && (
                      <div role="alert" className="text-[11px] text-amber-300">
                        {recordNotice}
                      </div>
                    )}
                    <p className="text-[10px] text-gray-500">
                      Records only representable events: named keys, Ctrl+C, advertised Ctrl
                      chords, and printable text. Combinations owned by the browser or OS never
                      reach this page and cannot be recorded.
                    </p>
                  </div>

                  {/* Notation editor with live normalized preview */}
                  <div className="space-y-1">
                    <label className="block text-xs text-gray-400">
                      Notation
                      <textarea
                        value={notationText}
                        onChange={event => onNotationChange(event.target.value)}
                        disabled={isBuiltin}
                        rows={2}
                        spellCheck={false}
                        placeholder={'"text" enter up*3 ctrl+s'}
                        className="mt-1 w-full rounded border border-gray-700 bg-gray-950 px-2 py-1.5 font-mono text-xs text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-50"
                      />
                    </label>
                    {notationErrors.map((error, index) => (
                      <div key={index} role="alert" className="text-[11px] text-red-300">
                        offset {error.offset}: {error.message}
                      </div>
                    ))}
                    {notationErrors.length === 0 && events.length > 0 && (
                      <div
                        data-testid="macro-preview"
                        className="font-mono text-[11px] text-gray-400"
                      >
                        {previewText}
                      </div>
                    )}
                    {!notationDerivable && unmappableNotice && (
                      <div className="text-[11px] text-amber-300">{unmappableNotice}</div>
                    )}
                    {events.length > 0 && (
                      <details className="text-[10px] text-gray-500">
                        <summary className="cursor-pointer">event JSON</summary>
                        <pre className="mt-1 overflow-x-auto rounded bg-gray-950 p-1.5 font-mono">
                          {JSON.stringify(events)}
                        </pre>
                      </details>
                    )}
                    {saveErrors.map((error, index) => (
                      <div key={index} role="alert" className="text-[11px] text-red-300">
                        {error.offset !== null ? `offset ${error.offset}: ` : ''}
                        {error.message}
                      </div>
                    ))}
                  </div>
                </div>

                {/* Actions */}
                <div className="flex flex-wrap items-center gap-2 border-t border-gray-700/30 px-4 py-3">
                  <button
                    type="button"
                    disabled={busy || events.length === 0 || notationErrors.length > 0}
                    onClick={runSendTest}
                    className="min-h-[44px] rounded bg-emerald-700 px-3 py-1.5 text-xs text-white hover:bg-emerald-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  >
                    Send Test
                  </button>
                  {!isBuiltin && (
                    <button
                      type="button"
                      disabled={!canSave}
                      onClick={() => void runSave()}
                      className="min-h-[44px] rounded bg-emerald-700 px-3 py-1.5 text-xs text-white hover:bg-emerald-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      {saving ? 'Saving…' : 'Save'}
                    </button>
                  )}
                  {selected && (
                    <button
                      type="button"
                      disabled={actionBusy}
                      onClick={() => void runDuplicate(selected)}
                      className="min-h-[44px] rounded bg-gray-800 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      Duplicate
                    </button>
                  )}
                  {selected && !isBuiltin && !confirmingDelete && (
                    <button
                      type="button"
                      disabled={actionBusy}
                      onClick={() => setConfirmingDelete(true)}
                      className="min-h-[44px] rounded bg-gray-800 px-3 py-1.5 text-xs text-red-300 hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      Delete
                    </button>
                  )}
                  {selected && !isBuiltin && confirmingDelete && (
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-[11px] text-red-300">Delete this macro?</span>
                      <button
                        type="button"
                        disabled={actionBusy}
                        onClick={() => void runDelete()}
                        className="min-h-[44px] rounded bg-red-700 px-3 py-1.5 text-xs text-white hover:bg-red-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-red-500"
                      >
                        Confirm delete
                      </button>
                      <button
                        type="button"
                        disabled={actionBusy}
                        onClick={() => setConfirmingDelete(false)}
                        className="min-h-[44px] rounded bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      >
                        Keep
                      </button>
                    </span>
                  )}
                </div>
              </>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
                <p className="text-xs text-gray-400">
                  Select a macro from the list, or start a new one.
                </p>
                <button
                  type="button"
                  onClick={startDraft}
                  className="flex min-h-[44px] items-center gap-1 rounded bg-emerald-700 px-3 py-1.5 text-xs text-white hover:bg-emerald-600 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  <Plus size={14} aria-hidden />
                  New Macro
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
