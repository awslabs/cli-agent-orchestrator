import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { ValidationFindings, OMISSION_MESSAGE } from '../components/ValidationFindings'
import { ProfileEditorModal } from '../components/ProfileEditorModal'
import { ProfileValidationMessage } from '../api'

const okJson = (data: unknown) => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  json: () => Promise.resolve(data),
  text: () => Promise.resolve(JSON.stringify(data)),
})

const errJson = (status: number, detail: unknown) => ({
  ok: false,
  status,
  statusText: 'Error',
  json: () => Promise.resolve({ detail }),
  text: () => Promise.resolve(JSON.stringify({ detail })),
})

const SOURCE = `---\nname: local-agent\n---\n\n# Body.\n`

/** Build a backend-shaped truncated payload: 99 findings + trailing marker = 100. */
function truncatedPayload(markerSeverity: 'error' | 'warning'): ProfileValidationMessage[] {
  const findings: ProfileValidationMessage[] = Array.from({ length: 99 }, (_, i) => ({
    severity: 'error' as const,
    message: `finding ${i}`,
    path: `frontmatter.field${i}`,
  }))
  findings.push({ severity: markerSeverity, message: OMISSION_MESSAGE })
  return findings
}

afterEach(() => vi.restoreAllMocks())

describe('ValidationFindings — truncation contract rendering (stage 5)', () => {
  it('renders all findings and exactly one omission marker, last', () => {
    render(<ValidationFindings findings={truncatedPayload('error')} />)
    const list = screen.getByTestId('validation-findings')
    // 99 regular rows + 1 marker row = the backend's 100-finding ceiling
    expect(within(list).getAllByRole('listitem')).toHaveLength(100)
    const markers = within(list).getAllByTestId('omission-marker')
    expect(markers).toHaveLength(1)
    const items = within(list).getAllByRole('listitem')
    expect(items[items.length - 1]).toHaveTextContent(OMISSION_MESSAGE)
    expect(within(list).getByText('list truncated')).toBeInTheDocument()
  })

  it('an error-severity marker is flagged as hiding errors', () => {
    render(<ValidationFindings findings={truncatedPayload('error')} />)
    expect(screen.getByTestId('omission-marker')).toHaveTextContent(/errors among omitted/i)
  })

  it('a warning-severity marker is NOT flagged as hiding errors', () => {
    render(<ValidationFindings findings={truncatedPayload('warning')} />)
    const marker = screen.getByTestId('omission-marker')
    expect(marker).toHaveTextContent(OMISSION_MESSAGE)
    expect(marker).not.toHaveTextContent(/errors among omitted/i)
  })

  it('the marker message mid-list is treated as an ordinary finding, not a marker', () => {
    // Contract: the marker appears exactly once, LAST. A same-text message in
    // any other position is user/producer content and must render normally.
    const findings: ProfileValidationMessage[] = [
      { severity: 'warning', message: OMISSION_MESSAGE },
      { severity: 'error', message: 'real error', path: 'x' },
    ]
    render(<ValidationFindings findings={findings} />)
    expect(screen.queryByTestId('omission-marker')).not.toBeInTheDocument()
    expect(screen.getByText('1 error')).toBeInTheDocument()
    expect(screen.getByText('1 warning')).toBeInTheDocument()
  })

  it('counts exclude the marker and split by severity', () => {
    const findings: ProfileValidationMessage[] = [
      { severity: 'error', message: 'e1' },
      { severity: 'warning', message: 'w1' },
      { severity: 'warning', message: 'w2' },
      { severity: 'warning', message: OMISSION_MESSAGE },
    ]
    render(<ValidationFindings findings={findings} />)
    expect(screen.getByText('1 error')).toBeInTheDocument()
    expect(screen.getByText('2 warnings')).toBeInTheDocument()
  })

  it('renders nothing for an empty findings list', () => {
    render(<ValidationFindings findings={[]} />)
    expect(screen.queryByTestId('validation-findings')).not.toBeInTheDocument()
  })
})

describe('editor save — errors block, warnings allow (stage 5)', () => {
  function editorFetch(validateResponse: any) {
    return vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return validateResponse(url, opts)
      if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
      if (opts?.method === 'PUT') return okJson({ name: 'local-agent', warnings: [] })
      return okJson([])
    })
  }

  async function openEditorAndSave(mock: ReturnType<typeof vi.fn>) {
    render(<ProfileEditorModal open={true} mode="edit" name="local-agent" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    return mock
  }

  it('error findings block the save: validate fires, PUT does not', async () => {
    const mock = editorFetch(() =>
      okJson({ valid: false, messages: [{ severity: 'error', message: 'name is required', path: 'frontmatter.name' }] }))
    vi.stubGlobal('fetch', mock)
    await openEditorAndSave(mock)

    expect(mock.mock.calls.filter(([u]) => String(u).includes('/validate'))).toHaveLength(1)
    expect(mock.mock.calls.filter(([, o]) => o?.method === 'PUT')).toHaveLength(0)
    expect(screen.getByRole('alert')).toHaveTextContent(/validation failed/i)
    const list = screen.getByTestId('validation-findings')
    expect(within(list).getByText('name is required')).toBeInTheDocument()
    expect(within(list).getByText('frontmatter.name')).toBeInTheDocument()
  })

  it('warning-only findings do not block: the PUT proceeds', async () => {
    const mock = editorFetch(() =>
      okJson({ valid: true, messages: [{ severity: 'warning', message: 'description is recommended' }] }))
    vi.stubGlobal('fetch', mock)
    await openEditorAndSave(mock)

    expect(mock.mock.calls.filter(([u]) => String(u).includes('/validate'))).toHaveLength(1)
    expect(mock.mock.calls.filter(([, o]) => o?.method === 'PUT')).toHaveLength(1)
  })

  it('a truncated error payload blocks the save and renders the marker', async () => {
    const mock = editorFetch(() => okJson({ valid: false, messages: truncatedPayload('error') }))
    vi.stubGlobal('fetch', mock)
    await openEditorAndSave(mock)

    expect(mock.mock.calls.filter(([, o]) => o?.method === 'PUT')).toHaveLength(0)
    expect(screen.getByTestId('omission-marker')).toHaveTextContent(OMISSION_MESSAGE)
    expect(within(screen.getByTestId('validation-findings')).getAllByRole('listitem')).toHaveLength(100)
  })

  it('an unparseable document (validate 400) blocks the save with the server detail', async () => {
    const mock = editorFetch(() => errJson(400, 'Profile document is not valid frontmatter.'))
    vi.stubGlobal('fetch', mock)
    await openEditorAndSave(mock)

    expect(mock.mock.calls.filter(([, o]) => o?.method === 'PUT')).toHaveLength(0)
    expect(screen.getByRole('alert')).toHaveTextContent(/not valid frontmatter/)
  })

  it('validates the exact document being persisted for a clone (rewritten name)', async () => {
    const mock = vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/source')) return okJson({ name: 'local-agent', content: SOURCE })
      if (opts?.method === 'POST' && String(url).endsWith('/agents/profiles')) {
        return okJson({ name: JSON.parse(opts.body).name, warnings: [] })
      }
      return okJson([])
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfileEditorModal open={true} mode="clone" name="local-agent" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByRole('textbox', { name: /profile source/i })
    fireEvent.click(screen.getByRole('button', { name: /create copy/i }))
    await act(async () => {})

    const validate = mock.mock.calls.find(([u]) => String(u).includes('/validate'))
    expect(validate).toBeTruthy()
    const validatedContent = JSON.parse(validate![1].body).content
    // The clone's default name is local-agent-copy; validation must see the
    // rewritten document, not the original source.
    expect(validatedContent).toContain('name: "local-agent-copy"')
    const post = mock.mock.calls.find(([u, o]) => o?.method === 'POST' && String(u).endsWith('/agents/profiles'))
    expect(JSON.parse(post![1].body).content).toBe(validatedContent)
  })
})
