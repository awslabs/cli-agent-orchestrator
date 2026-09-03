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

  it('extractFrontmatterName never matches a name: line in the markdown body', () => {
    // The unbounded scan continued past the closing --- when the frontmatter
    // had no name:, pre-filling the name box from body prose.
    expect(extractFrontmatterName('---\ndescription: d\n---\nbody\nname: decoy\n')).toBeNull()
    expect(extractFrontmatterName('no frontmatter here\nname: decoy\n')).toBeNull()
    // Bounding must not break the normal case
    expect(extractFrontmatterName('---\ndescription: d\nname: real\n---\nbody\nname: decoy\n')).toBe('real')
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

  it('a JSON draft survives collapsing and reopening Advanced (display matches what saves)', async () => {
    // The textarea was uncontrolled (defaultValue): a collapse/reopen
    // remounted it EMPTY while the draft silently persisted into the POST --
    // the user saw a blank field but the typed JSON still saved.
    vi.stubGlobal('fetch', routedFetch())
    await openScratch()
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i }))
    fireEvent.change(screen.getByRole('textbox', { name: 'mcpServers' }), { target: { value: '{"srv": {"command": "uvx"}}' } })
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i })) // collapse
    fireEvent.click(screen.getByRole('button', { name: /advanced properties/i })) // reopen
    expect(screen.getByRole('textbox', { name: 'mcpServers' })).toHaveValue('{"srv": {"command": "uvx"}}')
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

describe('ProfileCreateModal — stale preview invalidation (#692 review)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const A_BODY = '---\nname: template-a-agent\ndescription: A\n---\n\nTEMPLATE-A-BODY\n'

  it("drops template A's in-flight preview after switching to B: no stale content, Create stays disabled", async () => {
    // Reproduces the maintainer's P1 probe: A's render is released only after
    // the user has switched to B, whose schema is still loading -- the exact
    // window where the stale response used to silently re-arm Create.
    let releaseA!: () => void
    const aGate = new Promise<any>(res => {
      releaseA = () => res(okJson({ template: 'aws/sqs-monitor', content: A_BODY }))
    })
    const mock = routedFetch({
      'aws/stepfunction/schema': () => new Promise(() => {}), // B schema never resolves
      '/agents/profiles/templates/preview': (_u: string, opts: any) =>
        JSON.parse(opts.body).template === 'aws/sqs-monitor'
          ? aGate
          : okJson({ template: 'aws/stepfunction', content: RENDERED }),
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    // Debounce fires: A's preview request is now in flight, response gated
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))

    // Switch to B while A's render is still in flight
    await pickOption('Template', 'aws/stepfunction')
    await act(async () => {})

    // A's stale response lands
    await act(async () => { releaseA() })

    // Nothing of A may surface: no preview pane, no pre-filled name, and
    // Create must stay disabled even with a name typed in.
    expect(screen.queryByTestId('template-preview')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'victim' } })
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
    const posts = mock.mock.calls.filter(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(posts).toHaveLength(0)
  })

  it('a preview in flight at close does not re-land after reopen', async () => {
    // The modal stays mounted across close (`open` prop), so a response that
    // outlives a close/reopen cycle used to satisfy the old seq check and
    // restore a preview for a template that is no longer even selected.
    let releaseA!: () => void
    const aGate = new Promise<any>(res => {
      releaseA = () => res(okJson({ template: 'aws/sqs-monitor', content: A_BODY }))
    })
    const mock = routedFetch({ '/agents/profiles/templates/preview': () => aGate })
    vi.stubGlobal('fetch', mock)
    const { rerender } = render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10)) // A's render in flight

    rerender(<ProfileCreateModal open={false} onClose={() => {}} onCreated={() => {}} />)
    rerender(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})

    // The pre-close response lands after reopen: must be dropped entirely
    await act(async () => { releaseA() })
    expect(screen.queryByTestId('template-preview')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Profile name' })).toHaveValue('')
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
  })

  it("template schemas resolving out of order never leave A's form under B's selection", async () => {
    // getTemplateSchema previously had no staleness guard: a fast A->B switch
    // with reordered resolution rendered A's fields under B's selection.
    const A_SCHEMA = { type: 'object', properties: { a_only_field: { type: 'string' } } }
    let releaseASchema!: () => void
    const aGate = new Promise<any>(res => { releaseASchema = () => res(okJson(A_SCHEMA)) })
    const mock = routedFetch({ 'aws/sqs-monitor/schema': () => aGate })
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor') // A schema in flight (gated)
    await act(async () => {})
    await pickOption('Template', 'aws/stepfunction') // B schema resolves immediately
    await act(async () => {})
    expect(screen.getByRole('textbox', { name: 'queue_url' })).toBeInTheDocument()

    // A's schema lands late: must be discarded, B's form untouched
    await act(async () => { releaseASchema() })
    expect(screen.queryByRole('textbox', { name: 'a_only_field' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'queue_url' })).toBeInTheDocument()
  })
})

describe('ProfileCreateModal — round-3 review fixes (#692)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function openScratchMode() {
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
  }

  it('the header X is inert while a save is in flight', async () => {
    let releasePost!: () => void
    const postGate = new Promise<any>(res => { releasePost = () => res(okJson({ name: 'x', warnings: [] })) })
    const onClose = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (String(url).includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (String(url).endsWith('/agents/profiles') && opts?.method === 'POST') return postGate
      if (String(url).includes('/agents/profiles/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={onClose} onCreated={() => {}} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    // POST parked on the gate: X must be inert, matching Cancel and backdrop
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).not.toHaveBeenCalled()

    await act(async () => { releasePost() })
    expect(onClose).toHaveBeenCalledTimes(1) // normal success close
  })

  it('the scratch-mode document embeds the TRIMMED name, matching the POST', async () => {
    // 'my-agent ' (trailing space) previously produced a frontmatter of
    // name: "my-agent " while the POST sent "my-agent" -- the pre-save gate
    // then blocked on a pattern error whose defect is invisible.
    const mock = routedFetch()
    vi.stubGlobal('fetch', mock)
    await openScratchMode()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'my-agent ' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    const post = mock.mock.calls.find(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(post![1].body)
    expect(body.name).toBe('my-agent')
    expect(body.content).toContain('name: "my-agent"')
    expect(body.content).not.toContain('my-agent ')
  })

  it('a failed profile-schema fetch surfaces an error, not a permanent spinner', async () => {
    vi.stubGlobal('fetch', routedFetch({
      '/agents/profiles/schema': () => errJson(500, 'schema exploded'),
    }))
    await openScratchMode()
    expect(screen.getByRole('alert')).toHaveTextContent('schema exploded')
    expect(screen.queryByTestId('profile-schema-loading')).not.toBeInTheDocument()
  })

  it('switching modes clears findings, save error, and red boundaries from the other mode', async () => {
    // A template-mode validation failure previously stayed rendered on the
    // scratch form: findings panel, saveError box, and red borders on
    // same-named fields (name, provider, ...).
    const mock = routedFetch({
      '/agents/profiles/validate': () => okJson({
        valid: false,
        messages: [{ severity: 'error', message: 'template-mode failure', path: 'name' }],
      }),
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})
    // Findings + saveError rendered in template mode
    expect(screen.getByText('template-mode failure')).toBeInTheDocument()
    const nameBox = screen.getByRole('textbox', { name: 'Profile name' })
    expect(nameBox.className).toContain('border-red-500')

    // Switch to From scratch: all error surfaces from template mode clear
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    expect(screen.queryByText('template-mode failure')).not.toBeInTheDocument()
    expect(screen.queryByText(/validation failed/i)).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Profile name' }).className).not.toContain('border-red-500')
  })
})

describe('mode round-trip must not strand template mode (adversarial probe)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a failed template-schema load survives a scratch round-trip: error stays visible, no permanent spinner', async () => {
    // previewError doubles as the template-schema load error. If the
    // mode-switch clear wipes it while templateSchema is still null, the
    // render condition (template && !templateSchema && !previewError) falls
    // back to the loading spinner FOREVER -- the same defect class as the
    // scratch-mode schema spinner fixed this round.
    vi.stubGlobal('fetch', routedFetch({
      'aws/sqs-monitor/schema': () => errJson(500, 'template schema exploded'),
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    expect(screen.getByText(/template schema exploded/)).toBeInTheDocument()

    // Round-trip through From scratch and back
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    fireEvent.click(screen.getByRole('tab', { name: 'From template' }))
    await act(async () => {})

    // The load error must still be visible; a spinner with no in-flight
    // request would be unrecoverable without closing the modal.
    expect(screen.getByText(/template schema exploded/)).toBeInTheDocument()
    expect(screen.queryByTestId('template-schema-loading')).not.toBeInTheDocument()
  })
})

describe('round-4 review: async ordering (#692)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const OLD_BODY = '---\nname: old-config-agent\ndescription: A\n---\n\nOLD-CONFIG-BODY\n'

  it("a preview in flight for config A cannot land during config B's debounce window", async () => {
    // The generation must advance when the FORM STATE changes, not when the
    // debounced request is issued: without the early bump, A's response
    // matched the sequence mid-debounce, installed A's body, and re-armed
    // Create while the form displayed B (haofeif's round-4 P1 probe).
    let releaseA!: () => void
    let call = 0
    const mock = routedFetch({
      '/agents/profiles/templates/preview': () => {
        call++
        if (call === 1) return new Promise<any>(res => { releaseA = () => res(okJson({ template: 'aws/sqs-monitor', content: OLD_BODY })) })
        return okJson({ template: 'aws/sqs-monitor', content: RENDERED })
      },
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10)) // A in flight, gated

    // Edit config to B; A's response is released DURING B's debounce window
    fireEvent.change(screen.getByRole('textbox', { name: 'queue_url' }), { target: { value: 'https://sqs.new' } })
    await act(async () => { releaseA() })

    // A must be discarded: no stale body, Create still gated on the pending render
    expect(screen.queryByText(/OLD-CONFIG-BODY/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'victim' } })
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()

    // B's own render lands normally once the debounce fires
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    expect(screen.getByTestId('template-preview')).toHaveTextContent('# You watch queues.')
  })

  it('mode tabs are disabled while a save is in flight', async () => {
    let releasePost!: () => void
    const postGate = new Promise<any>(res => { releasePost = () => res(okJson({ name: 'x', warnings: [] })) })
    vi.stubGlobal('fetch', vi.fn(async (url: string, opts?: any) => {
      if (String(url).includes('/agents/profiles/validate')) return okJson({ valid: true, messages: [] })
      if (String(url).endsWith('/agents/profiles') && opts?.method === 'POST') return postGate
      if (String(url).includes('/agents/profiles/schema')) return okJson(PROFILE_SCHEMA)
      return okJson([])
    }))
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
    await act(async () => {})
    fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})

    // Mid-save: switching modes would let the old mode's result repopulate
    // errors on the new form -- both tabs must be inert
    expect(screen.getByRole('tab', { name: 'From template' })).toBeDisabled()
    expect(screen.getByRole('tab', { name: 'From scratch' })).toBeDisabled()

    await act(async () => { releasePost() })
  })
})

describe('round-5 review: a SETTLED preview cannot outlive its template (#692)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const A_BODY = '---\nname: template-a-agent\ndescription: A\n---\n\nTEMPLATE-A-BODY\n'

  it("template A's fully-settled preview is cleared when B is selected with its schema pending", async () => {
    // Round-4 closed the in-flight window; this is the adjacent settled-state
    // case (haofeif's round-5 P1): A's preview has ALREADY landed when the
    // user switches to B whose schema is pending/failed. The cannot-render
    // branch used to leave the settled preview in state, so canCreate stayed
    // true and Create persisted A's content while the UI identified B.
    const mock = routedFetch({
      'aws/stepfunction/schema': () => new Promise(() => {}), // B schema never resolves
      '/agents/profiles/templates/preview': () => okJson({ template: 'aws/sqs-monitor', content: A_BODY }),
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
    await act(async () => {})
    await pickOption('Template', 'aws/sqs-monitor')
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS + 10))
    await act(async () => {})
    // A's preview has fully settled: pane visible, Create armed
    expect(screen.getByTestId('template-preview')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create profile/i })).toBeEnabled()

    // Switch to B; its schema stays pending, so nothing can render for B
    await pickOption('Template', 'aws/stepfunction')
    await act(async () => {})

    // Nothing of A may remain actionable: no preview pane, Create disabled,
    // and no POST can carry A's body under B's identity
    expect(screen.queryByTestId('template-preview')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create profile/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /create profile/i }))
    await act(async () => {})
    const posts = mock.mock.calls.filter(([u, o]: any[]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
    expect(posts).toHaveLength(0)
  })
})
