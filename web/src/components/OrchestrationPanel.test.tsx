import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { OrchestrationPanel } from './OrchestrationPanel'
import { useStore } from '../store'
import { api } from '../api'

vi.mock('../store', () => ({
  useStore: vi.fn()
}))

vi.mock('../api', () => ({
  api: {
    sessions: {
      delete: vi.fn()
    }
  }
}))

describe('OrchestrationPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty state when no orchestrations', () => {
    ;(useStore as any).mockReturnValue({ sessions: [] })

    render(<OrchestrationPanel />)
    
    expect(screen.getByText(/no active orchestrations/i)).toBeInTheDocument()
  })

  it('renders orchestration list with supervisor info', () => {
    ;(useStore as any).mockReturnValue({
      sessions: [
        { id: 'sup-1', name: 'supervisor', agent_name: 'code_supervisor', status: 'PROCESSING', terminals: [] },
        { id: 'worker-1', name: 'worker1', agent_name: 'developer', status: 'PROCESSING', parent_session: 'sup-1', terminals: [] },
        { id: 'worker-2', name: 'worker2', agent_name: 'reviewer', status: 'IDLE', parent_session: 'sup-1', terminals: [] }
      ]
    })

    render(<OrchestrationPanel />)
    
    expect(screen.getByText(/code_supervisor/)).toBeInTheDocument()
    expect(screen.getByText(/2 workers/i)).toBeInTheDocument()
  })

  it('shows flow diagram when orchestration selected', () => {
    ;(useStore as any).mockReturnValue({
      sessions: [
        { id: 'sup-1', name: 'supervisor', agent_name: 'code_supervisor', status: 'PROCESSING', terminals: [] },
        { id: 'worker-1', name: 'worker1', agent_name: 'developer', status: 'PROCESSING', parent_session: 'sup-1', terminals: [] }
      ]
    })

    render(<OrchestrationPanel />)
    
    // Click on orchestration to select it
    fireEvent.click(screen.getByText(/code_supervisor/))
    
    // Should show worker in flow diagram
    expect(screen.getByText(/developer/)).toBeInTheDocument()
  })

  it('displays worker status correctly', () => {
    ;(useStore as any).mockReturnValue({
      sessions: [
        { id: 'sup-1', name: 'supervisor', agent_name: 'code_supervisor', status: 'PROCESSING', terminals: [] },
        { id: 'worker-1', name: 'worker1', agent_name: 'developer', status: 'ERROR', parent_session: 'sup-1', terminals: [] },
        { id: 'worker-2', name: 'worker2', agent_name: 'reviewer', status: 'IDLE', parent_session: 'sup-1', terminals: [] }
      ]
    })

    render(<OrchestrationPanel />)
    
    fireEvent.click(screen.getByText(/code_supervisor/))
    
    // Status badges should be visible
    expect(screen.getByText(/error/i)).toBeInTheDocument()
    expect(screen.getByText(/idle/i)).toBeInTheDocument()
  })

  it('Stop All button stops orchestration', async () => {
    ;(useStore as any).mockReturnValue({
      sessions: [
        { id: 'sup-1', name: 'supervisor', agent_name: 'code_supervisor', status: 'PROCESSING', terminals: [] },
        { id: 'worker-1', name: 'worker1', agent_name: 'developer', status: 'PROCESSING', parent_session: 'sup-1', terminals: [] }
      ]
    })
    ;(api.sessions.delete as any).mockResolvedValue({})

    render(<OrchestrationPanel />)
    
    fireEvent.click(screen.getByText(/code_supervisor/))
    fireEvent.click(screen.getByRole('button', { name: /stop all/i }))
    
    // Wait for async operations
    await vi.waitFor(() => {
      expect(api.sessions.delete).toHaveBeenCalledWith('worker-1')
      expect(api.sessions.delete).toHaveBeenCalledWith('sup-1')
    })
  })
})
