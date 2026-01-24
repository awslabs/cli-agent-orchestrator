import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AgentPanel } from './AgentPanel'
import { useStore } from '../store'

vi.mock('../store', () => ({
  useStore: vi.fn()
}))

vi.mock('../api', () => ({
  api: {
    agents: { list: vi.fn(() => Promise.resolve([])) },
    sessions: {
      list: vi.fn(() => Promise.resolve([])),
      create: vi.fn(() => Promise.resolve({ id: 'new-session' })),
      delete: vi.fn(() => Promise.resolve()),
      context: vi.fn(() => Promise.resolve({ total: 0 }))
    },
    tasks: { list: vi.fn(() => Promise.resolve([])) }
  },
  createTerminalStream: vi.fn(() => ({ close: vi.fn() }))
}))

vi.mock('./TerminalView', () => ({
  TerminalView: () => <div data-testid="terminal-view">Terminal</div>
}))

const mockSupervisorSession = {
  id: 'supervisor-1',
  name: 'supervisor-session',
  status: 'BUSY' as const,
  terminals: [],
  agent_name: 'code_supervisor'
}

const mockWorkerSession = {
  id: 'worker-1',
  name: 'worker-session',
  status: 'PROCESSING' as const,
  terminals: [],
  agent_name: 'developer',
  parent_session: 'supervisor-1'
}

const mockStandaloneSession = {
  id: 'standalone-1',
  name: 'standalone-session',
  status: 'IDLE' as const,
  terminals: [],
  agent_name: 'generalist'
}

describe('AgentPanel Session Hierarchy', () => {
  const baseStore = {
    tasks: [],
    setTasks: vi.fn(),
    agents: [],
    setAgents: vi.fn(),
    sessions: [],
    setSessions: vi.fn(),
    activeSession: null,
    setActiveSession: vi.fn(),
    autoModeSessions: new Set<string>(),
    toggleAutoMode: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders sessions without parent_session at top level', () => {
    ;(useStore as any).mockReturnValue({
      ...baseStore,
      sessions: [mockStandaloneSession]
    })

    render(<AgentPanel />)
    
    // Standalone session should be visible at top level
    expect(screen.getByText('generalist')).toBeInTheDocument()
  })

  it('renders worker sessions nested under parent supervisor', () => {
    ;(useStore as any).mockReturnValue({
      ...baseStore,
      sessions: [mockSupervisorSession, mockWorkerSession]
    })

    render(<AgentPanel />)
    
    // Both sessions should be visible
    expect(screen.getByText('code_supervisor')).toBeInTheDocument()
    expect(screen.getByText('developer')).toBeInTheDocument()
    
    // Worker should have visual nesting indicator
    const workerElement = screen.getByText('developer').closest('[data-worker]')
    expect(workerElement).toHaveAttribute('data-worker', 'true')
  })

  it('shows worker count badge on supervisor session', () => {
    ;(useStore as any).mockReturnValue({
      ...baseStore,
      sessions: [mockSupervisorSession, mockWorkerSession]
    })

    render(<AgentPanel />)
    
    // Supervisor should show worker count
    expect(screen.getByText(/1 worker/i)).toBeInTheDocument()
  })

  it('groups multiple workers under same supervisor', () => {
    const worker2 = {
      ...mockWorkerSession,
      id: 'worker-2',
      agent_name: 'log-diver',
      parent_session: 'supervisor-1'
    }

    ;(useStore as any).mockReturnValue({
      ...baseStore,
      sessions: [mockSupervisorSession, mockWorkerSession, worker2]
    })

    render(<AgentPanel />)
    
    // Should show 2 workers badge
    expect(screen.getByText(/2 workers/i)).toBeInTheDocument()
  })
})
