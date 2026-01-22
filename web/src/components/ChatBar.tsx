import { useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'

export function ChatBar() {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const { setTasks } = useStore()

  const handleSubmit = async () => {
    if (!input.trim()) return
    setLoading(true)
    try {
      // Check for decompose command
      if (input.toLowerCase().startsWith('break down') || input.toLowerCase().startsWith('decompose')) {
        const text = input.replace(/^(break down|decompose):?\s*/i, '')
        const result = await api.tasks.decompose(text)
        alert(`Created ${result.count} tasks`)
        api.tasks.list().then(setTasks)
      } else {
        // Create single task
        await api.tasks.create({ title: input })
        api.tasks.list().then(setTasks)
      }
      setInput('')
    } catch (e) {
      console.error(e)
    }
    setLoading(false)
  }

  return (
    <div className="flex gap-2 p-3 bg-gray-800 border-b border-gray-700">
      <span className="text-xl">💬</span>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
        placeholder="Ask Kiro to break down tasks, query status, etc..."
        className="flex-1 px-3 py-2 bg-gray-900 border border-gray-600 rounded"
        disabled={loading}
      />
      <button 
        onClick={handleSubmit} 
        disabled={loading}
        className="px-4 py-2 bg-blue-600 rounded disabled:opacity-50"
      >
        {loading ? '...' : 'Send'}
      </button>
    </div>
  )
}
