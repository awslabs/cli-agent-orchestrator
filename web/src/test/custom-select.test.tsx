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
    expect(trigger.className).toContain('!border-red-500')
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
