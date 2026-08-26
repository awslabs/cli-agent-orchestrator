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
  kind?: string
  detailMeta?: Record<string, unknown>
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
      let kind: string | undefined
      let detailMeta: Record<string, unknown> | undefined
      try {
        const body = await res.json()
        if (body && typeof body.detail === 'string') detail = body.detail
        if (body && body.detail && typeof body.detail === 'object') {
          detailMeta = body.detail as Record<string, unknown>
          if (typeof detailMeta.message === 'string') detail = detailMeta.message
          if (typeof detailMeta.kind === 'string') kind = detailMeta.kind
        }
      } catch { /* non-JSON error body */ }
      const err: ApiError = new Error(`${res.status} ${res.statusText}`)
      err.status = res.status
      err.detail = detail
      err.kind = kind
      err.detailMeta = detailMeta
      throw err
    }
    // A 204 No Content — e.g. the workflow-run DELETE (U7/#504) — has no JSON
    // to parse. Neither does ANY successful response whose body is empty or
    // whitespace-only, which is a real deployment condition on this
    // SSE-adjacent surface: an intermediary proxy can strip a body. Read the
    // body as text once and parse it ourselves so both cases return undefined
    // instead of letting res.json() throw. Additive: every pre-existing
    // endpoint returns a non-empty JSON body, so existing callers are
    // unaffected.
    const text = await res.text()
    if (text.trim() === '') return undefined as T
    return JSON.parse(text) as T
  } finally {
    clearTimeout(timeout)
  }
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

export interface TerminalMeta {
  id: string
  tmux_session: string
  tmux_window: string
  provider: string
  agent_profile: string | null
  created_at: string | null
  last_active: string | null
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

// ── Workflow run journal (Issue #504 / U8) ──────────────────────────────
// Wire shapes for the durable workflow-run read APIs consumed by the
// Workflows web surface. Each interface mirrors a server Pydantic model in
// src/cli_agent_orchestrator/api/main.py (RunInspection, StepInspection,
// EventTimelinePage, RunComparison, DiagnosticBundle) or a sibling-intent
// (#505) shape (RunSummaryRow). All read-only over already-built endpoints;
// U8 adds NO server code.

/**
 * One durable event row (server EventRow, workflow_journal.py — all 21 columns).
 * `seq` is the SOLE ordering authority (never order by `ts`). The two
 * `terminal_offset_*` fields are the null-offset seam: currently ALWAYS null until
 * offset-capture emission is wired — SyncedTerminalPane degrades gracefully
 * on null and lights up unchanged the moment they populate.
 */
export interface WorkflowEvent {
  run_id: string
  seq: number
  event_type: string
  event_schema_version: number
  ts: string
  step_id?: string | null
  attempt?: number | null
  state?: string | null
  elapsed_ms?: number | null
  provider?: string | null
  agent_profile?: string | null
  engine?: string | null
  terminal_id?: string | null
  terminal_offset_start?: number | null
  terminal_offset_len?: number | null
  error_kind?: string | null
  reason?: string | null
  validation_result?: string | null
  output_ref?: string | null
  iteration?: number | null
  which_guard_fired?: string | null
}

/**
 * A DECLARED hole in a run's event sequence (server GapMarker). The API
 * declares gaps as data — the client renders exactly what it is told and
 * NEVER infers a gap from event numbering (BR-4).
 */
export interface GapMarker {
  after_seq: number
  before_seq: number
  missing_count: number
  reason: string
}

/** One step's durable projection inside a RunInspection (server StepInspection). */
export interface StepInspection {
  id: string
  state: string
  attempts: number
  output_json?: string | null
  error?: string | null
  error_kind?: string | null
  terminal_id?: string | null
  reprompted?: number | null
  call_fingerprint?: string | null
}

/** Enriched single-run inspection (server RunInspection, FR-5.1). */
export interface RunInspection {
  run_id: string
  workflow_name: string
  state: string
  current_step_id?: string | null
  started_at: string
  finished_at?: string | null
  tier: string
  steps: StepInspection[]
}

/** A batch page of a run's ordered event timeline (server EventTimelinePage). */
export interface EventTimelinePage {
  events: WorkflowEvent[]
  gaps: GapMarker[]
  next_after_seq: number | null
}

/** An offset-ranged terminal-log read (server TerminalOutputRange, U5/FR-4.3). */
export interface TerminalOutputRange {
  terminal_id: string
  offset: number
  length: number
  data: string
}

/** One run's side of an aligned comparison step (server StepComparisonSide). */
export interface StepComparisonSide {
  attempts: number
  duration_ms?: number | null
  provider?: string | null
  agent_profile?: string | null
  validation?: string | null
  state: string
  error_kind?: string | null
  reprompted?: number | null
}

/**
 * One aligned step across two runs (server StepComparison). `status` is
 * `aligned` | `added` (only in compare) | `removed` (only in baseline) — a
 * step present in one run and absent in the other is ALWAYS surfaced (BR-1).
 */
export interface StepComparison {
  step_id: string
  status: 'aligned' | 'added' | 'removed' | string
  a?: StepComparisonSide | null
  b?: StepComparisonSide | null
}

/** A reference-level output/artifact difference for an aligned step (server OutputDiff). */
export interface OutputDiff {
  step_id: string
  a_refs: string[]
  b_refs: string[]
}

/** Compare two runs by aligned step (server RunComparison, FR-8). */
export interface RunComparison {
  baseline_run_id: string
  compare_run_id: string
  steps: StepComparison[]
  output_diffs: OutputDiff[]
}

/** A step's terminal outcome inside a DiagnosticBundle (server StepOutcome). */
export interface StepOutcome {
  step_id: string
  state: string
  error_kind?: string | null
}

/** Provider/agent/engine metadata for a bundle (server BundleEnvironment). */
export interface BundleEnvironment {
  providers: string[]
  agent_profiles: string[]
  engines: string[]
}

/** A terminal-log offset-range reference in a bundle (server TerminalReference). */
export interface TerminalReference {
  terminal_id: string
  offset_start?: number | null
  offset_len?: number | null
}

/** Terminal + artifact references for a bundle (server BundleReferences). */
export interface BundleReferences {
  terminals: TerminalReference[]
  artifacts: string[]
}

/** A retention-safe, size-limited output excerpt (server BundleExcerpt). */
export interface BundleExcerpt {
  step_id: string
  excerpt: string
}

/** A run's troubleshooting export bundle (server DiagnosticBundle, FR-9). */
export interface DiagnosticBundle {
  spec_id: string
  spec_content_hash: string
  inputs: string
  events: WorkflowEvent[]
  gaps: GapMarker[]
  step_outcomes: StepOutcome[]
  environment: BundleEnvironment
  references: BundleReferences
  excerpts: BundleExcerpt[]
  capture_enabled: boolean
}

/**
 * The run-list row owned by sibling intent #505 (FINAL, stable shape — #505 is
 * done). CONSUMED here, not built. `GET /workflows/runs` returns an array of
 * these; U8 never reimplements the list endpoint.
 */
export interface RunSummaryRow {
  run_id: string
  workflow_name: string
  state: string
  tier: string
  started_at: string
  finished_at: string | null
  current_step_id: string | null
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
  deleteSession: async (name: string) => {
    const result = await fetchJSON<{ success: boolean; deleted: string[]; errors: any[] }>(`/sessions/${name}`, { method: 'DELETE' })
    if (!result?.success || (result.errors && result.errors.length > 0)) {
      const err: ApiError = new Error(result?.errors?.[0]?.error || 'Session cleanup is pending; retry delete')
      err.status = 409
      err.detail = err.message
      throw err
    }
    return result
  },

  // Terminals
  getTerminalStatus: (id: string) =>
    fetchJSON<Terminal>(`/terminals/${id}`).then(t => t.status),
  getTerminalOutput: (id: string, mode: 'full' | 'last' = 'full') =>
    fetchJSON<{ output: string; mode: string }>(`/terminals/${id}/output?mode=${mode}`),
  sendInput: (id: string, message: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${id}/input?message=${encodeURIComponent(message)}`, { method: 'POST' }),
  exitTerminal: (id: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${id}/exit`, { method: 'POST' }),
  deleteTerminal: async (id: string) => {
    const result = await fetchJSON<{ success: boolean }>(`/terminals/${id}`, { method: 'DELETE' })
    if (!result?.success) {
      const err: ApiError = new Error('Terminal cleanup is pending; retry delete')
      err.status = 409
      err.detail = err.message
      throw err
    }
    return result
  },
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

  // ── Workflow run journal (Issue #504 / U8) — read + delete over the
  // already-built durable-journal APIs. All additive; no server code here.

  // #505's run list (FINAL RunSummaryRow shape). Consumed, not built.
  listWorkflowRuns: () => fetchJSON<RunSummaryRow[]>('/workflows/runs'),

  // U3 inspect — enriched single-run projection (journal-authoritative).
  inspectWorkflowRun: (runId: string) =>
    fetchJSON<RunInspection>(`/workflows/runs/${encodeURIComponent(runId)}`),

  // U3/U4 batch event page. `afterSeq` is the replay/next-page cursor (events
  // with seq strictly greater); omit to read from the start of the timeline.
  getWorkflowRunEvents: (runId: string, afterSeq?: number) => {
    const q = afterSeq != null ? `?after_seq=${afterSeq}` : ''
    return fetchJSON<EventTimelinePage>(`/workflows/runs/${encodeURIComponent(runId)}/events${q}`)
  },

  // U6 compare — aligned-step diff of the path run against `against` (BR-1:
  // added/removed steps always surfaced; a missing side is a 404).
  compareWorkflowRuns: (runId: string, against: string) =>
    fetchJSON<RunComparison>(
      `/workflows/runs/${encodeURIComponent(runId)}/compare?against=${encodeURIComponent(against)}`,
    ),

  // U6 diagnostics — the run's troubleshooting bundle (redaction + size limits
  // are enforced server-side; this is a plain read).
  getWorkflowRunDiagnostics: (runId: string) =>
    fetchJSON<DiagnosticBundle>(`/workflows/runs/${encodeURIComponent(runId)}/diagnostics`, {
      timeoutMs: 30000,
    }),

  // U7 delete — cascade-delete a run's durable data. 204 No Content on success
  // (fetchJSON returns undefined). Idempotent server-side (unknown id -> 204).
  deleteWorkflowRun: (runId: string) =>
    fetchJSON<void>(`/workflows/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),

  // U5 terminal offset-range read — the bytes produced around a selected event
  // (FR-7.3). Called by SyncedTerminalPane only when an event carries non-null
  // terminal_offset_start/len (the null-offset seam). `length` is capped server-side.
  getTerminalOutputRange: (terminalId: string, offset: number, length: number) =>
    fetchJSON<TerminalOutputRange>(
      `/terminals/${encodeURIComponent(terminalId)}/output/range?offset=${offset}&length=${length}`,
    ),
}
