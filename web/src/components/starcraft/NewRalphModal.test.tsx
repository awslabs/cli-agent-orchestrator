import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { NewRalphModal } from './NewRalphModal'
import { useStarCraftStore } from '../../stores/starcraftStore'

vi.mock('../../stores/starcraftStore', async () => {
  const actual = await vi.importActual('../../stores/starcraftStore')
  return { ...actual, useStarCraftStore: vi.fn() }
})

vi.mock('../../api', () => ({
  api: {
    ralph: { create: vi.fn(() => Promise.resolve({ id: 'ralph-1', beadId: 'b1' })) }
  }
}))

describe('NewRalphModal', () => {
  const mockStore = { addRalphLoop: vi.fn(), agentsOnMap: [{ id: 'a1' }] }
  const onClose = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useStarCraftStore as any).mockReturnValue(mockStore)
  })

  it('renders modal with title', () => {
    render(<NewRalphModal onClose={onClose} />)
    expect(screen.getByText('🔄 New Ralph Loop')).toBeTruthy()
  })

  it('renders prompt textarea', () => {
    render(<NewRalphModal onClose={onClose} />)
    expect(screen.getByPlaceholderText('Enter task prompt...')).toBeTruthy()
  })

  it('renders iteration inputs', () => {
    render(<NewRalphModal onClose={onClose} />)
    expect(screen.getByText('Min Iterations')).toBeTruthy()
    expect(screen.getByText('Max Iterations')).toBeTruthy()
  })

  it('closes on cancel', () => {
    render(<NewRalphModal onClose={onClose} />)
    fireEvent.click(screen.getByText('Cancel'))
    expect(onClose).toHaveBeenCalled()
  })

  it('closes on backdrop click', () => {
    const { container } = render(<NewRalphModal onClose={onClose} />)
    const backdrop = container.querySelector('.fixed.inset-0')
    fireEvent.click(backdrop!)
    expect(onClose).toHaveBeenCalled()
  })

  it('disables start button when prompt empty', () => {
    render(<NewRalphModal onClose={onClose} />)
    const startBtn = screen.getByText('Start Loop')
    expect(startBtn.hasAttribute('disabled')).toBe(true)
  })

  it('enables start button when prompt entered', () => {
    render(<NewRalphModal onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText('Enter task prompt...'), { target: { value: 'Build API' } })
    const startBtn = screen.getByText('Start Loop')
    expect(startBtn.hasAttribute('disabled')).toBe(false)
  })

  it('updates min iterations', () => {
    render(<NewRalphModal onClose={onClose} />)
    const inputs = screen.getAllByRole('spinbutton')
    fireEvent.change(inputs[0], { target: { value: '5' } })
    expect(inputs[0]).toHaveValue(5)
  })

  it('updates max iterations', () => {
    render(<NewRalphModal onClose={onClose} />)
    const inputs = screen.getAllByRole('spinbutton')
    fireEvent.change(inputs[1], { target: { value: '15' } })
    expect(inputs[1]).toHaveValue(15)
  })

  it('creates ralph loop on submit', async () => {
    const { api } = await import('../../api')
    render(<NewRalphModal onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText('Enter task prompt...'), { target: { value: 'Build API' } })
    fireEvent.click(screen.getByText('Start Loop'))
    await waitFor(() => {
      expect(api.ralph.create).toHaveBeenCalledWith({ prompt: 'Build API', max_iterations: 10, min_iterations: 3 })
      expect(mockStore.addRalphLoop).toHaveBeenCalled()
      expect(onClose).toHaveBeenCalled()
    })
  })

  it('creates ralph loop even on API error', async () => {
    const { api } = await import('../../api')
    ;(api.ralph.create as any).mockRejectedValueOnce(new Error('API error'))
    render(<NewRalphModal onClose={onClose} />)
    fireEvent.change(screen.getByPlaceholderText('Enter task prompt...'), { target: { value: 'Build API' } })
    fireEvent.click(screen.getByText('Start Loop'))
    await waitFor(() => {
      expect(mockStore.addRalphLoop).toHaveBeenCalled()
      expect(onClose).toHaveBeenCalled()
    })
  })
})
