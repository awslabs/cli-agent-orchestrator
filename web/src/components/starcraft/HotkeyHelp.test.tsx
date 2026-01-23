import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { HotkeyHelp } from './HotkeyHelp'

describe('HotkeyHelp', () => {
  const onClose = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders modal with title', () => {
    render(<HotkeyHelp onClose={onClose} />)
    expect(screen.getByText('⌨️ Keyboard Shortcuts')).toBeTruthy()
  })

  it('shows hotkey list', () => {
    render(<HotkeyHelp onClose={onClose} />)
    expect(screen.getByText('Space')).toBeTruthy()
    expect(screen.getByText('Tab')).toBeTruthy()
    expect(screen.getByText('Escape')).toBeTruthy()
  })

  it('closes on backdrop click', () => {
    const { container } = render(<HotkeyHelp onClose={onClose} />)
    const backdrop = container.querySelector('.fixed.inset-0')
    fireEvent.click(backdrop!)
    expect(onClose).toHaveBeenCalled()
  })

  it('closes on any key press', () => {
    render(<HotkeyHelp onClose={onClose} />)
    fireEvent.keyDown(window, { key: 'a' })
    expect(onClose).toHaveBeenCalled()
  })

  it('does not close on modal content click', () => {
    render(<HotkeyHelp onClose={onClose} />)
    fireEvent.click(screen.getByText('⌨️ Keyboard Shortcuts'))
    expect(onClose).not.toHaveBeenCalled()
  })
})
