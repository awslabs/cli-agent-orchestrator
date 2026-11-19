// Test double for '@kirocrew/app-sdk/ui', shared by vitest (vitest.config.ts alias) and `make dev`
// (e2e/rig/dev_host/app-sdk-ui.mjs re-exports this file). It copies the DOM contract of the Kiro
// Crew host's components at v0.12.2 (website/src): roles, ARIA attributes, test ids and the
// variant classes, not their code or look. `data-kit` names the component for the page's tests;
// the host does not render it. sdk-stub.test.tsx pins every export against the host source.
import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  type ButtonHTMLAttributes,
  type ComponentPropsWithoutRef,
  type InputHTMLAttributes,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

const cx = (...parts: Array<string | false | null | undefined>) => parts.filter(Boolean).join(' ')

/** components/ui.tsx:12-40 */
export function Card({ children, className, ...rest }: Omit<ComponentPropsWithoutRef<'div'>, 'dangerouslySetInnerHTML'>) {
  return (
    <div data-kit="Card" className={cx('card-glow border border-border bg-card rounded-lg mb-4 shadow-sm', className)} {...rest}>
      {children}
    </div>
  )
}

/** components/ui.tsx:42-45 */
export function CardTitle({ children, className, ...rest }: Omit<ComponentPropsWithoutRef<'h3'>, 'dangerouslySetInnerHTML'>) {
  return (
    <h3 className={cx('text-sm font-semibold text-text-strong', className)} {...rest}>
      {children}
    </h3>
  )
}

export type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; danger?: boolean; busy?: boolean }

/** components/ui.tsx:82-110. `busy` keeps focus (aria-disabled, not disabled) and swallows the
 *  click; primary wins over danger; no default `type`, as on the host. */
export const Btn = forwardRef<HTMLButtonElement, BtnProps>(function Btn({ children, primary, danger, busy, className, onClick, ...rest }, ref) {
  const variant = primary
    ? 'busy-cue-primary bg-accent text-accent-fg border-accent'
    : danger
      ? 'busy-cue-danger border-border bg-transparent text-danger'
      : 'border-border bg-transparent text-text'
  return (
    <button
      ref={ref}
      data-kit="Btn"
      aria-disabled={busy || undefined}
      aria-busy={busy || undefined}
      data-busy={busy || undefined}
      onClick={(e) => {
        if (busy) {
          e.preventDefault()
          return
        }
        onClick?.(e)
      }}
      className={cx('busy-cue inline-flex items-center rounded-md border text-[13px]', variant, className)}
      {...rest}
    >
      {children}
    </button>
  )
})

/** components/ui.tsx:196 */
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...props }, ref) {
  return <input ref={ref} data-kit="Input" className={cx('bg-bg-elevated border border-border rounded-md text-text text-sm', className)} {...props} />
})

const BADGE: Record<'ok' | 'err' | 'warn' | 'aim' | 'muted', string> = {
  ok: 'bg-ok-subtle text-ok-text',
  err: 'bg-danger-subtle text-danger-text',
  warn: 'bg-warn-subtle text-warn-text',
  aim: 'bg-aim-subtle text-aim',
  muted: 'bg-[var(--bg-hover)] text-[var(--muted)]',
}

/** components/ui.tsx:218-233 */
export function Badge({
  variant,
  children,
  className,
  ...rest
}: { variant: keyof typeof BADGE; children: ReactNode } & Omit<ComponentPropsWithoutRef<'span'>, 'children' | 'dangerouslySetInnerHTML'>) {
  return (
    <span data-kit="Badge" className={cx('inline-flex items-center rounded-full text-[13px] font-mono whitespace-nowrap', BADGE[variant], className)} {...rest}>
      {children}
    </span>
  )
}

/** components/ui.tsx:285 */
export function Skeleton({ className, ...props }: ComponentPropsWithoutRef<'div'>) {
  return <div data-slot="skeleton" data-kit="Skeleton" className={cx('bg-bg-hover animate-pulse rounded-md', className)} {...props} />
}

/** components/ui.tsx:367 */
export function EmptyState({ icon, title, subtitle, action, testId = 'empty-state' }: { icon: ReactNode; title: string; subtitle?: string; action?: ReactNode; testId?: string }) {
  return (
    <div data-kit="EmptyState" className="flex flex-col items-center justify-center py-12 gap-2" data-testid={testId}>
      <div className="text-[40px] opacity-[.12] select-none">{icon}</div>
      <div className="text-muted text-sm font-medium" data-testid={`${testId}-title`}>
        {title}
      </div>
      {subtitle && (
        <div className="text-muted text-[13px] text-center" data-testid={`${testId}-subtitle`}>
          {subtitle}
        </div>
      )}
      {action && (
        <div className="mt-2" data-testid={`${testId}-action`}>
          {action}
        </div>
      )}
    </div>
  )
}

/** components/ui.tsx:449 */
export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2 px-4 md:px-6 pt-2 pb-3" data-testid="page-header">
      <div className="min-w-0">
        <div className="text-2xl font-bold tracking-tight text-text-strong" data-testid="page-title">
          {title}
        </div>
        {subtitle && (
          <div className="text-muted text-sm mt-1" data-testid="page-subtitle">
            {subtitle}
          </div>
        )}
      </div>
      {actions && <div className="flex items-center gap-2.5 flex-wrap justify-end">{actions}</div>}
    </div>
  )
}

/** components/ui.tsx:474-505 */
export function Toggle({ checked, onChange, disabled, label, describedBy }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string; describedBy?: string; tone?: 'accent' | 'muted' }) {
  return (
    <div
      role="switch"
      data-kit="Toggle"
      aria-checked={checked}
      aria-disabled={disabled || undefined}
      aria-label={label}
      aria-describedby={describedBy}
      tabIndex={disabled ? -1 : 0}
      onClick={() => {
        if (!disabled) onChange(!checked)
      }}
      onKeyDown={(e) => {
        if (!disabled && (e.key === ' ' || e.key === 'Enter')) {
          e.preventDefault()
          onChange(!checked)
        }
      }}
      className={cx('w-9 h-5 rounded-full relative shrink-0 cursor-pointer', disabled && 'opacity-40', checked ? 'bg-accent' : 'bg-border')}
    >
      <div className="absolute top-0.5 w-4 h-4 rounded-full" />
    </div>
  )
}

/** components/ErrorNotice.tsx, the block variant. The Ask-the-agent hand-off is host plumbing the
 *  page keeps off (askAgent defaults to false) and is not rendered here. */
export function ErrorNotice({
  id,
  message,
  title,
  testId,
  announce = true,
  className,
  footer,
}: {
  id?: string
  message?: string | null
  title?: string
  testId?: string
  announce?: boolean
  className?: string
  footer?: ReactNode
  askAgent?: boolean
}) {
  if (!message) return null
  return (
    <div
      data-kit="ErrorNotice"
      role={announce ? 'alert' : undefined}
      id={id}
      data-testid={testId}
      className={cx('rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 flex items-start gap-2 text-[13px] text-danger-text', className)}
    >
      <div className="min-w-0 flex-1 whitespace-pre-wrap">
        {title && <strong className="font-semibold">{title} </strong>}
        {message}
        {footer && <div className="mt-1 font-normal">{footer}</div>}
      </div>
    </div>
  )
}

/** hooks/useDialogFocusTrap.ts:12-14. The host also drops elements with no `offsetParent` (hidden
 *  ones); jsdom lays nothing out, so that filter is left out here. */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

const tabbables = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.tabIndex >= 0)

type ModalProps = {
  open: boolean
  onClose: () => void
  title: ReactNode
  ariaLabel?: string
  footer?: ReactNode
  headerActions?: ReactNode
  maxWidth?: number
  guardAccidentalDismiss?: boolean
  dismissDisabled?: boolean
  children?: ReactNode
}

/** Mounted only while open, like the host's ModalDialog: focus moves in on mount (in a passive
 *  effect that runs after the children's, so it wins over a child's own focus call) and back to
 *  the opener on unmount; Tab wraps inside. */
function ModalDialog({ onClose, title, ariaLabel, footer, headerActions, maxWidth = 640, dismissDisabled = false, children }: Omit<ModalProps, 'open' | 'guardAccidentalDismiss'>) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = `${useId()}-title`
  const dismiss = useCallback(() => {
    if (!dismissDisabled) onClose()
  }, [dismissDisabled, onClose])

  useEffect(() => {
    const restoreTo = document.activeElement as HTMLElement | null
    const root = ref.current
    if (root) (tabbables(root)[0] ?? root).focus({ preventScroll: true })
    return () => restoreTo?.focus?.({ preventScroll: true })
  }, [])

  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const root = ref.current
      if (e.key !== 'Tab' || !root) return
      const items = tabbables(root)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      const forward = !e.shiftKey && active === last
      const backward = e.shiftKey && (active === first || active === root)
      const outside = !root.contains(active)
      if (!forward && !backward && !outside) return
      e.preventDefault()
      ;(backward ? last : first).focus()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  return (
    <div
      ref={ref}
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel ? undefined : titleId}
      tabIndex={-1}
      data-kit="Modal"
      className="bg-card border border-border rounded-xl w-full flex flex-col pointer-events-auto"
      style={{ maxWidth, maxHeight: '90vh' }}
    >
      <div className="flex items-center justify-between gap-3 px-5 min-h-12 py-2 border-b border-border">
        <span id={titleId} className="text-base font-semibold text-text-strong truncate">
          {title}
        </span>
        <div className="flex items-center gap-1.5 shrink-0">
          {headerActions}
          <button aria-label="Close" className="p-1.5 rounded-md text-muted" disabled={dismissDisabled} onClick={dismiss}>
            <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>
      {children != null && children !== false && children !== '' && <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4">{children}</div>}
      {footer && <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-end gap-2">{footer}</div>}
    </div>
  )
}

/** components/Modal.tsx:213-265. Escape (bubble phase on window, skipped when a nested control
 *  consumed it) and a backdrop click are the accidental dismissals: refused while
 *  `guardAccidentalDismiss` or `dismissDisabled`. The X is refused only while `dismissDisabled`. */
export function Modal({ open, onClose, guardAccidentalDismiss = false, dismissDisabled = false, ...rest }: ModalProps) {
  const softDismiss = useCallback(() => {
    if (!guardAccidentalDismiss && !dismissDisabled) onClose()
  }, [guardAccidentalDismiss, dismissDisabled, onClose])

  useLayoutEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) softDismiss()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, softDismiss])

  if (!open) return null
  return createPortal(
    <>
      <div data-modal-backdrop="" className="fixed inset-0 bg-bg/60 z-[100]" onClick={softDismiss} />
      <div className="fixed inset-0 z-[101] flex items-center justify-center p-safe-or-8 pointer-events-none">
        <ModalDialog onClose={onClose} dismissDisabled={dismissDisabled} {...rest} />
      </div>
    </>,
    document.body,
  )
}

export interface SelectProps {
  options: string[]
  optionLabels?: string[]
  value: string
  onChange: (value: string) => void
  clearLabel?: string
  disabled?: boolean
  'aria-label'?: string
  title?: string
  className?: string
}

/** kirocrew-ui/Select.tsx:57-102, rendered the way the host renders it on touch
 *  (components/SimpleSelect.tsx:125): a native list. The desktop Radix popup is covered by e2e. */
export function Select({ options, optionLabels, value, onChange, clearLabel, disabled, 'aria-label': ariaLabel, title, className }: SelectProps) {
  const selectable = options.includes(value) || (value === '' && clearLabel !== undefined)
  return (
    <select data-kit="Select" aria-label={ariaLabel} title={title} disabled={disabled} value={value} onChange={(e) => onChange(e.target.value)} className={className}>
      {!selectable && <option value={value}>{value}</option>}
      {clearLabel !== undefined && <option value="">{clearLabel}</option>}
      {options.map((o, i) => (
        <option key={o} value={o}>
          {optionLabels?.[i] ?? o}
        </option>
      ))}
    </select>
  )
}

/** guide/trustRoot.ts:39 */
export const guideCaution = { 'data-guide-caution': '' } as const
