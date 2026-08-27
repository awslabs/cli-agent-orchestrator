import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { ProfileCreateModal, buildFrontmatter, rewriteFrontmatterName, extractFrontmatterName, PREVIEW_DEBOUNCE_MS } from '../components/ProfileCreateModal'

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

const TEMPLATES = [
  { name: 'aws/sqs-monitor', description: 'Poll an SQS queue' },
  { name: 'aws/stepfunction', description: 'Trigger Step Functions' },
]

const TEMPLATE_SCHEMA = {
  type: 'object',
  properties: {
    queue_url: { type: 'string', description: 'Full SQS queue URL' },
    poll_interval_seconds: { type: 'integer', minimum: 1, default: 10 },
  },
  required: ['queue_url'],
  additionalProperties: false,
}

const PROFILE_SCHEMA = {
  type: 'object',
  required: ['name'],
  properties: {
    name: { type: 'string' },
    description: { type: 'string' },
    provider: { type: 'string', description: 'Provider override' },
    model: { type: 'string' },
    tags: { type: 'array', items: { type: 'string' } },
    capabilities: { type: 'array', items: { type: 'string' } },
    engine: { type: 'string', enum: ['v2', 'kas'] },
    role: { type: 'string', description: 'Agent role.' },
    mcpServers: { type: 'object' },
    useLegacyMcpJson: { type: 'boolean' },
    provider_init_timeout: { type: 'integer', minimum: 1 },
  },
  additionalProperties: false,
}

const RENDERED = `---\nname: sqs-monitor-agent\ndescription: Poll an SQS queue\n---\n\n# You watch queues.\n`

function routedFetch(overrides: Record<string, (url: string, opts?: any) => any> = {}) {
  return vi.fn(async (url: string, opts?: any) => {
    for (const [needle, handler] of Object.entries(overrides)) {
      if (url.includes(needle)) return handler(url, opts)
    }
    if (url.includes('/agents/providers')) return okJson([
      { name: 'kiro_cli', binary: 'kiro-cli', installed: true },
      { name: 'claude_code', binary: 'claude', installed: false },
    ])
    if (url.includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
    if (url.includes('/agents/profiles/templates/aws/')) return okJson(TEMPLATE_SCHEMA)
    if (url.includes('/agents/profiles/templates/preview')) return okJson({ template: 'aws/sqs-monitor', content: RENDERED })
    if (url.includes('/agents/profiles/templates')) return okJson(TEMPLATES)
    if (url.includes('/agents/profiles/schema')) return okJson(PROFILE_SCHEMA)
    if (url.endsWith('/agents/profiles') && opts?.method === 'POST') return okJson({ name: JSON.parse(opts.body).name, warnings: [] })
    return okJson([])
  })
}

afterEach(() => vi.restoreAllMocks())

/** CustomSelect interaction: click the labelled trigger, then click an option
 *  in the portaled menu (rendered at document.body, found by testid). */
async function pickOption(triggerName: string | RegExp, optionText: string | RegExp) {
  fireEvent.click(screen.getByRole('button', { name: triggerName }))
  const menu = screen.getByTestId('custom-select-menu')
  const opt = within(menu).getAllByRole('button').find(b =>
    typeof optionText === 'string' ? b.textContent?.includes(optionText) : optionText.test(b.textContent ?? ''))
  if (!opt) throw new Error(`option ${optionText} not found`)
  fireEvent.click(opt)
}

describe('frontmatter helpers', () => {
  it('buildFrontmatter emits JSON-valued YAML and skips empty values', () => {
    const fm = buildFrontmatter({ name: 'x', description: 'd', tags: ['a', 'b'], mcpServers: { s: { command: 'uvx' } }, skip: undefined, blank: '' })
    expect(fm).toBe('---\nname: "x"\ndescription: "d"\ntags: ["a","b"]\nmcpServers: {"s":{"command":"uvx"}}\n---\n')
  })

  it('rewriteFrontmatterName replaces only the frontmatter name line', () => {
    const out = rewriteFrontmatterName(RENDERED, 'my-agent')
    expect(out).toContain('name: "my-agent"')
    expect(out).not.toContain('sqs-monitor-agent')
    expect(out).toContain('# You watch queues.')
  })

  it('extractFrontmatterName reads the rendered default', () => {
    expect(extractFrontmatterName(RENDERED)).toBe('sqs-monitor-agent')
  })
})

describe('frontmatter helpers — adversarial probe regressions', () => {
  const DOC = "---\nname: old\ndescription: costs $& fees\n---\n\nbody"

  it('names containing String.replace $-patterns are inserted verbatim', () => {
    // $& / $' in a replacement STRING expand to match text; the helpers must
    // use replacement functions so the typed name survives byte-for-byte.
    for (const name of ["$&", "a$'b", 'x$1y']) {
      const out = rewriteFrontmatterName(DOC, name)
      expect(out.split('\n')[1]).toBe(`name: ${JSON.stringify(name)}`)
      expect(out).toContain('costs $& fees') // rest of the document untouched
      expect(out).toContain('body')
    }
  })

  it('a document whose frontmatter contains $& is not mangled by a rename', () => {
    const out = rewriteFrontmatterName(DOC, 'newname')
    expect(out.split('\n')[1]).toBe('name: "newname"')
    expect(out).toContain('description: costs $& fees')
    // The frontmatter block appears exactly once — no nested duplication
    expect(out.match(/^---$/gm)).toHaveLength(2)
  })

  it('CRLF documents are rewritten, not silently no-opped', () => {
    const crlf = "---\r\nname: old\r\ndescription: d\r\n---\r\nbody"
    const out = rewriteFrontmatterName(crlf, 'newname')
    expect(out).not.toBe(crlf) // the old bug: regex missed \r\n and returned input
    expect(out).toContain('name: "newname"')
    expect(out).toContain('body')
  })

  it('extractFrontmatterName reads CRLF frontmatter without trailing \\r', () => {
    expect(extractFrontmatterName("---\r\nname: crlf-agent\r\n---\r\nbody")).toBe('crlf-agent')
  })
})

describe('ProfileCreateModal — template flow (stage 3)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function openWithTemplate(mock: ReturnType<typeof vi.fn>) {
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    // listProfileTemplates + getProfileSchema fire on open
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    // schema-driven fields render from the template schema
    expect(screen.getByRole('textbox', { name: 'queue_url' })).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'poll_interval_seconds' })).toBeInTheDocument()
    // preview fires after the debounce
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
  }

  it('renders schema fields, previews after debounce, and defaults the name from frontmatter', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    await openWithTemplate(mock)
    expect(screen.getByTestId('template-preview')).toHaveTextContent('# You watch queues.')
    // Name field followed the rendered frontmatter default
    expect(screen.getByRole('textbox', { name: 'Profile name' })).toHaveValue('sqs-monitor-agent')
    const previews = mock.mock.calls.filter(([u]) => String(u).includes('/preview'))
    expect(previews).toHaveLength(1)
  })

  it('coalesces config edits into one preview call and POSTs the name-rewritten render', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    await openWithTemplate(mock)

    // Burst of config edits inside the debounce window -> one more preview
    fireEvent.change(screen.getByRole('textbox', { name: 'queue_url' }), { target: { value: 'https://sqs.q1' } })
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS - 50))
    fireEvent.change(screen.getByRole('textbox', { name: 'queue_url' }), { target: { value: 'https://sqs.q12' } })
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    expect(mock.mock.calls.filter(([u]) => String(u).includes('/preview'))).toHaveLength(2)

    // Override the name, then create
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'my-poller' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    const post = mock.mock.calls.find(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(post![1].body)
    expect(body.name).toBe('my-poller')
    expect(body.content).toContain('name: "my-poller"')
    expect(body.content).toContain('# You watch queues.')
  })

  it('renders a multi-line preview error with line breaks preserved', async () => {
    // Real backend format (agent_scaffold.render_template): header line then
    // one '  - <error>' line per missing property, joined with \n. HTML
    // collapses newlines, so the error box must use whitespace-pre-line.
    const detail = "Config validation failed for 'aws/sqs-dlq-check':\n  - (root): 'profile' is a required property\n  - (root): 'region' is a required property"
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/templates/aws/')) return okJson(TEMPLATE_SCHEMA)
      if (url.includes('/templates/preview')) return errJson(400, detail)
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))

    const status = screen.getByRole('status')
    // The full text is present AND the newline-preserving style is applied,
    // so each '- ...' error renders on its own line.
    expect(status).toHaveTextContent(/required property/)
    const span = status.querySelector('span.whitespace-pre-line') as HTMLElement
    expect(span).not.toBeNull()
    expect(span.textContent).toContain("\n  - (root): 'profile' is a required property")
    expect(span.textContent).toContain("\n  - (root): 'region' is a required property")
  })

  it('the displayed preview reflects the typed profile name, not the template default', async () => {
    // The POST rewrites the frontmatter name to the chosen one; the preview
    // must show that same document, or the user reasonably concludes their
    // name will be ignored.
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    await openWithTemplate(mock)
    // Before overriding, the name field auto-follows the template default,
    // so the displayed (rewritten) preview carries that default name.
    expect(screen.getByTestId('template-preview').textContent).toContain('sqs-monitor-agent')

    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'my-poller' } })
    const previewText = screen.getByTestId('template-preview').textContent as string
    expect(previewText).toContain('name: "my-poller"')
    expect(previewText).not.toContain('sqs-monitor-agent')
  })

  it('a failing template-schema fetch surfaces an error instead of crashing', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/templates/aws/')) return errJson(500, 'template store unreadable')
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    expect(screen.getByRole('status')).toHaveTextContent('template store unreadable')
    // Create stays disabled: no schema, no preview
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
  })

  it('outlines the template config fields named by the preview error', async () => {
    // The reported bug: aws/dynamodb-delete with required 'region' left empty
    // showed the preview error text but no red boundary on the region field.
    // Both backend error forms must map to a field: '(root): 'X' is a
    // required property' (missing) and 'X: <message>' (bad value).
    const detail = "Config validation failed for 'aws/sqs-monitor':\n  - (root): 'queue_url' is a required property\n  - poll_interval_seconds: 0 is less than the minimum of 1"
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/templates/aws/')) return okJson(TEMPLATE_SCHEMA)
      if (url.includes('/templates/preview')) return errJson(400, detail)
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))

    // Missing required field: thick red boundary with NO user input given
    expect(screen.getByRole('textbox', { name: 'queue_url' }).className).toContain('!border-red-500')
    // Bad-value field: also outlined
    expect(screen.getByRole('spinbutton', { name: 'poll_interval_seconds' }).className).toContain('!border-red-500')
  })

  it('create is disabled without a preview, and ENABLES once preview + name exist', async () => {
    // Positive case included so 'always disabled' cannot pass this test.
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()

    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    // Preview rendered + name auto-followed from frontmatter -> enabled
    expect(screen.getByRole('button', { name: /create profile/i })).toBeEnabled()

    // Clearing the name disables it again
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: '' } })
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
  })

  it('surfaces a 409 conflict from the create POST', async () => {
    // Custom routing: the create POST 409s while template sub-paths resolve.
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/templates/aws/')) return okJson(TEMPLATE_SCHEMA)
      if (url.includes('/templates/preview')) return okJson({ template: 'aws/sqs-monitor', content: RENDERED })
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') return errJson(409, "Profile 'sqs-monitor-agent' already exists in the local store.")
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent(/already exists/)
  })
})

describe('ProfileCreateModal — from-scratch flow (stage 3)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function openScratch() {
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
  }

  it('generates the form from the server schema: primary fields visible, advanced behind expander', async () => {
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    // Primary fields from PROFILE_SCHEMA
    expect(screen.getByRole('textbox', { name: 'description' })).toBeInTheDocument()
    // provider is a styled CustomSelect fed by the provider registry
    expect(screen.getByRole('button', { name: 'provider' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'tags' })).toBeInTheDocument()
    // Advanced fields hidden until expanded
    expect(screen.queryByRole('button', { name: 'engine' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    // enum -> select, boolean -> checkbox, integer -> number, object -> JSON textarea
    expect(screen.getByRole('button', { name: 'engine' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'useLegacyMcpJson' })).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: 'provider_init_timeout' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'mcpServers' })).toBeInTheDocument()
  })

  it('invalid JSON in an object field shows an error and blocks create', async () => {
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'scratch-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    fireEvent.change(screen.getByRole('textbox', { name: 'mcpServers' }), { target: { value: '{ not json' } })
    expect(screen.getByText(/invalid json/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
    // Fixing the JSON re-enables create
    fireEvent.change(screen.getByRole('textbox', { name: 'mcpServers' }), { target: { value: '{"srv": {"command": "uvx"}}' } })
    expect(screen.queryByText(/invalid json/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create profile/i })).toBeEnabled()
  })

  it('provider renders as a styled select (Flows look) fed by the live registry', async () => {
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    const trigger = screen.getByRole('button', { name: 'provider' })
    fireEvent.click(trigger)
    // The menu is portaled to document.body so modal scroll containers
    // cannot clip it — find it by testid, not by DOM adjacency.
    const menu = screen.getByTestId('custom-select-menu')
    const texts = within(menu).getAllByRole('button').map(b => b.textContent)
    // Uninstalled providers carry the 'Not installed' sublabel, same as Flows
    expect(texts).toEqual(['(unset)', 'kiro_cli', 'claude_codeNot installed'])
    // Uninstalled stays selectable
    fireEvent.click(within(menu).getAllByRole('button')[2])
    expect(trigger).toHaveTextContent('claude_code')
  })

  it('provider falls back to a free text input when the registry is unavailable', async () => {
    vi.stubGlobal('fetch', routedFetch({ '/agents/providers': () => errJson(500, 'registry down') }))
    await openScratch()
    expect(screen.queryByRole('button', { name: 'provider' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'provider' })).toBeInTheDocument()
  })

  it('role is a text input with built-in datalist suggestions, free text preserved', async () => {
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    // An input[type=text] with a list attribute has implicit role 'combobox'
    const role = screen.getByRole('combobox', { name: 'role' })
    // Datalist attached with the three built-in roles
    expect(role).toHaveAttribute('list', 'field-role-suggestions')
    const datalist = screen.getByTestId('role-suggestions')
    expect(Array.from(datalist.querySelectorAll('option')).map(o => o.getAttribute('value')))
      .toEqual(['supervisor', 'developer', 'reviewer'])
    // Free text: a custom settings.json role is accepted
    fireEvent.change(role, { target: { value: 'my-custom-role' } })
    expect(role).toHaveValue('my-custom-role')
  })

  it('a JSON array is rejected for an object field', async () => {
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    fireEvent.change(screen.getByRole('textbox', { name: 'mcpServers' }), { target: { value: '[1,2]' } })
    expect(screen.getByText(/must be a json object/i)).toBeInTheDocument()
  })

  it('POSTs frontmatter built from the form plus the markdown body', async () => {
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    await openScratch()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'scratch-agent' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'description' }), { target: { value: 'Does things' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'tags' }), { target: { value: 'sqs, monitoring' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'System prompt' }), { target: { value: '# Be helpful.' } })
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    fireEvent.change(screen.getByRole('textbox', { name: 'mcpServers' }), { target: { value: '{"srv": {"command": "uvx"}}' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    const post = mock.mock.calls.find(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(post![1].body)
    expect(body.name).toBe('scratch-agent')
    expect(body.content).toContain('name: "scratch-agent"')
    expect(body.content).toContain('description: "Does things"')
    expect(body.content).toContain('tags: ["sqs","monitoring"]')
    expect(body.content).toContain('mcpServers: {"srv":{"command":"uvx"}}')
    expect(body.content).toContain('# Be helpful.')
  })

  it('marks the field named by an error finding with a red boundary', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) {
        return okJson({ valid: false, messages: [
          { severity: 'error', message: 'is not a known provider', path: 'provider' },
          { severity: 'error', message: 'name must match', path: 'name' },
        ] })
      }
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    await openScratch()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'x-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    // The named fields carry the red boundary; an untouched field does not
    expect(screen.getByRole('textbox', { name: 'provider' }).className).toContain('!border-red-500')
    expect(screen.getByRole('textbox', { name: 'provider' }).className).toContain('border-2')
    expect(screen.getByRole('textbox', { name: 'Profile name' }).className).toContain('!border-red-500')
    expect(screen.getByRole('textbox', { name: 'description' }).className).not.toContain('!border-red-500')
    // And the findings render as a bulleted list, one per finding
    const list = screen.getByTestId('validation-findings')
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(within(list).getAllByText('•')).toHaveLength(2)
  })

  it('auto-expands Advanced when the error targets a hidden advanced field (dotted path)', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) {
        return okJson({ valid: false, messages: [
          { severity: 'error', message: 'url is invalid', path: 'mcpServers.docs.url' },
        ] })
      }
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    await openScratch()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'x-agent' } })
    // Advanced section starts collapsed — mcpServers not in the document
    expect(screen.queryByRole('textbox', { name: 'mcpServers' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    // The dotted path roots at mcpServers: section auto-opens, field is red
    const field = screen.getByRole('textbox', { name: 'mcpServers' })
    expect(field.className).toContain('!border-red-500')
  })

  it('a 400 write rejection renders findings AND marks the named field red', async () => {
    // Regression: errors that only surface at write time (e.g. frontmatter
    // name mismatch — checked by the write route, not by /validate) must get
    // the same treatment as pre-save findings: red field boundary + bulleted
    // list, not a flat text list with no field highlight.
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/validate')) return okJson({ valid: true, messages: [] })
      if (url.includes('/templates')) return okJson(TEMPLATES)
      if (url.includes('/schema')) return okJson(PROFILE_SCHEMA)
      if (url.endsWith('/agents/profiles') && opts?.method === 'POST') {
        return errJson(400, { message: 'Profile validation failed', errors: [{ severity: 'error', message: 'is not valid', path: 'provider' }] })
      }
      return okJson([])
    }))
    await openScratch()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'bad-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})
    expect(screen.getByRole('alert')).toHaveTextContent('Profile validation failed')
    const list = screen.getByTestId('validation-findings')
    expect(within(list).getByText('is not valid')).toBeInTheDocument()
    expect(within(list).getByText('provider')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'provider' }).className).toContain('!border-red-500')
  })
})
