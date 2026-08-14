import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { StatusBadge } from '../components/StatusBadge'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { ConfirmModal } from '../components/ConfirmModal'
import { FALLBACK_PROVIDERS } from '../components/AgentPanel'
import type { Annotation } from '../api'
import { projectedTerminal } from './projectedTerminal'

function workItem(phase: string): Annotation {
  return {
    namespace: 'cao.work-state',
    kind: 'work-item',
    version: 1,
    label: phase === 'in-round' ? 'active' : phase,
    semantic_role: 'info',
    priority: 50,
    subject: { type: 'terminal', terminal_id: 'term-1', generation: 'gen-1' },
    valid_until: '2999-01-01T00:00:00Z',
    colour_key: null,
    details: { phase },
    source: 'test',
  }
}

describe('StatusBadge', () => {
  it('renders idle status', () => {
    render(<StatusBadge status="idle" />)
    expect(screen.getByText('Idle')).toBeInTheDocument()
  })

  it('renders processing status', () => {
    render(<StatusBadge status="processing" />)
    expect(screen.getByText('Processing')).toBeInTheDocument()
  })

  it('renders completed status as turn finished, not task completion', () => {
    render(<StatusBadge status="completed" />)
    expect(screen.getByText('Turn finished')).toBeInTheDocument()
  })

  it('surfaces the turn-finished explanation on a standalone completed badge', () => {
    render(<StatusBadge status="completed" />)
    const badge = screen.getByRole('note', { name: 'Turn finished' })
    // Accessible description: the explanation is reachable without any terminal
    // metadata, so a screen reader user hears what the status does not imply.
    expect(badge).toHaveAccessibleDescription(
      'The provider finished its current turn. This does not mean the assigned task, report, or campaign is complete.'
    )
    // Native-title fallback gives sighted hover the same copy on the standalone pill.
    expect(badge.getAttribute('title')).toBe(
      'The provider finished its current turn. This does not mean the assigned task, report, or campaign is complete.'
    )
  })

  it('gives every standalone completed badge its own accessible-description element', () => {
    render(
      <>
        <StatusBadge status="completed" />
        <StatusBadge status="completed" />
      </>
    )
    const badges = screen.getAllByRole('note', { name: 'Turn finished' })
    expect(badges).toHaveLength(2)
    const [first, second] = badges
    const firstId = first.getAttribute('aria-describedby')
    const secondId = second.getAttribute('aria-describedby')
    // Distinct ids, so a badge's description can never resolve to a sibling's.
    expect(firstId).not.toBeNull()
    expect(secondId).not.toBeNull()
    expect(firstId).not.toBe(secondId)
    const firstDesc = firstId ? document.getElementById(firstId) : null
    const secondDesc = secondId ? document.getElementById(secondId) : null
    expect(firstDesc).not.toBeNull()
    expect(secondDesc).not.toBeNull()
    expect(firstDesc).not.toBe(secondDesc)
    // Each id resolves to an explanation element belonging to its own badge.
    expect(first.contains(firstDesc)).toBe(true)
    expect(second.contains(secondDesc)).toBe(true)
    expect(firstDesc?.textContent).toBe(
      'The provider finished its current turn. This does not mean the assigned task, report, or campaign is complete.'
    )
    expect(secondDesc?.textContent).toBe(firstDesc?.textContent)
  })

  it('shows the turn-finished explanation inside the hover evidence card when terminal metadata is supplied', async () => {
    const terminal = projectedTerminal({
      status: 'completed',
      status_confidence: 'high',
      status_reason: 'provider reported its turn finished',
      status_signals: [],
    })
    render(<StatusBadge status="completed" terminal={terminal} />)
    fireEvent.mouseEnter(screen.getByRole('note', { name: /Turn finished/ }))
    const card = await screen.findByTestId('status-hovercard')
    expect(card.textContent).toContain(
      'The provider finished its current turn. This does not mean the assigned task, report, or campaign is complete.'
    )
    // The evidence card is the single hover surface — no stacked native title.
    expect(screen.getByRole('note', { name: /Turn finished/ }).getAttribute('title')).toBeNull()
  })

  it('renders error status', () => {
    render(<StatusBadge status="error" />)
    expect(screen.getByText('Error')).toBeInTheDocument()
  })

  it('renders waiting_user_answer status', () => {
    render(<StatusBadge status="waiting_user_answer" />)
    expect(screen.getByText('Awaiting Input')).toBeInTheDocument()
  })

  it('renders a managed native terminal as live without claiming turn activity', () => {
    render(<StatusBadge status="not_fifo_monitored" />)
    expect(screen.getByText('Managed Live')).toBeInTheDocument()
  })

  it('pulses Managed Active only while pane rendering is recent', () => {
    const recent = projectedTerminal({
      status: 'not_fifo_monitored',
      status_signals: [{ name: 'liveness', state: 'available', value: 5 }],
    })
    const quiet = projectedTerminal({
      status: 'not_fifo_monitored',
      status_signals: [{ name: 'liveness', state: 'available', value: 31 }],
    })
    const { rerender } = render(<StatusBadge status="not_fifo_monitored" terminal={recent} />)
    const active = screen.getByRole('note', { name: /Managed Active/ })
    expect(active.querySelector('.animate-pulse')).not.toBeNull()

    rerender(<StatusBadge status="not_fifo_monitored" terminal={quiet} />)
    const live = screen.getByRole('note', { name: /Managed Live/ })
    expect(live.querySelector('.animate-pulse')).toBeNull()
  })

  it('lets a durable parked checkpoint suppress an incidental activity pulse', () => {
    const terminal = projectedTerminal({
      status: 'not_fifo_monitored',
      status_signals: [{ name: 'liveness', state: 'available', value: 0 }],
    })
    render(<StatusBadge status="not_fifo_monitored" terminal={terminal} annotations={[workItem('parked')]} />)
    const parked = screen.getByRole('note', { name: /Managed Parked/ })
    expect(parked.querySelector('.animate-pulse')).toBeNull()
  })

  it('keeps a quiet managed processing claim in evidence instead of the headline', async () => {
    const terminal = projectedTerminal({
      status: 'processing',
      status_confidence: 'high',
      status_reason: 'classified from the rendered pane',
      status_signals: [
        { name: 'screen', state: 'available', value: 'processing' },
        { name: 'liveness', state: 'available', value: 725 },
      ],
    })
    render(<StatusBadge status="processing" terminal={terminal} annotations={[workItem('in-round')]} />)
    const badge = screen.getByRole('note', { name: /Managed Live/ })
    expect(badge.textContent).not.toContain('Processing')

    fireEvent.mouseEnter(badge)
    const card = await screen.findByTestId('status-hovercard')
    expect(card.textContent).toContain('Processing')
    expect(card.textContent).toContain('no pane rendering change for 12m 5s')
  })

  it('renders proven-dead and superseded dispositions explicitly', () => {
    const { rerender } = render(<StatusBadge status="dead" />)
    expect(screen.getByText('Dead')).toBeInTheDocument()
    rerender(<StatusBadge status="superseded" />)
    expect(screen.getByText('Superseded')).toBeInTheDocument()
  })

  it('renders null status as unknown', () => {
    render(<StatusBadge status={null} />)
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })

  it('explains the status decision and every contributing signal on hover', async () => {
    const terminal = projectedTerminal({
      status: 'processing',
      status_confidence: 'high',
      status_reason: "classified from the rendered pane by the provider's own screen detector",
      status_signals: [
        { name: 'screen', state: 'available', value: 'processing' },
        { name: 'liveness', state: 'available', value: 184 },
        { name: 'activity', state: 'available', value: 901 },
      ],
    })
    render(<StatusBadge status="processing" terminal={terminal} />)

    fireEvent.mouseEnter(screen.getByRole('note', { name: /Processing/ }))
    const card = await screen.findByTestId('status-hovercard')
    // The card is a structured <dl>; assert the evidence facts rather than a
    // layout-specific sentence assembled from sibling nodes.
    expect(card.textContent).toContain('High')
    expect(card.textContent).toContain("classified from the rendered pane by the provider's own screen detector")
    expect(card.textContent).toContain('Available · Processing')
    expect(card.textContent).toContain('no pane rendering change for 3m 4s')
    expect(card.textContent).toContain('15m 1s since CAO last sent input')
    expect(within(card).getByText('Reason')).toBeTruthy()
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
    const expected = ['kiro_cli', 'claude_code', 'q_cli', 'codex', 'gemini_cli', 'hermes', 'kimi_cli', 'copilot_cli', 'opencode_cli', 'cursor_cli']
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
