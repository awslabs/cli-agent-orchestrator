import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { api, WorkflowSpec } from '../api'
import { WorkflowLauncher } from '../components/workflow/WorkflowLauncher'
import { WorkflowSpecViewer } from '../components/workflow/WorkflowSpecViewer'

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

describe('WorkflowSpecViewer', () => {
  beforeEach(() => {
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false })
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: null })
    HTMLElement.prototype.scrollTo = vi.fn()
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
    expect(screen.getByLabelText('Workflow spec source').textContent).toBe(PYTHON_SOURCE)
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

  it('scrolls to and marks a finding line', () => {
    renderViewer()
    fireEvent.click(screen.getByRole('button', { name: /line 2.*demo-rule/i }))
    expect(HTMLElement.prototype.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 0 }))
    expect(screen.getByLabelText('Marked line 2')).toBeInTheDocument()
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
    const python = screen.getByLabelText('Workflow spec source')
    expect(python.querySelector('.text-fuchsia-300')).toHaveTextContent('def')
    expect(python.querySelector('.text-blue-300')).toHaveTextContent('greet')
    unmount()

    const jsonSpec = { name: 'yaml_flow', path: '/wf/yaml_flow.yaml', source: { unsafe: '<img src=x onerror=alert(1)>' } } as unknown as WorkflowSpec
    renderViewer(jsonSpec)
    const fallback = screen.getByLabelText('Workflow spec source')
    expect(fallback.querySelector('span[class]')).toBeNull()
    expect(fallback.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(fallback.querySelector('img')).toBeNull()
  })
})
