import { useState, useEffect } from 'react'
import { useStore } from '../store'

interface Terminal {
  id: string
  name: string
  agent_profile: string
  status: string
  session_name: string
  port: number
}

export function AgentPanel() {
  const { agents, setAgents } = useStore()
  const [terminals, setTerminals] = useState<Terminal[]>([])
  const [activeIdx, setActiveIdx] = useState<number | null>(null)

  const refresh = async () => {
    const sessions = await fetch('/sessions').then(r => r.json())
    setAgents(sessions)
    const terms: Terminal[] = []
    for (let i = 0; i < sessions.length; i++) {
      const s = sessions[i]
      const data = await fetch(`/sessions/${s.id}`).then(r => r.json())
      if (data.terminals?.[0]) {
        terms.push({ ...data.terminals[0], session_name: s.id, port: 7681 + i })
      }
    }
    setTerminals(terms)
  }

  useEffect(() => { refresh() }, [])

  const S: Record<string, string> = { 
    completed: 'text-green-400', 
    processing: 'text-yellow-400 animate-pulse',
    idle: 'text-blue-400',
    error: 'text-red-400'
  }

  return (
    <div className="bg-gray-800 rounded p-4 col-span-2">
      <div className="flex justify-between mb-3">
        <h2 className="font-bold">🤖 AGENTS ({terminals.length})</h2>
        <button onClick={refresh} className="px-2 text-sm bg-gray-700 rounded">↻</button>
      </div>

      {/* Agent cards with embedded terminals */}
      <div className="space-y-3">
        {terminals.map((t, idx) => (
          <div key={t.id} className="bg-gray-700 rounded overflow-hidden">
            <div 
              className="flex items-center gap-2 p-2 cursor-pointer hover:bg-gray-600"
              onClick={() => setActiveIdx(activeIdx === idx ? null : idx)}
            >
              <span className={S[t.status] || S.idle}>●</span>
              <span className="font-mono font-bold">{t.name}</span>
              <span className="text-xs text-gray-400">{t.agent_profile}</span>
              <span className="text-xs text-gray-500">{t.status}</span>
              <span className="ml-auto text-xs text-gray-500">port {t.port}</span>
              <span className="text-gray-400">{activeIdx === idx ? '▼' : '▶'}</span>
            </div>
            {activeIdx === idx && (
              <iframe
                src={`http://localhost:${t.port}/`}
                className="w-full h-80 border-t border-gray-600 bg-black"
                title={`Terminal ${t.name}`}
              />
            )}
          </div>
        ))}
        {terminals.length === 0 && <div className="text-gray-500 text-sm">No agents - click ↻</div>}
      </div>
    </div>
  )
}
