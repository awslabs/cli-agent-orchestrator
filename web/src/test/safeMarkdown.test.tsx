// Safe Markdown/plain-text rendering (design §9, §10).
//
// Every test in this file renders the REAL component with an attack payload
// and asserts on the produced DOM or on spies watching the network surfaces —
// never on the sanitiser helper alone, because a helper returning a safe
// string proves nothing about what the component wired it into.
//
// FIXTURE DISCLOSURE — cond-0477: The communications catalog fixtures in
// sibling test files that carry a bound task_occurrence_id model a state no
// shipped conductor writer currently produces — all current writers record
// task_occurrence_id = NULL (cond-0477). The fork's contract is the published
// index format and a bound occurrence is a legal value of it. The API reports
// `coverage:"complete"`, `total:0` with no reason code for the unbound case,
// so the reader cannot distinguish "unbound" from "genuinely empty" — a known
// limitation that resolves when cond-0477 lands.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { SafeContentView, SafeMarkdown } from '../components/SafeContentView'
import * as safeMarkdownLib from '../lib/safeMarkdown'
import {
  isMarkdownMediaType,
  markdownBudgetBreach,
  safeDownloadName,
  safeLinkHref,
  MAX_MARKDOWN_RENDER_BYTES,
} from '../lib/safeMarkdown'

function renderMd(content: string) {
  return render(<SafeMarkdown content={content} />)
}

/** Every element attribute in the container, flattened. */
function allAttrs(container: HTMLElement): string[] {
  const out: string[] = []
  for (const el of Array.from(container.querySelectorAll('*'))) {
    for (const attr of Array.from(el.attributes)) out.push(`${attr.name}=${attr.value}`)
  }
  return out
}

beforeEach(() => {
  vi.spyOn(globalThis, 'fetch')
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('GFM still works', () => {
  it('renders a table, a task list, and strikethrough', () => {
    const { container } = renderMd(
      ['| a | b |', '| - | - |', '| 1 | 2 |', '', '- [x] done', '- [ ] todo', '', '~~gone~~'].join('\n'),
    )
    expect(container.querySelector('table')).not.toBeNull()
    expect(container.querySelector('td')!.textContent).toBe('1')
    const boxes = container.querySelectorAll('input[type="checkbox"]')
    expect(boxes).toHaveLength(2)
    expect((boxes[0] as HTMLInputElement).checked).toBe(true)
    expect((boxes[0] as HTMLInputElement).disabled).toBe(true)
    expect(container.querySelector('del')!.textContent).toBe('gone')
  })

  it('renders http and https links with safe window semantics', () => {
    const { container } = renderMd('[a](https://example.com/x) and [b](http://example.com/y)')
    const [a, b] = Array.from(container.querySelectorAll('a'))
    expect(a.getAttribute('href')).toBe('https://example.com/x')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
    expect(b.getAttribute('href')).toBe('http://example.com/y')
  })

  it('autolinks a bare https URL and keeps www autolinks on http', () => {
    const { container } = renderMd('<https://example.com> and www.example.com')
    const hrefs = Array.from(container.querySelectorAll('a')).map(a => a.getAttribute('href'))
    expect(hrefs).toContain('https://example.com')
    expect(hrefs).toContain('http://www.example.com')
  })
})

describe('raw HTML executes nothing', () => {
  it('<script> produces no script element; the text is inert or omitted', () => {
    const { container } = renderMd('before\n\n<script>alert(1)</script>\n\nafter')
    expect(container.querySelector('script')).toBeNull()
    expect(allAttrs(container).join(' ')).not.toContain('alert')
    // The surrounding Markdown still rendered.
    expect(container.textContent).toContain('before')
    expect(container.textContent).toContain('after')
  })

  it('<img src=x onerror=alert(1)> produces no img and no event handler', () => {
    const { container } = renderMd('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
    expect(allAttrs(container).some(a => a.startsWith('onerror='))).toBe(false)
  })

  it('an HTML block with an onclick handler produces no element carrying it', () => {
    const { container } = renderMd('<div onclick="alert(1)">hello</div>')
    expect(container.querySelector('div[onclick]')).toBeNull()
    expect(allAttrs(container).some(a => /^on/i.test(a))).toBe(false)
  })

  it('no rendered document ever carries an on* attribute', () => {
    const payloads = [
      '<svg onload=alert(1)>',
      '<a href="https://x" onclick="alert(1)">y</a>',
      '<form action="https://evil.example"><button>go</button></form>',
      '<iframe src="https://evil.example"></iframe>',
    ]
    for (const p of payloads) {
      const { container, unmount } = renderMd(p)
      expect(allAttrs(container).some(a => /^on/i.test(a))).toBe(false)
      expect(container.querySelector('iframe')).toBeNull()
      expect(container.querySelector('form')).toBeNull()
      unmount()
    }
  })
})

describe('URL policy', () => {
  it.each([
    ['javascript:alert(1)'],
    ['JaVaScRiPt:alert(1)'],
    ['java&#115;cript:alert(1)'],
    ['data:text/html,<script>alert(1)</script>'],
    ['vbscript:msgbox(1)'],
    ['file:///etc/passwd'],
    ['//evil.example/protocol-relative'],
    ['../../etc/passwd'],
    ['./sibling.md'],
    ['custom-scheme:do-thing'],
  ])('blocks %s at the DOM, not just in a helper', (url) => {
    const { container } = renderMd(`[click](${url})`)
    expect(container.querySelector('a')).toBeNull()
    expect(allAttrs(container).join(' ')).not.toContain(url.replace(/&#115;/g, 's'))
    // The link text survives as inert, readable, labelled text.
    expect(screen.getByText('click')).toBeInTheDocument()
    expect(container.querySelector('[data-blocked-link="true"]')).not.toBeNull()
  })

  it('blocks a javascript: autolink', () => {
    const { container } = renderMd('<javascript:alert(1)>')
    expect(container.querySelector('a')).toBeNull()
  })

  it('blocks a javascript: reference-style link', () => {
    const { container } = renderMd('[click][x]\n\n[x]: javascript:alert(1)')
    expect(container.querySelector('a')).toBeNull()
    expect(screen.getByText('click')).toBeInTheDocument()
  })
})

describe('images fetch nothing', () => {
  it('a remote image becomes a non-fetching placeholder with its alt text', () => {
    const imageCtor = vi.fn()
    vi.stubGlobal('Image', class {
      constructor() {
        imageCtor()
      }
    })
    const { container } = renderMd('![tracking pixel](https://evil.example/pixel.png)')
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('[src]')).toBeNull()
    expect(allAttrs(container).join(' ')).not.toContain('evil.example')
    const placeholder = screen.getByTestId('md-image-placeholder')
    expect(placeholder).toHaveTextContent('tracking pixel')
    expect(imageCtor).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('even a data:-URL image is not rendered as an element', () => {
    const { container } = renderMd('![x](data:image/png;base64,AAAA)')
    expect(container.querySelector('img')).toBeNull()
    expect(allAttrs(container).join(' ')).not.toContain('data:image')
  })
})

describe('budgets fail visibly', () => {
  it('a document over the byte budget shows the named state, not an excerpt', () => {
    const content = `word `.repeat(MAX_MARKDOWN_RENDER_BYTES / 5 + 10)
    render(<SafeContentView content={content} mediaType="text/markdown" downloadBase="doc" />)
    const notice = screen.getByTestId('markdown-render-budget')
    expect(notice).toHaveTextContent('Too large to render')
    expect(notice).toHaveTextContent('Raw')
    expect(screen.queryByTestId('md-rendered')).toBeNull()
  })

  it('a marker-dense document trips the node budget under the byte budget', () => {
    const content = '*a* '.repeat(20_000) // ~80 KiB, tens of thousands of nodes
    expect(markdownBudgetBreach(content)).toBe('nodes')
    render(<SafeContentView content={content} mediaType="text/markdown" downloadBase="doc" />)
    const notice = screen.getByTestId('markdown-render-budget')
    expect(notice).toHaveTextContent('Too complex to render')
    expect(screen.queryByTestId('md-rendered')).toBeNull()
  })

  it('the raw view remains available for an over-budget document, complete', () => {
    const content = `# heading\n\n${'line of text\n'.repeat(50_000)}`
    render(<SafeContentView content={content} mediaType="text/markdown" downloadBase="doc" />)
    expect(screen.getByTestId('markdown-render-budget')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Raw' }))
    const raw = screen.getByTestId('content-raw')
    expect(raw.textContent).toBe(content) // complete, not an excerpt
  })

  it('the breach is computed once per (content, mode), not once per render', async () => {
    vi.useFakeTimers()
    try {
      const spy = vi.spyOn(safeMarkdownLib, 'markdownBudgetBreach')
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
      const content = '# memo\n\nsome content'
      render(<SafeContentView content={content} mediaType="text/markdown" downloadBase="doc" />)
      expect(spy).toHaveBeenCalledTimes(1)
      // A Copy click re-renders twice — setCopied(true), then the 2s reset —
      // and neither re-render may re-pay the counting parse.
      await act(async () => {
        fireEvent.click(screen.getByTestId('content-copy'))
      })
      expect(screen.getByTestId('content-copy')).toHaveTextContent('Copied')
      act(() => {
        vi.advanceTimersByTime(2100)
      })
      expect(screen.getByTestId('content-copy')).toHaveTextContent('Copy')
      expect(spy).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('plain text stays literal', () => {
  it('Markdown-looking characters in text/plain render as text', () => {
    const content = '# not a heading\n\n**not bold** [not a link](https://example.com)\n\n- not\n- a\n- list'
    render(<SafeContentView content={content} mediaType="text/plain" downloadBase="doc" />)
    const raw = screen.getByTestId('content-raw')
    expect(raw.textContent).toBe(content)
    expect(raw.tagName).toBe('PRE')
    expect(screen.queryByTestId('md-rendered')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Rendered' })).toBeNull()
  })

  it('an unknown media type takes the plain-text path', () => {
    expect(isMarkdownMediaType('application/x-whatever')).toBe(false)
    render(<SafeContentView content={'# x'} mediaType="application/x-whatever" downloadBase="doc" />)
    expect(screen.getByTestId('content-raw')).toBeInTheDocument()
  })

  it('media-type parameters do not confuse the markdown decision', () => {
    expect(isMarkdownMediaType('text/markdown; charset=utf-8')).toBe(true)
    expect(isMarkdownMediaType('Text/Markdown')).toBe(true)
  })
})

describe('copy and download', () => {
  it('copies the exact content', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const content = 'exact bytes\nwith  spacing'
    render(<SafeContentView content={content} mediaType="text/plain" downloadBase="doc" />)
    fireEvent.click(screen.getByTestId('content-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(content))
  })

  it('downloads a Blob of the exact content under the sanitized display name', async () => {
    const blobs: Blob[] = []
    const clicks: HTMLAnchorElement[] = []
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: (blob: Blob) => {
        blobs.push(blob)
        return `blob:${blobs.length}`
      },
      revokeObjectURL: () => {},
    })
    const realClick = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function () {
      clicks.push(this)
    }
    const content = '# report\n'
    render(
      <SafeContentView content={content} mediaType="text/markdown" downloadBase="communication-c1" displayName="final-report.md" />,
    )
    fireEvent.click(screen.getByTestId('content-download'))
    expect(clicks).toHaveLength(1)
    expect(clicks[0].download).toBe('final-report.md')
    expect(blobs).toHaveLength(1)
    // jsdom Blob predates .text(); FileReader reads the same bytes.
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(reader.error)
      reader.readAsText(blobs[0])
    })
    expect(text).toBe(content)
    HTMLAnchorElement.prototype.click = realClick
  })
})

describe('helper contracts the DOM tests lean on', () => {
  it('safeLinkHref admits only absolute http(s)', () => {
    expect(safeLinkHref('https://example.com')).toBe('https://example.com')
    expect(safeLinkHref('  https://example.com/x  ')).toBe('https://example.com/x')
    expect(safeLinkHref('javascript:alert(1)')).toBeNull()
    expect(safeLinkHref('java\tscript:alert(1)')).toBeNull()
    expect(safeLinkHref('//evil.example')).toBeNull()
    expect(safeLinkHref('/local/path')).toBeNull()
    expect(safeLinkHref('')).toBeNull()
  })

  it('safeDownloadName strips paths, dotfiles, and controls; falls back by media type', () => {
    expect(safeDownloadName('report.md', 'x', 'text/markdown')).toBe('report.md')
    expect(safeDownloadName('../../etc/passwd', 'x', 'text/plain')).toBe('passwd')
    expect(safeDownloadName('C:\\tmp\\evil.exe', 'x', 'text/plain')).toBe('evil.exe')
    expect(safeDownloadName('..', 'comm-1', 'text/markdown')).toBe('comm-1.md')
    expect(safeDownloadName('', 'comm-1', 'text/plain')).toBe('comm-1.txt')
    expect(safeDownloadName(null, 'att-2', undefined)).toBe('att-2.txt')
    expect(safeDownloadName('a'.repeat(100), 'comm-3', 'text/markdown')).toBe('comm-3.md')
  })
})
