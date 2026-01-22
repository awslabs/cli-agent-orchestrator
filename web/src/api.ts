const API = '/api'

export const api = {
  tasks: {
    list: () => fetch(`${API}/tasks`).then(r => r.json()),
    create: (data: { title: string; priority?: number }) => fetch(`${API}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    wip: (id: string) => fetch(`${API}/tasks/${id}/wip`, { method: 'POST' }).then(r => r.json()),
    close: (id: string) => fetch(`${API}/tasks/${id}/close`, { method: 'POST' }).then(r => r.json()),
    delete: (id: string) => fetch(`${API}/tasks/${id}`, { method: 'DELETE' })
  },
  ralph: {
    status: () => fetch(`${API}/ralph`).then(r => r.json()),
    start: (data: { prompt: string; min_iterations?: number; max_iterations?: number }) => fetch(`${API}/ralph`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    stop: () => fetch(`${API}/ralph/stop`, { method: 'POST' })
  },
  agents: { list: () => fetch('/sessions').then(r => r.json()) }
}
