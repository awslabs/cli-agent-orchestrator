import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { StatusBadge } from '../components/StatusBadge'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { ConfirmModal } from '../components/ConfirmModal'
import { FALLBACK_PROVIDERS } from '../components/AgentPanel'
import { ProfilesPanel } from '../components/ProfilesPanel'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    listProfiles: vi.fn(),
  },
}))

const mockProfiles = [
  { name: 'code_supervisor', description: 'Supervisor Agent', source: 'built-in' },
  { name: 'developer', description: 'Developer Agent', source: 'installed' },
  { name: 'reviewer', description: 'Reviewer Agent', source: 'kiro' },
]

describe('StatusBadge', () => {
  it('renders idle status', () => {
    render(<StatusBadge status="idle" />)
    expect(screen.getByText('Idle')).toBeInTheDocument()
  })

  it('renders processing status', () => {
    render(<StatusBadge status="processing" />)
    expect(screen.getByText('Processing')).toBeInTheDocument()
  })

  it('renders completed status', () => {
    render(<StatusBadge status="completed" />)
    expect(screen.getByText('Completed')).toBeInTheDocument()
  })

  it('renders error status', () => {
    render(<StatusBadge status="error" />)
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('renders waiting_user_answer status', () => {
    render(<StatusBadge status="waiting_user_answer" />)
    expect(screen.getByText('Awaiting Input')).toBeInTheDocument()
  })

  it('renders null status as unknown', () => {
    render(<StatusBadge status={null} />)
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })
})

describe('ErrorBoundary', () => {
  // Suppress console.error for intentional error throws
  const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

  afterAll(() => consoleSpy.mockRestore())

  function ThrowingComponent(): JSX.Element {
    throw new Error('Test error')
  }

  it('catches errors and shows fallback', () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent />
      </ErrorBoundary>
    )
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
  })

  it('renders children when no error', () => {
    render(
      <ErrorBoundary>
        <div>Hello</div>
      </ErrorBoundary>
    )
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })
})

describe('ConfirmModal', () => {
  it('renders when open', () => {
    render(
      <ConfirmModal
        open={true}
        title="Delete Item"
        message="Are you sure?"
        details={[]}
        confirmLabel="Delete"
        variant="danger"
        loading={false}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    )
    expect(screen.getByText('Delete Item')).toBeInTheDocument()
    expect(screen.getByText('Are you sure?')).toBeInTheDocument()
    expect(screen.getByText('Delete')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('does not render when closed', () => {
    render(
      <ConfirmModal
        open={false}
        title="Delete Item"
        message="Are you sure?"
        details={[]}
        confirmLabel="Delete"
        variant="danger"
        loading={false}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    )
    expect(screen.queryByText('Delete Item')).not.toBeInTheDocument()
  })

  it('shows details when provided', () => {
    render(
      <ConfirmModal
        open={true}
        title="Confirm"
        message="Check details"
        details={[{ label: 'Name', value: 'test-flow' }, { label: 'Schedule', value: '0 9 * * *' }]}
        confirmLabel="OK"
        variant="danger"
        loading={false}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    )
    expect(screen.getByText('Name')).toBeInTheDocument()
    expect(screen.getByText('test-flow')).toBeInTheDocument()
    expect(screen.getByText('Schedule')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    render(
      <ConfirmModal
        open={true}
        title="Deleting"
        message="Please wait"
        details={[]}
        confirmLabel="Delete"
        variant="danger"
        loading={true}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    )
    const button = screen.getByText('Closing...').closest('button')
    expect(button).toBeDisabled()
  })
})

describe('FALLBACK_PROVIDERS', () => {
  it('includes opencode_cli', () => {
    expect(FALLBACK_PROVIDERS).toContain('opencode_cli')
  })

  it('includes all known providers', () => {
    const expected = ['kiro_cli', 'claude_code', 'q_cli', 'codex', 'gemini_cli', 'kimi_cli', 'copilot_cli', 'opencode_cli']
    for (const p of expected) {
      expect(FALLBACK_PROVIDERS).toContain(p)
    }
  })

  it('maps to enabled select options with default underscore label', () => {
    // Simulates the fallback option construction used in AgentPanel
    const options = FALLBACK_PROVIDERS.map(n => ({
      value: n,
      label: n.replace(/_/g, ' '),
      disabled: false,
    }))
    const opencodeOption = options.find(o => o.value === 'opencode_cli')
    expect(opencodeOption).toBeDefined()
    // opencode_cli uses the default underscore-to-space replacement
    expect(opencodeOption!.label).toBe('opencode cli')
    expect(opencodeOption!.disabled).toBe(false)

    const kiroOption = options.find(o => o.value === 'kiro_cli')
    expect(kiroOption).toBeDefined()
    expect(kiroOption!.label).toBe('kiro cli')
  })

  it('provides an opencode_cli option on empty providers', () => {
    // Simulates: when providers.length === 0, fallback is used
    const noProviders: any[] = []
    const effective = noProviders.length > 0 ? noProviders : FALLBACK_PROVIDERS.map(n => ({ name: n, binary: '', installed: true }))
    const names = effective.map(p => p.name)
    expect(names).toContain('opencode_cli')
  })
})

describe('ProfilesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state before data arrives', () => {
    vi.mocked(api.listProfiles).mockReturnValue(new Promise(() => {}))
    render(<ProfilesPanel />)
    expect(screen.getByText('Loading profiles...')).toBeInTheDocument()
  })

  it('shows empty state when API returns []', async () => {
    vi.mocked(api.listProfiles).mockResolvedValue([])
    render(<ProfilesPanel />)
    await waitFor(() => {
      expect(screen.getByText('No profiles found.')).toBeInTheDocument()
    })
    expect(screen.getByText(/cao install/)).toBeInTheDocument()
  })

  it('renders each profile with name, description, and source badge', async () => {
    vi.mocked(api.listProfiles).mockResolvedValue(mockProfiles)
    render(<ProfilesPanel />)
    await waitFor(() => {
      expect(screen.getByText('code_supervisor')).toBeInTheDocument()
    })
    expect(screen.getByText('developer')).toBeInTheDocument()
    expect(screen.getByText('reviewer')).toBeInTheDocument()
    expect(screen.getByText('built-in')).toBeInTheDocument()
    expect(screen.getByText('installed')).toBeInTheDocument()
    expect(screen.getByText('kiro')).toBeInTheDocument()
  })

  it('clicking a row expands it; clicking again collapses; only one expanded at a time', async () => {
    vi.mocked(api.listProfiles).mockResolvedValue(mockProfiles)
    render(<ProfilesPanel />)
    await waitFor(() => {
      expect(screen.getByText('code_supervisor')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText('code_supervisor'))
    expect(screen.getByText('Supervisor Agent')).toBeInTheDocument()

    fireEvent.click(screen.getByText('developer'))
    expect(screen.getByText('Developer Agent')).toBeInTheDocument()
    expect(screen.queryByText('Supervisor Agent')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('developer'))
    expect(screen.queryByText('Developer Agent')).not.toBeInTheDocument()
  })

  it('source badge uses blue for built-in and emerald otherwise', async () => {
    vi.mocked(api.listProfiles).mockResolvedValue(mockProfiles)
    render(<ProfilesPanel />)
    await waitFor(() => {
      expect(screen.getByText('code_supervisor')).toBeInTheDocument()
    })

    expect(screen.getByText('built-in').className).toMatch(/bg-blue/)
    expect(screen.getByText('installed').className).toMatch(/bg-emerald/)
    expect(screen.getByText('kiro').className).toMatch(/bg-emerald/)
  })

  it('filters out profiles with "managed by AIM" in description', async () => {
    const profilesWithAIM = [
      ...mockProfiles,
      { name: 'aim_agent', description: 'This agent is managed by AIM', source: 'built-in' },
    ]
    vi.mocked(api.listProfiles).mockResolvedValue(profilesWithAIM)
    render(<ProfilesPanel />)
    await waitFor(() => {
      expect(screen.getByText('code_supervisor')).toBeInTheDocument()
    })
    expect(screen.queryByText('aim_agent')).not.toBeInTheDocument()
  })
})
