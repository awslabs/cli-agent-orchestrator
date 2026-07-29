import { useEffect, useRef, useState, type ClipboardEvent } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { X, Terminal as TermIcon, Paperclip, RotateCcw } from 'lucide-react'
import {
  api,
  type ApiError,
  type AttachmentRefusalBody,
  type ControlInputCapabilities,
  type ImageAttachmentRecord,
  type ImageCapabilityBlock,
  type MacroRecord,
  type OperatorMessageBlock,
} from '../api'
import { type SequenceEvent } from '../lib/sequenceRecorder'
import { StreamingEngine, type SendResult, type TraceEntry } from '../lib/streaming'
import { StreamingPanel } from './StreamingPanel'
import { FavoriteStrip } from './FavoriteStrip'
import { MacroLibraryModal } from './MacroLibraryModal'

interface TerminalViewProps {
  terminalId: string
  provider?: string
  agentProfile?: string | null
  onClose: () => void
}

const TERMINAL_FONT_SIZE = 14

// Fallback row height (px) used only when the container has not been laid out
// yet, so a touch delta can still be turned into whole wheel notches.
const DEFAULT_LINE_HEIGHT = Math.round(TERMINAL_FONT_SIZE * 1.2)
const CONTROL_UNSUPPORTED_STATUSES = new Set([404, 405, 501])
const CONTROL_AMBIGUOUS_STATUSES = new Set([408, 425, 500, 502, 503, 504])

// §3.2: the full sixteen-name key set. The streaming toggle arms only when
// the live capabilities advertise streaming support AND this whole set
// (§6.1); anything less degrades per the §3.5 old-server rows.
const FULL_SEQUENCE_KEY_SET = [
  'Escape', 'C-c', 'C-s', 'Enter', 'Backspace',
  'Up', 'Down', 'Left', 'Right',
  'Home', 'End', 'PageUp', 'PageDown',
  'Delete', 'Insert', 'Tab',
]

// The composer's delivery path is the control-input path; its 512-byte cap
// is a contract feature (F8) and the status line names it live (§7.1).
const MAX_COMPOSER_BYTES = 512

const EXPECTED_IDENTITY_FIELDS = [
  'terminal_id',
  'terminal_incarnation',
  'terminal_generation',
  'pane_birth_id',
  'provider_process_id',
  'provider',
  'native_session_id',
  'execution_mode',
  'session_name',
]

// Refusal reasons that mean the pinned identity drifted; on these the
// disarm explanation refetches identity so it names the new generation (§6.4).
const IDENTITY_REFUSAL_CODES = new Set(['stale-generation', 'identity-mismatch', 'pane-dead'])

// ── Lane C: image attachments (§8.4/§8.7) ─────────────────────────────

// The editable draft token, per §8.4: `[Image #N]` is plain text the
// operator can place, edit, or delete; the chip with the same N is its
// visual twin.
const IMAGE_TOKEN_PATTERN = /\[Image #(\d+)\]/g

// One attachment in the composer draft: the server record once staged,
// the local File for the thumbnail and for retrying a failed upload.
interface DraftAttachment {
  localId: string
  token: number
  file: File
  previewUrl: string
  state: 'staging' | 'ready' | 'failed'
  record?: ImageAttachmentRecord
  error?: string
}

const MIME_TO_FORMAT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpeg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function isWheelMouseReport(data: string): boolean {
  const sgr = /^\x1b\[<(\d+);\d+;\d+[Mm]$/.exec(data)
  if (sgr) return (Number(sgr[1]) & 64) === 64

  // Legacy X10 mouse reports encode the button and coordinates as three
  // bytes after ESC [ M. Keep this narrow so printable input and paste stay
  // blocked on managed transcript panes.
  if (data.length === 6 && data.startsWith('\x1b[M')) {
    const button = data.charCodeAt(3) - 32
    return button >= 0 && (button & 64) === 64
  }
  return false
}

/** The 9-field expected_identity bound into every control-input request. */
function pickExpectedIdentity(identity: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(EXPECTED_IDENTITY_FIELDS.map(key => [key, identity[key] ?? null]))
}

/** Normalize a typed control-input body (POST 200 or journaled GET record). */
function typedOutcome(body: Record<string, unknown>): {
  outcome: string
  reasonCode?: string
  reasonDetail?: string
} {
  // r15: the control-input wire field is `detail`; `reason_detail` is the
  // managed-bridge spelling.  Both normalize into the typed outcome — a
  // missing discriminator would otherwise misclassify a pauseable
  // pane-busy as an unrecognized disarm on the live path.
  const detail = body.reason_detail ?? body.detail
  return {
    outcome: String(body.outcome || 'unknown'),
    reasonCode: body.reason_code ? String(body.reason_code) : undefined,
    reasonDetail: detail != null ? String(detail) : undefined,
  }
}

/** The reason a typed outcome names for a status line (r15: `detail`
 * included, so an explainable result never renders empty). */
function outcomeReason(body: Record<string, unknown>): unknown {
  return body.reason_code || body.reason_detail || body.detail
}

interface PerTerminalProviderControlEntry {
  steer_chords?: string[]
  dispatch_grace_ms?: number
  // Lane C (§8.6) and r15 (§6.7) blocks: present only when this terminal's
  // exact provider build carries the proof — the send authority, unlike
  // the top-level discovery union.
  operator_message?: OperatorMessageBlock
  image?: ImageCapabilityBlock
  interactive_streaming?: { supported: boolean }
}

/**
 * The per-terminal provider_controls entry from the identity route's
 * control_input block (§3.5): the exact chord set and dispatch grace the
 * server would admit for THIS terminal's provider+build. The top-level
 * capabilities union is discovery only — it never licenses a send.
 */
function perTerminalProviderControls(
  identity: Record<string, unknown>,
): PerTerminalProviderControlEntry | undefined {
  const block = identity?.control_input as Record<string, unknown> | undefined
  const controls = block?.provider_controls as
    | Record<string, PerTerminalProviderControlEntry>
    | undefined
  if (!controls) return undefined
  const provider = identity.provider
  return typeof provider === 'string' ? controls[provider] : undefined
}

function perTerminalSteerChords(identity: Record<string, unknown>): string[] {
  return perTerminalProviderControls(identity)?.steer_chords ?? []
}

export function TerminalView({ terminalId, provider, agentProfile, onClose }: TerminalViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const managedRef = useRef<boolean | null>(null)
  const [managed, setManaged] = useState(false)
  const [generation, setGeneration] = useState<string | undefined>()
  const [executionMode, setExecutionMode] = useState<string | undefined>()
  const [nativeControlSupported, setNativeControlSupported] = useState(false)
  const [nativeControlResolved, setNativeControlResolved] = useState(false)
  const [message, setMessage] = useState('')
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('')
  const [controlBusy, setControlBusy] = useState(false)
  const [controlStatus, setControlStatus] = useState('')
  const [sequenceSupported, setSequenceSupported] = useState(false)
  const [capabilities, setCapabilities] = useState<ControlInputCapabilities | null>(null)
  // Lane C/r15: the per-terminal, build-exact Lane C blocks from the
  // identity route — the ONLY send authority for the composer affordances
  // (§8.6/D9). The top-level capabilities union is discovery only.
  const [perTerminalLaneC, setPerTerminalLaneC] = useState<{
    operatorMessage?: OperatorMessageBlock
    image?: ImageCapabilityBlock
  }>({})

  // ── Lane B: macro library + streaming state ──────────────────────────
  const [macros, setMacros] = useState<MacroRecord[]>([])
  const [macroQuarantine, setMacroQuarantine] = useState<
    { count: number | null; path: string } | undefined
  >()
  const [macrosUnavailable, setMacrosUnavailable] = useState(false)
  const [macroModalOpen, setMacroModalOpen] = useState(false)
  // The per-terminal advertised chord set: the ONLY chord send authority
  // (§3.5 — the top-level union is discovery only, never a license).
  const [advertisedChords, setAdvertisedChords] = useState<ReadonlySet<string>>(new Set())
  const [streamingArmed, setStreamingArmed] = useState(false)
  const [arming, setArming] = useState(false)
  const [streamingTrace, setStreamingTrace] = useState<TraceEntry[]>([])
  const [streamingTick, setStreamingTick] = useState(0)
  const [disarmInfo, setDisarmInfo] = useState<{ reason: string; reasonCode?: string } | null>(null)
  const [streamingTarget, setStreamingTarget] = useState<{
    provider: string
    profile: string | null
    generationShort: string
  }>({ provider: provider ?? 'unknown', profile: agentProfile ?? null, generationShort: '—' })
  const engineRef = useRef<StreamingEngine | null>(null)
  // r15 (§6.7): whether armed batches declare `payload_class: "interactive"`.
  // Set at arm from the per-terminal, build-exact block; never from the
  // top-level union, and never by macros/composer/automation.
  const declareInteractiveRef = useRef(false)
  const macrosButtonRef = useRef<HTMLButtonElement>(null)
  const streamingWsCloseRef = useRef<() => void>(() => {})

  // ── Lane C: composer image attachments (§8.4/§8.7) ───────────────────
  const [attachments, setAttachments] = useState<DraftAttachment[]>([])
  // Announced via the visually-hidden aria-live region (§8.7 item 8).
  const [attachmentNotice, setAttachmentNotice] = useState('')
  const nextTokenRef = useRef(1)
  const attachmentsRef = useRef<DraftAttachment[]>([])
  attachmentsRef.current = attachments
  const fileInputRef = useRef<HTMLInputElement>(null)
  const composerInputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    // Object URLs are the only thing to release; records live server-side.
    const current = attachmentsRef
    return () => {
      current.current.forEach(attachment => URL.revokeObjectURL(attachment.previewUrl))
    }
  }, [])

  useEffect(() => {
    managedRef.current = null
    setPerTerminalLaneC({})
    api.getManagedControl(terminalId)
      .then(result => {
        managedRef.current = result.managed
        setManaged(result.managed)
        setGeneration(result.generation)
        setExecutionMode(result.execution_mode)
        setNativeControlSupported(false)
        setNativeControlResolved(result.execution_mode !== 'native_tui')
        if (result.managed && result.execution_mode === 'native_tui') {
          api.getControlInputCapabilities()
            .then(liveCapabilities => {
              setCapabilities(liveCapabilities)
              setNativeControlSupported(
                liveCapabilities.execution_modes.includes('native_tui')
                && liveCapabilities.literal_write === true
                && liveCapabilities.bracketed_paste === false
                && liveCapabilities.enter_required === true,
              )
              // v3 is the macro/streaming floor: a server that does not
              // advertise it offers the literal bar alone, and the absence
              // of the library affordances is stated rather than silently
              // missing (§3.5).
              const v3 = (liveCapabilities.request_schema_versions ?? []).includes(3)
              setSequenceSupported(v3)
              if (v3) loadMacroLibrary()
              // The composer's Lane C affordances resolve from THIS
              // terminal's build-exact identity controls, never the
              // top-level discovery union (P1.4/§8.6). Failure is fail-closed.
              api.getControlIdentity(terminalId)
                .then(identity => {
                  const entry = perTerminalProviderControls(identity)
                  setPerTerminalLaneC({
                    operatorMessage: entry?.operator_message,
                    image: entry?.image,
                  })
                })
                .catch(() => setPerTerminalLaneC({}))
              setNativeControlResolved(true)
            })
            .catch(() => {
              setNativeControlSupported(false)
              setNativeControlResolved(true)
              setControlStatus('native control capability unavailable')
            })
        }
      })
      .catch(() => {
        // Unknown control identity is not proof this is an ordinary TUI.
        // Keep terminal input fail-closed rather than pasting into a possibly
        // managed bridge pane when the identity lookup is unavailable.
        managedRef.current = null
        setManaged(false)
        setGeneration(undefined)
        setExecutionMode(undefined)
        setNativeControlSupported(false)
        setNativeControlResolved(false)
        setControlStatus('terminal control identity unavailable')
      })
  }, [terminalId])

  // The macro library (§5.4): fetched for the terminal's provider/profile.
  // A 404 is the old-server signal — the library UI is hidden behind a
  // notice, never a fallback to local storage as the authoritative store
  // (§3.5). The per-terminal chord set comes from the identity route's
  // control_input.provider_controls entry — the only chord send authority;
  // absent means the empty set (fail closed, D9).
  const loadMacroLibrary = () => {
    api
      .listMacros({ provider, profile: agentProfile ?? undefined })
      .then(response => {
        setMacros(response.macros)
        setMacroQuarantine(response.quarantine)
        setMacrosUnavailable(false)
      })
      .catch((error: ApiError) => {
        if (error.status === 404) {
          setMacros([])
          setMacrosUnavailable(true)
        } else {
          setControlStatus('macro library unavailable')
        }
      })
    api
      .getControlIdentity(terminalId)
      .then(identity => {
        setAdvertisedChords(new Set(perTerminalSteerChords(identity)))
      })
      .catch(() => setAdvertisedChords(new Set()))
  }

  // Disarm streaming when the terminal is closed or swapped out: timers die
  // with the view and nothing typed later is ever sent (§6.4).
  useEffect(() => {
    return () => {
      engineRef.current?.disarm('terminal view closed')
      engineRef.current = null
    }
  }, [terminalId])

  // Environment disarm (§6.4): page hidden / pagehide while armed.
  useEffect(() => {
    if (!streamingArmed) return
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        engineRef.current?.disarm('page hidden')
      }
    }
    const onPageHide = () => engineRef.current?.disarm('page hidden')
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', onPageHide)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', onPageHide)
    }
  }, [streamingArmed])

  // Environment disarm (§6.4): the output websocket closing while armed.
  // The websocket lives in the xterm effect below; it calls this ref.
  useEffect(() => {
    streamingWsCloseRef.current = () => {
      if (streamingArmed) engineRef.current?.disarm('output websocket closed')
    }
  }, [streamingArmed])

  const runManagedOperation = async (body: {
    action: string
    message?: string
    config_id?: string
    value?: string
    instruction?: string
  }) => {
    const operationId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`${body.action}: submitting…`)
    try {
      const response = await api.beginManagedOperation(terminalId, {
        ...body,
        operation_id: operationId,
        generation,
      })
      const state = String(response.receipt.state || 'unknown')
      const reason = response.receipt.reason_code || response.receipt.reason_detail
      setControlStatus(`${body.action}: ${state}${reason ? ` — ${String(reason)}` : ''}`)
      if (body.action === 'route-query') {
        const result = response.receipt.result as Record<string, unknown> | undefined
        if (result?.model) setModel(String(result.model))
        if (result?.effort) setEffort(String(result.effort))
      }
      if (body.action === 'follow-up' && response.success) setMessage('')
    } catch (error) {
      // The request may have crossed the provider boundary before the HTTP
      // response was lost. Query the same durable operation; never invent a
      // second ID and repeat a potentially accepted effect.
      try {
        const reconciled = await api.queryManagedOperation(
          terminalId,
          operationId,
          generation,
        )
        const state = String(reconciled.receipt.state || 'unknown')
        const reason = reconciled.receipt.reason_code || reconciled.receipt.reason_detail
        setControlStatus(
          `${body.action}: ${state}${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `${body.action}: response unavailable; operation ${operationId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  const runNativeControl = async (text: string, label: string) => {
    const controlId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`${label}: submitting… (${controlId})`)
    try {
      const identity = await api.getControlIdentity(terminalId)
      const expectedIdentity = Object.fromEntries(
        [
          'terminal_id',
          'terminal_incarnation',
          'terminal_generation',
          'pane_birth_id',
          'provider_process_id',
          'provider',
          'native_session_id',
          'execution_mode',
          'session_name',
        ].map(key => [key, identity[key] ?? null]),
      )
      const response = await api.sendControlInput(terminalId, {
        control_id: controlId,
        text,
        enter: true,
        expected_identity: expectedIdentity,
      })
      const outcome = String(response.outcome || 'unknown')
      const reason = outcomeReason(response)
      setControlStatus(
        `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
      )
      if (label === 'send' && outcome === 'accepted') setMessage('')
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        setControlStatus(
          `${label}: unsupported (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        setControlStatus(
          `${label}: refused (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      try {
        const response = await api.queryControlInput(controlId)
        const outcome = String(response.outcome || 'unknown')
        const reason = outcomeReason(response)
        setControlStatus(
          `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `${label}: response unavailable; control ${controlId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  // ── Lane C: attachment staging and the operator-message send (§8.3-8.5) ──

  const updateAttachment = (localId: string, patch: Partial<DraftAttachment>) => {
    setAttachments(prev =>
      prev.map(attachment =>
        attachment.localId === localId ? { ...attachment, ...patch } : attachment,
      ),
    )
  }

  /** Insert `[Image #N]` markers at the caret in ONE functional update
   * (P1.3): a batch composes with any other pending message edit instead
   * of each token overwriting the previous from a stale closure. */
  const insertTokensAtCaret = (tokens: number[]) => {
    if (tokens.length === 0) return
    const markers = tokens.map(token => `[Image #${token}]`).join('')
    const input = composerInputRef.current
    const focused = input && document.activeElement === input
    const start = focused ? (input.selectionStart ?? message.length) : message.length
    const end = focused ? (input.selectionEnd ?? message.length) : message.length
    setMessage(prev => {
      const safeStart = Math.min(start, prev.length)
      const safeEnd = Math.min(Math.max(end, safeStart), prev.length)
      return prev.slice(0, safeStart) + markers + prev.slice(safeEnd)
    })
    requestAnimationFrame(() => {
      if (composerInputRef.current) {
        composerInputRef.current.focus()
        composerInputRef.current.selectionStart = start + markers.length
        composerInputRef.current.selectionEnd = start + markers.length
      }
    })
  }

  const uploadAttachment = async (localId: string, file: File) => {
    try {
      const { attachment } = await api.uploadAttachment(terminalId, file)
      // The token may have been deleted while the upload was in flight:
      // stage it, then delete the orphaned record so nothing lingers.
      if (!attachmentsRef.current.some(candidate => candidate.localId === localId)) {
        void api.deleteAttachment(terminalId, attachment.attachment_id).catch(() => {})
        return
      }
      updateAttachment(localId, { state: 'ready', record: attachment, error: undefined })
      setAttachmentNotice(
        `Image #${attachmentsRef.current.find(c => c.localId === localId)?.token}: ` +
        `${attachment.display_filename}, ${formatBytes(attachment.size_bytes)}, ready`,
      )
    } catch (error) {
      const apiError = error as ApiError
      const body = apiError.body as AttachmentRefusalBody | undefined
      const detail = body?.detail || apiError.detail || 'the upload failed'
      updateAttachment(localId, {
        state: 'failed',
        record: body?.attachment,
        error: detail,
      })
      setAttachmentNotice(`Image upload failed — ${detail}`)
    }
  }

  /** Stage one picker/paste batch (P1.3): the remaining slot count is
   * computed once, the batch is accepted into one functional state update
   * with one linked editable token per accepted file, and selection can
   * never exceed the advertised maximum or overwrite earlier tokens. */
  const stageFiles = (files: File[]) => {
    if (files.length === 0) return
    const advertised = imageBlock?.formats ?? []
    const remainingSlots = Math.max(0, maxAttachments - attachmentsRef.current.length)
    const accepted = files.slice(0, remainingSlots)
    const skipped = files.length - accepted.length
    if (skipped > 0) {
      setControlStatus(
        `at most ${maxAttachments} images ride one operator message — ` +
        `${accepted.length} accepted, ${skipped} skipped; ` +
        `${remainingSlots - accepted.length} slots remain`,
      )
    }
    if (accepted.length === 0) return
    const drafts: DraftAttachment[] = accepted.map(file => {
      const format = MIME_TO_FORMAT[file.type]
      const formatAdvertised = format !== undefined && advertised.includes(format)
      return {
        localId: crypto.randomUUID(),
        token: nextTokenRef.current++,
        file,
        previewUrl: URL.createObjectURL(file),
        state: formatAdvertised ? 'staging' : 'failed',
        error: formatAdvertised
          ? undefined
          : `${file.type || 'this content type'} is not advertised by this provider ` +
            `(${advertised.join(', ') || 'none'}); unproven formats are refused, not converted`,
      }
    })
    setAttachments(prev => [...prev, ...drafts])
    insertTokensAtCaret(drafts.map(draft => draft.token))
    for (const draft of drafts) {
      if (draft.state === 'staging') {
        setAttachmentNotice(`Image #${draft.token} uploading…`)
        void uploadAttachment(draft.localId, draft.file)
      } else {
        setAttachmentNotice(`Image #${draft.token} refused — ${draft.error}`)
      }
    }
  }

  /** Remove the chip and its token; the server record is deleted best-effort. */
  const removeAttachment = (target: DraftAttachment) => {
    setMessage(prev => prev.replace(`[Image #${target.token}]`, ''))
    if (target.record) {
      void api.deleteAttachment(terminalId, target.record.attachment_id).catch(() => {})
    }
    URL.revokeObjectURL(target.previewUrl)
    setAttachments(prev => prev.filter(attachment => attachment.localId !== target.localId))
    setAttachmentNotice(`Image #${target.token} removed`)
    composerInputRef.current?.focus()
  }

  const retryAttachment = (target: DraftAttachment) => {
    if (target.record) {
      void api.deleteAttachment(terminalId, target.record.attachment_id).catch(() => {})
    }
    updateAttachment(target.localId, { state: 'staging', record: undefined, error: undefined })
    setAttachmentNotice(`Image #${target.token} uploading…`)
    void uploadAttachment(target.localId, target.file)
  }

  /** Message edits detach any attachment whose token was deleted (§8.7.4). */
  const handleMessageChange = (value: string) => {
    setMessage(value)
    const present = new Set(
      [...value.matchAll(IMAGE_TOKEN_PATTERN)].map(match => Number(match[1])),
    )
    for (const attachment of attachmentsRef.current) {
      if (!present.has(attachment.token)) {
        if (attachment.record) {
          void api.deleteAttachment(terminalId, attachment.record.attachment_id).catch(() => {})
        }
        URL.revokeObjectURL(attachment.previewUrl)
        setAttachments(prev =>
          prev.filter(candidate => candidate.localId !== attachment.localId),
        )
        setAttachmentNotice(`Image #${attachment.token} detached`)
      }
    }
  }

  const handleComposerPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData?.files ?? [])
    if (files.length === 0) return // plain-text paste: ordinary text paste
    event.preventDefault()
    if (!imageAttachAvailable) {
      setControlStatus(
        'image attachments are unavailable on this terminal’s provider build — ' +
        'no proven image support is advertised; text and multiline still send',
      )
      return
    }
    stageFiles(files)
  }

  /** The §8.3 send: one typed operation, one reconcile, never a resend. */
  const runOperatorMessage = async () => {
    const operationId = crypto.randomUUID()
    const text = message
    const tokenMap: Record<string, string> = {}
    const attachmentIds: string[] = []
    for (const attachment of attachmentsRef.current) {
      if (attachment.record) {
        tokenMap[String(attachment.token)] = attachment.record.attachment_id
        attachmentIds.push(attachment.record.attachment_id)
      }
    }
    // The one post-accept path (P1.5): a direct accepted POST and an
    // exact-id reconcile that resolves `accepted` are the same fact, so
    // both retire the draft and chips identically — the operation id stays
    // named in the status for any later reconcile, and with the draft gone
    // a new click cannot mint a second operation for the same message.
    const applyAccepted = () => {
      attachmentsRef.current.forEach(attachment => URL.revokeObjectURL(attachment.previewUrl))
      setAttachments([])
      nextTokenRef.current = 1
      setMessage('')
    }
    setControlBusy(true)
    setControlStatus(`operator message: submitting… (${operationId})`)
    try {
      const identity = await api.getControlIdentity(terminalId)
      const response = await api.submitOperatorMessage(terminalId, {
        operation_id: operationId,
        text,
        attachments: attachmentIds,
        token_map: tokenMap,
        expected_identity: pickExpectedIdentity(identity),
      })
      const outcome = String(response.outcome || 'unknown')
      const reason = outcomeReason(response)
      setControlStatus(
        `operator message: ${outcome} (${operationId})${reason ? ` — ${String(reason)}` : ''}`,
      )
      if (outcome === 'accepted') applyAccepted()
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        setControlStatus(
          `operator message: unsupported (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        setControlStatus(
          `operator message: refused (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      // Lost response: exactly one exact-id reconcile, never a resend (§8.3).
      try {
        const response = await api.reconcileOperatorMessage(operationId)
        const outcome = String(response.outcome || 'unknown')
        const reason = outcomeReason(response)
        setControlStatus(
          `operator message: ${outcome} (${operationId})${reason ? ` — ${String(reason)}` : ''}`,
        )
        // An accepted reconcile is the same post-accept fact as a direct
        // response (P1.5): retire the draft and chips identically.
        if (outcome === 'accepted') applyAccepted()
      } catch {
        setControlStatus(
          `operator message: response unavailable; operation ${operationId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  // --- Lane B: macro sends and streaming (§5.4 send path, §6) -------------
  //
  // Sending a macro is NOT a store operation (§5.4): the client takes the
  // resolved events and sends an ordinary v3 control-input request (D2)
  // with the deployed per-send identity re-proof and outcome reporting.
  // Streaming pins the identity once at arm instead (§6.3 step 4).

  const runNativeSequence = async (
    events: SequenceEvent[],
    label = 'sequence',
    options?: { commandClass?: boolean },
  ) => {
    const controlId = crypto.randomUUID()
    setControlBusy(true)
    setControlStatus(`${label}: submitting… (${controlId})`)
    try {
      const identity = await api.getControlIdentity(terminalId)
      const expectedIdentity = pickExpectedIdentity(identity)
      const response = await api.sendControlInput(terminalId, {
        control_id: controlId,
        events,
        // §4.1: only the registry Compact built-in declares command-class,
        // and only after the command_controls block is advertised (gated by
        // the caller). Streaming and ordinary macros NEVER set this field.
        ...(options?.commandClass ? { payload_class: 'command' as const } : {}),
        expected_identity: expectedIdentity,
      })
      const outcome = String(response.outcome || 'unknown')
      const reason = outcomeReason(response)
      const perEvent = Array.isArray(response.events)
        ? ` [${(response.events as Array<Record<string, unknown>>)
            .map(entry => String(entry.outcome ?? '—'))
            .join(', ')}]`
        : ''
      setControlStatus(
        `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}${perEvent}`,
      )
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        setControlStatus(
          `${label}: unsupported (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        setControlStatus(
          `${label}: refused (HTTP ${apiError.status})`
          + (apiError.detail ? ` — ${apiError.detail}` : ''),
        )
        return
      }
      // The response may have crossed the pane boundary before it was
      // lost. Resolve by the exact control id; never re-send the events.
      try {
        const response = await api.queryControlInput(controlId)
        const outcome = String(response.outcome || 'unknown')
        const reason = outcomeReason(response)
        setControlStatus(
          `${label}: ${outcome} (${controlId})${reason ? ` — ${String(reason)}` : ''}`,
        )
      } catch {
        setControlStatus(
          `${label}: response unavailable; control ${controlId} retained for reconciliation`,
        )
      }
    } finally {
      setControlBusy(false)
    }
  }

  // One tap = one v3 request (§7.2, D2). The Compact built-in is
  // command-class (§4.1): it declares payload_class "command" only when the
  // server advertised the command_controls block; otherwise it sends
  // without the field and the guard-absent notice says why (§3.5).
  const sendMacro = (macro: MacroRecord) => {
    void runNativeSequence(macro.events, `macro "${macro.name}"`, {
      commandClass: macro.builtin_kind === 'compact' && commandGuardAvailable,
    })
  }

  // §6.3 step 7 + §3.4: the POST's typed body wins; an ambiguous transport
  // result (including a lost response) is reconciled by exactly one
  // exact-id GET — the journaled record is the truth and a batch is never
  // re-sent. The engine routes the resolved outcome per §6.4. An armed
  // batch declares `payload_class: "interactive"` only when the
  // per-terminal block advertised it (§6.7) — the declaration rides the
  // same POST body, the wire sequence is otherwise unchanged.
  const sendStreamBatch = async (
    controlId: string,
    events: SequenceEvent[],
    expectedIdentity: Record<string, unknown>,
  ): Promise<SendResult> => {
    try {
      const response = await api.sendControlInput(terminalId, {
        control_id: controlId,
        events,
        ...(declareInteractiveRef.current ? { payload_class: 'interactive' as const } : {}),
        expected_identity: expectedIdentity,
      })
      return { kind: 'resolved', result: typedOutcome(response) }
    } catch (error) {
      const apiError = error as ApiError
      if (apiError.status && CONTROL_UNSUPPORTED_STATUSES.has(apiError.status)) {
        return { kind: 'resolved', result: { outcome: 'unsupported', reasonDetail: apiError.detail } }
      }
      if (
        apiError.status
        && !CONTROL_AMBIGUOUS_STATUSES.has(apiError.status)
        && apiError.status >= 400
        && apiError.status < 500
      ) {
        return { kind: 'resolved', result: { outcome: 'refused', reasonDetail: apiError.detail } }
      }
      try {
        const record = await api.queryControlInput(controlId)
        return { kind: 'resolved', result: typedOutcome(record) }
      } catch {
        return { kind: 'reconcile-failed' }
      }
    }
  }

  // §6.1 arming: fetch managed-control, control-identity, and capabilities
  // fresh; pin the 9-field expected_identity and the per-terminal chord
  // set; display provider / agent profile / generation in the armed
  // header. Arming replaces the composer with the capture surface; the
  // ordinary composer is restored on disarm with its draft preserved.
  const armStreaming = async () => {
    setArming(true)
    setControlStatus('')
    try {
      const [managedControl, identity, liveCapabilities] = await Promise.all([
        api.getManagedControl(terminalId),
        api.getControlIdentity(terminalId),
        api.getControlInputCapabilities(),
      ])
      if (!managedControl.managed || managedControl.execution_mode !== 'native_tui') {
        setControlStatus('streaming: terminal is no longer native-managed')
        return
      }
      const fullKeySet = FULL_SEQUENCE_KEY_SET.every(key =>
        liveCapabilities.sequence?.keys?.includes(key),
      )
      if (liveCapabilities.streaming?.supported !== true || !fullKeySet) {
        setControlStatus('streaming: server predates streaming')
        return
      }
      const expectedIdentity = pickExpectedIdentity(identity)
      const providerEntry = perTerminalProviderControls(identity)
      const chords = new Set(providerEntry?.steer_chords ?? [])
      // §6.7 (r15): armed batches declare interactive only when THIS
      // terminal's build-exact block advertises it. An old or unpinned
      // server omits the block: the armed surface keeps the §6.4 readiness
      // behavior (busy turns pause batches) and says so — never a
      // speculative bypass.
      const interactiveAdvertised = providerEntry?.interactive_streaming?.supported === true
      declareInteractiveRef.current = interactiveAdvertised
      const graceMs =
        providerEntry?.dispatch_grace_ms ??
        (provider ? liveCapabilities.provider_controls?.[provider]?.dispatch_grace_ms : undefined)
      const generationShort = String(
        identity.terminal_generation ?? managedControl.generation ?? '—',
      ).slice(0, 6)
      const engine = new StreamingEngine(
        {
          coalesceWindowMs: liveCapabilities.streaming.coalesce_window_ms ?? 200,
          dispatchGraceMs: graceMs,
          declareInteractive: interactiveAdvertised,
          advertisedChords: chords,
        },
        {
          onSendBatch: (controlId, events) => sendStreamBatch(controlId, events, expectedIdentity),
          onTrace: trace => setStreamingTrace(trace),
          onDisarm: (reason, reasonCode) => {
            setStreamingArmed(false)
            setDisarmInfo({ reason, reasonCode })
            if (reasonCode && IDENTITY_REFUSAL_CODES.has(reasonCode)) {
              // §6.4: on any identity refusal, refetch identity so the
              // explanation names the new generation.
              api
                .getControlIdentity(terminalId)
                .then(fresh => {
                  const gen = String(fresh.terminal_generation ?? '?').slice(0, 6)
                  setDisarmInfo(info =>
                    info ? { ...info, reason: `${info.reason} — current generation ${gen}` } : info,
                  )
                })
                .catch(() => {})
            }
          },
          onChange: () => setStreamingTick(tick => tick + 1),
        },
      )
      engineRef.current = engine
      setAdvertisedChords(chords)
      setStreamingTarget({
        provider: provider ?? 'unknown',
        profile: agentProfile ?? null,
        generationShort,
      })
      setStreamingTrace([])
      setDisarmInfo(null)
      setStreamingArmed(true)
      if (!interactiveAdvertised) {
        setControlStatus(
          'streaming: interactive declaration unavailable on this server — ' +
          'busy provider turns pause batches (§6.4), nothing is bypassed',
        )
      }
    } catch {
      setControlStatus('streaming could not arm: capabilities or identity fetch failed')
    } finally {
      setArming(false)
    }
  }

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: TERMINAL_FONT_SIZE,
      fontFamily: 'JetBrains Mono, Menlo, Monaco, Consolas, monospace',
      scrollback: 10000,
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
        cursor: '#58a6ff',
        selectionBackground: '#264f78',
        black: '#0d1117',
        red: '#ff7b72',
        green: '#3fb950',
        yellow: '#d29922',
        blue: '#58a6ff',
        magenta: '#bc8cff',
        cyan: '#39d353',
        white: '#c9d1d9',
      },
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    // Touch-scroll support. xterm.js core forwards DOM `wheel` events as
    // mouse-wheel escape sequences whenever the attached app is in
    // mouse-tracking mode (here: tmux with `mouse on`), which is why a desktop
    // mouse wheel scrolls the terminal. xterm has no touch handling, so a finger
    // swipe on a phone produces no wheel event and nothing scrolls. Translate a
    // single-finger vertical swipe into synthetic `wheel` events dispatched onto
    // xterm's root element, driving that same already-working path so the swipe
    // produces the same kind of mouse-wheel report the server already acts on.
    // (Equivalent in effect to a desktop wheel — not necessarily byte-identical,
    // since coordinates and delta encoding differ; see the caveats below.)
    //
    // Emit line-mode deltas (DOM_DELTA_LINE, one notch per row of travel) rather
    // than pixel deltas: xterm 6 treats a small pixel-mode wheel delta as a
    // trackpad and damps it to ~0.3 of a line, which would make touch scrolling
    // roughly 3x too slow. The line-mode path bypasses that damping, so one row
    // of finger travel scrolls one line.
    //
    // Scope/caveats (kept deliberately minimal; both worth noting upstream):
    //   - Mouse-tracking only. This scrolls when the attached app is in
    //     mouse-tracking mode (tmux `mouse on`, or alt-buffer apps that report
    //     the wheel). In a plain shell the synthetic event reaches no listener —
    //     xterm's own scrollback viewport listens on a descendant element, not
    //     term.element — so a swipe does nothing there.
    //   - Single pane. The synthetic wheel carries default coordinates (col 1,
    //     row 1), so a multi-pane tmux layout routes every swipe to the
    //     top-left pane rather than the pane under the finger.
    // Desktop behavior is untouched (touch-only listeners).
    const wheelTarget = term.element ?? el

    // Row height in px, so a swipe distance maps to a matching number of wheel
    // notches. Derived from the live geometry (no private xterm internals);
    // falls back to a font-size estimate before the first layout.
    const rowHeight = (): number => {
      const rows = term.rows
      const height = el.clientHeight
      return rows > 0 && height > 0 ? height / rows : DEFAULT_LINE_HEIGHT
    }

    let touchY: number | null = null
    let scrollAcc = 0

    const onTouchStart = (ev: TouchEvent) => {
      if (ev.touches.length !== 1) {
        touchY = null
        return
      }
      touchY = ev.touches[0].clientY
      scrollAcc = 0
    }

    const onTouchMove = (ev: TouchEvent) => {
      if (touchY === null || ev.touches.length !== 1) return
      const y = ev.touches[0].clientY
      // Finger moving up (clientY decreasing) scrolls toward newer output,
      // matching a downward mouse-wheel notch.
      scrollAcc += touchY - y
      touchY = y
      const lineH = rowHeight()
      while (Math.abs(scrollAcc) >= lineH) {
        const dir = scrollAcc > 0 ? 1 : -1
        // One line-mode notch per row of accumulated travel. The pixel
        // accumulator (lineH per notch) sets the cadence; DOM_DELTA_LINE keeps
        // xterm 6 from damping the delta as a trackpad gesture.
        wheelTarget.dispatchEvent(
          new WheelEvent('wheel', {
            deltaY: dir,
            deltaMode: WheelEvent.DOM_DELTA_LINE,
            bubbles: true,
            cancelable: true,
          })
        )
        scrollAcc -= dir * lineH
      }
      // Stop the browser from panning the page / rubber-banding instead.
      ev.preventDefault()
    }

    const endTouch = () => {
      touchY = null
      scrollAcc = 0
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', endTouch, { passive: true })
    el.addEventListener('touchcancel', endTouch, { passive: true })

    // Connect WebSocket
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${location.host}/terminals/${terminalId}/ws`)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      // Fit once the connection is live so we send correct dimensions
      fitAddon.fit()
      ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
      }
    }

    ws.onclose = () => {
      term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n')
      // §6.4 environment disarm: the output websocket closing while armed.
      streamingWsCloseRef.current()
    }

    // Copy selection to clipboard on mouse-up
    term.onSelectionChange(() => {
      const selection = term.getSelection()
      if (selection) {
        navigator.clipboard.writeText(selection).catch(() => {})
      }
    })

    // Ctrl+Shift+C to copy selection
    term.attachCustomKeyEventHandler((e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'C') {
        const selection = term.getSelection()
        if (selection) navigator.clipboard.writeText(selection).catch(() => {})
        return false
      }
      return true
    })

    // onData handles ALL input including paste — xterm.js
    // receives pasted text through the browser's input system
    term.onData((data) => {
      // A managed pane is a rendered view of a private provider RPC stream.
      // Its stdin is deliberately not a provider input channel; controls use
      // the exact generation-bound API above.  Hold input while managed status
      // is unresolved so an early paste cannot leak into the wrong transport.
      if (
        ws.readyState === WebSocket.OPEN
        && (
          managedRef.current === false
          || (managedRef.current === true && isWheelMouseReport(data))
        )
      ) {
        ws.send(JSON.stringify({ type: 'input', data }))
      }
    })

    // Handle resize — debounce to avoid flooding
    let resizeTimer: ReturnType<typeof setTimeout>
    const resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }))
        }
      }, 50)
    })
    resizeObserver.observe(el)

    // Initial fit after layout settles
    const initialFit = requestAnimationFrame(() => {
      fitAddon.fit()
    })

    term.focus()

    return () => {
      cancelAnimationFrame(initialFit)
      clearTimeout(resizeTimer)
      resizeObserver.disconnect()
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', endTouch)
      el.removeEventListener('touchcancel', endTouch)
      ws.close()
      term.dispose()
    }
  }, [terminalId])

  const nativeManaged = managed && executionMode === 'native_tui'
  const acpManaged = managed && executionMode === 'acp'

  // ── Lane B capability gating (§3.5/D9: advertisement, never probing) ──
  const fullKeySetAdvertised = FULL_SEQUENCE_KEY_SET.every(key =>
    capabilities?.sequence?.keys?.includes(key),
  )
  const streamingAdvertised = capabilities?.streaming?.supported === true && fullKeySetAdvertised
  const providerControlEntry = provider ? capabilities?.provider_controls?.[provider] : undefined
  // §3.5: provider_controls absent → built-ins hidden (user macros still
  // available when v3 is advertised); providers with no registry entry hide
  // the built-ins and the modal states why (§13, OD3).
  const builtinsVisible = providerControlEntry != null
  // §4.1 rule 4: payload_class is sent only when command_controls is
  // advertised — never earlier, never as a shape probe.
  const commandGuardAvailable = capabilities?.command_controls != null
  const visibleMacros = macros.filter(macro =>
    macro.origin === 'builtin' ? builtinsVisible : sequenceSupported,
  )
  const favorites = visibleMacros.filter(macro => macro.favorite)
  const compactGuardNotice =
    !commandGuardAvailable && visibleMacros.some(macro => macro.builtin_kind === 'compact')
      ? 'prefill-concatenation guard unavailable on this server'
      : null
  const composerBytes = new TextEncoder().encode(message).length

  // ── Lane C composer routing (§8.5): one composer, two explicitly named
  // operations — never silent truncation, never a surprise 422. A text-only
  // single-line draft ≤ 512 bytes rides the deployed control-input path
  // byte-identically; anything else uses the operator-message path when the
  // provider advertises it, or disables Send with the reason when not (D9:
  // the advertised capability, never a probe). The blocks come from the
  // per-terminal, build-exact identity controls — not the top-level
  // discovery union (P1.4): a kimi 0.29.0/0.29.1 build advertises the
  // message block without the image block, and the composer reflects it.
  const operatorMessageBlock = perTerminalLaneC.operatorMessage
  const imageBlock = perTerminalLaneC.image
  const operatorMessageAvailable = operatorMessageBlock?.supported === true
  const imageAttachAvailable = operatorMessageAvailable && imageBlock?.supported === true
  const maxMessageBytes = operatorMessageBlock?.max_text_bytes ?? 8192
  const maxAttachments = operatorMessageBlock?.max_attachments ?? 4
  const hasAttachments = attachments.length > 0
  const needsOperatorMessage =
    hasAttachments || message.includes('\n') || composerBytes > MAX_COMPOSER_BYTES
  const unresolvedAttachments = attachments.filter(attachment => attachment.state !== 'ready')
  const overMessageLimit = needsOperatorMessage && composerBytes > maxMessageBytes
  const hasContent = message.trim().length > 0 || hasAttachments
  const canSend =
    !controlBusy
    && hasContent
    && !(needsOperatorMessage && !operatorMessageAvailable)
    && unresolvedAttachments.length === 0
    && !overMessageLimit
  const sendDraft = () => {
    if (needsOperatorMessage) void runOperatorMessage()
    else void runNativeControl(message.trim(), 'send')
  }

  return (
    // marginTop: 0 — DashboardHome lays this view out as a later sibling
    // inside a `space-y-6` container, whose `> * + *` rule would otherwise
    // apply a 24 px margin-top even to this fixed overlay, leaving a
    // dashboard strip visible above the supposedly fullscreen terminal.
    <div className="fixed inset-0 z-50 flex flex-col" style={{ background: '#0d1117', marginTop: 0 }}>
      {/* Header — wraps within the viewport so Close stays reachable at
          mobile widths instead of overflowing past the right edge. */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-gray-700/50 bg-gray-900 px-4 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <TermIcon size={16} className="shrink-0 text-emerald-400" />
          <span className="truncate text-sm font-mono text-gray-300">{terminalId}</span>
          {provider && <span className="text-xs text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{provider}</span>}
          {agentProfile && <span className="text-xs text-emerald-400 bg-emerald-900/30 px-2 py-0.5 rounded">{agentProfile}</span>}
          {managed && (
            <span className="text-xs text-cyan-300 bg-cyan-900/30 px-2 py-0.5 rounded">
              {nativeManaged
                ? 'Managed native TUI · identity-bound controls'
                : acpManaged
                  ? 'Managed ACP · read-only transcript'
                  : 'Managed mode unknown · controls unavailable'}
            </span>
          )}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-3">
          <span className="hidden text-[10px] text-gray-600 sm:block">Click X to close</span>
          <button
            onClick={onClose}
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-1 text-gray-500 transition-colors hover:text-white"
            title="Close terminal"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      {nativeManaged && nativeControlSupported && (
        <div
          data-testid="native-control-area"
          className="shrink min-h-0 overflow-y-auto border-b border-gray-700/50 bg-gray-950 px-4 py-2 space-y-2"
        >
          {streamingArmed && engineRef.current ? (
            <StreamingPanel
              engine={engineRef.current}
              provider={streamingTarget.provider}
              agentProfile={streamingTarget.profile}
              generationShort={streamingTarget.generationShort}
              trace={streamingTrace}
              tick={streamingTick}
              onStop={() => engineRef.current?.disarm('operator stopped streaming')}
              onClearTrace={() => engineRef.current?.clearTrace()}
            />
          ) : (
            <div data-testid="composer-row" className="flex flex-wrap items-center gap-2">
              {imageAttachAvailable && (
                <>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept={imageBlock?.formats.map(format => `image/${format}`).join(',')}
                    multiple
                    className="hidden"
                    aria-hidden="true"
                    tabIndex={-1}
                    data-testid="attachment-file-input"
                    onChange={event => {
                      const files = Array.from(event.target.files ?? [])
                      if (files.length > 0) stageFiles(files)
                      // Reset so picking the same file twice still fires.
                      event.target.value = ''
                    }}
                  />
                  <button
                    type="button"
                    aria-label="Attach an image"
                    title={`Attach an image (${(imageBlock?.formats ?? []).join(', ')})`}
                    disabled={controlBusy}
                    onClick={() => fileInputRef.current?.click()}
                    className="flex min-h-[36px] min-w-[36px] items-center justify-center rounded bg-gray-800 px-2 py-1.5 text-gray-200 transition-colors hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  >
                    <Paperclip size={15} />
                  </button>
                </>
              )}
              <textarea
                ref={composerInputRef}
                value={message}
                onChange={event => handleMessageChange(event.target.value)}
                onPaste={handleComposerPaste}
                onKeyDown={event => {
                  // Enter sends (the accepted §8.5 behavior); Shift+Enter
                  // composes a newline — a multiline draft routes to the
                  // operator-message path, named live in the routing line.
                  if (event.key === 'Enter' && !event.shiftKey && canSend) {
                    event.preventDefault()
                    sendDraft()
                  }
                }}
                rows={Math.min(4, message.split('\n').length)}
                placeholder="Send a message to the native composer…"
                aria-label="Message to the native composer"
                className="min-h-[44px] min-w-[44px] flex-1 resize-none rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm text-gray-200 focus:border-emerald-500 focus:outline-none sm:min-h-0 sm:min-w-0"
              />
              <button
                disabled={!canSend}
                onClick={sendDraft}
                className="min-h-[44px] min-w-[44px] rounded bg-emerald-700 px-3 py-1.5 text-xs text-white transition-colors hover:bg-emerald-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500 sm:min-h-[36px] sm:min-w-0"
              >
                Send
              </button>
              <button
                type="button"
                aria-pressed={false}
                disabled={controlBusy || arming || !streamingAdvertised}
                onClick={() => void armStreaming()}
                title={
                  streamingAdvertised
                    ? 'Arm streaming mode: type directly to the terminal in bounded identity-bound batches'
                    : 'Streaming needs a server advertising streaming support and the full key set'
                }
                className="min-h-[44px] min-w-[44px] rounded bg-gray-800 px-3 py-1.5 text-xs text-gray-200 transition-colors hover:bg-gray-700 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500 sm:min-h-[36px] sm:min-w-0"
              >
                {arming ? 'Arming…' : 'Streaming'}
              </button>
              {sequenceSupported && !macrosUnavailable && (
                <button
                  type="button"
                  ref={macrosButtonRef}
                  onClick={() => setMacroModalOpen(true)}
                  className="relative min-h-[44px] min-w-[44px] rounded bg-gray-800 px-3 py-1.5 text-xs text-gray-200 transition-colors hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-500 sm:min-h-[36px] sm:min-w-0"
                >
                  Macros
                  {visibleMacros.length > 0 && (
                    <span className="ml-1.5 rounded-full bg-gray-700 px-1.5 py-0.5 text-[10px] text-gray-300">
                      {visibleMacros.length}
                    </span>
                  )}
                </button>
              )}
            </div>
          )}
          {!streamingArmed && attachments.length > 0 && (
            <ul
              role="list"
              aria-label="Image attachments"
              data-testid="attachment-strip"
              className="flex max-h-14 items-stretch gap-2 overflow-x-auto"
            >
              {attachments.map(attachment => (
                <li
                  key={attachment.localId}
                  className="flex items-center gap-1.5 rounded border border-gray-700 bg-gray-900 p-1"
                >
                  <span className="relative shrink-0">
                    <img
                      src={attachment.previewUrl}
                      alt={
                        `Image #${attachment.token}: ` +
                        `${attachment.record?.display_filename ?? attachment.file.name}, ` +
                        `${formatBytes(attachment.record?.size_bytes ?? attachment.file.size)}, ` +
                        `${attachment.state === 'staging' ? 'uploading' : attachment.state}`
                      }
                      className="h-9 w-9 rounded object-cover"
                    />
                    <span
                      aria-hidden="true"
                      className="absolute -left-1 -top-1 rounded bg-emerald-800 px-1 text-[9px] font-semibold text-emerald-100"
                    >
                      {attachment.token}
                    </span>
                  </span>
                  <span className="flex min-w-0 flex-col justify-center">
                    <span className="max-w-28 truncate text-[10px] text-gray-300">
                      {attachment.record?.display_filename ?? attachment.file.name}
                    </span>
                    {attachment.state === 'staging' && (
                      <span className="text-[9px] text-gray-500">uploading…</span>
                    )}
                    {attachment.state === 'ready' && (
                      <span className="text-[9px] text-emerald-400">ready</span>
                    )}
                    {attachment.state === 'failed' && (
                      <span className="max-w-40 truncate text-[9px] text-amber-300" title={attachment.error}>
                        {attachment.error ?? 'upload failed'}
                      </span>
                    )}
                  </span>
                  {attachment.state === 'failed' && (
                    <button
                      type="button"
                      aria-label={`Retry image #${attachment.token} upload`}
                      disabled={controlBusy}
                      onClick={() => retryAttachment(attachment)}
                      className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-400 transition-colors hover:text-white disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    >
                      <RotateCcw size={14} />
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label={`Remove image #${attachment.token}`}
                    disabled={controlBusy}
                    onClick={() => removeAttachment(attachment)}
                    className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-gray-500 transition-colors hover:text-white disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                  >
                    <X size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {disarmInfo && !streamingArmed && (
            <div
              role="alert"
              className="space-y-1 rounded border border-amber-600/60 bg-amber-950/30 px-3 py-2"
            >
              <div className="text-xs text-amber-200">
                Streaming disarmed: {disarmInfo.reason}
              </div>
              {streamingTrace.length > 0 && (
                <div className="max-h-24 space-y-0.5 overflow-y-auto font-mono text-[10px] text-gray-400">
                  {streamingTrace.map((entry, index) => (
                    <div key={index}>
                      {entry.preview || '(no events)'} — {entry.outcome}
                      {entry.reasonCode ? `/${entry.reasonCode}` : ''} ({entry.controlIdShort})
                      {entry.note ? ` ${entry.note}` : ''}
                    </div>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-2 pt-1">
                <button
                  type="button"
                  disabled={arming || !streamingAdvertised}
                  onClick={() => void armStreaming()}
                  className="min-h-[36px] rounded bg-emerald-700 px-3 py-1 text-xs text-white transition-colors hover:bg-emerald-600 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  Re-arm
                </button>
                <button
                  type="button"
                  onClick={() => {
                    engineRef.current = null
                    setDisarmInfo(null)
                    setStreamingTrace([])
                  }}
                  className="min-h-[36px] rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 transition-colors hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}
          <div className="flex items-center gap-2 text-[11px] text-gray-400">
            <span>Cancel, route, effort, and resume controls are unavailable for native TUI sessions.</span>
            <span className="min-w-0 truncate">{controlStatus}</span>
          </div>
          <div className="text-[10px] text-gray-400" data-testid="composer-route-status">
            {needsOperatorMessage ? (
              operatorMessageAvailable ? (
                <>
                  operator message — {formatBytes(composerBytes)}
                  {hasAttachments &&
                    `, ${attachments.length} image${attachments.length > 1 ? 's' : ''}`}
                  {unresolvedAttachments.length > 0 && (
                    <span className="text-amber-300"> — waiting on image uploads</span>
                  )}
                </>
              ) : (
                <span className="text-amber-300">
                  operator message unavailable for this provider — this draft needs the
                  operator-message path (&gt;{MAX_COMPOSER_BYTES} bytes, multiline, or an
                  image), which this provider does not advertise
                </span>
              )
            ) : (
              <>delivers as control input · {composerBytes}/{MAX_COMPOSER_BYTES} B</>
            )}
            {overMessageLimit && (
              <span className="text-amber-300">
                {' '}
                — over the {maxMessageBytes}-byte operator-message limit; trim the draft
                (it is refused whole, never silently truncated)
              </span>
            )}
          </div>
          {operatorMessageAvailable && !imageAttachAvailable && (
            <div className="text-[10px] text-gray-600" data-testid="image-unavailable-note">
              Image attachments are unavailable on this terminal&apos;s provider build — the
              build-exact controls advertise no proven image support (kimi image delivery
              is proven on the pinned 0.29.2 build only); text and multiline operator
              messages still send.
            </div>
          )}
          <div aria-live="polite" className="sr-only" data-testid="attachment-notice">
            {attachmentNotice}
          </div>
          {nativeControlResolved && !sequenceSupported && (
            <div className="text-[10px] text-gray-600">
              Macros and streaming need control-input schema v3; this server offers the literal
              control only.
            </div>
          )}
          {sequenceSupported && !streamingAdvertised && (
            <div className="text-[10px] text-gray-600">
              Streaming is unavailable: this server predates streaming (or does not advertise the
              full key set).
            </div>
          )}
          {sequenceSupported && macrosUnavailable && (
            <div className="text-[10px] text-gray-600">
              The macro library is unavailable on this server.
            </div>
          )}
          {macroModalOpen && (
            <MacroLibraryModal
              provider={provider}
              agentProfile={agentProfile}
              macros={macros}
              quarantine={macroQuarantine}
              builtinsVisible={builtinsVisible}
              commandGuardAvailable={commandGuardAvailable}
              advertisedChords={advertisedChords}
              busy={controlBusy}
              onClose={() => {
                setMacroModalOpen(false)
                macrosButtonRef.current?.focus()
              }}
              onSend={sendMacro}
              onChanged={loadMacroLibrary}
            />
          )}
        </div>
      )}
      {/* §7.2 favorites ride a reserved, non-shrinking row directly above the
          terminal. The control area scrolls when it grows tall (armed
          streaming at mobile widths); a strip inside it could be clipped
          beneath the fitted xterm (installed-QA P2). Reserved here, the
          Compact/Stop favorites are always fully visible and operable,
          armed or not. */}
      {nativeManaged && nativeControlSupported && favorites.length > 0 && (
        <div
          data-testid="favorite-strip-row"
          className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-1"
        >
          <FavoriteStrip
            favorites={favorites}
            disabled={controlBusy}
            onSend={sendMacro}
            guardNotice={compactGuardNotice}
          />
        </div>
      )}
      {nativeManaged && nativeControlResolved && !nativeControlSupported && (
        <div className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-2 text-[11px] text-amber-300">
          Native control input is unsupported by this server; no dashboard actions are available.
          <span className="ml-2 text-gray-500">{controlStatus}</span>
        </div>
      )}
      {acpManaged && (
        <div className="shrink-0 border-b border-gray-700/50 bg-gray-950 px-4 py-2 space-y-2">
          <div className="flex gap-2">
            <input
              value={message}
              onChange={event => setMessage(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && message.trim() && !controlBusy) {
                  void runManagedOperation({ action: 'follow-up', message: message.trim() })
                }
              }}
              placeholder="Send a provider-native follow-up…"
              className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-900 px-3 py-1.5 text-sm text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !message.trim()}
              onClick={() => void runManagedOperation({ action: 'follow-up', message: message.trim() })}
              className="rounded bg-emerald-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Send
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'cancel' })}
              className="rounded bg-amber-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Cancel turn
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'compact' })}
              className="rounded bg-indigo-700 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              Compact
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'route-query' })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Query route
            </button>
            <input
              value={model}
              onChange={event => setModel(event.target.value)}
              placeholder="model"
              className="w-56 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !model.trim()}
              onClick={() => void runManagedOperation({ action: 'route-set', config_id: 'model', value: model.trim() })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Set model
            </button>
            <input
              value={effort}
              onChange={event => setEffort(event.target.value)}
              placeholder="effort"
              className="w-32 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 focus:border-emerald-500 focus:outline-none"
            />
            <button
              disabled={controlBusy || !effort.trim()}
              onClick={() => void runManagedOperation({ action: 'route-set', config_id: 'thinking', value: effort.trim() })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Set effort
            </button>
            <button
              disabled={controlBusy}
              onClick={() => void runManagedOperation({ action: 'resume-status' })}
              className="rounded bg-gray-800 px-3 py-1 text-xs text-gray-200 disabled:opacity-40"
            >
              Resume status
            </button>
            <span className="min-w-0 truncate text-[11px] text-gray-500">{controlStatus}</span>
          </div>
        </div>
      )}
      {/* Terminal — absolute positioning gives xterm.js real pixel dimensions to measure.
          The floor keeps the armed streaming terminal at or above half the
          viewport on mobile; the control area above scrolls instead of
          squeezing the xterm below it. FitAddon floors the fit to whole rows,
          so the visible .xterm lands up to one row short of this wrapper
          (390×844 armed: wrapper 422px but .xterm 416px). The +10px pads the
          floor by that row-quantization slack so the actual visible terminal —
          not just the wrapper — clears 50dvh. */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden', minHeight: 'calc(50dvh + 10px)' }}>
        <div ref={containerRef} style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }} />
      </div>
    </div>
  )
}
