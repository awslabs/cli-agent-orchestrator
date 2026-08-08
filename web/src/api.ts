const BASE = ''  // Vite proxy handles routing to backend

/**
 * Error thrown by fetchJSON on a non-OK response. Carries the HTTP status and
 * the server's `detail` string so callers can branch on them (e.g. the graph
 * export's 422 secret-gate path surfaces `detail` — the matched PATTERN name
 * only, never the memory bytes). `message` stays "<status> <statusText>" for
 * back-compat with existing callers.
 */
export interface ApiError extends Error {
  status?: number
  detail?: string
  /** The parsed JSON error body, when there was one (e.g. §5.3 `{errors}`). */
  body?: unknown
}

async function fetchJSON<T>(url: string, opts?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), opts?.timeoutMs ?? 10000)
  try {
    const res = await fetch(`${BASE}${url}`, { ...opts, signal: controller.signal })
    if (!res.ok) {
      // Best-effort read of the JSON error body to expose the server's
      // `detail` without leaking a full response. A non-JSON body is fine —
      // detail just stays undefined.
      let detail: string | undefined
      let body: unknown
      try {
        body = await res.json()
        if (body && typeof (body as { detail?: unknown }).detail === 'string') {
          detail = (body as { detail: string }).detail
        }
      } catch { /* non-JSON error body */ }
      const err: ApiError = new Error(`${res.status} ${res.statusText}`)
      err.status = res.status
      err.detail = detail
      err.body = body
      throw err
    }
    return res.json()
  } finally {
    clearTimeout(timeout)
  }
}

export type SessionLifecycleValue = 'working' | 'pausing' | 'paused' | 'complete' | 'stopped'

// The DECLARED dimension. Deliberately not merged into `Session`, whose
// `status` is tmux attach state (`active`/`detached`) and is a third
// unrelated thing again.
export interface SessionLifecycle {
  session_name: string
  lifecycle: SessionLifecycleValue
  restore_to: string | null
  archived: boolean
  kind: 'campaign' | 'service'
  declared_by: string | null
  note: string | null
  pause_deadline_at: string | null
  epoch: number
  declared: boolean
  suppresses_marshal?: boolean
  pause_overdue?: boolean
  unreadable?: string
}

export interface StopImpactWorker {
  terminal_id: string
  provider: string
  agent_profile: string | null
  reason: string | null
  resumable: boolean
}

export interface StopImpact {
  session_name: string
  live_workers: number
  resumable: StopImpactWorker[]
  not_resumable: StopImpactWorker[]
  resume_machinery_available: boolean
  resume_machinery_reason: string
  one_way_for_every_worker?: boolean
  unreadable?: string
}

export interface Session {
  id: string
  name: string
  status: string
}

export interface Terminal {
  id: string
  name: string
  provider: string
  session_name: string
  agent_profile: string | null
  status: string | null
  last_active: string | null
}

export interface SessionDetail {
  session: Session
  terminals: TerminalMeta[]
}

/**
 * One row of the shared terminal projection.
 *
 * This is `terminal_projection.project_row`'s ACTUAL output, key for key —
 * `/sessions/{name}` returns `project_session(...)` verbatim
 * (services/session_service.py), so anything this interface omits is a field
 * the dashboard cannot see and anything it invents is a field the server never
 * sends. It previously declared seven keys against the real twenty-five, and
 * one of those seven — `created_at` — is not among them: no `TerminalModel`
 * column and no projection key by that name exists, so `t.created_at` was
 * permanently `undefined` in production while three test fixtures fed it a
 * value and made the dead branch look covered. That is the specific failure
 * mode this shape exists to prevent, so keep it driven from `project_row`.
 *
 * `terminal_id`/`name`/`session_name` are the canonical spellings both human
 * views are required to agree on; `id`/`tmux_session`/`tmux_window` are the
 * pre-existing display keys the projection deliberately keeps alongside them.
 */
export interface TerminalMeta {
  // Identity — canonical spellings.
  terminal_id: string
  name: string | null
  session_name: string | null
  // Identity — the pre-existing display keys, kept by the projection.
  id: string
  tmux_session: string | null
  tmux_window: string | null
  // Provenance.
  provider: string | null
  agent_profile: string | null
  caller_id: string | null
  generation: string | null
  callback_target_generation: string | null
  protocol_vintage: string
  // Recorded pane identity.
  server_socket_path: string | null
  session_id: string | null
  window_id: string | null
  pane_id: string | null
  pane_pid: number | string | null
  native_session_id: string | null
  // Observed liveness — never the stored lifecycle.
  lifecycle_state: string
  lifecycle_reason: string | null
  superseded_by_terminal_id: string | null
  superseded_by_generation: string | null
  /** Stated rather than inferred from `status`: no classification is coming. */
  fifo_monitored: boolean
  /** Provider status for a live pane; the lifecycle word otherwise. */
  status: string | null
  last_active: string | null
}

// ── Conductor annotations (work-state design §9.5) ─────────────────────
// The wire shape of GET /annotations. The fork is a bounded, confined
// pass-through: `kind`, `semantic_role` and `subject.type` are the conductor's
// vocabulary and are deliberately typed as open strings here, so the conductor
// can add to any of them without a change on this side. The renderer resolves
// `semantic_role` against the six roles in design-tokens/tokens.json and falls
// back to `neutral` for anything else.

export interface AnnotationSubject {
  /** "terminal" | "task" | "campaign" today; open by design. */
  type: string
  terminal_id?: string | null
  /**
   * The generation this annotation was derived against. The renderer fences on
   * it: an annotation whose generation differs from the projection row's is
   * dropped, so a stale claim cannot outlive the obligation that produced it.
   */
  generation?: string | null
  task_id?: string | null
  campaign?: string | null
  /**
   * A subject type invented later brings its own identifier, and the route
   * carries a bounded number of them through. Typed open here so the renderer
   * can draw whatever arrived instead of an anonymous chip — placement is
   * durable without this, identity is not.
   */
  [key: string]: string | null | undefined
}

export interface Annotation {
  namespace: string
  kind: string
  version: number
  label: string
  semantic_role: string
  priority: number
  subject: AnnotationSubject
  /** Past this the renderer greys the chip (§9.6). */
  valid_until?: string | null
  /**
   * An OPAQUE identity token. The conductor decides what identity it
   * expresses; this side only hashes it into a palette slot, which is why a
   * change of grouping policy needs no change here.
   *
   * Three states, and the renderer branches on PRESENCE AND EMPTINESS ONLY:
   * absent (or null) is a severity chip coloured by `semantic_role`; `''` is
   * an identity chip with no colour; a non-empty value is an identity chip
   * coloured by the hash.
   */
  colour_key?: string | null
  /** Derived facets only — never worker-authored free text (§7). */
  details?: Record<string, string>
  /** The conductor project directory that published it; never a path. */
  source?: string | null
}

export interface AnnotationSourceReason {
  source: string
  reason: string
}

export interface AnnotationsResponse {
  annotation_schema: string
  /** "complete" | "partial" | "truncated" | "unavailable" */
  coverage: string
  sources_read: number
  sources_failed: number
  items_dropped: number
  items_omitted: number
  /** Facets that did not fit the bounded detail bag, or had no display form. */
  facets_dropped?: number
  reasons: AnnotationSourceReason[]
  annotations: Annotation[]
}

/**
 * Known profile source values the backend can emit.
 * Using `string` (not a closed union) so new provider-discovered directories
 * and custom agent directories are accepted without repeated type widening.
 */
export type AgentProfileSource = string

export interface AgentProfileInfo {
  name: string
  description: string
  source: AgentProfileSource
  // Other enabled directories that also define this profile name (the winner
  // above is what loads). Empty/absent when the name is unique. (GH #280)
  duplicated_in?: string[]
}

export interface AgentDirsSettings {
  agent_dirs: Record<string, string>
  extra_dirs: string[]
  // Directory paths toggled OFF: kept in the list but skipped when scanning
  // for agent profiles. (GH #280/#281)
  disabled_dirs?: string[]
}

export interface InboxMessage {
  id: string
  sender_id: string
  receiver_id: string
  message: string
  status: 'pending' | 'delivered' | 'failed'
  created_at: string | null
}

export interface Flow {
  name: string
  file_path: string
  schedule: string
  agent_profile: string
  provider: string
  script: string | null
  last_run: string | null
  next_run: string | null
  enabled: boolean
  prompt_template: string | null
}

export interface ProviderInfo {
  name: string
  binary: string
  installed: boolean
}

export interface MemoryStatus {
  enabled: boolean
}

export interface MemorySummary {
  key: string
  scope: string
  scope_id: string | null
  memory_type: string
  tags: string
  created_at: string
  updated_at: string
}

export interface MemoryDetail extends MemorySummary {
  content: string
}

// ── Graph layer (Issue #348) ────────────────────────────────────────────
// Wire shape of GET /graph/{provider}. Mirrors the server's GraphView.to_dict
// (src/cli_agent_orchestrator/api/main.py get_graph_endpoint). `attrs` is an
// open bag — the renderer reads is_hub / is_orphan but the server may add more.
export interface GraphNode {
  id: string
  kind: string
  label: string
  status: string
  attrs: Record<string, unknown>
}

export interface GraphEdge {
  source: string
  target: string
  type: string
  attrs: Record<string, unknown>
}

export interface GraphView {
  nodes: GraphNode[]
  edges: GraphEdge[]
  meta: Record<string, unknown>
}

// The v3 event wire shape (§3.1): text, key, or chord.
export interface WireEvent {
  type: 'text' | 'key' | 'chord'
  text?: string
  key?: string
  chord?: string
}

// ── Control-input capabilities (§3.5) ─────────────────────────────────
// The deployed keys plus the additive §3.5 blocks Lane B gates on. Unknown
// keys are ignored by construction (the interface is additive); absence of
// a block drives the old-server degradation rows of §3.5.
export interface ProviderControlBlock {
  compact?: { events: WireEvent[] }
  stop?: { events: WireEvent[] }
  steer_chords?: string[]
  dispatch_grace_ms?: number
  // §8.6 additive Lane C blocks (absent on old servers and unproven builds).
  operator_message?: OperatorMessageBlock
  image?: ImageCapabilityBlock
}

// ── Lane C: operator messages + image attachments (§8.3/§8.4/§8.6) ─────

export interface OperatorMessageBlock {
  supported: boolean
  max_text_bytes: number
  multiline: boolean
  max_attachments: number
}

export interface ImageCapabilityBlock {
  supported: boolean
  formats: string[]
  max_bytes: number
  max_width: number
  max_height: number
  mechanism: string
  reference_template?: string
  evidence?: string
}

export interface ImageAttachmentRecord {
  attachment_id: string
  terminal_id: string
  state: 'staging' | 'ready' | 'failed' | 'removed' | 'submitted'
  format: string | null
  content_type: string | null
  width: number | null
  height: number | null
  size_bytes: number
  display_filename: string
  bound_operation_id: string | null
  error: { reason_code: string; detail: string } | null
  created_at: string
  updated_at: string
}

/** The typed refusal body of a failed upload/delete (422/409). */
export interface AttachmentRefusalBody {
  outcome?: string
  reason_code?: string
  detail?: string
  attachment?: ImageAttachmentRecord
}

export interface ControlInputCapabilities {
  protocol: string
  execution_modes: string[]
  literal_write: boolean
  bracketed_paste: boolean
  enter_required: boolean
  request_schema_versions?: number[]
  sequence?: {
    event_types: string[]
    keys: string[]
    max_events: number
    max_text_bytes: number
  }
  streaming?: {
    supported: boolean
    max_in_flight: number
    coalesce_window_ms: number
  }
  provider_controls?: Record<string, ProviderControlBlock>
  command_controls?: {
    composer_nonempty_guard: boolean
  }
}

// ── Operator macro library (§5.4) ─────────────────────────────────────
export interface MacroScope {
  kind: 'global' | 'provider' | 'profile'
  provider?: string
  profile?: string
}

export interface MacroRecord {
  id: string
  name: string
  description: string | null
  scope: MacroScope
  events: WireEvent[]
  favorite: boolean
  origin: 'builtin' | 'user'
  mutable: boolean
  builtin_kind?: 'compact' | 'stop'
  created_at: string | null
  updated_at: string | null
}

export interface MacroListResponse {
  macros: MacroRecord[]
  quarantine?: { count: number | null; path: string }
}

export interface MacroWriteBody {
  name: string
  description?: string
  scope: MacroScope
  events?: WireEvent[]
  notation?: string
  favorite?: boolean
}

export interface MacroNotationParseResult {
  events: WireEvent[]
  preview: string
}

/** The §5.3 422 error body shape (offset may be null for non-notation errors). */
export interface MacroErrorsBody {
  errors?: Array<{ offset: number | null; message: string }>
}

// Request body for POST /graph/{provider}/export. `dest` MUST be a relative
// name; the server confines it under CAO_GRAPH_EXPORT_ROOT and rejects
// absolute/traversal paths with 400.
export interface GraphExportBody {
  sink: string
  dest: string
  options?: Record<string, unknown>
}

export interface GraphExportResult {
  written_files: string[]
  sink: string
  dest: string
}

// ---------------------------------------------------------------------------
// Issue tracker
// ---------------------------------------------------------------------------

/**
 * The enumerations the server will accept, fetched from /tracker/vocabulary.
 *
 * Deliberately not hard-coded here: a dropdown that offers a status the server
 * rejects is a bug the UI cannot detect, and this list has already grown once
 * (P0 arrived when the real ledger turned out to use it).
 */
export interface TrackerVocabulary {
  statuses: string[]
  terminal_statuses: string[]
  item_kinds?: string[]
  statuses_by_kind?: Record<string, string[]>
  terminal_statuses_by_kind?: Record<string, string[]>
  severities: string[]
  scope_kinds: string[]
  link_kinds: string[]
  project_statuses: string[]
}

export interface TrackerScope {
  id: number
  project_id?: string
  kind: string
  value: string
  created_at: string | null
}

export interface TrackerProject {
  id: string
  name: string
  description: string
  status: string
  issue_prefix: string
  next_issue_number: number
  created_at: string | null
  updated_at: string | null
  counts?: { total: number; open: number; by_status?: Record<string, number>; by_kind?: Record<string, { total: number; open: number }>; all_total?: number; all_open?: number }
  scopes?: TrackerScope[]
}

export interface TrackerComment {
  id: number
  author: string | null
  body: string
  created_at: string | null
}

export interface TrackerEvent {
  id: number
  actor: string | null
  kind: string
  field: string | null
  old_value: string | null
  new_value: string | null
  created_at: string | null
}

export interface TrackerLink {
  id: number
  kind: string
  from_key: string
  to_key: string
}

export interface TrackerIssue {
  key: string
  project_id: string
  kind: 'issue' | 'feature'
  title: string
  body: string
  status: string
  severity: string
  component: string | null
  reporter: string | null
  assignee: string | null
  labels: string[]
  failing_command: string | null
  evidence: string | null
  resolution: string | null
  session_name: string | null
  terminal_id: string | null
  source_path: string | null
  duplicate_of: string | null
  origin: string
  created_at: string | null
  updated_at: string | null
  closed_at: string | null
  /** Present only on the create response: which scope matched. */
  resolved_by?: string | null
  /** Present only on the detail response. */
  comments?: TrackerComment[]
  events?: TrackerEvent[]
  links?: TrackerLink[]
}

export interface TrackerIssuePage {
  total: number
  limit: number
  offset: number
  issues: TrackerIssue[]
}

export interface TrackerIssueFilters {
  projectId?: string
  kind?: 'issue' | 'feature' | 'all'
  status?: string[]
  severity?: string[]
  component?: string
  assignee?: string
  label?: string
  q?: string
  openOnly?: boolean
  limit?: number
  offset?: number
  order?: string
}

export interface TrackerStats {
  project_id: string | null
  total: number
  open: number
  by_status: Record<string, number>
  by_severity: Record<string, number>
  by_component: Record<string, number>
}

function trackerQuery(filters?: TrackerIssueFilters): string {
  if (!filters) return ''
  const parts: string[] = []
  if (filters.projectId) parts.push(`project_id=${encodeURIComponent(filters.projectId)}`)
  // Repeated params, not a comma list: the server reads them as an OR.
  for (const s of filters.status ?? []) parts.push(`status=${encodeURIComponent(s)}`)
  for (const s of filters.severity ?? []) parts.push(`severity=${encodeURIComponent(s)}`)
  if (filters.kind) parts.push(`kind=${encodeURIComponent(filters.kind)}`)
  if (filters.component) parts.push(`component=${encodeURIComponent(filters.component)}`)
  if (filters.assignee) parts.push(`assignee=${encodeURIComponent(filters.assignee)}`)
  if (filters.label) parts.push(`label=${encodeURIComponent(filters.label)}`)
  if (filters.q) parts.push(`q=${encodeURIComponent(filters.q)}`)
  if (filters.openOnly) parts.push('open_only=true')
  if (filters.limit) parts.push(`limit=${filters.limit}`)
  if (filters.offset) parts.push(`offset=${filters.offset}`)
  if (filters.order) parts.push(`order=${encodeURIComponent(filters.order)}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export const api = {
  // Agent Profiles & Providers
  listProfiles: () => fetchJSON<AgentProfileInfo[]>('/agents/profiles'),
  listProviders: () => fetchJSON<ProviderInfo[]>('/agents/providers'),

  // Settings
  getAgentDirs: () => fetchJSON<AgentDirsSettings>('/settings/agent-dirs'),
  setAgentDirs: (data: { agent_dirs?: Record<string, string>; extra_dirs?: string[]; disabled_dirs?: string[] }) =>
    fetchJSON<AgentDirsSettings>('/settings/agent-dirs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),

  // Sessions
  listSessions: () => fetchJSON<Session[]>('/sessions'),
  getSession: (name: string) => fetchJSON<SessionDetail>(`/sessions/${name}`),
  createSession: (provider: string, agentProfile: string, sessionName?: string, workingDirectory?: string) =>
    fetchJSON<Terminal>(`/sessions?provider=${encodeURIComponent(provider)}&agent_profile=${encodeURIComponent(agentProfile)}${sessionName ? `&session_name=${encodeURIComponent(sessionName)}` : ''}${workingDirectory ? `&working_directory=${encodeURIComponent(workingDirectory)}` : ''}`, { method: 'POST', timeoutMs: 90000 }),
  deleteSession: (name: string) => fetchJSON<{ success: boolean; deleted: string[]; errors: any[] }>(`/sessions/${name}`, { method: 'DELETE' }),

  // Session lifecycle — what a session has DECLARED it is doing, which is a
  // different dimension from a terminal's observed `lifecycle_state`. The
  // value sets are disjoint (`working|pausing|paused|complete|stopped` here
  // versus `live|superseded|dead|unknown-liveness` there) and they must never
  // be rendered as one field.
  //
  // Never 404s: an undeclared session reads as `working`, because every
  // session that predates the table would otherwise read as unknown and the
  // fire marshal's suppression has to fail toward watching.
  getSessionLifecycle: (name: string) =>
    fetchJSON<SessionLifecycle>(`/sessions/${encodeURIComponent(name)}/lifecycle`),
  // Only what the dashboard actually calls. The write wrappers were added
  // ahead of the controls that would use them, which is the same shape as
  // the defect this branch exists to fix — a tested surface nothing invokes.
  // They come back with the buttons.
  getStopImpact: (name: string) =>
    fetchJSON<StopImpact>(`/sessions/${encodeURIComponent(name)}/stop-impact`),

  // Conductor annotations (§9.5). The route takes no parameters — that is the
  // security property, not an omission — and never errors: an absent or
  // unreadable conductor state root answers `coverage: "unavailable"` with an
  // empty list, which renders exactly as the dashboard did before it existed.
  getAnnotations: () => fetchJSON<AnnotationsResponse>('/annotations'),

  // Terminals
  getTerminalStatus: (id: string) =>
    fetchJSON<Terminal>(`/terminals/${id}`).then(t => t.status),
  getTerminalOutput: (id: string, mode: 'full' | 'last' = 'full') =>
    fetchJSON<{ output: string; mode: string }>(`/terminals/${id}/output?mode=${mode}`),
  sendInput: (id: string, message: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${id}/input?message=${encodeURIComponent(message)}`, { method: 'POST' }),
  getManagedControl: (id: string) =>
    fetchJSON<{ managed: boolean; generation?: string; provider?: string; execution_mode?: string; vintage?: string }>(`/terminals/${id}/managed-control`),
  getControlInputCapabilities: () =>
    fetchJSON<ControlInputCapabilities>('/control-input/capabilities'),
  getControlIdentity: (id: string) =>
    fetchJSON<Record<string, unknown>>(`/terminals/${id}/control-identity`),
  sendControlInput: (
    id: string,
    body: {
      control_id: string
      text?: string
      enter?: boolean
      // v3 structured sequences: the request carries events OR the v1/v2
      // fields, never both.
      events?: WireEvent[]
      // v4 declaration carrier. "command" (§4.1): sent ONLY by the registry
      // Compact built-in send (and supervisor/provider command controls),
      // and only after the server advertises the command_controls block.
      // "interactive" (§6.7, r15): sent ONLY by the armed manual streaming
      // capture, and only after the per-terminal, build-exact
      // interactive_streaming block advertises support. Macros, favorites,
      // the prose composer, operator messages, and inbox/automation never
      // set either.
      payload_class?: 'command' | 'interactive'
      expected_identity: Record<string, unknown>
    }
  ) =>
    fetchJSON<Record<string, unknown>>(`/terminals/${id}/control-input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      timeoutMs: 15000,
    }),
  queryControlInput: (controlId: string) =>
    fetchJSON<Record<string, unknown>>(`/control-input/${encodeURIComponent(controlId)}`),

  // Lane C: operator messages + image attachments (§8.3/§8.4). Typed
  // outcomes travel as 200 like control-input; a 404 on either route is the
  // old-server signal and resolves to `unsupported` — never a resend.
  submitOperatorMessage: (
    id: string,
    body: {
      operation_id: string
      text: string
      attachments: string[]
      token_map: Record<string, string>
      expected_identity: Record<string, unknown>
    }
  ) =>
    fetchJSON<Record<string, unknown>>(`/terminals/${id}/operator-message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      // The server's write deadline is 20s; the client must outlast it.
      timeoutMs: 25000,
    }),
  reconcileOperatorMessage: (operationId: string) =>
    fetchJSON<Record<string, unknown>>(`/operator-message/${encodeURIComponent(operationId)}`),
  uploadAttachment: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    // No Content-Type header: the browser sets the multipart boundary.
    return fetchJSON<{ attachment: ImageAttachmentRecord }>(
      `/terminals/${id}/attachments`,
      { method: 'POST', body: form, timeoutMs: 15000 },
    )
  },
  listAttachments: (id: string) =>
    fetchJSON<{ attachments: ImageAttachmentRecord[] }>(`/terminals/${id}/attachments`),
  deleteAttachment: (id: string, attachmentId: string) =>
    fetchJSON<{ deleted: boolean; attachment: ImageAttachmentRecord }>(
      `/terminals/${id}/attachments/${encodeURIComponent(attachmentId)}`,
      { method: 'DELETE' },
    ),

  // Operator macro library (§5.4). Sending a macro is NOT a store
  // operation: the client takes the resolved events and sends an ordinary
  // v3 control-input request (D2) — these routes only manage the library.
  listMacros: (filters?: { provider?: string; profile?: string }) => {
    const params = [
      filters?.provider ? `provider=${encodeURIComponent(filters.provider)}` : '',
      filters?.profile ? `profile=${encodeURIComponent(filters.profile)}` : '',
    ].filter(Boolean).join('&')
    return fetchJSON<MacroListResponse>(`/macros${params ? `?${params}` : ''}`)
  },
  createMacro: (body: MacroWriteBody) =>
    fetchJSON<MacroRecord>('/macros', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateMacro: (macroId: string, body: MacroWriteBody) =>
    fetchJSON<MacroRecord>(`/macros/${encodeURIComponent(macroId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteMacro: (macroId: string) =>
    fetchJSON<{ deleted: string }>(`/macros/${encodeURIComponent(macroId)}`, { method: 'DELETE' }),
  duplicateMacro: (macroId: string, name?: string) =>
    fetchJSON<MacroRecord>(`/macros/${encodeURIComponent(macroId)}/duplicate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(name ? { name } : {}),
    }),
  parseMacroNotation: (notation: string) =>
    fetchJSON<MacroNotationParseResult>('/macros/parse-notation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notation }),
    }),
  beginManagedOperation: (
    id: string,
    body: {
      action: string
      operation_id: string
      generation?: string
      message?: string
      config_id?: string
      value?: string
      instruction?: string
    }
  ) =>
    fetchJSON<{ success: boolean; receipt: Record<string, unknown> }>(
      `/terminals/${id}/managed-operations`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        timeoutMs: 50000,
      }
    ),
  queryManagedOperation: (id: string, operationId: string, generation?: string) =>
    fetchJSON<{ receipt: Record<string, unknown> }>(
      `/terminals/${id}/managed-operations/${encodeURIComponent(operationId)}${generation ? `?generation=${encodeURIComponent(generation)}` : ''}`,
      { timeoutMs: 35000 }
    ),
  exitTerminal: (id: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${id}/exit`, { method: 'POST' }),
  deleteTerminal: (id: string) => fetchJSON<{ success: boolean }>(`/terminals/${id}`, { method: 'DELETE' }),
  getWorkingDirectory: (id: string) =>
    fetchJSON<{ working_directory: string | null }>(`/terminals/${id}/working-directory`),
  addTerminalToSession: (sessionName: string, provider: string, agentProfile: string, workingDirectory?: string) =>
    fetchJSON<Terminal>(`/sessions/${sessionName}/terminals?provider=${encodeURIComponent(provider)}&agent_profile=${encodeURIComponent(agentProfile)}${workingDirectory ? `&working_directory=${encodeURIComponent(workingDirectory)}` : ''}`, { method: 'POST', timeoutMs: 90000 }),

  // Inbox
  getInboxMessages: (terminalId: string, limit?: number, status?: string) =>
    fetchJSON<InboxMessage[]>(`/terminals/${terminalId}/inbox/messages?limit=${limit || 50}${status ? `&status=${status}` : ''}`),
  sendInboxMessage: (receiverId: string, senderId: string, message: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${receiverId}/inbox/messages?sender_id=${senderId}&message=${encodeURIComponent(message)}`, { method: 'POST' }),

  // Flows
  listFlows: () => fetchJSON<Flow[]>('/flows'),
  createFlow: (data: { name: string; schedule: string; agent_profile: string; provider?: string; prompt_template: string }) =>
    fetchJSON<Flow>('/flows', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
      timeoutMs: 30000,
    }),
  deleteFlow: (name: string) => fetchJSON<{ success: boolean }>(`/flows/${name}`, { method: 'DELETE' }),
  enableFlow: (name: string) => fetchJSON<{ success: boolean }>(`/flows/${name}/enable`, { method: 'POST' }),
  disableFlow: (name: string) => fetchJSON<{ success: boolean }>(`/flows/${name}/disable`, { method: 'POST' }),
  runFlow: (name: string) => fetchJSON<{ executed: boolean }>(`/flows/${name}/run`, { method: 'POST', timeoutMs: 90000 }),

  // Memory
  getMemoryStatus: () => fetchJSON<MemoryStatus>('/settings/memory'),
  listMemories: (filters?: { scope?: string; type?: string; scopeId?: string; limit?: number }) => {
    const params = [
      filters?.scope ? `scope=${encodeURIComponent(filters.scope)}` : '',
      filters?.type ? `type=${encodeURIComponent(filters.type)}` : '',
      filters?.scopeId ? `scope_id=${encodeURIComponent(filters.scopeId)}` : '',
      filters?.limit ? `limit=${filters.limit}` : '',
    ].filter(Boolean).join('&')
    return fetchJSON<MemorySummary[]>(`/memory${params ? `?${params}` : ''}`)
  },
  getMemory: (key: string, scope?: string, scopeId?: string) => {
    const params = [
      scope ? `scope=${encodeURIComponent(scope)}` : '',
      scopeId ? `scope_id=${encodeURIComponent(scopeId)}` : '',
    ].filter(Boolean).join('&')
    return fetchJSON<MemoryDetail>(`/memory/${encodeURIComponent(key)}${params ? `?${params}` : ''}`)
  },
  deleteMemory: (key: string, scope: string, scopeId?: string) =>
    fetchJSON<{ success: boolean }>(`/memory/${encodeURIComponent(key)}?scope=${encodeURIComponent(scope)}${scopeId ? `&scope_id=${encodeURIComponent(scopeId)}` : ''}`, { method: 'DELETE' }),
  clearMemories: (scope: string, scopeId?: string) =>
    fetchJSON<{ success: boolean; deleted_count: number }>(`/memory?scope=${encodeURIComponent(scope)}${scopeId ? `&scope_id=${encodeURIComponent(scopeId)}` : ''}`, { method: 'DELETE' }),

  // Graph (Issue #348). The projection runs wiki_lint (ripgrep detectors)
  // server-side, so both routes get a wide timeout — a populated scope can take
  // ~30s typical, up to ~148s under load. Errors surface as ApiError (status +
  // server detail) for the caller.
  getGraph: (provider = 'memory', scope?: string, scopeId?: string) => {
    const params = [
      scope ? `scope=${encodeURIComponent(scope)}` : '',
      scopeId ? `scope_id=${encodeURIComponent(scopeId)}` : '',
    ].filter(Boolean).join('&')
    return fetchJSON<GraphView>(
      `/graph/${encodeURIComponent(provider)}${params ? `?${params}` : ''}`,
      { timeoutMs: 120000 },
    )
  },
  exportGraph: (provider = 'memory', body: GraphExportBody, scope?: string, scopeId?: string) => {
    const params = [
      scope ? `scope=${encodeURIComponent(scope)}` : '',
      scopeId ? `scope_id=${encodeURIComponent(scopeId)}` : '',
    ].filter(Boolean).join('&')
    return fetchJSON<GraphExportResult>(
      `/graph/${encodeURIComponent(provider)}/export${params ? `?${params}` : ''}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ options: {}, ...body }),
        timeoutMs: 60000,
      },
    )
  },

  // Issue tracker
  getTrackerVocabulary: () => fetchJSON<TrackerVocabulary>('/tracker/vocabulary'),

  listTrackerProjects: (includeArchived = false) =>
    fetchJSON<TrackerProject[]>(`/tracker/projects${includeArchived ? '?include_archived=true' : ''}`),
  getTrackerProject: (id: string) =>
    fetchJSON<TrackerProject>(`/tracker/projects/${encodeURIComponent(id)}`),
  createTrackerProject: (body: {
    name: string
    id?: string
    description?: string
    issue_prefix?: string
    scopes?: Array<{ kind: string; value: string }>
  }) =>
    fetchJSON<TrackerProject>('/tracker/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateTrackerProject: (
    id: string,
    body: { name?: string; description?: string; status?: string; issue_prefix?: string },
  ) =>
    fetchJSON<TrackerProject>(`/tracker/projects/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteTrackerProject: (id: string, force = false) =>
    fetchJSON<{ id: string; deleted: boolean; issues_deleted: number }>(
      `/tracker/projects/${encodeURIComponent(id)}${force ? '?force=true' : ''}`,
      { method: 'DELETE' },
    ),
  addTrackerScope: (projectId: string, body: { kind: string; value: string }) =>
    fetchJSON<TrackerScope & { created: boolean }>(
      `/tracker/projects/${encodeURIComponent(projectId)}/scopes`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    ),
  removeTrackerScope: (projectId: string, scopeId: number) =>
    fetchJSON<{ id: number; deleted: boolean }>(
      `/tracker/projects/${encodeURIComponent(projectId)}/scopes/${scopeId}`,
      { method: 'DELETE' },
    ),

  listTrackerIssues: (filters?: TrackerIssueFilters) =>
    fetchJSON<TrackerIssuePage>(`/tracker/issues${trackerQuery(filters)}`),
  getTrackerIssue: (key: string) =>
    fetchJSON<TrackerIssue>(`/tracker/issues/${encodeURIComponent(key)}`),
  createTrackerIssue: (body: Record<string, unknown>) =>
    fetchJSON<TrackerIssue>('/tracker/issues', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ origin: 'dashboard', ...body }),
    }),
  updateTrackerIssue: (key: string, body: Record<string, unknown>) =>
    fetchJSON<TrackerIssue>(`/tracker/issues/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteTrackerIssue: (key: string) =>
    fetchJSON<{ key: string; deleted: boolean }>(`/tracker/issues/${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),
  addTrackerComment: (key: string, body: { body: string; author?: string }) =>
    fetchJSON<TrackerComment>(`/tracker/issues/${encodeURIComponent(key)}/comments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  addTrackerLink: (key: string, body: { to_key: string; kind: string; actor?: string }) =>
    fetchJSON<TrackerLink & { created: boolean }>(
      `/tracker/issues/${encodeURIComponent(key)}/links`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    ),
  removeTrackerLink: (key: string, linkId: number) =>
    fetchJSON<{ id: number; deleted: boolean }>(
      `/tracker/issues/${encodeURIComponent(key)}/links/${linkId}`,
      { method: 'DELETE' },
    ),
  getTrackerStats: (projectId?: string) =>
    fetchJSON<TrackerStats>(
      `/tracker/issues/stats${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`,
    ),
  listTrackerFeatures: (filters?: TrackerIssueFilters) =>
    fetchJSON<TrackerIssuePage>(`/tracker/features${trackerQuery({ ...filters, kind: 'feature' })}`),
  getTrackerFeature: (key: string) =>
    fetchJSON<TrackerIssue>(`/tracker/features/${encodeURIComponent(key)}`),
  createTrackerFeature: (body: Record<string, unknown>) =>
    fetchJSON<TrackerIssue>('/tracker/features', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ origin: 'dashboard', ...body }),
    }),
  updateTrackerFeature: (key: string, body: Record<string, unknown>) =>
    fetchJSON<TrackerIssue>(`/tracker/features/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteTrackerFeature: (key: string) =>
    fetchJSON<{ key: string; deleted: boolean }>(`/tracker/features/${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),
  addTrackerFeatureComment: (key: string, body: { body: string; author?: string }) =>
    fetchJSON<TrackerComment>(`/tracker/features/${encodeURIComponent(key)}/comments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  addTrackerFeatureLink: (key: string, body: { to_key: string; kind: string; actor?: string }) =>
    fetchJSON<TrackerLink & { created: boolean }>(
      `/tracker/features/${encodeURIComponent(key)}/links`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    ),
  removeTrackerFeatureLink: (key: string, linkId: number) =>
    fetchJSON<{ id: number; deleted: boolean }>(
      `/tracker/features/${encodeURIComponent(key)}/links/${linkId}`,
      { method: 'DELETE' },
    ),
  getTrackerFeatureStats: (projectId?: string) =>
    fetchJSON<TrackerStats>(`/tracker/features/stats${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`),
}
