/**
 * Bearer token for an auth-enabled cao-server, held for this browser tab.
 *
 * The server can run with authentication on (an IdP, or a standalone
 * `CAO_AUTH_LOCAL_TOKEN`). Every scope-gated REST route then wants
 * `Authorization: Bearer <token>`, the terminal WebSocket handshake wants the
 * same token as `?token=` (a browser cannot set headers on a WebSocket), and
 * the workflow event stream wants `?access_token=`. Until now this bundle
 * sent none of them and got 401s (#807).
 *
 * Where the token comes from, in order:
 *   1. the URL fragment on load — open `http://host:port/#token=<token>` and
 *      `captureTokenFromFragment()` moves it into sessionStorage and strips it
 *      from the address bar, so it is never sent to the server as part of a
 *      URL, never lands in a bookmark, and is gone when the tab closes;
 *   2. the Settings page, which writes the same sessionStorage key.
 *
 * sessionStorage, not localStorage: scoped to the tab, cleared on close, not
 * shared with other tabs of the same origin. With no token stored every call
 * is byte-for-byte what it was, so the default no-auth deployment is unchanged.
 */

const STORAGE_KEY = 'cao.auth.token'

/** The stored token, or null. Never throws (storage may be unavailable). */
export function getToken(): string | null {
  try {
    const value = sessionStorage.getItem(STORAGE_KEY)
    return value && value.trim() ? value.trim() : null
  } catch {
    return null
  }
}

/**
 * Store (or, with null / empty, clear) the token for this tab.
 *
 * Returns true only when storage actually holds the requested state afterwards.
 * `sessionStorage` can throw (`SecurityError` in a locked-down context,
 * `QuotaExceededError`) or silently no-op; a caller that reported success on a
 * failed write would leave requests anonymous while telling the user a token
 * is set, so the result is read back and compared.
 */
export function setToken(token: string | null): boolean {
  const wanted = token && token.trim() ? token.trim() : null
  try {
    if (wanted) sessionStorage.setItem(STORAGE_KEY, wanted)
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    return false
  }
  return getToken() === wanted
}

/**
 * Move a `#token=...` fragment into sessionStorage and strip it from the URL.
 *
 * Call once, before the app renders. Other fragment parameters are kept.
 * Returns true when a token was captured AND stored; if storage refused it the
 * fragment is left in place so the failure is visible rather than silent.
 */
export function captureTokenFromFragment(): boolean {
  const hash = location.hash
  if (!hash || hash.length < 2) return false
  const params = new URLSearchParams(hash.slice(1))
  const token = params.get('token')
  if (!token || !token.trim()) return false
  if (!setToken(token)) return false
  params.delete('token')
  const rest = params.toString()
  history.replaceState(null, '', `${location.pathname}${location.search}${rest ? `#${rest}` : ''}`)
  return true
}

/**
 * `headers` with `Authorization: Bearer <token>` added when a token is stored.
 *
 * Returns the input untouched when there is no token, so a no-auth deployment
 * sends exactly the headers it always did. Any header shape `fetch` accepts
 * is normalised to a plain record.
 */
export function withAuth(headers?: HeadersInit): HeadersInit | undefined {
  const token = getToken()
  if (!token) return headers
  const merged = new Headers(headers)
  merged.set('Authorization', `Bearer ${token}`)
  return Object.fromEntries(merged.entries())
}

/** `?name=<token>` (or `&name=` when `existing` already has a query) — empty without a token. */
export function tokenQuery(name: 'token' | 'access_token', hasQuery: boolean): string {
  const token = getToken()
  if (!token) return ''
  return `${hasQuery ? '&' : '?'}${name}=${encodeURIComponent(token)}`
}
