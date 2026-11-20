// The SDK pieces this page uses. The Kiro Crew host provides the real package at runtime
// through its import map (nothing is installed from npm); tests alias these two module names
// to ui/test/sdk-stub/. Anything not declared here must not be imported (the stub in ui/test/sdk-stub mirrors this file).
declare module '@kirocrew/app-sdk' {
  export interface AppApiError extends Error {
    name: 'AppApiError'
    status: number
    body: string
  }
  export interface AppApiClient {
    get<T = unknown>(path: string, init?: RequestInit): Promise<T>
    post<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
    put<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
  }
  export function useAppApi(): AppApiClient
  /** `active` is optional: treat undefined as true. */
  export function useAppInfo(): { name?: string; version?: string; active?: boolean }
  export function useNavBadge(): (count: number) => void
  /** Present on Kiro Crew 0.12.2. Read through a namespace import: an older host lacks it, and a
   *  named import of a missing export would fail the whole bundle. */
  export const useChatLauncher:
    | undefined
    | (() => { openChat(opts?: { slotKey?: string; message?: string; autoSend?: boolean }): void })
}

// The host's App Kit at Kiro Crew 0.12.2 (website/src/kirocrew-ui/index.ts). Only these names may be
// used, and only the base-list ones by name: ui/scripts/check-bundle.mjs enforces the named-import
// allowlist on the built bundle. Select and guideCaution are Level-1 additions, read through the
// namespace in src/components/kit.tsx, so they are typed as possibly undefined.
declare module '@kirocrew/app-sdk/ui' {
  import type { ButtonHTMLAttributes, ComponentPropsWithoutRef, ForwardRefExoticComponent, InputHTMLAttributes, ReactNode, RefAttributes } from 'react'
  export function PageHeader(props: { title: string; subtitle?: string; actions?: ReactNode }): JSX.Element
  export function Card(props: ComponentPropsWithoutRef<'div'>): JSX.Element
  export function CardTitle(props: ComponentPropsWithoutRef<'h3'>): JSX.Element
  /** `busy` keeps focus (aria-disabled) and swallows the click; `disabled` stays native. */
  export const Btn: ForwardRefExoticComponent<
    ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; danger?: boolean; busy?: boolean } & RefAttributes<HTMLButtonElement>
  >
  export const Input: ForwardRefExoticComponent<InputHTMLAttributes<HTMLInputElement> & RefAttributes<HTMLInputElement>>
  export function Badge(
    props: { variant: 'ok' | 'err' | 'warn' | 'aim' | 'muted'; children: ReactNode } & Omit<ComponentPropsWithoutRef<'span'>, 'children'>,
  ): JSX.Element
  export function Skeleton(props: ComponentPropsWithoutRef<'div'>): JSX.Element
  /** `subtitle` is a plain string: no markup inside it. */
  export function EmptyState(props: { icon: ReactNode; title: string; subtitle?: string; action?: ReactNode; testId?: string }): JSX.Element
  /** A `div role="switch"` named only by `label` (aria-label). */
  export function Toggle(props: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string; describedBy?: string; tone?: 'accent' | 'muted' }): JSX.Element
  /** Renders nothing for an empty `message`; `askAgent` defaults to false and stays off here. */
  export function ErrorNotice(props: {
    id?: string
    message?: string | null
    title?: string
    testId?: string
    announce?: boolean
    className?: string
    footer?: ReactNode
  }): JSX.Element | null
  /** Portals outside the page; focus moves to the first tabbable (the X) and back on close. */
  export function Modal(props: {
    open: boolean
    onClose: () => void
    title: ReactNode
    ariaLabel?: string
    footer?: ReactNode
    headerActions?: ReactNode
    maxWidth?: number
    guardAccidentalDismiss?: boolean
    dismissDisabled?: boolean
    children: ReactNode
  }): JSX.Element | null
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
  /** Level-1 addition: undefined on a host that lacks it. */
  export const Select: undefined | ((props: SelectProps) => JSX.Element)
  /** Level-1 addition: undefined on a host that lacks it. */
  export const guideCaution: undefined | { readonly 'data-guide-caution': '' }
}
