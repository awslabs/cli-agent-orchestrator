import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { api, WorkflowSpec } from '../api'
import { WorkflowLauncher } from '../components/workflow/WorkflowLauncher'
import { tokensByLine, WorkflowSpecViewer } from '../components/workflow/WorkflowSpecViewer'
import { tokenizePython } from '../components/workflow/pythonHighlight'

const DEF = {
  name: 'review_flow', source_path: '/wf/review_flow.py', mode: 'script',
  step_count: null, description: '', indexed_at: '',
}
const PYTHON_SOURCE = 'def greet(name):\n    print(f"hello {name}")\n'
const SPEC: WorkflowSpec = {
  name: 'review_flow',
  path: '/wf/review_flow.py',
  content_hash: '1234567890abcdef',
  source: PYTHON_SOURCE,
  findings: [{ line: 2, severity: 'warning', rule_id: 'demo-rule', message: 'Check this call' }],
}

function renderViewer(spec: WorkflowSpec = SPEC, extra: Partial<React.ComponentProps<typeof WorkflowSpecViewer>> = {}) {
  const returnButton = document.createElement('button')
  document.body.appendChild(returnButton)
  const returnFocusRef = createRef<HTMLButtonElement>()
  returnFocusRef.current = returnButton
  const onClose = vi.fn()
  const result = render(
    <WorkflowSpecViewer
      open
      workflowName="review_flow"
      spec={spec}
      loading={false}
      error={null}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      {...extra}
    />,
  )
  return { ...result, onClose, returnButton }
}

const renderedSource = () =>
  screen.getAllByTestId(/^spec-line-code-/).map(line => line.textContent ?? '').join('\n')

describe('WorkflowSpecViewer', () => {
  beforeEach(() => {
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false })
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: null })
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    HTMLElement.prototype.scrollIntoView = vi.fn()
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    document.body.replaceChildren()
  })

  it('opens from View spec with the exact fetched source, and Exit restores focus', async () => {
    localStorage.clear()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const view = await screen.findByRole('button', { name: 'View spec' })
    await waitFor(() => expect(api.getWorkflowSpec).toHaveBeenCalled())
    view.focus()
    fireEvent.click(view)

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(renderedSource()).toBe(PYTHON_SOURCE)
    expect(screen.getByRole('button', { name: 'Exit' })).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(view).toHaveFocus()
  })

  it('opens the dialog immediately and shows loading while the fetch is in flight', async () => {
    localStorage.clear()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    let resolveSpec!: (spec: WorkflowSpec) => void
    const pendingSpec = new Promise<WorkflowSpec>(resolve => { resolveSpec = resolve })
    const getSpec = vi.spyOn(api, 'getWorkflowSpec')
      .mockResolvedValueOnce({ name: 'review_flow', inputs: {} })
      .mockReturnValueOnce(pendingSpec)
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const view = await screen.findByRole('button', { name: 'View spec' })
    await waitFor(() => expect(getSpec).toHaveBeenCalledTimes(1))
    fireEvent.click(view)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Loading workflow spec…')
    resolveSpec(SPEC)
    expect(await screen.findByTestId('spec-code-scroll')).toBeInTheDocument()
  })

  it('closes on Escape and locks body scrolling while open', () => {
    const { onClose, unmount, returnButton } = renderViewer()
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    expect(document.body.style.overflow).toBe('')
    expect(returnButton).toHaveFocus()
  })

  it('leaves native full screen with Escape without closing the dialog', () => {
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: document.body })
    const { onClose } = renderViewer()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('offers real full screen when enabled and hides the toggle otherwise', async () => {
    expect(renderViewer().queryByRole('button', { name: 'Full screen' })).toBeNull()
    cleanup()
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true })
    const requestFullscreen = vi.fn().mockResolvedValue(undefined)
    HTMLElement.prototype.requestFullscreen = requestFullscreen
    renderViewer()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    await waitFor(() => expect(requestFullscreen).toHaveBeenCalledOnce())
  })

  it('includes full-screen styling on the dialog', () => {
    const { getByRole } = renderViewer()
    expect(getByRole('dialog').className).toContain('[&:fullscreen]:h-screen')
    expect(getByRole('dialog').className).toContain('[&:fullscreen]:rounded-none')
  })

  it('resyncs the full screen label when reopened after Exit', async () => {
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true })
    let fullscreenElement: Element | null = null
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true,
      get: () => fullscreenElement,
    })
    HTMLElement.prototype.requestFullscreen = vi.fn().mockImplementation(async function (this: HTMLElement) {
      fullscreenElement = this
      document.dispatchEvent(new Event('fullscreenchange'))
    })
    const { rerender } = renderViewer()

    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(await screen.findByRole('button', { name: 'Leave full screen' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    fullscreenElement = null
    rerender(
      <WorkflowSpecViewer
        open={false}
        workflowName="review_flow"
        spec={SPEC}
        loading={false}
        error={null}
        onClose={vi.fn()}
        returnFocusRef={createRef<HTMLButtonElement>()}
      />,
    )
    rerender(
      <WorkflowSpecViewer
        open
        workflowName="review_flow"
        spec={SPEC}
        loading={false}
        error={null}
        onClose={vi.fn()}
        returnFocusRef={createRef<HTMLButtonElement>()}
      />,
    )
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
  })

  it.each([false, true])('keeps each number with its code and marks/scrolls a finding row (wrap=%s)', wrap => {
    renderViewer()
    if (wrap) fireEvent.click(screen.getByRole('button', { name: 'Wrap lines' }))
    for (const lineNumber of [1, 2, 3]) {
      const row = screen.getByTestId(`spec-line-${lineNumber}`)
      expect(row).toContainElement(screen.getByTestId(`spec-line-number-${lineNumber}`))
      expect(row).toContainElement(screen.getByTestId(`spec-line-code-${lineNumber}`))
    }
    fireEvent.click(screen.getByRole('button', { name: /line 2.*demo-rule/i }))
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({ block: 'center' })
    expect(screen.getByTestId('spec-line-2').className).toContain('bg-amber-500/10')
    expect(screen.getByTestId('spec-line-1').className).not.toContain('bg-amber-500/10')
  })

  it('clamps an out-of-range finding line to the available source', () => {
    renderViewer({ ...SPEC, findings: [{ line: 99, severity: 'warning', rule_id: 'demo-rule', message: 'Check this call' }] })
    fireEvent.click(screen.getByRole('button', { name: /line 99.*demo-rule/i }))
    expect(screen.getByTestId('spec-line-3').className).toContain('bg-amber-500/10')
  })

  it('applies wrapping classes when Wrap lines is enabled', () => {
    renderViewer()
    fireEvent.click(screen.getByRole('button', { name: 'Wrap lines' }))
    expect(screen.getByTestId('spec-line-code-1').className).toContain('whitespace-pre-wrap')
    expect(screen.getByTestId('spec-line-code-1').className).toContain('break-all')
  })

  it('preserves source exactly when token rows are joined with newlines', () => {
    const source = 'def example():\n    return """one\ntwo"""\n'
    const lines = tokensByLine(tokenizePython(source))
    expect(lines.map(line => line.map(token => token.text).join('')).join('\n')).toBe(source)
  })

  it('traps Tab and Shift+Tab within the dialog', () => {
    renderViewer()
    const first = screen.getByRole('button', { name: 'Copy content hash' })
    const last = screen.getByTestId('spec-code-scroll')
    last.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(first).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
  })

  it('skips a control that the browser does not render', () => {
    renderViewer()
    const copy = screen.getByRole('button', { name: 'Copy content hash' })
    Object.defineProperty(copy, 'checkVisibility', { configurable: true, value: () => false })
    const exit = screen.getByRole('button', { name: 'Exit' })
    const region = screen.getByTestId('spec-code-scroll')
    exit.focus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(region).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(exit).toHaveFocus()
  })

  it('tabs from Wrap lines to the labelled code scroll region', () => {
    renderViewer()
    const wrap = screen.getByRole('button', { name: 'Wrap lines' })
    const scroller = screen.getByTestId('spec-code-scroll')
    wrap.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(scroller).toHaveFocus()
    expect(scroller).toHaveAttribute('role', 'region')
    expect(scroller).toHaveAttribute('tabindex', '0')
    expect(scroller).toHaveAccessibleName('Workflow spec source')
    expect(scroller).toContainElement(screen.getByTestId('spec-line-1'))
  })

  it('shows copied only after clipboard writeText succeeds', async () => {
    renderViewer()
    const copy = screen.getByRole('button', { name: 'Copy content hash' })
    fireEvent.click(copy)
    expect(copy.querySelector('.text-emerald-400')).toBeNull()

    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce())
    expect(copy.querySelector('.text-emerald-400')).toBeNull()

    writeText.mockResolvedValue(undefined)
    fireEvent.click(copy)
    await waitFor(() => expect(copy.querySelector('.text-emerald-400')).not.toBeNull())
  })

  it('shows a fetch failure inside the already-open dialog', async () => {
    localStorage.clear()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    const getSpec = vi.spyOn(api, 'getWorkflowSpec')
      .mockResolvedValueOnce({ name: 'review_flow', inputs: {} })
      .mockRejectedValueOnce(new Error('network unavailable'))
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    await waitFor(() => expect(getSpec).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'View spec' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('network unavailable')
  })

  it('opens immediately for a second click while a previous request is loading', async () => {
    localStorage.clear()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    let resolveFirst!: (spec: WorkflowSpec) => void
    const first = new Promise<WorkflowSpec>(resolve => { resolveFirst = resolve })
    const getSpec = vi.spyOn(api, 'getWorkflowSpec')
      .mockResolvedValueOnce({ name: 'review_flow', inputs: {} })
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce(SPEC)
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const view = await screen.findByRole('button', { name: 'View spec' })
    await waitFor(() => expect(getSpec).toHaveBeenCalledTimes(1))
    fireEvent.click(view)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    fireEvent.click(view)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    resolveFirst({ ...SPEC, source: 'old source' })
    await waitFor(() => expect(renderedSource()).toBe(PYTHON_SOURCE))
  })

  it('keeps only the newest View spec response', async () => {
    localStorage.clear()
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    let resolveFirst!: (spec: WorkflowSpec) => void
    let resolveSecond!: (spec: WorkflowSpec) => void
    const first = new Promise<WorkflowSpec>(resolve => { resolveFirst = resolve })
    const second = new Promise<WorkflowSpec>(resolve => { resolveSecond = resolve })
    const getSpec = vi.spyOn(api, 'getWorkflowSpec')
      .mockResolvedValueOnce({ name: 'review_flow', inputs: {} })
      .mockReturnValueOnce(first)
      .mockReturnValueOnce(second)
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const view = await screen.findByRole('button', { name: 'View spec' })
    await waitFor(() => expect(getSpec).toHaveBeenCalledTimes(1))
    fireEvent.click(view)
    fireEvent.click(view)
    await act(async () => { resolveSecond({ ...SPEC, source: 'new source' }) })
    await waitFor(() => expect(renderedSource()).toBe('new source'))
    await act(async () => { resolveFirst({ ...SPEC, source: 'old source' }) })
    expect(renderedSource()).toBe('new source')
  })

  it('groups workflow findings under their accessible name', () => {
    renderViewer()
    expect(screen.getByRole('group', { name: 'Workflow findings' })).toBeInTheDocument()
  })

  it('colours Python spans, but leaves a JSON fallback plain', () => {
    const { unmount } = renderViewer()
    const python = screen.getByTestId('spec-code-scroll')
    expect(python.querySelector('.text-fuchsia-300')).toHaveTextContent('def')
    expect(python.querySelector('.text-blue-300')).toHaveTextContent('greet')
    unmount()

    const jsonSpec = { name: 'yaml_flow', path: '/wf/yaml_flow.yaml', source: { unsafe: '<img src=x onerror=alert(1)>' } } as unknown as WorkflowSpec
    renderViewer(jsonSpec)
    const fallback = screen.getByTestId('spec-code-scroll')
    expect(fallback.querySelector('[data-testid^="spec-line-code-"] > span[class]')).toBeNull()
    expect(renderedSource()).toContain('<img src=x onerror=alert(1)>')
    expect(fallback.querySelector('img')).toBeNull()
  })

  it('leaves a string-source YAML spec plain', () => {
    renderViewer({ ...SPEC, path: '/wf/yaml_flow.yaml', source: 'steps:\n  - run: echo hello\n' })
    const yaml = screen.getByTestId('spec-code-scroll')
    const tokens = yaml.querySelectorAll('[data-testid^="spec-line-code-"] > span')
    expect(tokens).toHaveLength(2)
    expect([...tokens].every(token => token.className === '')).toBe(true)
    expect(yaml.querySelector('.text-fuchsia-300')).toBeNull()
  })
})
