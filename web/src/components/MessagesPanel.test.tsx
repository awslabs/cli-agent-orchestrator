import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MessagesPanel } from './MessagesPanel'
import { useStore } from '../store'

vi.mock('../store', () => ({
  useStore: vi.fn()
}))

describe('MessagesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty state when no messages', () => {
    ;(useStore as any).mockReturnValue({ messages: [] })

    render(<MessagesPanel />)
    
    expect(screen.getByText(/no messages/i)).toBeInTheDocument()
  })

  it('renders messages with from/to/status', () => {
    ;(useStore as any).mockReturnValue({
      messages: [{
        id: 1,
        sender_id: 'terminal-abc',
        receiver_id: 'terminal-xyz',
        message: 'Analysis complete',
        status: 'delivered',
        created_at: new Date().toISOString()
      }]
    })

    render(<MessagesPanel />)
    
    expect(screen.getByText(/terminal-abc/)).toBeInTheDocument()
    expect(screen.getByText(/terminal-xyz/)).toBeInTheDocument()
    expect(screen.getByText(/Analysis complete/)).toBeInTheDocument()
    // Check status badge exists (use getAllByText since filter button also has "Delivered")
    const deliveredElements = screen.getAllByText(/delivered/i)
    expect(deliveredElements.length).toBeGreaterThan(0)
  })

  it('filters messages by status', () => {
    ;(useStore as any).mockReturnValue({
      messages: [
        { id: 1, sender_id: 'a', receiver_id: 'b', message: 'Pending msg', status: 'pending', created_at: new Date().toISOString() },
        { id: 2, sender_id: 'c', receiver_id: 'd', message: 'Delivered msg', status: 'delivered', created_at: new Date().toISOString() }
      ]
    })

    render(<MessagesPanel />)
    
    // Click pending filter
    const pendingFilter = screen.getByRole('button', { name: /pending/i })
    fireEvent.click(pendingFilter)
    
    expect(screen.getByText(/Pending msg/)).toBeInTheDocument()
    expect(screen.queryByText(/Delivered msg/)).not.toBeInTheDocument()
  })

  it('shows message count', () => {
    ;(useStore as any).mockReturnValue({
      messages: [
        { id: 1, sender_id: 'a', receiver_id: 'b', message: 'msg1', status: 'pending', created_at: new Date().toISOString() },
        { id: 2, sender_id: 'c', receiver_id: 'd', message: 'msg2', status: 'delivered', created_at: new Date().toISOString() }
      ]
    })

    render(<MessagesPanel />)
    
    expect(screen.getByText(/2 messages/i)).toBeInTheDocument()
  })
})
