const API = '/api'

export const api = {
  // V2 Agents
  agents: {
    list: () => fetch(`${API}/v2/agents`).then(r => r.json()),
    get: (name: string) => fetch(`${API}/v2/agents/${name}`).then(r => r.json()),
    create: (data: { name: string; description?: string; steering?: string }) => 
      fetch(`${API}/v2/agents`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    update: (name: string, data: { name: string; description?: string; steering?: string }) =>
      fetch(`${API}/v2/agents/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    delete: (name: string) => fetch(`${API}/v2/agents/${name}`, { method: 'DELETE' })
  },

  // V2 Sessions
  sessions: {
    list: () => fetch(`${API}/v2/sessions`).then(r => r.json()),
    get: (id: string) => fetch(`${API}/v2/sessions/${id}`).then(r => r.json()),
    create: (data: { agent_name: string; provider?: string }) =>
      fetch(`${API}/v2/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    delete: (id: string) => fetch(`${API}/v2/sessions/${id}`, { method: 'DELETE' }),
    input: (id: string, message: string, raw: boolean = false) =>
      fetch(`${API}/v2/sessions/${id}/input?message=${encodeURIComponent(message)}&raw=${raw}`, { method: 'POST' }),
    output: (id: string) => fetch(`${API}/v2/sessions/${id}/output`).then(r => r.json()),
    context: (id: string) => fetch(`${API}/v2/sessions/${id}/context`).then(r => r.json()),
    autoMode: (id: string, enabled: boolean) =>
      fetch(`${API}/v2/sessions/${id}/auto-mode`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) }).then(r => r.json()),
    updatePosition: (id: string, x: number, y: number) =>
      fetch(`${API}/v2/sessions/${id}/position`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) }).then(r => r.json())
  },

  // V2 Activity
  activity: {
    list: (sessionId?: string) => fetch(`${API}/v2/activity${sessionId ? `?session_id=${sessionId}` : ''}`).then(r => r.json())
  },

  // Beads (Tasks)
  tasks: {
    list: () => fetch(`${API}/tasks`).then(r => r.json()),
    get: (id: string) => fetch(`${API}/tasks/${id}`).then(r => r.json()),
    create: (data: { title: string; description?: string; priority?: number }) => 
      fetch(`${API}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    update: (id: string, data: Partial<{ title: string; description: string; priority: number; status: string }>) =>
      fetch(`${API}/tasks/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    wip: (id: string) => fetch(`${API}/tasks/${id}/wip`, { method: 'POST' }).then(r => r.json()),
    close: (id: string) => fetch(`${API}/tasks/${id}/close`, { method: 'POST' }).then(r => r.json()),
    delete: (id: string) => fetch(`${API}/tasks/${id}`, { method: 'DELETE' }),
    assign: (id: string, sessionId: string) =>
      fetch(`${API}/v2/beads/${id}/assign`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId }) }).then(r => r.json()),
    assignAgent: (id: string, agentName: string, provider: string = 'kiro_cli') =>
      fetch(`${API}/v2/beads/${id}/assign-agent`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent_name: agentName, provider }) }).then(r => r.json()),
    unassignSession: (sessionId: string) =>
      fetch(`${API}/tasks/unassign-session/${sessionId}`, { method: 'POST' }).then(r => r.json()),
    decompose: (text: string) =>
      fetch(`${API}/v2/beads/decompose`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) }).then(r => r.json()),
    updatePosition: (id: string, x: number, y: number) =>
      fetch(`${API}/v2/beads/${id}/position`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) }).then(r => r.json())
  },

  // Context Learning
  learn: {
    trigger: (sessionId: string) => fetch(`${API}/v2/learn/sessions/${sessionId}`, { method: 'POST' }).then(r => r.json()),
    proposals: () => fetch(`${API}/v2/learn/proposals`).then(r => r.json()),
    approve: (id: string) => fetch(`${API}/v2/learn/proposals/${id}/approve`, { method: 'POST' }).then(r => r.json()),
    reject: (id: string) => fetch(`${API}/v2/learn/proposals/${id}/reject`, { method: 'POST' }).then(r => r.json()),
    memories: () => fetch(`${API}/v2/learn/memories`).then(r => r.json()),
    addMemory: (title: string, content: string) => fetch(`${API}/v2/learn/memories?title=${encodeURIComponent(title)}&content=${encodeURIComponent(content)}`, { method: 'POST' }).then(r => r.json()),
    stats: () => fetch(`${API}/v2/learn/stats`).then(r => r.json()),
    context: () => fetch(`${API}/v2/learn/context`).then(r => r.json())
  },

  // Ralph
  ralph: {
    list: () => fetch(`${API}/v2/ralph`).then(r => r.json()),
    get: (id: string) => fetch(`${API}/v2/ralph/${id}`).then(r => r.json()),
    create: (data: { prompt: string; min_iterations?: number; max_iterations?: number; agent_count?: number }) => 
      fetch(`${API}/v2/ralph`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    delete: (id: string) => fetch(`${API}/v2/ralph/${id}`, { method: 'DELETE' }),
    status: () => fetch(`${API}/ralph`).then(r => r.json()),
    start: (data: { prompt: string; min_iterations?: number; max_iterations?: number }) => 
      fetch(`${API}/ralph`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    stop: () => fetch(`${API}/ralph/stop`, { method: 'POST' })
  },

  // Map state
  map: {
    getState: () => fetch(`${API}/v2/map/state`).then(r => r.json())
  },

  // Flows
  flows: {
    list: () => fetch(`${API}/v2/flows`).then(r => r.json()),
    get: (name: string) => fetch(`${API}/v2/flows/${name}`).then(r => r.json()),
    create: (data: { name: string; schedule: string; agent_profile: string; prompt: string; provider?: string }) =>
      fetch(`${API}/v2/flows`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    run: (name: string) => fetch(`${API}/v2/flows/${name}/run`, { method: 'POST' }).then(r => r.json()),
    enable: (name: string) => fetch(`${API}/v2/flows/${name}/enable`, { method: 'POST' }).then(r => r.json()),
    disable: (name: string) => fetch(`${API}/v2/flows/${name}/disable`, { method: 'POST' }).then(r => r.json()),
    delete: (name: string) => fetch(`${API}/v2/flows/${name}`, { method: 'DELETE' })
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
