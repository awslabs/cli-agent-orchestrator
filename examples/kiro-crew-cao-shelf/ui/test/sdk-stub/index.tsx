// Test double for '@kirocrew/app-sdk'. Behaves like the real scoped client where it matters:
// only declared paths are allowed, failures are Errors named AppApiError with status and body.
const DECLARED = ['/apps/cao-shelf/api']

export const stubState: {
  active: boolean | undefined
  badge: number[]
  permissionErrors: string[]
  opened: Array<{ slotKey?: string }>
} = {
  active: undefined,
  badge: [],
  permissionErrors: [],
  opened: [],
}

const launcher = { openChat: (opts: { slotKey?: string } = {}) => void stubState.opened.push(opts) }
const realLauncher = () => launcher

/** Like the real host's hook; `withoutChatLauncher()` makes it absent, as on an older host. */
export let useChatLauncher: (() => typeof launcher) | undefined = realLauncher

export function withoutChatLauncher(): void {
  useChatLauncher = undefined
}

export function resetStub(): void {
  stubState.active = undefined
  stubState.badge = []
  stubState.permissionErrors = []
  stubState.opened = []
  useChatLauncher = realLauncher
}

async function call<T>(method: string, path: string, body: unknown, init?: RequestInit): Promise<T> {
  if (!DECLARED.some((d) => path === d || path.startsWith(d + '/') || path.startsWith(d + '?'))) {
    stubState.permissionErrors.push(path)
    throw new Error(`AppApiPermissionError: ${path} not declared`)
  }
  const res = await globalThis.fetch(path, {
    ...init,
    method,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) {
    const e = new Error(`API ${res.status}: ${text}`) as Error & { status: number; body: string }
    e.name = 'AppApiError'
    e.status = res.status
    e.body = text
    throw e
  }
  return (text ? JSON.parse(text) : undefined) as T
}

const client = {
  get: <T,>(p: string, i?: RequestInit) => call<T>('GET', p, undefined, i),
  post: <T,>(p: string, b?: unknown, i?: RequestInit) => call<T>('POST', p, b, i),
  put: <T,>(p: string, b?: unknown, i?: RequestInit) => call<T>('PUT', p, b, i),
}

export function useAppApi() {
  return client
}
export function useAppInfo() {
  return { name: 'cao-shelf', version: '0.1.0', active: stubState.active }
}
const setBadge = (n: number) => {
  stubState.badge.push(n)
}
export function useNavBadge() {
  return setBadge
}
