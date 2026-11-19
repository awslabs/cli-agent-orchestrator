// The SDK UI stub copies the host's DOM contract (Kiro Crew v0.12.2, website/src): the roles,
// attributes and variant classes the page and its tests rely on. Each block names the host source
// it mirrors, so the stub can be re-checked when minKiroCrewVersion moves.
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import * as ui from './sdk-stub/ui'

const { Badge, Btn, Card, CardTitle, EmptyState, ErrorNotice, Input, Modal, PageHeader, Select, Skeleton, Toggle, guideCaution } = ui

describe('Btn (components/ui.tsx:82-110)', () => {
  it.each([
    ['primary', { primary: true }, 'busy-cue-primary', 'busy-cue-danger'],
    ['danger', { danger: true }, 'busy-cue-danger', 'busy-cue-primary'],
    ['primary wins over danger', { primary: true, danger: true }, 'busy-cue-primary', 'busy-cue-danger'],
  ])('%s carries its busy-cue variant class', (_n, props, has, hasNot) => {
    render(<Btn {...props}>Go</Btn>)
    const b = screen.getByRole('button', { name: 'Go' })
    expect(b).toHaveClass('busy-cue', has)
    expect(b).not.toHaveClass(hasNot)
  })

  it('secondary (default) has only busy-cue, and keeps the caller class and attributes', () => {
    render(<Btn className="cs-btn" type="button" aria-expanded={false} {...guideCaution}>Ask</Btn>)
    const b = screen.getByRole('button', { name: 'Ask' })
    expect(b).toHaveClass('busy-cue', 'cs-btn')
    expect(b).not.toHaveClass('busy-cue-primary')
    expect(b).not.toHaveClass('busy-cue-danger')
    expect(b).toHaveAttribute('type', 'button')
    expect(b).toHaveAttribute('aria-expanded', 'false')
    expect(b).toHaveAttribute('data-guide-caution', '')
    expect(b).not.toHaveAttribute('aria-disabled')
    expect(b).not.toHaveAttribute('data-busy')
  })

  it('busy swallows the click, keeps focus, and is aria-disabled, not disabled', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Btn busy onClick={onClick}>Setting up...</Btn>)
    const b = screen.getByRole('button', { name: 'Setting up...' })
    await user.click(b)
    expect(onClick).not.toHaveBeenCalled()
    expect(b).toHaveFocus()
    expect(b).toBeEnabled()
    expect(b).toHaveAttribute('aria-disabled', 'true')
    expect(b).toHaveAttribute('aria-busy', 'true')
    expect(b).toHaveAttribute('data-busy', 'true')
  })

  it('disabled stays native and the click is not delivered', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<Btn disabled onClick={onClick}>Stop...</Btn>)
    await user.click(screen.getByRole('button', { name: 'Stop...' }))
    expect(screen.getByRole('button', { name: 'Stop...' })).toBeDisabled()
    expect(onClick).not.toHaveBeenCalled()
  })

  it('forwards its ref to the button', () => {
    const ref = createRef<HTMLButtonElement>()
    render(<Btn ref={ref}>Done</Btn>)
    expect(ref.current).toBe(screen.getByRole('button', { name: 'Done' }))
  })
})

describe('Badge (components/ui.tsx:218-233)', () => {
  it.each([
    ['ok', ['bg-ok-subtle', 'text-ok-text']],
    ['err', ['bg-danger-subtle', 'text-danger-text']],
    ['warn', ['bg-warn-subtle', 'text-warn-text']],
    ['muted', ['bg-[var(--bg-hover)]', 'text-[var(--muted)]']],
  ] as const)('%s is a span with the text-safe classes', (variant, classes) => {
    render(<Badge variant={variant}>LIVE</Badge>)
    const b = screen.getByText('LIVE')
    expect(b.tagName).toBe('SPAN')
    expect(b).toHaveClass(...classes)
  })

  it('renders hostile text as text', () => {
    const { container } = render(<Badge variant="err">{'<img src=x onerror=alert(1)>'}</Badge>)
    expect(container.querySelector('img')).toBeNull()
    expect(container).toHaveTextContent('<img src=x onerror=alert(1)>')
  })
})

describe('Card and CardTitle (components/ui.tsx:12-45)', () => {
  it('Card is a div with the host card classes and passes role, label and class', () => {
    render(<Card role="article" aria-label="session x, parked" className="cs-card cs-k-parked">body</Card>)
    const c = screen.getByRole('article', { name: 'session x, parked' })
    expect(c.tagName).toBe('DIV')
    expect(c).toHaveClass('card-glow', 'border', 'border-border', 'bg-card', 'cs-card', 'cs-k-parked')
  })

  it('CardTitle is an h3', () => {
    render(<CardTitle>Settings</CardTitle>)
    expect(screen.getByRole('heading', { level: 3, name: 'Settings' })).toBeInTheDocument()
  })
})

describe('Input (components/ui.tsx:196)', () => {
  it('forwards ref and every attribute, so labels and limits keep working', async () => {
    const user = userEvent.setup()
    const ref = createRef<HTMLInputElement>()
    render(
      <>
        <label htmlFor="cwd">Working directory (required)</label>
        <Input id="cwd" ref={ref} maxLength={4} aria-describedby="h" className="cs-input" />
        <small id="h">absolute path</small>
      </>,
    )
    const input = screen.getByLabelText('Working directory (required)')
    expect(ref.current).toBe(input)
    expect(input).toHaveClass('cs-input')
    expect(input).toHaveAccessibleDescription('absolute path')
    await user.type(input, '/work/area')
    expect(input).toHaveValue('/wor')
  })
})

describe('Toggle (components/ui.tsx:474-505)', () => {
  function Harness({ disabled }: { disabled?: boolean }) {
    const [on, setOn] = useState(false)
    return <Toggle checked={on} onChange={setOn} disabled={disabled} label="Remember closed helpers" describedBy="d" />
  }

  it('is a switch named by its label, toggled by click, Space and Enter', async () => {
    const user = userEvent.setup()
    render(<><Harness /><span id="d">kept on this computer</span></>)
    const sw = screen.getByRole('switch', { name: 'Remember closed helpers' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(sw).toHaveAttribute('tabindex', '0')
    expect(sw).toHaveAccessibleDescription('kept on this computer')
    await user.click(sw)
    expect(sw).toHaveAttribute('aria-checked', 'true')
    sw.focus()
    await user.keyboard(' ')
    expect(sw).toHaveAttribute('aria-checked', 'false')
    await user.keyboard('{Enter}')
    expect(sw).toHaveAttribute('aria-checked', 'true')
  })

  it('disabled is aria-disabled, out of the tab order, and does not change', async () => {
    const user = userEvent.setup()
    render(<Harness disabled />)
    const sw = screen.getByRole('switch')
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    expect(sw).toHaveAttribute('tabindex', '-1')
    await user.click(sw)
    expect(sw).toHaveAttribute('aria-checked', 'false')
  })
})

describe('EmptyState, Skeleton, PageHeader (components/ui.tsx:285,367,449)', () => {
  it('EmptyState has the host test ids and leaves out what is not given', () => {
    const { rerender } = render(<EmptyState icon={null} title="No CAO sessions" subtitle="Start one" action={<button>Start session</button>} />)
    expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('No CAO sessions')
    expect(screen.getByTestId('empty-state-subtitle')).toHaveTextContent('Start one')
    expect(within(screen.getByTestId('empty-state-action')).getByRole('button', { name: 'Start session' })).toBeInTheDocument()
    rerender(<EmptyState icon={null} title="t" testId="other" />)
    expect(screen.queryByTestId('empty-state')).toBeNull()
    expect(screen.getByTestId('other-title')).toHaveTextContent('t')
    expect(screen.queryByTestId('other-subtitle')).toBeNull()
    expect(screen.queryByTestId('other-action')).toBeNull()
  })

  it('Skeleton is the host placeholder box and keeps the caller class', () => {
    const { container } = render(<Skeleton className="cs-skel" />)
    const s = container.firstElementChild!
    expect(s).toHaveAttribute('data-slot', 'skeleton')
    expect(s).toHaveClass('bg-bg-hover', 'animate-pulse', 'cs-skel')
  })

  it('PageHeader has the host test ids and renders the actions', () => {
    render(<PageHeader title="CAO Shelf" subtitle="sessions" actions={<button>Start session</button>} />)
    const h = screen.getByTestId('page-header')
    expect(within(h).getByTestId('page-title')).toHaveTextContent('CAO Shelf')
    expect(within(h).getByTestId('page-subtitle')).toHaveTextContent('sessions')
    expect(within(h).getByRole('button', { name: 'Start session' })).toBeInTheDocument()
  })
})

describe('ErrorNotice (components/ErrorNotice.tsx, block variant)', () => {
  it('is an alert with the title in strong and the message as text, with id and test id', () => {
    render(<ErrorNotice id="cs-reason" testId="banner" title="CAO is not reachable." message="The snapshot is 3 min old." />)
    const a = screen.getByRole('alert')
    expect(a).toHaveAttribute('id', 'cs-reason')
    expect(a).toHaveAttribute('data-testid', 'banner')
    expect(within(a).getByText('CAO is not reachable.').tagName).toBe('STRONG')
    expect(a).toHaveTextContent('CAO is not reachable. The snapshot is 3 min old.')
  })

  it('announce={false} drops the alert role and keeps the text', () => {
    render(<ErrorNotice announce={false} message="Heads up" />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Heads up')).toBeInTheDocument()
  })

  it.each([[''], [null], [undefined]])('renders nothing for an empty message (%s)', (message) => {
    const { container } = render(<ErrorNotice title="Save failed" message={message} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('never turns a CAO message into markup or a link', () => {
    const { container } = render(<ErrorNotice message={'see http://127.0.0.1:1/ <img src=x onerror=alert(1)>'} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('<img src=x onerror=alert(1)>')
  })
})

describe('Modal (components/Modal.tsx:84-265, hooks/useDialogFocusTrap.ts)', () => {
  type Opts = { dismissDisabled?: boolean; guardAccidentalDismiss?: boolean; ariaLabel?: string }

  function Harness({ opts = {}, onClose: spy }: { opts?: Opts; onClose?: () => void }) {
    const [open, setOpen] = useState(false)
    return (
      <div data-testid="page">
        <button onClick={() => setOpen(true)}>Open it</button>
        <Modal
          open={open}
          onClose={() => {
            spy?.()
            setOpen(false)
          }}
          title="Stop session x?"
          footer={<><Btn>Cancel</Btn><Btn danger>Stop 1 session</Btn></>}
          {...opts}
        >
          <p>body text</p>
          <input aria-label="first field" />
        </Modal>
      </div>
    )
  }

  async function open(opts?: Opts, onClose?: () => void) {
    const user = userEvent.setup()
    render(<Harness opts={opts} onClose={onClose} />)
    const opener = screen.getByRole('button', { name: 'Open it' })
    await user.click(opener)
    return { user, opener, dlg: screen.getByRole('dialog') }
  }

  it('renders nothing while closed', () => {
    render(<Harness />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('portals to document.body, is modal, and is named by its title', async () => {
    const { dlg } = await open()
    expect(screen.getByTestId('page').contains(dlg)).toBe(false)
    expect(document.body.contains(dlg)).toBe(true)
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(screen.getByRole('dialog', { name: 'Stop session x?' })).toBe(dlg)
  })

  it('an explicit ariaLabel names the dialog instead', async () => {
    await open({ ariaLabel: 'Confirm stop' })
    expect(screen.getByRole('dialog', { name: 'Confirm stop' })).toBeInTheDocument()
  })

  it('has an X button named Close and the footer outside the body', async () => {
    const { dlg } = await open()
    expect(within(dlg).getAllByRole('button', { name: 'Close' })).toHaveLength(1)
    const bodyRegion = screen.getByText('body text').parentElement!
    expect(bodyRegion.contains(within(dlg).getByRole('button', { name: 'Cancel' }))).toBe(false)
    expect(dlg.contains(within(dlg).getByRole('button', { name: 'Cancel' }))).toBe(true)
  })

  it('focuses the first tabbable (the X) on open and restores the opener on close', async () => {
    const { user, opener, dlg } = await open()
    expect(within(dlg).getByRole('button', { name: 'Close' })).toHaveFocus()
    await user.click(within(dlg).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })

  it('a child asking for focus on mount loses to the X', () => {
    render(
      <Modal open onClose={() => {}} title="t">
        <input aria-label="wants focus" autoFocus />
      </Modal>,
    )
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
  })

  it('Tab wraps from the last control to the X and Shift+Tab from the X to the last', async () => {
    const { user, dlg } = await open()
    const x = within(dlg).getByRole('button', { name: 'Close' })
    const last = within(dlg).getByRole('button', { name: 'Stop 1 session' })
    last.focus()
    await user.tab()
    expect(x).toHaveFocus()
    await user.tab({ shift: true })
    expect(last).toHaveFocus()
  })

  it('Escape closes', async () => {
    const onClose = vi.fn()
    const { user, opener } = await open({}, onClose)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })

  it('a backdrop click closes', async () => {
    const onClose = vi.fn()
    const { user } = await open({}, onClose)
    await user.click(document.querySelector('[data-modal-backdrop]')!)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('guardAccidentalDismiss ignores Escape and backdrop, and the X still closes', async () => {
    const onClose = vi.fn()
    const { user, dlg } = await open({ guardAccidentalDismiss: true }, onClose)
    await user.keyboard('{Escape}')
    await user.click(document.querySelector('[data-modal-backdrop]')!)
    expect(onClose).not.toHaveBeenCalled()
    await user.click(within(dlg).getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('dismissDisabled refuses Escape, backdrop and X, and renders the X disabled', async () => {
    const onClose = vi.fn()
    const { user, dlg } = await open({ dismissDisabled: true }, onClose)
    const x = within(dlg).getByRole('button', { name: 'Close' })
    expect(x).toBeDisabled()
    await user.keyboard('{Escape}')
    await user.click(document.querySelector('[data-modal-backdrop]')!)
    await user.click(x)
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('an Escape a nested control already consumed does not close', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(
      <Modal open onClose={onClose} title="t">
        <input aria-label="eats escape" onKeyDown={(e) => e.key === 'Escape' && e.preventDefault()} />
      </Modal>,
    )
    screen.getByLabelText('eats escape').focus()
    await user.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('Select (kirocrew-ui/Select.tsx:57-102, touch path SimpleSelect.tsx:125)', () => {
  it('is a native select with the label, title, clear row and labelled options', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <Select
        aria-label="Profile"
        title="Agent profile"
        options={['developer', 'supervisor']}
        optionLabels={['developer: writes code']}
        value=""
        clearLabel="Loading profiles..."
        onChange={onChange}
      />,
    )
    const sel = screen.getByRole('combobox', { name: 'Profile' })
    expect(sel.tagName).toBe('SELECT')
    expect(sel).toHaveAttribute('title', 'Agent profile')
    const opts = within(sel).getAllByRole('option')
    expect(opts.map((o) => [(o as HTMLOptionElement).value, o.textContent])).toEqual([
      ['', 'Loading profiles...'],
      ['developer', 'developer: writes code'],
      ['supervisor', 'supervisor'],
    ])
    expect(sel).toHaveValue('')
    await user.selectOptions(sel, 'supervisor')
    expect(onChange).toHaveBeenCalledWith('supervisor')
  })

  it('disabled stays native', () => {
    render(<Select aria-label="Profile" options={[]} value="" clearLabel="No profiles found" disabled onChange={() => {}} />)
    expect(screen.getByRole('combobox', { name: 'Profile' })).toBeDisabled()
  })
})

describe('guideCaution (guide/trustRoot.ts:39)', () => {
  it('is the data-guide-caution marker', () => {
    expect(guideCaution).toEqual({ 'data-guide-caution': '' })
  })
})
