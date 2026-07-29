import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { TerminalView } from '../components/TerminalView'

// Lane C composer tests (§8.5-§8.8): picker + clipboard image paste, the
// editable `[Image #N]` token ↔ chip linkage, the explicit routing status
// line, text+image send through the operator-message path, refusal and
// old-server degradation, and regressions for ordinary short sends.

const { FakeTerminal } = vi.hoisted(() => {
  class FakeTerminal {
    rows = 24
    cols = 80
    element: HTMLDivElement
    dataHandler: ((data: string) => void) | null = null
    constructor(_opts: unknown) {
      this.element = document.createElement('div')
    }
    loadAddon() {}
    open(parent: HTMLElement) {
      parent.appendChild(this.element)
    }
    onData(handler: (data: string) => void) {
      this.dataHandler = handler
    }
    onSelectionChange() {}
    attachCustomKeyEventHandler() {}
    getSelection() {
      return ''
    }
    focus() {}
    write() {}
    dispose() {}
  }
  return { FakeTerminal }
})

vi.mock('@xterm/xterm/css/xterm.css', () => ({}))
vi.mock('@xterm/xterm', () => ({ Terminal: FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit() {}
  },
}))

class FakeWebSocket {
  static OPEN = 1
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  onopen: (() => void) | null = null
  onmessage: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: string[] = []
  constructor(public url: string) {}
  send(data: string) {
    this.sent.push(data)
  }
  close() {}
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

const FULL_KEYS = [
  'Escape', 'C-c', 'C-s', 'Enter', 'Backspace',
  'Up', 'Down', 'Left', 'Right', 'Home', 'End',
  'PageUp', 'PageDown', 'Delete', 'Insert', 'Tab',
]

const IDENTITY = {
  terminal_id: 't-native',
  terminal_incarnation: 'incarnation-1',
  terminal_generation: 'generation-1',
  pane_birth_id: '%7',
  provider_process_id: '42@start',
  provider: 'kimi_cli',
  native_session_id: 'session-1',
  execution_mode: 'native_tui',
  session_name: 'cao-test',
  control_input: {
    schema_versions: [1, 2, 3, 4],
    sequence: { keys: FULL_KEYS, max_events: 32, max_text_bytes: 512 },
    provider_controls: {
      kimi_cli: {
        steer_chords: ['C-s'],
        dispatch_grace_ms: 5000,
        operator_message: { supported: true, max_text_bytes: 8192, multiline: true, max_attachments: 4 },
        image: {
          supported: true,
          formats: ['png'],
          max_bytes: 5242880,
          max_width: 8000,
          max_height: 8000,
          mechanism: 'staged-path-text',
          reference_template: 'Use the ReadMediaFile tool to read the image file at {path}.',
        },
      },
    },
  },
}

interface StubOptions {
  laneCBlocks?: boolean
  uploadStatus?: number
  uploadBody?: Record<string, unknown>
  operatorMessageOutcome?: Record<string, unknown>
  operatorMessageFails?: boolean
  reconcileBody?: Record<string, unknown>
}

function stubLaneCFetch(
  requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }>,
  options: StubOptions = {},
) {
  const {
    laneCBlocks = true,
    uploadStatus = 201,
    uploadBody,
    operatorMessageOutcome = { outcome: 'accepted', reason_code: 'delivered' },
    operatorMessageFails = false,
    reconcileBody = { outcome: 'ambiguous', reason_code: 'response-lost' },
  } = options
  let controlNumber = 0
  let uploadNumber = 0
  vi.stubGlobal('crypto', { randomUUID: () => `uuid-${++controlNumber}` })
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    const body =
      typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    requests.push({ url, method: init?.method, body, formData: typeof FormData !== 'undefined' && init?.body instanceof FormData })
    if (url.endsWith('/managed-control')) {
      return {
        ok: true,
        json: async () => ({ managed: true, generation: 'generation-1', execution_mode: 'native_tui' }),
      } as Response
    }
    if (url.endsWith('/control-input/capabilities')) {
      return {
        ok: true,
        json: async () => ({
          protocol: 'cao-control-input-v1',
          execution_modes: ['native_tui'],
          literal_write: true,
          bracketed_paste: false,
          enter_required: true,
          request_schema_versions: [1, 2, 3, 4],
          sequence: { event_types: ['chord', 'key', 'text'], keys: FULL_KEYS, max_events: 32, max_text_bytes: 512 },
          streaming: { supported: true, max_in_flight: 1, coalesce_window_ms: 200 },
          provider_controls: {
            kimi_cli: {
              compact: { events: [{ type: 'text', text: '/compact' }, { type: 'key', key: 'Enter' }] },
              stop: { events: [{ type: 'key', key: 'Escape' }] },
              steer_chords: ['C-s'],
              dispatch_grace_ms: 5000,
              ...(laneCBlocks
                ? {
                    operator_message: { supported: true, max_text_bytes: 8192, multiline: true, max_attachments: 4 },
                    image: {
                      supported: true,
                      formats: ['png'],
                      max_bytes: 5242880,
                      max_width: 8000,
                      max_height: 8000,
                      mechanism: 'staged-path-text',
                    },
                  }
                : {}),
            },
          },
          command_controls: { composer_nonempty_guard: true },
        }),
      } as Response
    }
    if (url.endsWith('/control-identity')) {
      return { ok: true, json: async () => IDENTITY } as Response
    }
    if (url.includes('/macros')) {
      return { ok: true, json: async () => ({ macros: [] }) } as Response
    }
    if (url.endsWith('/attachments') && init?.method === 'POST') {
      uploadNumber += 1
      if (uploadStatus !== 201) {
        return {
          ok: false,
          status: uploadStatus,
          statusText: 'Unprocessable Entity',
          json: async () =>
            uploadBody ?? {
              outcome: 'refused',
              reason_code: 'attachment-type-unsupported',
              detail: 'the uploaded content is not a valid PNG image',
              attachment: {
                attachment_id: `att-${uploadNumber}`,
                terminal_id: 't-native',
                state: 'failed',
                format: null,
                size_bytes: 4,
                display_filename: 'bad.png',
                bound_operation_id: null,
                error: { reason_code: 'attachment-type-unsupported', detail: 'not a valid PNG' },
                created_at: '2026-07-29T00:00:00Z',
                updated_at: '2026-07-29T00:00:00Z',
              },
            },
        } as Response
      }
      return {
        ok: true,
        status: 201,
        json: async () => ({
          attachment: {
            attachment_id: `att-${uploadNumber}`,
            terminal_id: 't-native',
            state: 'ready',
            format: 'png',
            content_type: 'image/png',
            width: 120,
            height: 80,
            size_bytes: 213,
            display_filename: 'shot.png',
            bound_operation_id: null,
            error: null,
            created_at: '2026-07-29T00:00:00Z',
            updated_at: '2026-07-29T00:00:00Z',
          },
        }),
      } as Response
    }
    if (url.endsWith('/attachments') && init?.method === 'GET') {
      return { ok: true, json: async () => ({ attachments: [] }) } as Response
    }
    if (url.includes('/attachments/') && init?.method === 'DELETE') {
      return {
        ok: true,
        json: async () => ({ deleted: true, attachment: { attachment_id: 'att-1', state: 'removed' } }),
      } as Response
    }
    if (url.endsWith('/operator-message') && init?.method === 'POST') {
      if (operatorMessageFails) {
        throw new Error('network lost mid-submit')
      }
      return { ok: true, json: async () => operatorMessageOutcome } as Response
    }
    if (url.includes('/operator-message/')) {
      return { ok: true, json: async () => reconcileBody } as Response
    }
    if (url.endsWith('/control-input') && init?.method === 'POST') {
      return {
        ok: true,
        json: async () => ({ control_id: body?.control_id, outcome: 'accepted' }),
      } as Response
    }
    throw new Error(`unexpected request: ${url}`)
  }))
}

function pngFile(name = 'shot.png'): File {
  return new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], name, { type: 'image/png' })
}

interface RequestRecord {
  url: string
  method?: string
  body?: Record<string, unknown>
  formData?: boolean
}

function operatorMessagePosts(requests: RequestRecord[]) {
  return requests.filter(r => r.url.endsWith('/operator-message') && r.method === 'POST')
}

function attachmentUploads(requests: RequestRecord[]) {
  return requests.filter(r => r.url.endsWith('/attachments') && r.method === 'POST')
}

function attachmentDeletes(requests: RequestRecord[]) {
  return requests.filter(r => r.url.includes('/attachments/') && r.method === 'DELETE')
}

beforeEach(() => {
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = FakeResizeObserver
  // jsdom has no object URLs; the chip thumbnails only need stable strings.
  vi.stubGlobal('URL', Object.assign(Object.create(URL), {
    createObjectURL: () => 'blob:mock-preview',
    revokeObjectURL: () => {},
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function renderNativeView(requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }>, options: StubOptions = {}) {
  stubLaneCFetch(requests, options)
  render(<TerminalView terminalId="t-native" provider="kimi_cli" agentProfile="spec-writer-k3" onClose={() => {}} />)
  await screen.findByText('Managed native TUI · identity-bound controls')
  const composer = await screen.findByPlaceholderText('Send a message to the native composer…')
  return composer
}

describe('TerminalView Lane C attachments (§8.7)', () => {
  it('stages a picker upload: token at caret, chip staging → ready, alt text and notice', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)

    fireEvent.change(composer, { target: { value: 'look at this' } })
    fireEvent.change(screen.getByTestId('attachment-file-input'), {
      target: { files: [pngFile()] },
    })

    const strip = await screen.findByTestId('attachment-strip')
    expect(strip).toBeTruthy()
    await waitFor(() => expect(attachmentUploads(requests)).toHaveLength(1))
    expect(attachmentUploads(requests)[0].formData).toBe(true)

    // The token landed in the draft and the chip went ready.
    expect((composer as HTMLInputElement).value).toBe('look at this[Image #1]')
    await screen.findByText('ready')
    const img = screen.getByRole('img', { name: /Image #1/ })
    expect(img.getAttribute('alt')).toBe('Image #1: shot.png, 213 B, ready')
    expect(screen.getByTestId('attachment-notice').textContent).toContain('Image #1')
    expect(screen.getByTestId('attachment-notice').textContent).toContain('ready')
    // The paperclip is keyboard-reachable with an accessible name.
    expect(screen.getByRole('button', { name: 'Attach an image' })).toBeTruthy()
    // Remove control meets the 44px touch-target rule with an accessible name.
    expect(screen.getByRole('button', { name: 'Remove image #1' })).toBeTruthy()
  })

  it('stages a clipboard image paste; plain-text paste stays an ordinary paste', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)

    fireEvent.paste(composer, { clipboardData: { files: [pngFile()], types: ['Files'] } })
    await screen.findByTestId('attachment-strip')
    expect((composer as HTMLInputElement).value).toBe('[Image #1]')
    await waitFor(() => expect(attachmentUploads(requests)).toHaveLength(1))

    // Plain-text paste: no files on the clipboard → no chip, no upload.
    fireEvent.paste(composer, { clipboardData: { files: [], types: ['text/plain'] } })
    expect(attachmentUploads(requests)).toHaveLength(1)
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
  })

  it('deleting the token detaches the attachment (DELETE + chip removed)', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.paste(composer, { clipboardData: { files: [pngFile()] } })
    await screen.findByTestId('attachment-strip')
    await waitFor(() => expect(attachmentUploads(requests)).toHaveLength(1))
    await screen.findByText('ready')

    fireEvent.change(composer, { target: { value: '' } })
    await waitFor(() => expect(attachmentDeletes(requests)).toHaveLength(1))
    expect(attachmentDeletes(requests)[0].url).toContain('/attachments/att-1')
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })

  it('removing the chip deletes the token from the draft and the record', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'before ' } })
    fireEvent.paste(composer, { clipboardData: { files: [pngFile()] } })
    await screen.findByText('ready')
    expect((composer as HTMLInputElement).value).toBe('before [Image #1]')

    fireEvent.click(screen.getByRole('button', { name: 'Remove image #1' }))
    expect((composer as HTMLInputElement).value).toBe('before ')
    await waitFor(() => expect(attachmentDeletes(requests)).toHaveLength(1))
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })

  it('a failed upload keeps an actionable chip: typed error, retry, remove', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests, { uploadStatus: 422 })
    fireEvent.paste(composer, { clipboardData: { files: [pngFile('bad.png')] } })
    await screen.findByTestId('attachment-strip')
    await waitFor(() => expect(attachmentUploads(requests)).toHaveLength(1))

    // The typed refusal detail renders on the chip (and is announced);
    // Send stays disabled while unresolved.
    const strip = await screen.findByTestId('attachment-strip')
    await waitFor(() =>
      expect(strip.textContent).toContain('not a valid PNG'),
    )
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Retry image #1 upload' })).toBeTruthy()
    expect(screen.getByTestId('attachment-notice').textContent).toContain('failed')

    // Remove is recoverable: the failed record is deleted, the chip and
    // token go away, and Send re-enables for the remaining draft.
    fireEvent.click(screen.getByRole('button', { name: 'Remove image #1' }))
    await waitFor(() => expect(attachmentDeletes(requests)).toHaveLength(1))
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })

  it('refuses a format the provider does not advertise with zero upload POSTs', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    const jpeg = new File([new Uint8Array([0xff, 0xd8])], 'photo.jpg', { type: 'image/jpeg' })
    fireEvent.paste(composer, { clipboardData: { files: [jpeg] } })

    const strip = await screen.findByTestId('attachment-strip')
    await waitFor(() =>
      expect(strip.textContent).toContain('not advertised by this provider'),
    )
    expect(attachmentUploads(requests)).toHaveLength(0)
    expect((composer as HTMLInputElement).value).toBe('[Image #1]')
  })

  it('caps attachments at the advertised maximum', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    for (let index = 0; index < 5; index += 1) {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile(`s${index}.png`)] } })
    }
    await screen.findByText(/at most 4 images/)
    expect(screen.getAllByRole('listitem')).toHaveLength(4)
    await waitFor(() => expect(attachmentUploads(requests)).toHaveLength(4))
  })
})

describe('TerminalView Lane C routing (§8.5)', () => {
  it('names the control-input path for a short text-only draft', async () => {
    const requests: Array<{ url: string; method?: string }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'short command' } })
    expect(screen.getByTestId('composer-route-status').textContent).toBe(
      'delivers as control input · 13/512 B',
    )
  })

  it('names the operator-message path with size and image count', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'x'.repeat(600) } })
    expect(screen.getByTestId('composer-route-status').textContent).toContain(
      'operator message — 600 B',
    )
    fireEvent.paste(composer, { clipboardData: { files: [pngFile()] } })
    await screen.findByText('ready')
    expect(screen.getByTestId('composer-route-status').textContent).toContain('1 image')
  })

  it('routes a >512-byte draft to the operator-message path, never truncating', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'x'.repeat(600) } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(operatorMessagePosts(requests)).toHaveLength(1))
    const post = operatorMessagePosts(requests)[0]
    expect(post.body?.operation_id).toBeTruthy()
    expect((post.body?.text as string).length).toBe(600)
    expect(post.body?.expected_identity).toMatchObject({
      terminal_id: 't-native',
      terminal_generation: 'generation-1',
      pane_birth_id: '%7',
    })
    // Not a single control-input POST: the short-command path is untouched.
    expect(requests.filter(r => r.url.endsWith('/control-input') && r.method === 'POST')).toHaveLength(0)
  })

  it('sends text+image as one operator message with the token map', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'what is this ' } })
    fireEvent.paste(composer, { clipboardData: { files: [pngFile()] } })
    await screen.findByText('ready')

    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(operatorMessagePosts(requests)).toHaveLength(1))
    const post = operatorMessagePosts(requests)[0]
    expect(post.body?.text).toBe('what is this [Image #1]')
    expect(post.body?.attachments).toEqual(['att-1'])
    expect(post.body?.token_map).toEqual({ '1': 'att-1' })

    // Accepted: the draft and chips clear.
    await waitFor(() => expect((composer as HTMLInputElement).value).toBe(''))
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })

  it('keeps the ordinary short send byte-identical (control-input v1 regression)', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests)
    fireEvent.change(composer, { target: { value: 'short command' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() =>
      expect(requests.filter(r => r.url.endsWith('/control-input') && r.method === 'POST')).toHaveLength(1),
    )
    const post = requests.find(r => r.url.endsWith('/control-input') && r.method === 'POST')
    expect(post?.body).toMatchObject({ text: 'short command', enter: true })
    expect(operatorMessagePosts(requests)).toHaveLength(0)
  })

  it('reconciles a lost operator-message response exactly once, never resending', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests, { operatorMessageFails: true })
    fireEvent.change(composer, { target: { value: 'x'.repeat(600) } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() =>
      expect(requests.filter(r => r.url.includes('/operator-message/') && !r.method)).toHaveLength(1),
    )
    // One POST, one exact-id reconcile GET, and the status names the
    // journaled ambiguous answer — no automatic second POST ever.
    expect(operatorMessagePosts(requests)).toHaveLength(1)
    await screen.findByText(/operator message: ambiguous/)
  })
})

describe('TerminalView Lane C degradation (§8.6/D9)', () => {
  it('hides the attachment affordances when the blocks are absent (old server)', async () => {
    const requests: Array<{ url: string; method?: string }> = []
    const composer = await renderNativeView(requests, { laneCBlocks: false })
    expect(screen.queryByRole('button', { name: 'Attach an image' })).toBeNull()

    // An over-limit draft disables Send with the explanation, never a 422.
    fireEvent.change(composer, { target: { value: 'x'.repeat(600) } })
    expect(screen.getByTestId('composer-route-status').textContent).toContain(
      'operator message unavailable for this provider',
    )
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true)
    // Short drafts still send through the deployed path.
    fireEvent.change(composer, { target: { value: 'short' } })
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('refuses a clipboard image with a status when attachments are unavailable', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    const composer = await renderNativeView(requests, { laneCBlocks: false })
    fireEvent.paste(composer, { clipboardData: { files: [pngFile()] } })
    await screen.findByText(/image attachments are unavailable for this provider/)
    expect(attachmentUploads(requests)).toHaveLength(0)
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })

  it('streaming armed replaces the composer; an image paste there uploads nothing (§6.2)', async () => {
    const requests: Array<{ url: string; method?: string; body?: Record<string, unknown>; formData?: boolean }> = []
    await renderNativeView(requests)
    fireEvent.click(screen.getByRole('button', { name: 'Streaming' }))
    // The composer input is unmounted while armed; the capture surface owns
    // paste and refuses images as keystrokes-only.
    const capture = await screen.findByRole('textbox', { name: /Streaming keystroke capture/ })
    expect(screen.queryByPlaceholderText('Send a message to the native composer…')).toBeNull()
    fireEvent.paste(capture, { clipboardData: { files: [pngFile()] } })
    expect(attachmentUploads(requests)).toHaveLength(0)
    expect(screen.queryByTestId('attachment-strip')).toBeNull()
  })
})
