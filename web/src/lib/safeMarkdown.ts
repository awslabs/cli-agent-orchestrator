// Safe rendering policy for captured communication content (design §9).
//
// Captured bodies and attachments are UNTRUSTED TEXT: authored by agents,
// stored and hashed faithfully, and never executed or interpreted. This module
// is the pure-policy half of the one genuine security boundary in the feature
// (the React half lives in components/SafeContentView.tsx):
//
//   * raw HTML is never re-hydrated (no rehype-raw, no
//     dangerouslySetInnerHTML anywhere in the feature);
//   * links are allow-listed to absolute http(s) — javascript:, data:, file:,
//     vbscript:, custom schemes, protocol-relative //host, and relative
//     filesystem navigation are all refused;
//   * images are replaced with a non-fetching placeholder;
//   * byte and node budgets make an over-budget document FAIL VISIBLY rather
//     than silently truncate. They bound the SIZE of the content admitted and
//     the NODE COUNT of the tree handed to the renderer — they do NOT bound
//     parse time. A marker-dense document under the byte budget can still
//     spend a long time inside the counting parse before the node rail trips
//     (measured on this build: 512 KiB of dense emphasis ≈ 62 s), so a
//     no-hang property is NOT claimed. The byte ceiling is an owner decision
//     (design §13.3), revisited from measured usage rather than loosened here.

import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'

/**
 * Design §13's Markdown-render budget. Documents larger than this are still
 * viewable raw and downloadable; they are simply never parsed as Markdown.
 */
export const MAX_MARKDOWN_RENDER_BYTES = 512 * 1024

/**
 * A node-count rail for pathological input under the byte budget (deeply
 * nested or marker-dense documents that reconcile far more elements than
 * their byte count suggests). A rail, not a tuning knob: exceeding it fails
 * visibly, exactly like the byte budget.
 */
export const MAX_MARKDOWN_NODES = 10_000

// ASCII whitespace plus every C0 control below it, so a scheme cannot be
// smuggled past the prefix check with an embedded tab or newline.
const WHITESPACE_AND_CONTROLS = /[\u0000-\u0020]+/g

// C0/C1 controls and DEL — never legal in a filename.
const FILENAME_CONTROLS = /[\u0000-\u001f\u007f-\u009f]+/g

/**
 * The href allow-list, applied twice at render time (react-markdown's
 * `urlTransform`, then the anchor component again).
 *
 * The CHECK runs on a compacted copy with ASCII whitespace/control characters
 * removed; the VALUE returned is the original, trimmed — a value that passed
 * the compacted prefix test still begins with http(s) after a browser's own
 * control-character stripping. Anything else — including protocol-relative
 * `//host` and relative paths — returns null, which the renderer draws as
 * inert text.
 */
export function safeLinkHref(raw: string | null | undefined): string | null {
  if (!raw) return null
  const trimmed = raw.trim()
  const compact = trimmed.replace(WHITESPACE_AND_CONTROLS, '')
  const lower = compact.toLowerCase()
  if (lower.startsWith('https://') || lower.startsWith('http://')) return trimmed
  return null
}

/** react-markdown's `urlTransform` hook: the allow-list, and nothing else. */
export function safeUrlTransform(url: string): string {
  return safeLinkHref(url) ?? ''
}

/**
 * Rendered-vs-raw is decided by the document's DECLARED media type, never by
 * sniffing the body: a plain-text report full of Markdown-looking characters
 * must render literally.
 */
export function isMarkdownMediaType(mediaType: string | null | undefined): boolean {
  if (!mediaType) return false
  return mediaType.split(';', 1)[0].trim().toLowerCase() === 'text/markdown'
}

/** Which budget refused the render, or null when rendering is within budget. */
export type MarkdownBudgetBreach = 'bytes' | 'nodes'

// One frozen processor for the counting pass. `.parse` applies remark-gfm's
// micromark/mdast extensions, so the counted tree is the tree react-markdown
// would render; the async `run` transforms are not needed to count nodes.
const countingProcessor = unified().use(remarkParse).use(remarkGfm)

/**
 * The budget check, run BEFORE react-markdown sees the content.
 *
 * A parse failure here is reported as a breach: the alternative is handing
 * the same input to the renderer and hoping its failure mode is prettier.
 * Failing visibly is the design's whole point (§10).
 */
export function markdownBudgetBreach(content: string): MarkdownBudgetBreach | null {
  if (new TextEncoder().encode(content).length > MAX_MARKDOWN_RENDER_BYTES) return 'bytes'
  try {
    const tree = countingProcessor.parse(content)
    let nodes = 0
    const walk = (node: { children?: unknown[] }) => {
      nodes += 1
      if (nodes > MAX_MARKDOWN_NODES) return
      if (Array.isArray(node.children)) {
        for (const child of node.children) walk(child as { children?: unknown[] })
      }
    }
    walk(tree as unknown as { children?: unknown[] })
    return nodes > MAX_MARKDOWN_NODES ? 'nodes' : null
  } catch {
    return 'nodes'
  }
}

/**
 * The filename a download may carry. `display_name` is conductor-authored
 * metadata, never a path: separators, control characters, and dotfile forms
 * are stripped, and an empty or over-long result falls back to a generated
 * name whose extension matches the declared media type.
 */
export function safeDownloadName(
  displayName: string | null | undefined,
  fallbackBase: string,
  mediaType: string | null | undefined,
): string {
  const stripped = (displayName ?? '')
    .split(/[\\/]/)
    .pop()!
    .replace(FILENAME_CONTROLS, '')
    .trim()
    .replace(/^\.+/, '')
  const ext = isMarkdownMediaType(mediaType) ? '.md' : '.txt'
  if (!stripped) return `${fallbackBase}${ext}`
  return stripped.length > 64 ? `${fallbackBase}${ext}` : stripped
}
