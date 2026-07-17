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
      try {
        const body = await res.json()
        if (body && typeof body.detail === 'string') detail = body.detail
      } catch { /* non-JSON error body */ }
      const err: ApiError = new Error(`${res.status} ${res.statusText}`)
      err.status = res.status
      err.detail = detail
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
