import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { NewBeadModal } from './NewBeadModal'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

vi.mock('../../api', () => ({
  api: {
    tasks: { create: vi.fn(() => Promise.resolve({ id: 'new-bead', title: 'Test' })) }
  }
}))

describe('NewBeadModal', () => {
  const mockStore = { addBead: vi.fn() }
  const onClose = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders modal with title', () => {
    render(<NewBeadModal onClose={onClose} />)
    expect(screen.getByText('📋 New Bead')).toBeTruthy()
  })

  it('renders title input', () => {
    render(<NewBeadModal onClose={onClose} />)
    expect(screen.getByPlaceholderText('Enter bead title...')).toBeTruthy()
  })

  it('renders priority buttons', () => {
    render(<NewBeadModal onClose={onClose} />)
    expect(screen.getByText('P1')).toBeTruthy()
    expect(screen.getByText('P2')).toBeTruthy()
    expect(screen.getByText('P3')).toBeTruthy()
  })

  it('closes on cancel', () => {
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.click(screen.getByText('Cancel'))
    expect(onClose).toHaveBeenCalled()
  })

  it('closes on backdrop click', () => {
    const { container } = render(<NewBeadModal onClose={onClose} />)
    const backdrop = container.querySelector('.fixed.inset-0')
    fireEvent.click(backdrop!)
    expect(onClose).toHaveBeenCalled()
  })

  it('does not close on modal content click', () => {
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.click(screen.getByText('📋 New Bead'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('disables create button when title empty', () => {
    render(<NewBeadModal onClose={onClose} />)
    const createBtn = screen.getByText('Create')
    expect(createBtn.hasAttribute('disabled')).toBe(true)
  })

  it('enables create button when title entered', () => {
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText('Enter bead title...'), { target: { value: 'Test task' } })
    const createBtn = screen.getByText('Create')
    expect(createBtn.hasAttribute('disabled')).toBe(false)
  })

  it('changes priority on button click', () => {
    render(<NewBeadModal onClose={onClose} />)
    const p1Btn = screen.getByText('P1')
    fireEvent.click(p1Btn)
    // P1 should now be selected (has different background)
    expect(p1Btn.style.background).toContain('ff4444')
  })

  it('creates bead on submit', async () => {
    const { api } = await import('../../api')
    render(<NewBeadModal onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText('Enter bead title...'), { target: { value: 'Test task' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => {
      expect(api.tasks.create).toHaveBeenCalledWith({ title: 'Test task', priority: 2 })
      expect(mockStore.addBead).toHaveBeenCalled()
      expect(onClose).toHaveBeenCalled()
    })
  })
})
