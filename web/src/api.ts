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
    return res.json()
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
  // Discovery metadata surfaced by list_agent_profiles(); all optional/additive
  // so existing callers are unaffected. `loadable === false` marks a profile the
  // load path would reject — the Profiles list badges it View-only (#510).
  loadable?: boolean
  capabilities?: string[]
  tags?: string[]
  role?: string
}

/**
 * One ranked search hit — mirrors the server `search_profiles` result shape
 * verbatim (GET /agents/profiles/search, #510). The list order is
 * server-provided (coverage → BM25Plus → name); the client MUST NOT re-sort or
 * recompute `score` (which is relative to the matched set, not a percentage).
 */
export interface ProfileSearchResult {
  name: string
  description: string
  capabilities: string[]
  tags: string[]
  role: string | null
  source: AgentProfileSource
  coverage: number
  score: number
}

/**
 * Validation outcome from POST /agents/profiles/validate (#510). `valid` is
 * true iff there are zero `[error]` messages; `[warn]` messages never block
 * save, mirroring the CLI's "exit 1 only on [error]".
 */
export interface ValidateProfileResult {
  valid: boolean
  errors: string[]
  warnings: string[]
}

/** Request body for POST /agents/profiles/validate — supply exactly one input. */
export interface ValidateProfilePayload {
  content?: string
  metadata?: Record<string, unknown>
}

/**
 * Full parsed profile from GET /agents/profiles/{name} — the server serializes
 * its AgentProfile with None fields excluded, so most fields are optional. The
 * open index signature carries provider-specific keys without repeated widening.
 */
export interface AgentProfileDetail {
  name: string
  description?: string
  role?: string
  provider?: string
  model?: string
  system_prompt?: string
  capabilities?: string[]
  tags?: string[]
  allowedTools?: string[]
  mcpServers?: Record<string, unknown>
  [key: string]: unknown
}

/** One scaffolding template descriptor — mirrors list_templates() (#510). */
export interface TemplateInfo {
  name: string
  description: string
  path: string
}

/**
 * A template's JSON-Schema (Draft 2020-12) from
 * GET /agents/profiles/templates/{name}/schema. Left as an open object — the
 * create form reads `properties`/`required` to build fields.
 */
export interface TemplateSchema {
  type?: string
  title?: string
  description?: string
  properties?: Record<string, JsonSchemaProperty>
  required?: string[]
  additionalProperties?: boolean
  [key: string]: unknown
}

/** One property node within a template schema, as far as the form widget reads it. */
export interface JsonSchemaProperty {
  type?: string
  description?: string
  enum?: string[]
  pattern?: string
  default?: unknown
  minimum?: number
  maximum?: number
  [key: string]: unknown
}

/**
 * Request body for POST /agents/profiles (create) and POST
 * /agents/profiles/preview. `provider` and `model` are REQUIRED explicit inputs
 * (ADR-006/F-1) — set as frontmatter on the rendered output server-side, never
 * merged into `config`.
 */
export interface CreateProfileRequest {
  template_name: string
  config: Record<string, unknown>
  provider: string
  model: string
}

/** Response for a create/edit write — the persisted local profile (#510). */
export interface ProfileWriteResult {
  name: string
  source: string
  path: string
}

/**
 * Render-only preview from POST /agents/profiles/preview (#510). Carries the
 * rendered profile `text` (frontmatter already patched with provider/model,
 * server-side) plus the validation split. NON-MUTATING — nothing is written.
 */
export interface PreviewProfileResult {
  text: string
  valid: boolean
  errors: string[]
  warnings: string[]
}

/** Request body for PUT /agents/profiles/{name} (edit — #510 U4). */
export interface UpdateProfileRequest {
  content: string
  provider: string
  model: string
}

/**
 * Request body for POST /agents/profiles/from-content (clone — #510 U4). Writes
 * a NEW local profile from raw `content` under a new `name`; the server refuses
 * to overwrite an existing name. `provider`/`model` arrive inside `content` and
 * are re-asserted on the body (ADR-006).
 */
export interface CreateFromContentRequest {
  name: string
  content: string
  provider: string
  model: string
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

export const api = {
  // Agent Profiles & Providers
  listProfiles: () => fetchJSON<AgentProfileInfo[]>('/agents/profiles'),
  listProviders: () => fetchJSON<ProviderInfo[]>('/agents/providers'),

  // Profile management (#510 U2). Search + validate are open-read; the server
  // owns all ranking/validation logic — these wrappers only carry data. Search
  // results come back in server order and MUST NOT be re-sorted client-side
  // (coverage → BM25Plus → name is U1's pinned contract). U3 appends its own
  // template/create methods to this block when it runs.
  searchProfiles: (q: string, limit?: number) =>
    fetchJSON<ProfileSearchResult[]>(
      `/agents/profiles/search?q=${encodeURIComponent(q)}${limit !== undefined ? `&limit=${limit}` : ''}`,
    ),
  getProfile: (name: string) =>
    fetchJSON<AgentProfileDetail>(`/agents/profiles/${encodeURIComponent(name)}`),
  validateProfile: (payload: ValidateProfilePayload) =>
    fetchJSON<ValidateProfileResult>('/agents/profiles/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),

  // Create-from-template flow (#510 U3). Templates + schema are open-read; the
  // preview is a server-side render (writes nothing); create writes to the local
  // store via the scope-gated endpoint. No scaffolding/validation logic here.
  listTemplates: () => fetchJSON<TemplateInfo[]>('/agents/profiles/templates'),
  getTemplateSchema: (name: string) =>
    // `name` is category/name (e.g. aws/stepfunction); the server route captures
    // it with a :path convertor, so the '/' is intentionally NOT encoded.
    fetchJSON<TemplateSchema>(`/agents/profiles/templates/${name}/schema`),
  previewProfile: (req: CreateProfileRequest) =>
    fetchJSON<PreviewProfileResult>('/agents/profiles/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }),
  createProfile: (req: CreateProfileRequest) =>
    fetchJSON<ProfileWriteResult>('/agents/profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }),

  // Edit + clone (#510 U4). Edit updates a local profile in place via PUT;
  // clone writes a NEW local profile from a built-in's (edited) content via
  // from-content (the built-in is never mutated). Both server-validated + guarded.
  updateProfile: (name: string, req: UpdateProfileRequest) =>
    fetchJSON<ProfileWriteResult>(`/agents/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }),
  createProfileFromContent: (req: CreateFromContentRequest) =>
    fetchJSON<ProfileWriteResult>('/agents/profiles/from-content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }),

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

  // Terminals
  getTerminalStatus: (id: string) =>
    fetchJSON<Terminal>(`/terminals/${id}`).then(t => t.status),
  getTerminalOutput: (id: string, mode: 'full' | 'last' = 'full') =>
    fetchJSON<{ output: string; mode: string }>(`/terminals/${id}/output?mode=${mode}`),
  sendInput: (id: string, message: string) =>
    fetchJSON<{ success: boolean }>(`/terminals/${id}/input?message=${encodeURIComponent(message)}`, { method: 'POST' }),
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
}
