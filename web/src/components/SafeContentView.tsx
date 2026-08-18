// The React half of the safe-rendering boundary (design §9), and the document
// toolbar every captured body/attachment is read through.
//
// `SafeMarkdown` renders GFM with three hard rules wired into the component
// map itself, so no caller can forget them:
//
//   * images NEVER reach the DOM — every `![alt](src)` becomes a non-fetching
//     placeholder carrying its alt text, so nothing can fetch, whatever the
//     src was;
//   * anchors pass the http(s) allow-list a second time after `urlTransform`,
//     and a refused link renders as inert muted text;
//   * raw HTML is not re-hydrated — react-markdown without rehype-raw drops
//     HTML nodes from the tree, and no code path here uses
//     dangerouslySetInnerHTML.
//
// Over-budget documents are a NAMED STATE with the raw/download path next to
// it — never a silent excerpt, never something that looks complete.

import { useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy, Download } from 'lucide-react'
import {
  isMarkdownMediaType,
  markdownBudgetBreach,
  safeDownloadName,
  safeLinkHref,
  safeUrlTransform,
  MAX_MARKDOWN_RENDER_BYTES,
} from '../lib/safeMarkdown'

/** Compact byte formatting for sizes and budget notices. */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

type ExtraProps = { node?: unknown }

/**
 * The only anchor this feature renders. `href` arrives already passed through
 * `urlTransform`; it is re-checked here because the component map is the last
 * code to touch the value before the DOM does. A refused link keeps its text
 * (the author wrote it; the operator can read it) and loses the navigation.
 */
function SafeAnchor({ node: _node, href, children }: ExtraProps & { href?: string; children?: React.ReactNode }) {
  const safe = safeLinkHref(href)
  if (!safe) {
    return (
      <span
        data-blocked-link="true"
        title="Link not shown: only absolute http(s) links are rendered"
        className="text-gray-500 underline decoration-dotted cursor-text"
      >
        {children}
      </span>
    )
  }
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" className="text-emerald-400 hover:text-emerald-300 underline">
      {children}
    </a>
  )
}

/**
 * The only image this feature renders: a placeholder, not an element with a
 * `src`. There is no code path from author bytes to a fetch because the
 * element that could perform one is never created.
 */
function MdImage({ node: _node, alt }: ExtraProps & { src?: string; alt?: string }) {
  return (
    <span
      role="img"
      aria-label={alt || 'image'}
      data-testid="md-image-placeholder"
      title="Remote images are not loaded"
      className="inline-block px-1.5 py-0.5 my-0.5 rounded border border-dashed border-gray-600 text-[11px] text-gray-400"
    >
      [image: {alt?.trim() || 'no description'}]
    </span>
  )
}

function el<Tag extends keyof React.JSX.IntrinsicElements>(tag: Tag, className: string) {
  return function MdElement({ node: _node, children, ...rest }: ExtraProps & { children?: React.ReactNode }) {
    const Tag2 = tag as 'div'
    return <Tag2 className={className} {...(rest as object)}>{children}</Tag2>
  }
}

const MD_COMPONENTS: Components = {
  a: SafeAnchor,
  img: MdImage,
  h1: el('h1', 'text-base font-bold text-white mt-3 mb-1.5'),
  h2: el('h2', 'text-sm font-bold text-white mt-3 mb-1.5'),
  h3: el('h3', 'text-sm font-semibold text-gray-100 mt-2.5 mb-1'),
  h4: el('h4', 'text-xs font-semibold text-gray-200 mt-2 mb-1'),
  h5: el('h5', 'text-xs font-semibold text-gray-200 mt-2 mb-1'),
  h6: el('h6', 'text-xs font-semibold text-gray-300 mt-2 mb-1'),
  p: el('p', 'my-1.5 leading-relaxed'),
  ul: el('ul', 'list-disc pl-5 my-1.5 space-y-0.5'),
  ol: el('ol', 'list-decimal pl-5 my-1.5 space-y-0.5'),
  li: el('li', 'leading-relaxed'),
  blockquote: el('blockquote', 'border-l-2 border-gray-600 pl-3 my-2 text-gray-400 italic'),
  hr: el('hr', 'border-gray-700 my-3'),
  pre: el('pre', 'bg-gray-950 border border-gray-800 rounded p-2 my-2 overflow-x-auto text-[11px]'),
  code: el('code', 'bg-gray-950/80 rounded px-1 py-0.5 text-[11px] font-mono text-emerald-200/90 break-all'),
  table: el('table', 'border-collapse my-2 text-[11px]'),
  thead: el('thead', 'border-b border-gray-600'),
  tr: el('tr', 'border-b border-gray-800'),
  th: el('th', 'text-left font-semibold text-gray-200 px-2 py-1'),
  td: el('td', 'px-2 py-1 align-top'),
}

// `input` (GFM task-list checkbox) stays disabled and inert; react-markdown
// renders it without handlers, which is exactly the required posture.

/** GFM Markdown, rendered inert. Budgets are the caller's job (SafeContentView). */
export function SafeMarkdown({ content }: { content: string }) {
  return (
    <div data-testid="md-rendered" className="text-xs text-gray-300">
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrlTransform} components={MD_COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  )
}

/**
 * One captured document — a communication body or an attachment — with the
 * controls the design requires on every one of them: Rendered/Raw (Markdown
 * only), copy the exact content, download the captured bytes under a safe
 * name. Plain text never acquires guessed Markdown semantics; the toggle is
 * not even offered for it.
 */
export function SafeContentView({
  content,
  mediaType,
  downloadBase,
  displayName,
}: {
  content: string
  mediaType: string | null | undefined
  /** Base for the generated download name when the display name is unusable. */
  downloadBase: string
  /** The document's conductor-authored display name; sanitized before use. */
  displayName?: string | null
}) {
  const markdown = isMarkdownMediaType(mediaType)
  const [mode, setMode] = useState<'rendered' | 'raw'>('rendered')
  const [copied, setCopied] = useState(false)
  const breach = markdown && mode === 'rendered' ? markdownBudgetBreach(content) : null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // The text is on screen and selectable either way; do not claim success.
    }
  }

  const download = () => {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = safeDownloadName(displayName, downloadBase, mediaType)
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1.5 flex-wrap">
        {markdown && (
          <div role="group" aria-label="View mode" className="flex rounded overflow-hidden border border-gray-700">
            <button
              type="button"
              onClick={() => setMode('rendered')}
              aria-pressed={mode === 'rendered'}
              className={`px-2 py-1 text-[11px] min-h-[28px] ${mode === 'rendered' ? 'bg-gray-700 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Rendered
            </button>
            <button
              type="button"
              onClick={() => setMode('raw')}
              aria-pressed={mode === 'raw'}
              className={`px-2 py-1 text-[11px] min-h-[28px] ${mode === 'raw' ? 'bg-gray-700 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'}`}
            >
              Raw
            </button>
          </div>
        )}
        <button
          type="button"
          onClick={copy}
          data-testid="content-copy"
          className="inline-flex items-center gap-1 px-2 py-1 min-h-[28px] rounded text-[11px] text-gray-300 bg-gray-800 hover:bg-gray-700 hover:text-white transition-colors"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button
          type="button"
          onClick={download}
          data-testid="content-download"
          className="inline-flex items-center gap-1 px-2 py-1 min-h-[28px] rounded text-[11px] text-gray-300 bg-gray-800 hover:bg-gray-700 hover:text-white transition-colors"
        >
          <Download size={12} />
          Download
        </button>
      </div>

      {breach ? (
        <div
          data-testid="markdown-render-budget"
          data-breach={breach}
          role="note"
          className="rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-200"
        >
          {breach === 'bytes'
            ? `Too large to render (${formatBytes(new TextEncoder().encode(content).length)} exceeds the ${formatBytes(MAX_MARKDOWN_RENDER_BYTES)} Markdown budget). Switch to Raw or download the document instead.`
            : 'Too complex to render safely (too many Markdown elements). Switch to Raw or download the document instead.'}
        </div>
      ) : markdown && mode === 'rendered' ? (
        <SafeMarkdown content={content} />
      ) : (
        <pre
          data-testid="content-raw"
          className="whitespace-pre-wrap break-words font-mono text-[11px] text-gray-300 bg-gray-950/50 rounded p-2 max-h-[50vh] overflow-y-auto"
        >
          {content}
        </pre>
      )}
    </div>
  )
}
