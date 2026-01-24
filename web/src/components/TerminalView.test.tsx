import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { TerminalView } from './TerminalView'

// Mock xterm with proper class
vi.mock('xterm', () => {
  return {
    Terminal: class MockTerminal {
      loadAddon = vi.fn()
      open = vi.fn()
      write = vi.fn()
      focus = vi.fn()
      dispose = vi.fn()
      onData = vi.fn()
      attachCustomKeyEventHandler = vi.fn()
    }
  }
})

vi.mock('xterm-addon-fit', () => {
  return {
    FitAddon: class MockFitAddon {
      fit = vi.fn()
    }
  }
})

// Mock API
vi.mock('../api', () => ({
  api: {
    sessions: {
      input: vi.fn().mockResolvedValue({}),
      output: vi.fn().mockResolvedValue({ output: 'test output', status: 'IDLE' }),
    },
  },
}))

// Mock WebSocket
const mockWs = { close: vi.fn(), send: vi.fn() }
vi.stubGlobal('WebSocket', class {
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  close = mockWs.close
  constructor() { setTimeout(() => this.onopen?.(), 10) }
})

describe('TerminalView', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders terminal container with session ID', async () => {
    render(<TerminalView sessionId="cao-12345678" />)
    expect(screen.getByText('12345678')).toBeInTheDocument()
  })

  it('shows connection status indicator', () => {
    const { container } = render(<TerminalView sessionId="test-session" />)
    const dot = container.querySelector('.rounded-full')
    expect(dot).toBeInTheDocument()
  })

  it('has fullscreen toggle button', () => {
    const { container } = render(<TerminalView sessionId="test-session" />)
    const button = container.querySelector('button')
    expect(button).toBeInTheDocument()
  })

  it('shows keyboard hint', () => {
    render(<TerminalView sessionId="test-session" />)
    expect(screen.getByText(/Ctrl\+Shift\+V/)).toBeInTheDocument()
  })

  it('creates terminal div for xterm', () => {
    const { container } = render(<TerminalView sessionId="test-session" />)
    const termDiv = container.querySelector('.flex-1.min-h-0')
    expect(termDiv).toBeInTheDocument()
  })
})
