import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ActivityFeed } from './ActivityFeed'
import { useStore } from '../store'

vi.mock('../store', () => ({
  useStore: vi.fn()
}))

describe('ActivityFeed Orchestration Events', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders orchestration_started event with correct icon', () => {
    ;(useStore as any).mockReturnValue({
      activity: [{
        type: 'orchestration_started',
        timestamp: new Date().toISOString(),
        message: 'Orchestration started for bead-1'
      }]
    })

    render(<ActivityFeed />)
    
    // Use getAllByText since the event type appears in both the label and message
    const elements = screen.getAllByText(/orchestration started/i)
    expect(elements.length).toBeGreaterThan(0)
    // Check that the event type label has the correct color class
    expect(elements[0]).toHaveClass('text-cyan-400')
  })

  it('renders worker_spawned event with correct icon', () => {
    ;(useStore as any).mockReturnValue({
      activity: [{
        type: 'worker_spawned',
        timestamp: new Date().toISOString(),
        message: 'Worker developer spawned'
      }]
    })

    render(<ActivityFeed />)
    
    expect(screen.getByText(/worker spawned/i)).toBeInTheDocument()
  })

  it('renders handoff_initiated event with correct icon', () => {
    ;(useStore as any).mockReturnValue({
      activity: [{
        type: 'handoff_initiated',
        timestamp: new Date().toISOString(),
        message: 'Handoff to reviewer'
      }]
    })

    render(<ActivityFeed />)
    
    expect(screen.getByText(/handoff initiated/i)).toBeInTheDocument()
  })

  it('renders message_sent event with correct icon', () => {
    ;(useStore as any).mockReturnValue({
      activity: [{
        type: 'message_sent',
        timestamp: new Date().toISOString(),
        message: 'Message from developer to supervisor'
      }]
    })

    render(<ActivityFeed />)
    
    expect(screen.getByText(/message sent/i)).toBeInTheDocument()
  })
})
