import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'

// #510 U3: api.ts wrappers for the create-from-template flow. These assert
// URL/verb/body only; render + validation + write correctness is a server (U1)
// guarantee.
describe('create-wizard API wrappers', () => {
  const mockFetch = vi.fn()

  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => vi.restoreAllMocks())

  function mockResponse(data: unknown, status = 200) {
    mockFetch.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: () => Promise.resolve(data),
    })
  }

  it('listTemplates GETs /agents/profiles/templates', async () => {
    const templates = [{ name: 'aws/sqs-monitor', description: 'Poll SQS', path: '/x' }]
    mockResponse(templates)
    const result = await api.listTemplates()
    expect(result).toEqual(templates)
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/templates',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })

  it('getTemplateSchema keeps the category/name slash unencoded (:path route)', async () => {
    mockResponse({ type: 'object', properties: {} })
    await api.getTemplateSchema('aws/sqs-monitor')
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/templates/aws/sqs-monitor/schema',
      expect.any(Object),
    )
  })

  it('previewProfile POSTs the create body to /preview (server render, no write)', async () => {
    const req = { template_name: 'aws/sqs-monitor', config: { region: 'us-east-1' }, provider: 'claude_code', model: 'opus' }
    mockResponse({ text: '---\nname: x\n---\n', valid: true, errors: [], warnings: [] })
    const result = await api.previewProfile(req)
    expect(result.valid).toBe(true)
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles/preview',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      }),
    )
  })

  it('previewProfile transmits the request body verbatim (transmit-fidelity, NOT the F-1 guard)', async () => {
    // NOTE: this is a TRANSMIT-FIDELITY check, not the F-1 rule. It hand-builds a
    // payload and inspects the wire body, proving api.previewProfile serializes
    // whatever it is handed without mangling — top-level fields stay top-level,
    // config stays untouched. It CANNOT catch an F-1 violation, because the F-1
    // mistake originates in the WIZARD assembling the payload, and the wizard is
    // not invoked here. The real F-1 guard is createWizard.test.tsx
    // ("preview F-1: provider/model are NOT put into the render config"), which
    // drives the actual wizard and fails when provider/model leak into config.
    mockFetch.mockClear() // isolate this call — the shared mock accumulates history
    mockResponse({ text: '---\nname: x\n---\n', valid: true, errors: [], warnings: [] })
    await api.previewProfile({
      template_name: 'aws/sqs-monitor',
      config: { region: 'us-east-1', queue_url: 'https://sqs.us-east-1.amazonaws.com/1/q' },
      provider: 'claude_code',
      model: 'opus',
    })
    const opts = mockFetch.mock.lastCall![1]
    const sent = JSON.parse((opts as RequestInit).body as string)
    // Fields are transmitted exactly as handed in — no re-shaping by the wrapper.
    expect(sent.provider).toBe('claude_code')
    expect(sent.model).toBe('opus')
    expect(sent.config).toEqual({ region: 'us-east-1', queue_url: 'https://sqs.us-east-1.amazonaws.com/1/q' })
  })

  it('createProfile POSTs to /agents/profiles', async () => {
    const req = { template_name: 'aws/sqs-monitor', config: {}, provider: 'claude_code', model: 'opus' }
    mockResponse({ name: 'sqs-monitor-agent', source: 'local', path: '/store/sqs-monitor-agent.md' })
    const result = await api.createProfile(req)
    expect(result.source).toBe('local')
    expect(mockFetch).toHaveBeenCalledWith(
      '/agents/profiles',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(req) }),
    )
  })

  it('createProfile surfaces a validation-error 400 as ApiError detail', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: { message: 'Profile validation failed', errors: ['[error] name'] } }),
    })
    await expect(
      api.createProfile({ template_name: 't', config: {}, provider: 'p', model: 'm' }),
    ).rejects.toMatchObject({ status: 400, detail: 'Profile validation failed' })
  })
})
