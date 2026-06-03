import { useState, useEffect, useRef } from 'react'
import { api, InboxMessage } from '../api'
import { X, Send, Mail, Loader2 } from 'lucide-react'

interface InboxPanelProps {
  terminalId: string
  onClose: () => void
}

type StatusFilter = 'all' | 'pending' | 'delivered' | 'failed'

const STATUS_FILTERS: { key: StatusFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Pending' },
  { key: 'delivered', label: 'Delivered' },
  { key: 'failed', label: 'Failed' },
]

function formatRelativeTime(dateStr: string | null): string {
  if (!dateStr) return ''
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diffSec = Math.floor((now - then) / 1000)
  if (diffSec < 0) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.floor(diffHr / 24)
  return `${diffDay}d ago`
}

function MessageStatusBadge({ status }: { status: InboxMessage['status'] }) {
  const config = {
    delivered: { bg: 'bg-emerald-400/10', text: 'text-emerald-400', label: 'Delivered' },
    pending: { bg: 'bg-amber-400/10', text: 'text-amber-400', label: 'Pending' },
    failed: { bg: 'bg-red-400/10', text: 'text-red-400', label: 'Failed' },
  }
  const c = config[status] || config.pending
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${c.bg} ${c.text}`}>
      {c.label}
    </span>
  )
}

export function InboxPanel({ terminalId, onClose }: InboxPanelProps) {
  const [messages, setMessages] = useState<InboxMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [sendText, setSendText] = useState('')
  const [sending, setSending] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const fetchMessages = async () => {
    try {
      const status = filter === 'all' ? undefined : filter
      const data = await api.getInboxMessages(terminalId, 50, status)
      setMessages(data)
    } catch {
      // silently fail — will retry
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    fetchMessages()
    const interval = setInterval(fetchMessages, 5000)
    return () => clearInterval(interval)
  }, [terminalId, filter])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSend = async () => {
    const text = sendText.trim()
    if (!text || sending) return
    setSending(true)
    try {
      await api.sendInboxMessage(terminalId, 'ui', text)
      setSendText('')
      await fetchMessages()
    } catch {
      // send failed — user can retry
    }
    setSending(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const isReceiver = (msg: InboxMessage) => msg.receiver_id === terminalId

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Modal */}
      <div className="relative bg-gray-900 border border-gray-700/50 rounded-2xl shadow-2xl w-full max-w-[600px] mx-4 flex flex-col" style={{ maxHeight: 'calc(100vh - 80px)' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700/50 shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-emerald-900/50 flex items-center justify-center">
              <Mail size={16} className="text-emerald-400" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-white">Agent Inbox</h3>
              <p className="text-[11px] text-gray-500">Messages between agents in this session <span className="font-mono">({terminalId})</span></p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-gray-500 hover:text-white transition-colors rounded-lg hover:bg-gray-800"
            title="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Filter Tabs */}
        <div className="px-5 py-3 border-b border-gray-700/30 shrink-0 overflow-x-auto">
          <div className="flex gap-2">
            {STATUS_FILTERS.map(f => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`px-3 py-1.5 text-xs font-medium rounded-full whitespace-nowrap transition-colors ${
                  filter === f.key
                    ? 'bg-emerald-600 text-white'
                    : 'bg-gray-800 text-gray-400 hover:text-white hover:bg-gray-700'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 min-h-[200px]">
          {loading && messages.length === 0 ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 size={20} className="animate-spin text-gray-500" />
            </div>
          ) : messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-gray-500">
              <Mail size={32} className="mb-3 opacity-40" />
              <p className="text-sm">No messages yet</p>
              <p className="text-xs text-gray-600 mt-1">Messages appear here when agents communicate via handoff, assign, or send_message. You can also send a message manually below.</p>
            </div>
          ) : (
            messages.map(msg => {
              const incoming = isReceiver(msg)
              return (
                <div
                  key={msg.id}
                  className={`flex flex-col ${incoming ? 'items-start' : 'items-end'}`}
                >
                  <div
                    className={`max-w-[85%] rounded-xl px-3.5 py-2.5 ${
                      incoming
                        ? 'bg-gray-800 border border-gray-700/40'
                        : 'bg-emerald-900/30 border border-emerald-700/30'
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[10px] font-mono text-gray-500">
                        {incoming ? msg.sender_id.slice(0, 8) : msg.receiver_id.slice(0, 8)}
                      </span>
                      <MessageStatusBadge status={msg.status} />
                    </div>
                    <p className="text-sm text-gray-200 whitespace-pre-wrap break-words">{msg.message}</p>
                    {msg.created_at && (
                      <p className="text-[10px] text-gray-600 mt-1">{formatRelativeTime(msg.created_at)}</p>
                    )}
                  </div>
                </div>
              )
            })
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Send Form */}
        <div className="px-5 py-4 border-t border-gray-700/50 shrink-0">
          <div className="flex gap-2">
            <input
              ref={inputRef}
              type="text"
              value={sendText}
              onChange={e => setSendText(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message..."
              className="flex-1 bg-gray-800 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2.5 focus:border-emerald-500 focus:outline-none placeholder-gray-600"
            />
            <button
              onClick={handleSend}
              disabled={!sendText.trim() || sending}
              className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-4 py-2.5 rounded-lg transition-colors"
            >
              {sending ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Send size={14} />
              )}
              Send
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
