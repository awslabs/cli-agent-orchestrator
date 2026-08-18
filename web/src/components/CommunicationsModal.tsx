// The task-scoped communications catalog modal (design §8.2).
//
// A READER, AND NOTHING ELSE. Nothing in this file mutates a task, infers an
// outcome, or labels anything "complete": `report_scope` is drawn as the
// author's claim on its own badge and never folded into a task state, and the
// provider turn badge is not involved at all. The modal renders the server's
// list order as returned (the total order is `recorded_at DESC,
// communication_id ASC`; a client-side sort on `recorded_at` alone is not
// that order) and passes the opaque cursor back verbatim.
//
// Failure isolation is structural: the list, the detail pane, and every
// attachment row own their state independently, so one failed fetch degrades
// exactly one region. Detail bodies are `Cache-Control: no-store` — a
// selection change refetches rather than caching bodies client-side, and
// every state here is leaveable: loaders resolve, errors carry a retry or a
// close, and the modal always closes with Escape, the backdrop, or the X.

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ChevronDown, ChevronRight, Copy, Download, FileText, X } from 'lucide-react'
import {
  api,
  type CatalogDocumentEntry,
  type CatalogReason,
  type CommunicationDetailResponse,
  type CommunicationListItem,
} from '../api'
import {
  catalogAvailability,
  contentReasonText,
  coverageReasonText,
  detailFailure,
  kindLabel,
  listFailure,
  readCommunicationsList,
  reportScopeBadge,
  type DetailFailure,
} from '../lib/communications'
import { fmtAbs, fmtRel } from '../lib/time'
import { safeDownloadName } from '../lib/safeMarkdown'
import { SafeContentView, formatBytes } from './SafeContentView'

const FOCUSABLE_SELECTOR = 'a[href], button, input, select, textarea, [tabindex]'

/** Visibility check that degrades to "visible" where layout is unavailable (jsdom). */
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

export interface CommunicationsModalProps {
  /** The task occurrence whose communications are listed. */
  taskOccurrenceId: string
  /** The deep-linked selection, or null for "the latest" / list-only. */
  selectedId: string | null
  /** Selection changed; the parent reflects it into the dashboard URL. */
  onSelect: (id: string | null) => void
  onClose: () => void
}

type ListState =
  | { status: 'loading' }
  | { status: 'failed'; failure: DetailFailure }
  | {
      status: 'ready'
      items: CommunicationListItem[]
      nextCursor: string | null
      total: number
      coverage: string
      reasons: CatalogReason[]
    }

type DetailState =
  | { status: 'loading' }
  | { status: 'failed'; failure: DetailFailure }
  | { status: 'ready'; response: CommunicationDetailResponse }

type AttachmentState =
  | { status: 'closed' }
  | { status: 'loading' }
  | { status: 'open'; content: string | null; reason: string | null }
  | { status: 'failed'; failure: DetailFailure }

function authorText(item: CommunicationListItem): string | null {
  if (!item.authored_by_id && !item.authored_by_type) return null
  return [item.authored_by_type, item.authored_by_id].filter(Boolean).join(' · ')
}

/**
 * One named attachment with its own open/copy/download actions. A failed or
 * tombstoned fetch degrades THIS row — the list, the body, and the other
 * attachments are unaffected.
 */
function AttachmentRow({ doc }: { doc: CatalogDocumentEntry }) {
  const [state, setState] = useState<AttachmentState>({ status: 'closed' })
  const [copied, setCopied] = useState(false)

  const open = async () => {
    if (state.status === 'open') {
      setState({ status: 'closed' })
      return
    }
    setState({ status: 'loading' })
    try {
      const res = await api.getCommunicationAttachment(doc.attachment_id)
      setState({ status: 'open', content: res.content ?? null, reason: res.reason ?? null })
    } catch (error) {
      setState({ status: 'failed', failure: detailFailure(error) })
    }
  }

  // Copy/download fetch the bytes on demand (bodies are no-store; nothing is
  // cached between actions). A tombstone or a fetch failure opens the row's
  // panel instead, so the reason is displayed next to the action that asked.
  // An already-open panel stays open while the action runs.
  const withContent = async (fn: (content: string) => void) => {
    setState(s => (s.status === 'open' ? s : { status: 'loading' }))
    try {
      const res = await api.getCommunicationAttachment(doc.attachment_id)
      if (res.content == null) {
        setState({ status: 'open', content: null, reason: res.reason ?? null })
        return
      }
      fn(res.content)
      setState(s => (s.status === 'loading' ? { status: 'closed' } : s))
    } catch (error) {
      setState({ status: 'failed', failure: detailFailure(error) })
    }
  }

  const copy = () =>
    withContent(content => {
      navigator.clipboard
        .writeText(content)
        .then(() => {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 2000)
        })
        .catch(() => {})
    })

  const download = () =>
    withContent(content => {
      const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // display_name is metadata, never a path; the fallback is generated.
      a.download = safeDownloadName(
        doc.display_name,
        `attachment-${doc.attachment_id.slice(0, 8)}`,
        doc.media_type,
      )
      a.click()
      URL.revokeObjectURL(url)
    })

  const redaction =
    doc.redaction_applied === true ? 'redacted' : doc.redaction_applied === false ? 'not redacted' : 'redaction unstated'

  return (
    <li data-testid="attachment-row" className="rounded border border-gray-800 bg-gray-950/40">
      <div className="flex items-center gap-2 flex-wrap px-2 py-1.5">
        <button
          type="button"
          onClick={open}
          aria-expanded={state.status === 'open'}
          data-testid="attachment-open"
          className="inline-flex items-center gap-1 min-w-0 text-left text-[11px] text-gray-200 hover:text-white"
        >
          {state.status === 'open' ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <FileText size={12} className="shrink-0 text-gray-400" />
          <span className="truncate font-medium">{doc.display_name || doc.attachment_id}</span>
        </button>
        <span className="text-[10px] text-gray-400">
          {doc.role} · {doc.media_type || 'unknown type'} · {formatBytes(doc.byte_size)} · {redaction}
        </span>
        {doc.content_state && doc.content_state !== 'present' && (
          <span
            data-testid="attachment-content-state"
            className="text-[10px] px-1 rounded bg-amber-900/40 text-amber-200 border border-amber-700/50"
          >
            {doc.content_state}
          </span>
        )}
        <span className="flex items-center gap-1 ml-auto">
          <button
            type="button"
            onClick={copy}
            data-testid="attachment-copy"
            className="inline-flex items-center gap-1 px-1.5 py-0.5 min-h-[28px] rounded text-[10px] text-gray-300 bg-gray-800 hover:bg-gray-700 hover:text-white"
          >
            <Copy size={11} />
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button
            type="button"
            onClick={download}
            data-testid="attachment-download"
            className="inline-flex items-center gap-1 px-1.5 py-0.5 min-h-[28px] rounded text-[10px] text-gray-300 bg-gray-800 hover:bg-gray-700 hover:text-white"
          >
            <Download size={11} />
            Download
          </button>
        </span>
      </div>
      <div className="px-2 pb-1.5 text-[10px] text-gray-400 font-mono truncate" title={doc.sha256}>
        sha256 {doc.sha256}
      </div>
      {state.status === 'loading' && (
        <p role="status" className="px-2 pb-2 text-[11px] text-gray-400">
          Loading attachment…
        </p>
      )}
      {state.status === 'failed' && (
        <div data-testid="attachment-error" role="status" className="px-2 pb-2 space-y-1">
          <p className="text-[11px] text-amber-200">{state.failure.message}</p>
          {state.failure.kind === 'unavailable' && (
            <button
              type="button"
              onClick={open}
              className="px-2 py-0.5 rounded text-[10px] bg-gray-800 text-gray-200 hover:bg-gray-700"
            >
              Retry
            </button>
          )}
        </div>
      )}
      {state.status === 'open' && (
        <div className="px-2 pb-2">
          {state.content != null ? (
            <SafeContentView
              content={state.content}
              mediaType={doc.media_type}
              downloadBase={`attachment-${doc.attachment_id.slice(0, 8)}`}
              displayName={doc.display_name}
            />
          ) : (
            <p data-testid="attachment-tombstone" role="status" className="text-[11px] text-amber-200">
              {state.reason
                ? contentReasonText(state.reason)
                : 'Content unavailable — the response carried no content and no reason.'}
            </p>
          )}
        </div>
      )}
    </li>
  )
}

/** The provenance/receipt expansion: metadata, drawn generically, never a path. */
function ProvenanceDetails({ item }: { item: CommunicationListItem }) {
  const scalarEntries: [string, unknown][] = [
    ['communication id', item.communication_id],
    ['kind', item.kind],
    ['report scope (author-claimed)', item.report_scope],
    ['task occurrence', item.task_occurrence_id],
    ['goal version', item.goal_version],
    ['project', item.project_id],
    ['session', item.session_id],
    ['lane', item.lane_id],
    ['authored by', authorText(item)],
    ['authored at', item.authored_at ? fmtAbs(item.authored_at) ?? item.authored_at : null],
    ['recorded at', item.recorded_at ? fmtAbs(item.recorded_at) ?? item.recorded_at : null],
    ['delivery state', item.delivery_state],
    ['visibility', item.visibility],
    ['supersedes', item.supersedes_communication_id],
    ['superseded by', item.superseded_by],
    ['request key', item.request_key],
  ]
  const body = item.body
  const bodyEntries: [string, unknown][] = body
    ? [
        ['body document', body.document_id],
        ['body attachment id', body.attachment_id],
        ['body blob', body.blob_id],
        ['body sha256', body.sha256],
        ['body size', `${body.byte_size} bytes`],
        ['body capture', body.capture_kind],
        ['body redaction applied', body.redaction_applied === null || body.redaction_applied === undefined ? null : String(body.redaction_applied)],
        ['body quarantine reason', body.quarantine?.reason],
        ['body quarantine actor', body.quarantine?.actor],
        ['body quarantined at', body.quarantine?.quarantined_at],
        ['body quarantine receipt', body.quarantine?.receipt_sha256],
      ]
    : []
  const entries = [...scalarEntries, ...bodyEntries].filter(([, v]) => v !== null && v !== undefined && v !== '')
  return (
    <details data-testid="provenance-details" className="group rounded border border-gray-800 bg-gray-950/40">
      <summary className="px-2 py-1.5 text-[11px] font-semibold text-gray-300 cursor-pointer hover:text-white select-none">
        Provenance &amp; receipt
      </summary>
      <dl className="px-2 pb-2 pt-1 border-t border-gray-800 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-[10px] text-gray-400">{key}</dt>
            <dd className="text-[10px] text-gray-300 font-mono break-all">{String(value)}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

export function CommunicationsModal({ taskOccurrenceId, selectedId, onSelect, onClose }: CommunicationsModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const [list, setList] = useState<ListState>({ status: 'loading' })
  const [detail, setDetail] = useState<DetailState | null>(null)
  const [detailNonce, setDetailNonce] = useState(0)
  // A deep link carrying a selection opens directly in the mobile reader —
  // the link names a record, and landing on the list instead would hide the
  // thing it points at behind one more tap.
  const [mobileView, setMobileView] = useState<'list' | 'reader'>(selectedId ? 'reader' : 'list')
  const [activeIndex, setActiveIndex] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState('')

  const items = list.status === 'ready' ? list.items : []
  // No explicit selection shows the latest communication (the list's first
  // row, in the server's total order). Only an explicit selection reaches the
  // URL; the implicit latest does not.
  const effectiveId = selectedId ?? items[0]?.communication_id ?? null

  // ── List fetch ────────────────────────────────────────────────────────────
  const loadList = useCallback(async () => {
    setList({ status: 'loading' })
    try {
      const body = await api.listCommunications(taskOccurrenceId)
      const page = readCommunicationsList(body)
      if (!page) {
        setList({
          status: 'failed',
          failure: {
            kind: 'unavailable',
            message: 'The catalog returned a response this build cannot read.',
          },
        })
        return
      }
      setList({
        status: 'ready',
        items: page.communications,
        nextCursor: page.next_cursor,
        total: page.total,
        coverage: page.coverage,
        reasons: page.reasons,
      })
    } catch (error) {
      setList({ status: 'failed', failure: listFailure(error) })
    }
  }, [taskOccurrenceId])

  useEffect(() => {
    loadList()
  }, [loadList])

  // ── Detail fetch. Bodies are no-store: a selection change refetches. ──────
  useEffect(() => {
    if (!effectiveId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetail({ status: 'loading' })
    api
      .getCommunication(effectiveId)
      .then(response => {
        if (!cancelled) setDetail({ status: 'ready', response })
      })
      .catch(error => {
        if (!cancelled) setDetail({ status: 'failed', failure: detailFailure(error) })
      })
    return () => {
      cancelled = true
    }
  }, [effectiveId, detailNonce])

  // ── Focus: initial focus in, restore to the invoking control out. ─────────
  useEffect(() => {
    const restore = document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    return () => {
      if (restore && document.contains(restore)) restore.focus()
    }
  }, [])

  // Keep the roving selection on the effective row as the list arrives.
  useEffect(() => {
    const index = items.findIndex(item => item.communication_id === effectiveId)
    if (index >= 0) setActiveIndex(index)
  }, [effectiveId, items])

  const loadMore = async () => {
    if (list.status !== 'ready' || !list.nextCursor) return
    setLoadingMore(true)
    setMoreError('')
    try {
      const body = await api.listCommunications(taskOccurrenceId, list.nextCursor)
      const page = readCommunicationsList(body)
      if (!page) throw new Error('malformed')
      // Appended in server order; never re-sorted client-side.
      setList({
        ...list,
        items: [...list.items, ...page.communications],
        nextCursor: page.next_cursor,
        coverage: page.coverage,
        reasons: page.reasons,
      })
    } catch {
      setMoreError('Could not load the next page. The loaded rows are unaffected.')
    } finally {
      setLoadingMore(false)
    }
  }

  const select = (id: string) => {
    onSelect(id)
    setMobileView('reader')
  }

  const onDialogKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
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

  const onListKeyDown = (event: React.KeyboardEvent) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const options = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])
    if (options.length === 0) return
    let next = activeIndex
    if (event.key === 'ArrowDown') next = Math.min(activeIndex + 1, options.length - 1)
    if (event.key === 'ArrowUp') next = Math.max(activeIndex - 1, 0)
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = options.length - 1
    setActiveIndex(next)
    options[next]?.focus()
  }

  const availability = list.status === 'ready' ? catalogAvailability(list.coverage, list.reasons) : null
  const scopeBadge = (item: CommunicationListItem) => reportScopeBadge(item.report_scope)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Communications for task occurrence ${taskOccurrenceId}`}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        data-testid="communications-modal"
        className="relative flex h-[100dvh] w-full flex-col overflow-hidden border border-gray-700/50 bg-gray-900 pb-[env(safe-area-inset-bottom)] shadow-2xl focus:outline-none lg:h-auto lg:max-h-[85vh] lg:max-w-[980px] lg:rounded-2xl"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-700/50 px-4 py-3">
          <div className="flex items-center gap-2 min-w-0">
            {mobileView === 'reader' && (
              <button
                type="button"
                aria-label="Back to communications list"
                onClick={() => {
                  setMobileView('list')
                  onSelect(null)
                }}
                data-testid="reader-back"
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-400 hover:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500 lg:hidden"
              >
                <ArrowLeft size={16} />
              </button>
            )}
            <h2 className="text-sm font-semibold text-white truncate">
              Communications
              <span className="ml-2 text-[10px] font-mono font-normal text-gray-400" title={taskOccurrenceId}>
                task {taskOccurrenceId.slice(0, 12)}
              </span>
            </h2>
          </div>
          <button
            type="button"
            aria-label="Close communications"
            onClick={onClose}
            data-testid="communications-close"
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-400 hover:text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body: list/detail split on desktop, one pane at a time on mobile. */}
        <div className="flex flex-1 min-h-0">
          {/* List pane */}
          <div
            className={`${
              mobileView === 'reader' ? 'hidden' : 'flex'
            } w-full lg:w-80 shrink-0 flex-col lg:flex border-r border-gray-700/50 min-h-0`}
          >
            {list.status === 'loading' && (
              <p role="status" className="p-4 text-xs text-gray-400">
                Loading communications…
              </p>
            )}
            {list.status === 'failed' && (
              <div role="status" className="p-4 space-y-2">
                <p data-testid="list-error" className="text-xs text-amber-200">
                  {list.failure.message}
                </p>
                {/* As with the detail pane, only `unavailable` retries: the
                    other kinds are deterministic answers about the build or
                    the link, and a Retry there can never succeed. */}
                {list.failure.kind === 'unavailable' && (
                  <button
                    type="button"
                    onClick={loadList}
                    data-testid="list-retry"
                    className="px-2 py-1 rounded text-[11px] bg-gray-800 text-gray-200 hover:bg-gray-700"
                  >
                    Retry
                  </button>
                )}
              </div>
            )}
            {list.status === 'ready' && availability === 'not-installed' && (
              <p role="status" data-testid="catalog-not-installed" className="p-4 text-xs text-gray-400">
                No communications catalog is installed on this deployment.
              </p>
            )}
            {list.status === 'ready' && availability === 'unreadable' && (
              <div role="status" className="p-4 space-y-2">
                <p data-testid="catalog-unreadable" className="text-xs text-amber-200">
                  The communications catalog could not be read.
                </p>
                <button
                  type="button"
                  onClick={loadList}
                  data-testid="list-retry"
                  className="px-2 py-1 rounded text-[11px] bg-gray-800 text-gray-200 hover:bg-gray-700"
                >
                  Retry
                </button>
              </div>
            )}
            {list.status === 'ready' && availability === 'available' && (
              <>
                {(list.coverage === 'partial' || list.coverage === 'truncated') && (
                  <div
                    data-testid="coverage-banner"
                    role="note"
                    className="m-2 rounded border border-amber-700/50 bg-amber-900/20 px-2 py-1.5 space-y-0.5"
                  >
                    <p className="text-[11px] font-semibold text-amber-200">
                      {list.coverage === 'partial'
                        ? 'This list is incomplete — some sources contributed less than they hold:'
                        : 'This list is truncated — not every row could be served:'}
                    </p>
                    {list.reasons.map((r, i) => (
                      <p key={`${r.source}:${r.reason}:${i}`} className="text-[10px] text-amber-100/80">
                        {coverageReasonText(r)}
                      </p>
                    ))}
                  </div>
                )}
                {items.length === 0 ? (
                  <p role="status" data-testid="communications-empty" className="p-4 text-xs text-gray-400">
                    The catalog reports no communications bound to this task occurrence.
                  </p>
                ) : (
                  <div
                    ref={listRef}
                    role="listbox"
                    aria-label="Communications"
                    onKeyDown={onListKeyDown}
                    className="flex-1 overflow-y-auto p-2 space-y-1"
                  >
                    {items.map((item, index) => {
                      const selected = item.communication_id === effectiveId
                      const scope = scopeBadge(item)
                      const rel = fmtRel(item.recorded_at)
                      return (
                        <button
                          key={item.communication_id}
                          type="button"
                          role="option"
                          aria-selected={selected}
                          tabIndex={index === activeIndex ? 0 : -1}
                          onClick={() => select(item.communication_id)}
                          data-testid="communication-item"
                          data-communication-id={item.communication_id}
                          className={`w-full text-left rounded px-2 py-1.5 space-y-0.5 focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
                            selected ? 'bg-gray-700/70' : 'bg-gray-800/50 hover:bg-gray-800'
                          }`}
                        >
                          <span className="flex items-center gap-2 min-w-0">
                            <span className="text-[11px] font-medium text-gray-100 truncate">
                              {item.title || kindLabel(item.kind)}
                            </span>
                            {rel && <span className="text-[10px] text-gray-400 shrink-0 ml-auto">{rel}</span>}
                          </span>
                          <span className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[10px] text-gray-400">{kindLabel(item.kind)}</span>
                            {scope && (
                              <span
                                data-testid="scope-badge"
                                title="Author-claimed report scope — not a task outcome"
                                className={`text-[10px] px-1 rounded border ${
                                  item.report_scope === 'final'
                                    ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
                                    : 'border-gray-600 bg-gray-800 text-gray-300'
                                }`}
                              >
                                {scope}
                              </span>
                            )}
                            {item.delivery_state && (
                              <span className="text-[10px] text-gray-400">{item.delivery_state}</span>
                            )}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                )}
                {list.nextCursor && (
                  <div className="p-2 border-t border-gray-800">
                    <button
                      type="button"
                      onClick={loadMore}
                      disabled={loadingMore}
                      data-testid="load-more"
                      className="w-full px-2 py-1.5 rounded text-[11px] bg-gray-800 text-gray-200 hover:bg-gray-700 disabled:opacity-50"
                    >
                      {loadingMore ? 'Loading…' : `Load more (${items.length} of ${list.total})`}
                    </button>
                    {moreError && (
                      <p role="status" data-testid="load-more-error" className="mt-1 text-[10px] text-amber-200">
                        {moreError}
                      </p>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Detail pane */}
          <div
            className={`${
              mobileView === 'reader' ? 'flex' : 'hidden'
            } lg:flex flex-1 min-w-0 flex-col min-h-0`}
          >
            {!effectiveId && list.status === 'ready' && availability === 'available' && (
              <p role="status" className="p-4 text-xs text-gray-400">
                Select a communication to read it.
              </p>
            )}
            {effectiveId && detail?.status === 'loading' && (
              <p role="status" className="p-4 text-xs text-gray-400">
                Loading communication…
              </p>
            )}
            {effectiveId && detail?.status === 'failed' && (
              <div role="status" className="p-4 space-y-2">
                <p
                  data-testid={`detail-${detail.failure.kind}`}
                  className="text-xs text-amber-200"
                >
                  {detail.failure.message}
                </p>
                {detail.failure.kind === 'unavailable' ? (
                  <button
                    type="button"
                    onClick={() => setDetailNonce(n => n + 1)}
                    data-testid="detail-retry"
                    className="px-2 py-1 rounded text-[11px] bg-gray-800 text-gray-200 hover:bg-gray-700"
                  >
                    Retry
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setMobileView('list')
                      onSelect(null)
                    }}
                    data-testid="detail-back-to-list"
                    className="px-2 py-1 rounded text-[11px] bg-gray-800 text-gray-200 hover:bg-gray-700"
                  >
                    Back to list
                  </button>
                )}
              </div>
            )}
            {effectiveId && detail?.status === 'ready' && (
              <DetailView key={effectiveId} response={detail.response} />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function DetailView({ response }: { response: CommunicationDetailResponse }) {
  const item = response.communication
  const body = item.body
  const scope = reportScopeBadge(item.report_scope)
  const author = authorText(item)
  const recorded = item.recorded_at ? fmtAbs(item.recorded_at) ?? item.recorded_at : null
  return (
    <div data-testid="communication-detail" className="flex-1 overflow-y-auto p-4 space-y-3 min-h-0">
      <div className="space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-sm font-semibold text-white">{item.title || kindLabel(item.kind)}</h3>
          {scope && (
            <span
              data-testid="scope-badge"
              title="Author-claimed report scope — not a task outcome"
              className={`text-[10px] px-1 rounded border ${
                item.report_scope === 'final'
                  ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
                  : 'border-gray-600 bg-gray-800 text-gray-300'
              }`}
            >
              {scope}
            </span>
          )}
        </div>
        <p className="text-[10px] text-gray-400">
          {[kindLabel(item.kind), author, recorded, item.delivery_state, item.goal_version ? `goal v${item.goal_version}` : null]
            .filter(Boolean)
            .join(' · ')}
        </p>
        {scope && (
          <p data-testid="scope-disclaimer" className="text-[10px] text-gray-400">
            Report scope is the author&apos;s claim about this document. It does not change or report the
            task&apos;s outcome.
          </p>
        )}
        {body && (
          <p className="text-[10px] text-gray-400 font-mono">
            {formatBytes(body.byte_size)} · sha256 {body.sha256.slice(0, 16)}…
          </p>
        )}
      </div>

      {response.content != null ? (
        <SafeContentView
          content={response.content}
          mediaType={body?.media_type}
          downloadBase={`communication-${item.communication_id.slice(0, 8)}`}
          displayName={body?.display_name}
        />
      ) : (
        <p
          data-testid="content-tombstone"
          role="status"
          className="rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-200"
        >
          {response.reason
            ? contentReasonText(response.reason)
            : 'Content unavailable — the response carried no content and no reason.'}
        </p>
      )}

      {item.documents.length > 0 && (
        <section aria-label="Attachments" className="space-y-1.5">
          <h4 className="text-[11px] font-semibold text-gray-300">
            Attachments ({item.documents.length})
          </h4>
          <ul className="space-y-1.5">
            {item.documents.map(doc => (
              <AttachmentRow key={doc.attachment_id} doc={doc} />
            ))}
          </ul>
        </section>
      )}

      <ProvenanceDetails item={item} />
    </div>
  )
}
