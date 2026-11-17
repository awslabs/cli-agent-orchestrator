import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, within, act } from '@testing-library/react'
import { CustomSelect } from '../components/CustomSelect'

// The portal/positioning rewrite (#692) backs Flows and Agents too; these
// tests pin the shared behaviours a form-level suite never exercises.

const OPTIONS = [
  { value: 'a', label: 'Alpha', sublabel: 'first letter' },
  { value: 'b', label: 'Beta' },
  { value: 'c', label: 'Gamma', disabled: true },
]

const openMenu = () => {
  fireEvent.click(screen.getByRole('button', { name: 'pick' }))
  return screen.getByTestId('custom-select-menu')
}

afterEach(() => vi.restoreAllMocks())

describe('CustomSelect — portaled menu', () => {
  it('renders the menu in a portal at document.body, not inside the trigger tree', () => {
    const { container } = render(
      <CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />,
    )
    const menu = openMenu()
    // Portal: the menu is OUTSIDE the component's own DOM tree...
    expect(container.contains(menu)).toBe(false)
    // ...and a direct child of document.body, immune to ancestor overflow.
    expect(menu.parentElement).toBe(document.body)
    expect(menu.style.position).toBe('fixed')
  })

  it('selects an option through the portal (outside-click handler must not eat it)', () => {
    const onChange = vi.fn()
    render(<CustomSelect ariaLabel="pick" value="" onChange={onChange} options={OPTIONS} />)
    const menu = openMenu()
    // mousedown outside `ref` used to close the menu before the click landed;
    // the handler must treat the portaled menu as inside.
    const beta = within(menu).getByRole('button', { name: /Beta/ })
    fireEvent.mouseDown(beta)
    expect(screen.getByTestId('custom-select-menu')).toBeInTheDocument()
    fireEvent.click(beta)
    expect(onChange).toHaveBeenCalledWith('b')
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
  })

  it('a mousedown outside both trigger and menu closes it; disabled options do not select', () => {
    const onChange = vi.fn()
    render(<CustomSelect ariaLabel="pick" value="" onChange={onChange} options={OPTIONS} />)
    const menu = openMenu()
    fireEvent.click(within(menu).getByRole('button', { name: /Gamma/ }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
  })

  it('flips upward when there is not enough room below the trigger', () => {
    // Trigger near the viewport bottom: 28px below < menu max height.
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
      top: 700, bottom: 740, left: 10, right: 210, width: 200, height: 40,
      x: 10, y: 700, toJSON: () => ({}),
    } as DOMRect)
    render(<CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />)
    const menu = openMenu()
    expect(menu.style.bottom).not.toBe('')   // anchored above the trigger
    expect(menu.style.top).toBe('')
  })

  it('opens downward when there is room below', () => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
      top: 100, bottom: 140, left: 10, right: 210, width: 200, height: 40,
      x: 10, y: 100, toJSON: () => ({}),
    } as DOMRect)
    render(<CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />)
    const menu = openMenu()
    expect(menu.style.top).not.toBe('')
    expect(menu.style.bottom).toBe('')
  })

  it('an ancestor scroll closes the menu; scrolling inside the menu does not', () => {
    render(<CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />)
    let menu = openMenu()
    // Scroll inside the menu's own option list: stays open.
    fireEvent.scroll(menu)
    expect(screen.getByTestId('custom-select-menu')).toBeInTheDocument()
    // Scroll anywhere else (a fixed menu cannot follow its trigger): closes.
    fireEvent.scroll(document.body)
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
    // Window resize also invalidates the computed position: closes.
    menu = openMenu()
    act(() => { window.dispatchEvent(new Event('resize')) })
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
  })

  it('Escape closes the menu', () => {
    render(<CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />)
    openMenu()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
  })
})

describe('CustomSelect — invalid prop (red boundary on selects)', () => {
  it('applies the thick red boundary when invalid, and not by default', () => {
    const { rerender } = render(
      <CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} />,
    )
    const trigger = screen.getByRole('button', { name: 'pick' })
    expect(trigger.className).not.toContain('border-red-500')
    rerender(<CustomSelect ariaLabel="pick" value="" onChange={() => {}} options={OPTIONS} invalid />)
    expect(trigger.className).toContain('border-red-500!')
    expect(trigger.className).toContain('ring-red-500/30')
  })

  it('shows the selected label with a checkmark, sublabels, and the placeholder when unset', () => {
    render(<CustomSelect ariaLabel="pick" value="a" onChange={() => {}} options={OPTIONS} placeholder="Choose..." />)
    expect(screen.getByRole('button', { name: /pick/ })).toHaveTextContent('Alpha')
    const menu = openMenu()
    expect(within(menu).getByText('first letter')).toBeInTheDocument()
    const alpha = within(menu).getByRole('button', { name: /Alpha/ })
    expect(alpha.className).toContain('text-emerald-300') // selected styling
  })
})

describe('CustomSelect — searchable', () => {
  const options = [
    { value: 'a', label: 'Alpha', sublabel: 'code review', group: 'First' },
    { value: 'b', label: 'Reviewer', sublabel: 'CODE analysis', group: 'Second' },
    { value: 'c', label: 'Review', sublabel: 'code work', group: 'First' },
    { value: 'd', label: 'Disabled review', sublabel: 'code', group: 'Second', disabled: true },
  ]
  const setup = () => {
    const onChange = vi.fn()
    render(<><button>Previous</button><CustomSelect searchable ariaLabel="Agent Profile" value="a" onChange={onChange} options={options} /><button>Next</button></>)
    const trigger = screen.getByRole('button', { name: 'Agent Profile' })
    fireEvent.click(trigger)
    return { trigger, input: screen.getByRole('combobox', { name: 'Agent Profile' }), onChange }
  }
  it('filters all terms in either order across name and description, preserving group order', () => {
    const { input } = setup()
    expect(input).toHaveFocus()
    expect(input).toHaveAttribute('aria-controls', screen.getByRole('listbox').id)
    fireEvent.change(input, { target: { value: '  REVIEW   code  ' } })
    expect(screen.getAllByRole('option')).toHaveLength(4)
    fireEvent.change(input, { target: { value: 'code review work' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(screen.queryByText('Second')).not.toBeInTheDocument()
    fireEvent.change(input, { target: { value: 'code' } })
    const menu = screen.getByTestId('custom-select-menu')
    expect(menu.textContent?.indexOf('First')).toBeLessThan(menu.textContent!.indexOf('Second'))
    fireEvent.change(input, { target: { value: 'no match' } })
    expect(screen.getByRole('status')).toHaveTextContent('No options match')
    fireEvent.change(input, { target: { value: '   ' } })
    expect(screen.getAllByRole('option')).toHaveLength(4)
  })
  it('navigates enabled options, selects with Enter and restores focus and empty query', () => {
    const { input, trigger, onChange } = setup()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: /Alpha, code review/ }).id)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: /Reviewer, CODE analysis/ }).id)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('b')
    expect(trigger).toHaveFocus()
    fireEvent.click(trigger)
    expect(screen.getByRole('combobox')).toHaveValue('')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    expect(trigger).toHaveFocus()
  })
  it('does not select disabled or hidden options and allows Tab in both directions', () => {
    const { input, trigger, onChange } = setup()
    fireEvent.click(screen.getByRole('option', { name: /Disabled/ }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.change(input, { target: { value: 'nothing' } })
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Tab' })
    expect(screen.getByRole('button', { name: 'Next' })).toHaveFocus()
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Tab', shiftKey: true })
    expect(screen.getByRole('button', { name: 'Previous' })).toHaveFocus()
  })
  it('leaves IME composition keys alone and keeps internal scroll open', () => {
    const { input, onChange } = setup()
    fireEvent.scroll(screen.getByTestId('custom-select-menu'))
    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: 'ArrowDown', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(onChange).not.toHaveBeenCalled()
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.compositionEnd(input)
    expect(screen.getByRole('combobox')).toHaveFocus()
  })
})

describe('CustomSelect — review regressions', () => {
  const setup = (value = 'b', options = OPTIONS) => {
    const onChange = vi.fn()
    const view = render(<CustomSelect searchable ariaLabel="pick" value={value} options={options} onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: 'pick' })
    fireEvent.click(trigger)
    return { ...view, trigger, input: screen.getByRole('combobox'), onChange }
  }

  it.each([true, false])('closes on the complete trigger mouse sequence (searchable=%s)', searchable => {
    const onChange = vi.fn()
    render(<CustomSelect searchable={searchable} ariaLabel="pick" value="b" options={OPTIONS} onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: 'pick' })
    fireEvent.click(trigger)
    const input = screen.queryByRole('combobox')
    if (input) fireEvent.change(input, { target: { value: 'Beta' } })
    fireEvent.mouseDown(trigger)
    if (input) fireEvent.blur(input, { relatedTarget: trigger })
    fireEvent.click(trigger)
    expect(screen.queryByTestId('custom-select-menu')).not.toBeInTheDocument()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(trigger)
    if (searchable) expect(screen.getByRole('combobox')).toHaveValue('')
    else expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it.each(['external', 'null', 'portal'])('handles %s blur destinations', destination => {
    const { input } = setup()
    const target = destination === 'portal' ? screen.getByRole('option', { name: 'Beta' }) : destination === 'external' ? document.body : null
    fireEvent.blur(input, { relatedTarget: target })
    expect(!!screen.queryByTestId('custom-select-menu')).toBe(destination === 'portal')
  })

  it('keeps input focus on noninteractive menu surfaces while permitting input mouse editing', () => {
    const { input, onChange } = setup('b', OPTIONS.map(o => ({ ...o, group: 'Letters' })))
    const menu = screen.getByTestId('custom-select-menu')
    for (const surface of [menu, screen.getByText('Letters')]) {
      expect(fireEvent.mouseDown(surface)).toBe(false)
      expect(input).toHaveFocus()
    }
    expect(fireEvent.mouseDown(input)).toBe(true)
    fireEvent.change(input, { target: { value: 'missing' } })
    expect(fireEvent.mouseDown(screen.getByRole('status'))).toBe(false)
    expect(input).toHaveFocus()
    expect(input).toHaveValue('missing')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.click(screen.getByRole('option', { name: 'Gamma' }))
    expect(onChange).not.toHaveBeenCalled()
    const beta = screen.getByRole('option', { name: 'Beta' })
    expect(fireEvent.mouseDown(beta)).toBe(false)
    fireEvent.click(beta)
    expect(onChange).toHaveBeenCalledExactlyOnceWith('b')
    expect(screen.getByRole('button', { name: 'pick' })).toHaveFocus()
  })

  it.each(['ArrowDown', 'ArrowUp'])('starts %s at the confirmed selection then moves to its neighbour', key => {
    const options = [...OPTIONS, { value: 'd', label: 'Delta' }]
    const { input, onChange } = setup('b', options)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key })
    const beta = screen.getByRole('option', { name: 'Beta' })
    expect(input).toHaveAttribute('aria-activedescendant', beta.id)
    expect(beta).toHaveClass('ring-2', 'text-emerald-300')
    expect(beta.querySelector('svg')).not.toBeNull()
    fireEvent.keyDown(input, { key })
    const next = screen.getByRole('option', { name: key === 'ArrowDown' ? 'Delta' : /Alpha/ })
    expect(input).toHaveAttribute('aria-activedescendant', next.id)
    expect(next).toHaveClass('ring-2', 'bg-gray-800')
    expect(beta).not.toHaveClass('ring-2')
    expect(screen.getAllByRole('option', { selected: true })).toEqual([beta])
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledExactlyOnceWith(key === 'ArrowDown' ? 'd' : 'a')
  })

  it.each(['', 'missing', 'c'])('falls back to rendered enabled extremes for invalid selection %s and clamps', value => {
    const { input } = setup(value)
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Beta' }).id)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Beta' }).id)
    fireEvent.change(input, { target: { value: ' ' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    const alpha = screen.getByRole('option', { name: /Alpha/ })
    expect(input).toHaveAttribute('aria-activedescendant', alpha.id)
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(input).toHaveAttribute('aria-activedescendant', alpha.id)
  })

  it('uses filtered extremes and rendered group order, then restores the selection rule after clearing', () => {
    const options = [
      { value: 'a', label: 'Match Alpha', group: 'First' },
      { value: 'b', label: 'Match Beta', group: 'Second' },
      { value: 'c', label: 'Match Charlie', group: 'First' },
    ]
    const { input } = setup('b', options)
    fireEvent.change(input, { target: { value: 'match' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Match Alpha' }).id)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Match Charlie' }).id)
    fireEvent.change(input, { target: { value: 'MATCH' } })
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Match Beta' }).id)
    fireEvent.change(input, { target: { value: '  ' } })
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Match Beta' }).id)
  })

  it('drops stale active references when options change and leaves empty/disabled lists inactive', () => {
    const { input, rerender, onChange } = setup('b')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    rerender(<CustomSelect searchable ariaLabel="pick" value="b" options={[OPTIONS[0]]} onChange={onChange} />)
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', screen.getByRole('option').id)
    for (const options of [[OPTIONS[2]], []]) {
      rerender(<CustomSelect searchable ariaLabel="pick" value="b" options={options} onChange={onChange} />)
      fireEvent.keyDown(input, { key: 'ArrowUp' })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(input).not.toHaveAttribute('aria-activedescendant')
      expect(onChange).not.toHaveBeenCalled()
    }
    expect(screen.getByText('No options available')).toBeInTheDocument()
    expect(fireEvent.mouseDown(screen.getByText('No options available'))).toBe(false)
  })

  it('uses generic search copy and accepts caller overrides without changing the trigger placeholder', () => {
    const { input, rerender, onChange } = setup('')
    expect(input).toHaveAttribute('placeholder', 'Search options...')
    fireEvent.change(input, { target: { value: 'missing' } })
    expect(screen.getByRole('status')).toHaveTextContent('No options match')
    rerender(<CustomSelect searchable ariaLabel="pick" value="" options={OPTIONS} onChange={onChange} placeholder="Pick letter" searchPlaceholder="Find letters" noResultsMessage="No letters" />)
    expect(input).toHaveAttribute('placeholder', 'Find letters')
    expect(screen.getByRole('status')).toHaveTextContent('No letters')
    expect(screen.getByRole('button', { name: 'pick' })).toHaveTextContent('Pick letter')
  })

  it('leaves Home and End to input editing', () => {
    const { input } = setup()
    for (const key of ['Home', 'End']) {
      expect(fireEvent.keyDown(input, { key })).toBe(true)
      expect(input).not.toHaveAttribute('aria-activedescendant')
    }
  })
})
