import { RefObject, useEffect, useMemo, useRef, useState } from 'react'
import { Check, Clipboard, Loader2, Maximize2, Minimize2, WrapText, X } from 'lucide-react'
import { WorkflowSpec } from '../../api'
import { PythonToken, PythonTokenType, tokenizePython } from './pythonHighlight'

interface WorkflowSpecViewerProps {
  open: boolean
  workflowName: string
  spec: WorkflowSpec | null
  loading: boolean
  error: string | null
  onClose: () => void
  returnFocusRef: RefObject<HTMLButtonElement | null>
}

const TOKEN_CLASS: Record<PythonTokenType, string> = {
  keyword: 'text-fuchsia-300',
  builtin: 'text-cyan-300',
  string: 'text-emerald-300',
  comment: 'text-gray-500 italic',
  number: 'text-amber-300',
  decorator: 'text-sky-300',
  definition: 'text-blue-300',
  operator: 'text-gray-400',
  plain: 'text-gray-200',
}

const shortHash = (hash: string) => hash.length > 12 ? hash.slice(0, 12) : hash

/** Split tokens at newlines without changing their text within each source line. */
export function tokensByLine(tokens: PythonToken[]): PythonToken[][] {
  const lines: PythonToken[][] = [[]]
  for (const token of tokens) {
    let start = 0
    let newline = token.text.indexOf('\n')
    while (newline !== -1) {
      if (newline > start) lines[lines.length - 1].push({ ...token, text: token.text.slice(start, newline) })
      lines.push([])
      start = newline + 1
      newline = token.text.indexOf('\n', start)
    }
    if (start < token.text.length) lines[lines.length - 1].push({ ...token, text: token.text.slice(start) })
  }
  return lines
}

export function WorkflowSpecViewer({
  open, workflowName, spec, loading, error, onClose, returnFocusRef,
}: WorkflowSpecViewerProps) {
  const exitRef = useRef<HTMLButtonElement>(null)
  const viewerRef = useRef<HTMLDivElement>(null)
  const codeScrollRef = useRef<HTMLDivElement>(null)
  const lineRefs = useRef<(HTMLDivElement | null)[]>([])
  const [wrap, setWrap] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [markedLine, setMarkedLine] = useState<number | null>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const fullscreenEnabled = typeof document !== 'undefined' && document.fullscreenEnabled

  const sourceIsString = typeof spec?.source === 'string'
  const source = sourceIsString ? spec.source as string : spec ? JSON.stringify(spec, null, 2) : ''
  const isPython = sourceIsString && (!spec?.path || /\.py$/i.test(spec.path))
  const tokens = useMemo(
    () => isPython ? tokenizePython(source) : [{ type: 'plain' as const, text: source }],
    [isPython, source],
  )
  const lines = useMemo(() => tokensByLine(tokens), [tokens])

  useEffect(() => {
    if (!open) return
    const oldOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    exitRef.current?.focus()
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !document.fullscreenElement) onCloseRef.current()
      if (event.key === 'Tab' && viewerRef.current) {
        const focusable = Array.from(viewerRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )).filter(element => element.getAttribute('aria-hidden') !== 'true')
        if (!focusable.length) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (event.shiftKey && (document.activeElement === first || !viewerRef.current.contains(document.activeElement))) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && (document.activeElement === last || !viewerRef.current.contains(document.activeElement))) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    const fullscreenChange = () => setFullscreen(!!document.fullscreenElement)
    window.addEventListener('keydown', keydown)
    document.addEventListener('fullscreenchange', fullscreenChange)
    return () => {
      document.body.style.overflow = oldOverflow
      window.removeEventListener('keydown', keydown)
      document.removeEventListener('fullscreenchange', fullscreenChange)
      returnFocusRef.current?.focus()
    }
  }, [open, returnFocusRef])

  useEffect(() => {
    if (open) {
      setWrap(false)
      setMarkedLine(null)
      setCopied(false)
    }
  }, [open, workflowName])

  if (!open) return null

  const toggleFullscreen = async () => {
    if (document.fullscreenElement) await document.exitFullscreen()
    else await viewerRef.current?.requestFullscreen()
  }
  const copyHash = async () => {
    if (!spec?.content_hash || !navigator.clipboard?.writeText) return
    try {
      await navigator.clipboard.writeText(spec.content_hash)
      setCopied(true)
    } catch {
      // Clipboard access can be denied by browser permissions. Keep the copy
      // control available, but do not claim success.
    }
  }
  const goToLine = (line: number) => {
    const safeLine = Math.max(1, Math.min(lines.length, line))
    setMarkedLine(safeLine)
    lineRefs.current[safeLine - 1]?.scrollIntoView({ block: 'center' })
  }

  return (
    <div className="fixed inset-0 z-60 flex items-center justify-center" role="presentation">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-xs" />
      <div
        ref={viewerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="workflow-spec-title"
        className="relative flex h-[94vh] w-[94vw] flex-col overflow-hidden rounded-2xl border border-gray-700/60 bg-gray-900 shadow-2xl [&:fullscreen]:h-screen [&:fullscreen]:w-screen [&:fullscreen]:rounded-none"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-gray-800 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="workflow-spec-title" className="truncate text-base font-semibold text-white">
              {workflowName}
            </h2>
            {spec?.path && (
              <p className="truncate font-mono text-xs text-gray-500" title={spec.path}>{spec.path}</p>
            )}
          </div>
          {spec?.content_hash && (
            <div className="hidden items-center gap-1 rounded-lg bg-gray-950 px-2 py-1 font-mono text-xs text-gray-400 sm:flex">
              <span title={spec.content_hash}>{shortHash(spec.content_hash)}</span>
              <button type="button" onClick={copyHash} aria-label="Copy content hash" className="p-1 hover:text-white">
                {copied ? <Check size={14} className="text-emerald-400" aria-hidden="true" /> : <Clipboard size={14} aria-hidden="true" />}
              </button>
            </div>
          )}
          {fullscreenEnabled && (
            <button
              type="button"
              onClick={toggleFullscreen}
              className="inline-flex items-center gap-1.5 rounded-lg border border-gray-700 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
            >
              {fullscreen ? <Minimize2 size={15} aria-hidden="true" /> : <Maximize2 size={15} aria-hidden="true" />}
              {fullscreen ? 'Leave full screen' : 'Full screen'}
            </button>
          )}
          <button
            ref={exitRef}
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1.5 rounded-lg bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-700"
          >
            <X size={16} aria-hidden="true" /> Exit
          </button>
        </header>

        {loading && (
          <div className="flex flex-1 items-center justify-center gap-2 text-sm text-gray-400" role="status">
            <Loader2 size={18} className="animate-spin" aria-hidden="true" /> Loading workflow spec…
          </div>
        )}
        {!loading && error && (
          <div className="m-4 rounded-lg border border-red-700/50 bg-red-950/30 p-4 text-sm text-red-300" role="alert">
            {error}
          </div>
        )}
        {!loading && !error && spec && (
          <>
            {(spec.findings ?? []).length > 0 && (
              <div className="flex shrink-0 gap-2 overflow-x-auto border-b border-gray-800 px-4 py-2" aria-label="Workflow findings">
                {(spec.findings ?? []).map((finding, index) => (
                  <button
                    key={`${finding.line}-${finding.rule_id}-${index}`}
                    type="button"
                    onClick={() => goToLine(finding.line)}
                    className="shrink-0 rounded-lg border border-amber-800/60 bg-amber-950/30 px-2.5 py-1 text-left font-mono text-xs text-amber-200 hover:bg-amber-900/40"
                  >
                    line {finding.line} [{finding.severity}] {finding.rule_id}: {finding.message}
                  </button>
                ))}
              </div>
            )}
            <div className="flex shrink-0 justify-end border-b border-gray-800 px-4 py-2">
              <button
                type="button"
                aria-pressed={wrap}
                onClick={() => setWrap(value => !value)}
                className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              >
                <WrapText size={14} aria-hidden="true" /> Wrap lines
              </button>
            </div>
            <div
              ref={codeScrollRef}
              aria-label="Workflow spec source"
              className="relative min-h-0 flex-1 overflow-auto bg-[#080b10]"
              data-testid="spec-code-scroll"
            >
              <div className={`relative min-h-full py-4 ${wrap ? 'w-full' : 'min-w-max'}`}>
                {lines.map((lineTokens, lineIndex) => {
                  const lineNumber = lineIndex + 1
                  return (
                    <div
                      key={lineNumber}
                      ref={element => { lineRefs.current[lineIndex] = element }}
                      data-testid={`spec-line-${lineNumber}`}
                      className={`flex min-h-[21px] min-w-full font-mono text-[13px] leading-[21px] ${
                        markedLine === lineNumber ? 'bg-amber-500/10 ring-1 ring-inset ring-amber-600/30' : ''
                      }`}
                    >
                      <span
                        data-testid={`spec-line-number-${lineNumber}`}
                        aria-hidden="true"
                        className="sticky left-0 z-20 w-14 shrink-0 select-none border-r border-gray-800 bg-[#080b10] px-3 text-right text-gray-600"
                      >
                        {lineNumber}
                      </span>
                      <span
                        data-testid={`spec-line-code-${lineNumber}`}
                        className={`min-w-0 flex-1 px-4 text-gray-200 ${wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'}`}
                      >
                        {lineTokens.map((token, tokenIndex) => (
                          <span key={tokenIndex} className={isPython ? TOKEN_CLASS[token.type] : undefined}>{token.text}</span>
                        ))}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
