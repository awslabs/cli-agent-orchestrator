import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../components/StatusBadge'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { ConfirmModal } from '../components/ConfirmModal'
import { providerLabel, FALLBACK_PROVIDERS } from '../components/AgentPanel'

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

describe('providerLabel', () => {
  it('renders opencode_cli as "opencode"', () => {
    expect(providerLabel('opencode_cli')).toBe('opencode')
  })

  it('keeps existing underscore-to-space behavior for kiro_cli', () => {
    expect(providerLabel('kiro_cli')).toBe('kiro cli')
  })

  it('keeps existing behavior for claude_code', () => {
    expect(providerLabel('claude_code')).toBe('claude code')
  })

  it('keeps existing behavior for providers without underscores', () => {
    expect(providerLabel('codex')).toBe('codex')
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

  it('maps to enabled select options with correct label', () => {
    // Simulates the fallback option construction used in AgentPanel
    const options = FALLBACK_PROVIDERS.map(n => ({
      value: n,
      label: providerLabel(n),
      disabled: false,
    }))
    const opencodeOption = options.find(o => o.value === 'opencode_cli')
    expect(opencodeOption).toBeDefined()
    expect(opencodeOption!.label).toBe('opencode')
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
