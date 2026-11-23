import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('closes on Escape and locks body scrolling while open', () => {
    const { onClose, unmount, returnButton } = renderViewer()
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    expect(document.body.style.overflow).toBe('')
    expect(returnButton).toHaveFocus()
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

  it('preserves source exactly when token rows are joined with newlines', () => {
    const source = 'def example():\n    return """one\ntwo"""\n'
    const lines = tokensByLine(tokenizePython(source))
    expect(lines.map(line => line.map(token => token.text).join('')).join('\n')).toBe(source)
  })

  it('traps Tab and Shift+Tab within the dialog', () => {
    renderViewer()
    const first = screen.getByRole('button', { name: 'Copy content hash' })
    const last = screen.getByRole('button', { name: 'Wrap lines' })
    last.focus()
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(first).toHaveFocus()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
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
})
