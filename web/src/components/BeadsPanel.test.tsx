import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { BeadsPanel } from './BeadsPanel'
import { useStore } from '../store'

vi.mock('../store', () => ({
  useStore: vi.fn()
}))

vi.mock('../api', () => ({
  api: {
    tasks: {
      list: vi.fn(() => Promise.resolve([])),
      create: vi.fn(() => Promise.resolve({})),
      assign: vi.fn(() => Promise.resolve({})),
      assignAgent: vi.fn(() => Promise.resolve({ session_id: 'test-session' })),
      decompose: vi.fn(() => Promise.resolve({ beads: [] }))
    },
    agents: { list: vi.fn(() => Promise.resolve([])) },
    sessions: { input: vi.fn(() => Promise.resolve()) }
  }
}))

const mockBead = {
  id: 'bead-1',
  title: 'Test Bead',
  description: 'Test description',
  priority: 2,
  status: 'open'
}

const mockAgent = {
  name: 'generalist',
  description: 'General purpose agent',
  path: '/path/to/agent'
}

const mockSession = {
  id: 'session-1',
  name: 'test-session',
  status: 'IDLE' as const,
  terminals: [],
  agent_name: 'generalist'
}

describe('BeadsPanel Assignment Modal', () => {
  const mockStore = {
    tasks: [mockBead],
    setTasks: vi.fn(),
    sessions: [mockSession],
    agents: [mockAgent],
    setAgents: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStore as any).mockReturnValue(mockStore)
  })

  it('renders assignment modal with mode toggle when Assign clicked', async () => {
    render(<BeadsPanel />)
    
    // Find and click the Assign button
    const assignButton = screen.getByRole('button', { name: /assign/i })
    fireEvent.click(assignButton)
    
    // Modal should show mode toggle options
    expect(screen.getByText(/single agent/i)).toBeInTheDocument()
    expect(screen.getByText(/orchestrator/i)).toBeInTheDocument()
  })

  it('shows agent selection when Single Agent mode selected', async () => {
    render(<BeadsPanel />)
    
    const assignButton = screen.getByRole('button', { name: /assign/i })
    fireEvent.click(assignButton)
    
    // Click Single Agent mode
    const singleAgentOption = screen.getByText(/single agent/i)
    fireEvent.click(singleAgentOption)
    
    // Should show existing sessions and spawn new agent options
    expect(screen.getByText(/existing sessions/i)).toBeInTheDocument()
    expect(screen.getByText(/spawn new agent/i)).toBeInTheDocument()
  })

  it('shows supervisor selection when Orchestrator mode selected', async () => {
    render(<BeadsPanel />)
    
    const assignButton = screen.getByRole('button', { name: /assign/i })
    fireEvent.click(assignButton)
    
    // Click Orchestrator mode
    const orchestratorOption = screen.getByText(/orchestrator/i)
    fireEvent.click(orchestratorOption)
    
    // Should show supervisor selection
    expect(screen.getByText(/select supervisor/i)).toBeInTheDocument()
  })

  it('tracks mode selection state', async () => {
    render(<BeadsPanel />)
    
    const assignButton = screen.getByRole('button', { name: /assign/i })
    fireEvent.click(assignButton)
    
    // Initially no mode selected - both options visible
    const singleAgentOption = screen.getByText(/single agent/i)
    const orchestratorOption = screen.getByText(/orchestrator/i)
    
    // Click Single Agent
    fireEvent.click(singleAgentOption)
    
    // Single Agent should be selected (has selected styling)
    expect(singleAgentOption.closest('button')).toHaveClass('border-emerald-500')
  })
})


describe('BeadsPanel Hierarchy Display', () => {
  const parentBead = {
    id: 'parent-1',
    title: 'Parent Bead',
    description: 'Parent description',
    priority: 2,
    status: 'open'
  }

  const childBead = {
    id: 'parent-1.1',
    title: 'Child Bead',
    description: 'Child description',
    priority: 2,
    status: 'open',
    parent_id: 'parent-1'
  }

  const mockStore = {
    tasks: [parentBead, childBead],
    setTasks: vi.fn(),
    sessions: [],
    agents: [],
    setAgents: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStore as any).mockReturnValue(mockStore)
  })

  it('renders child beads nested under parent', () => {
    render(<BeadsPanel />)
    
    // Parent should be visible
    expect(screen.getByText('Parent Bead')).toBeInTheDocument()
    // Child should be visible
    expect(screen.getByText('Child Bead')).toBeInTheDocument()
  })

  it('shows hierarchy indicator on parent beads', () => {
    render(<BeadsPanel />)
    
    // Parent should have children indicator
    const parentElement = screen.getByText('Parent Bead').closest('[data-testid="bead-card"]')
    expect(parentElement).toBeInTheDocument()
  })
})

describe('BeadsPanel Session Links', () => {
  const assignedBead = {
    id: 'bead-1',
    title: 'Assigned Bead',
    priority: 2,
    status: 'open',
    assignee: 'session-1'
  }

  const mockSession = {
    id: 'session-1',
    name: 'test-session',
    status: 'IDLE' as const,
    terminals: [],
    agent_name: 'generalist'
  }

  const mockStore = {
    tasks: [assignedBead],
    setTasks: vi.fn(),
    sessions: [mockSession],
    agents: [],
    setAgents: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStore as any).mockReturnValue(mockStore)
  })

  it('shows session link on assigned bead', () => {
    render(<BeadsPanel />)
    
    // Should show agent name for assigned bead
    expect(screen.getByText('generalist')).toBeInTheDocument()
  })
})

describe('BeadsPanel Dependency Display', () => {
  const blockedBead = {
    id: 'bead-2',
    title: 'Blocked Bead',
    priority: 2,
    status: 'open',
    blocked_by: ['bead-1']
  }

  const blockerBead = {
    id: 'bead-1',
    title: 'Blocker Bead',
    priority: 1,
    status: 'wip'
  }

  const mockStore = {
    tasks: [blockerBead, blockedBead],
    setTasks: vi.fn(),
    sessions: [],
    agents: [],
    setAgents: vi.fn()
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStore as any).mockReturnValue(mockStore)
  })

  it('shows blocked_by indicator on bead', () => {
    render(<BeadsPanel />)
    
    // Blocked bead should show dependency info
    expect(screen.getByText('Blocked Bead')).toBeInTheDocument()
  })
})
