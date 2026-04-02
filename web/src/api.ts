const API = '/api'

async function fetchWithResilience(url: string, opts?: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const res = await fetch(url, { ...opts, signal: controller.signal })
    clearTimeout(timeout)
    return res
  } catch (e) {
    clearTimeout(timeout)
    // Single retry on network error
    const res = await fetch(url, opts)
    return res
  }
}

function jsonOr<T>(fallback: T) {
  return (res: Response): Promise<T> => res.ok ? res.json() : Promise.resolve(fallback)
}

export const api = {
  // V2 Agents
  agents: {
    list: () => fetchWithResilience(`${API}/v2/agents`).then(jsonOr([])),
    get: (name: string) => fetchWithResilience(`${API}/v2/agents/${name}`).then(jsonOr({})),
    create: (data: { name: string; description?: string; steering?: string }) =>
      fetchWithResilience(`${API}/v2/agents`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    update: (name: string, data: { name: string; description?: string; steering?: string }) =>
      fetchWithResilience(`${API}/v2/agents/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    delete: (name: string) => fetchWithResilience(`${API}/v2/agents/${name}`, { method: 'DELETE' })
  },

  // V2 Sessions
  sessions: {
    list: () => fetchWithResilience(`${API}/v2/sessions`).then(jsonOr([])),
    get: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}`).then(jsonOr({})),
    getChildren: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}/children`).then(jsonOr([])),
    create: (data: { agent_name: string; provider?: string }) =>
      fetchWithResilience(`${API}/v2/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    delete: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}`, { method: 'DELETE' }).then(jsonOr({})),
    input: (id: string, message: string, raw: boolean = false) =>
      fetchWithResilience(`${API}/v2/sessions/${id}/input?message=${encodeURIComponent(message)}&raw=${raw}`, { method: 'POST' }),
    output: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}/output`).then(jsonOr({})),
    context: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}/context`).then(jsonOr({})),
    getHistory: (id: string) => fetchWithResilience(`${API}/v2/sessions/${id}/history`).then(jsonOr({})),
    autoMode: (id: string, enabled: boolean) =>
      fetchWithResilience(`${API}/v2/sessions/${id}/auto-mode`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) }).then(jsonOr({})),
    updatePosition: (id: string, x: number, y: number) =>
      fetchWithResilience(`${API}/v2/sessions/${id}/position`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) }).then(jsonOr({}))
  },

  // V2 Activity
  activity: {
    list: (sessionId?: string) => fetchWithResilience(`${API}/v2/activity${sessionId ? `?session_id=${sessionId}` : ''}`).then(jsonOr([]))
  },

  // Beads (Tasks)
  tasks: {
    list: () => fetchWithResilience(`${API}/tasks`).then(jsonOr([])),
    get: (id: string) => fetchWithResilience(`${API}/tasks/${id}`).then(jsonOr({})),
    create: (data: { title: string; description?: string; priority?: number }) =>
      fetchWithResilience(`${API}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    createChild: (parentId: string, data: { title: string; description?: string; priority?: number }) =>
      fetchWithResilience(`${API}/v2/beads/${parentId}/children`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    getChildren: (parentId: string) => fetchWithResilience(`${API}/v2/beads/${parentId}/children`).then(jsonOr([])),
    update: (id: string, data: Partial<{ title: string; description: string; priority: number; status: string }>) =>
      fetchWithResilience(`${API}/tasks/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    wip: (id: string) => fetchWithResilience(`${API}/tasks/${id}/wip`, { method: 'POST' }).then(jsonOr({})),
    close: (id: string) => fetchWithResilience(`${API}/tasks/${id}/close`, { method: 'POST' }).then(jsonOr({})),
    delete: (id: string) => fetchWithResilience(`${API}/tasks/${id}`, { method: 'DELETE' }),
    assign: (id: string, sessionId: string) =>
      fetchWithResilience(`${API}/v2/beads/${id}/assign`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId }) }).then(jsonOr({})),
    assignAgent: (id: string, agentName: string, provider: string = 'kiro_cli') =>
      fetchWithResilience(`${API}/v2/beads/${id}/assign-agent`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent_name: agentName, provider }) }).then(jsonOr({})),
    unassignSession: (sessionId: string) =>
      fetchWithResilience(`${API}/tasks/unassign-session/${sessionId}`, { method: 'POST' }).then(jsonOr({})),
    decompose: (text: string) =>
      fetchWithResilience(`${API}/v2/beads/decompose`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) }).then(jsonOr({})),
    updatePosition: (id: string, x: number, y: number) =>
      fetchWithResilience(`${API}/v2/beads/${id}/position`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) }).then(jsonOr({}))
  },

  // Epics
  epics: {
    create: (data: { title: string; steps: string[]; description?: string; priority?: number; sequential?: boolean; max_concurrent?: number; labels?: string[] }) =>
      fetchWithResilience(`${API}/v2/epics`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    get: (id: string) => fetchWithResilience(`${API}/v2/epics/${id}`).then(jsonOr({})),
    getReady: (id: string) => fetchWithResilience(`${API}/v2/epics/${id}/ready`).then(jsonOr([])),
  },

  // Orchestrator
  orchestrator: {
    launch: (data?: { provider?: string; agent_profile?: string }) =>
      fetchWithResilience(`${API}/v2/orchestrator/launch`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) }).then(jsonOr({})),
  },

  // Context Learning
  learn: {
    trigger: (sessionId: string) => fetchWithResilience(`${API}/v2/learn/sessions/${sessionId}`, { method: 'POST' }).then(jsonOr({})),
    fromTerminal: (terminalId: string, outcome: string = 'neutral') =>
      fetchWithResilience(`${API}/v2/learn/terminals/${terminalId}?outcome=${outcome}`, { method: 'POST' }).then(jsonOr({})),
    diffProposals: (status?: string) => fetchWithResilience(`${API}/v2/learn/diff-proposals${status ? `?status=${status}` : ''}`).then(jsonOr([])),
    approveDiff: (id: string) => fetchWithResilience(`${API}/v2/learn/diff-proposals/${id}/approve`, { method: 'POST' }).then(jsonOr({})),
    rejectDiff: (id: string, feedback?: string) => fetchWithResilience(`${API}/v2/learn/diff-proposals/${id}/reject${feedback ? `?feedback=${encodeURIComponent(feedback)}` : ''}`, { method: 'POST' }).then(jsonOr({})),
    proposals: () => fetchWithResilience(`${API}/v2/learn/proposals`).then(jsonOr([])),
    approve: (id: string) => fetchWithResilience(`${API}/v2/learn/proposals/${id}/approve`, { method: 'POST' }).then(jsonOr({})),
    reject: (id: string) => fetchWithResilience(`${API}/v2/learn/proposals/${id}/reject`, { method: 'POST' }).then(jsonOr({})),
    memories: () => fetchWithResilience(`${API}/v2/learn/memories`).then(jsonOr([])),
    addMemory: (title: string, content: string) => fetchWithResilience(`${API}/v2/learn/memories?title=${encodeURIComponent(title)}&content=${encodeURIComponent(content)}`, { method: 'POST' }).then(jsonOr({})),
    stats: () => fetchWithResilience(`${API}/v2/learn/stats`).then(jsonOr({})),
    context: () => fetchWithResilience(`${API}/v2/learn/context`).then(jsonOr({}))
  },

  // Ralph
  ralph: {
    list: () => fetchWithResilience(`${API}/v2/ralph`).then(jsonOr([])),
    get: (id: string) => fetchWithResilience(`${API}/v2/ralph/${id}`).then(jsonOr({})),
    create: (data: { prompt: string; min_iterations?: number; max_iterations?: number; agent_count?: number }) =>
      fetchWithResilience(`${API}/v2/ralph`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    delete: (id: string) => fetchWithResilience(`${API}/v2/ralph/${id}`, { method: 'DELETE' }),
    status: () => fetchWithResilience(`${API}/ralph`).then(jsonOr({})),
    start: (data: { prompt: string; min_iterations?: number; max_iterations?: number }) =>
      fetchWithResilience(`${API}/ralph`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    stop: () => fetchWithResilience(`${API}/ralph/stop`, { method: 'POST' })
  },

  // Map state
  map: {
    getState: () => fetchWithResilience(`${API}/v2/map/state`).then(jsonOr({}))
  },

  // Flows
  flows: {
    list: () => fetchWithResilience(`${API}/v2/flows`).then(jsonOr([])),
    get: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}`).then(jsonOr({})),
    create: (data: { name: string; schedule: string; agent_profile: string; prompt: string; provider?: string }) =>
      fetchWithResilience(`${API}/v2/flows`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(jsonOr({})),
    run: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}/run`, { method: 'POST' }).then(jsonOr({})),
    enable: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}/enable`, { method: 'POST' }).then(jsonOr({})),
    disable: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}/disable`, { method: 'POST' }).then(jsonOr({})),
    delete: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}`, { method: 'DELETE' }),
    executions: (name: string) => fetchWithResilience(`${API}/v2/flows/${name}/executions`).then(jsonOr({})),
    executionLog: (id: number) => fetchWithResilience(`${API}/v2/flows/executions/${id}/log`).then(jsonOr({})),
  },

  // Messages
  messages: {
    list: (terminalId: string, status?: string, limit?: number) => {
      const params = new URLSearchParams()
      if (status) params.set('status', status)
      if (limit) params.set('limit', limit.toString())
      const query = params.toString()
      return fetchWithResilience(`${API}/terminals/${terminalId}/inbox/messages${query ? `?${query}` : ''}`).then(jsonOr([]))
    }
  },

  // Terminals (sub-agents)
  terminals: {
    output: (terminalId: string) => fetchWithResilience(`${API}/v2/terminals/${terminalId}/output`).then(jsonOr({})),
    input: (terminalId: string, text: string) =>
      fetchWithResilience(`${API}/v2/terminals/${terminalId}/input`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) }).then(jsonOr({}))
  }
}

// WebSocket helpers
export function createTerminalStream(sessionId: string, onData: (data: { type: string; data: string; status: string }) => void) {
  const ws = new WebSocket(`ws://${location.host}/api/v2/sessions/${sessionId}/stream`)
  ws.onmessage = (e) => onData(JSON.parse(e.data))
  return ws
}

export function createActivityStream(onData: (data: unknown) => void) {
  const ws = new WebSocket(`ws://${location.host}/api/v2/activity/stream`)
  ws.onmessage = (e) => onData(JSON.parse(e.data))
  return ws
}
