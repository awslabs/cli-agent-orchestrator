import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AgentDescription, normalizeDescription } from '../components/AgentDescription'
import { api, Terminal } from '../api'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const result = (description: string | null) => ({ id: 'abcd1234', description } as Terminal)
const open = () => fireEvent.click(screen.getByRole('button', { name: 'Edit agent description' }))
const input = () => screen.getByRole('textbox', { name: 'Agent description' })

it.each([
  ['a\u0000\u0007\b\u001b\t\u007f\u009fb', 'ab'],
  ['a\r\nb\u0085c\u2028d\u2029e', 'a b c d e'],
  ['\u0000\u001b\t\u007f\u009f', null],
  ['😀'.repeat(64) + '\u0000', '😀'.repeat(64)],
  ['  Review\r\ncode  ', 'Review code'],
  ['\u0085Review\u2028code\u0085', 'Review code'],
  ['\u001f\tReview\u001f', 'Review'],
  ['\ufeffReview\ufeff', '\ufeffReview\ufeff'],
  ['  \n  ', null],
])('normalizes whitespace like Python (%j)', (value, expected) => {
  expect(normalizeDescription(value!)).toBe(expected)
})

describe('AgentDescription', () => {
  it('shows no placeholder, focuses the draft and cancels with Escape', () => {
    render(<AgentDescription id="abcd1234" description={null} onSaved={vi.fn()} />)
    expect(screen.queryByRole('textbox')).toBeNull()
    open()
    expect(input()).toHaveFocus()
    fireEvent.change(input(), { target: { value: 'Draft' } })
    fireEvent.keyDown(input(), { key: 'Escape' })
    open()
    expect(input()).toHaveValue('')
  })

  it('renders the full title and cancels with the button', () => {
    render(<AgentDescription id="abcd1234" description="Review code" onSaved={vi.fn()} />)
    expect(screen.getByText('Review code')).toHaveAttribute('title', 'Review code')
    open()
    fireEvent.change(input(), { target: { value: 'Other' } })
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.getByText('Review code')).toBeInTheDocument()
  })

  it.each(['x', '😀'])('accepts 64 code points and rejects 65 (%s)', async character => {
    const update = vi.spyOn(api, 'updateTerminalDescription').mockResolvedValue(result(character.repeat(64)))
    const saved = vi.fn()
    render(<AgentDescription id="abcd1234" description={null} onSaved={saved} />)
    open()
    fireEvent.change(input(), { target: { value: character.repeat(65) } })
    fireEvent.click(screen.getByText('Save'))
    expect(screen.getByRole('alert')).toHaveTextContent('64')
    expect(input()).toHaveValue(character.repeat(65))
    expect(update).not.toHaveBeenCalled()
    fireEvent.change(input(), { target: { value: character.repeat(64) } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    await waitFor(() => expect(saved).toHaveBeenCalledWith(character.repeat(64)))
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('keeps the draft after a failure and across polling updates, then clears it', async () => {
    const update = vi.spyOn(api, 'updateTerminalDescription').mockRejectedValueOnce(new Error('Offline')).mockResolvedValue(result(null))
    const saved = vi.fn()
    const { rerender } = render(<AgentDescription id="abcd1234" description="Old" onSaved={saved} />)
    open()
    fireEvent.change(input(), { target: { value: 'Draft' } })
    rerender(<AgentDescription id="abcd1234" description="Polled" onSaved={saved} />)
    expect(input()).toHaveValue('Draft')
    fireEvent.click(screen.getByText('Save'))
    await screen.findByText('Offline')
    expect(input()).toHaveValue('Draft')
    fireEvent.change(input(), { target: { value: '  ' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(saved).toHaveBeenCalledWith(null))
    expect(update).toHaveBeenLastCalledWith('abcd1234', null)
  })

  it('normalizes line breaks and prevents duplicate requests while saving', async () => {
    let resolve!: (terminal: Terminal) => void
    const update = vi.spyOn(api, 'updateTerminalDescription').mockImplementation(() => new Promise(done => { resolve = done }))
    render(<AgentDescription id="abcd1234" description={null} onSaved={vi.fn()} />)
    open()
    fireEvent.change(input(), { target: { value: '  Review code  ' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect(update).toHaveBeenCalledTimes(1)
    expect(update).toHaveBeenCalledWith('abcd1234', 'Review code')
    expect(screen.getByText('Saving...')).toBeDisabled()
    resolve(result('Review code'))
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
  })
})
