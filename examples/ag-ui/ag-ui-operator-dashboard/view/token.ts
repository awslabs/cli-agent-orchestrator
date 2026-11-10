/**
 * Which server the dashboard talks to, and whether it may send the stored token there.
 *
 * `?server=` must be an http or https URL; anything else is refused and the
 * page shows why instead of connecting. The token in `sessionStorage.caoToken`
 * is attached on its own only when the server is on this machine
 * (`localhost`, `127.0.0.1` or `[::1]`). For any other server, the operator must
 * confirm first, so a crafted `?server=` link cannot send the token somewhere
 * else unnoticed.
 */

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);
export const DEFAULT_SERVER = "http://127.0.0.1:9889";

/** The parsed server URL, or `null` unless it is http(s). */
export function parseServer(server: string): URL | null {
  try {
    const url = new URL(server);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

export function isLoopback(server: string): boolean {
  const url = parseServer(server);
  return url !== null && LOOPBACK.has(url.hostname);
}

export type TokenChoice =
  | { ok: true; server: string; token?: string; note: string }
  | { ok: false; server: string; note: string };

export function chooseToken(
  search: string,
  storage: Pick<Storage, "getItem">,
  confirm: (message: string) => boolean,
): TokenChoice {
  const server = new URLSearchParams(search).get("server") ?? DEFAULT_SERVER;
  const url = parseServer(server);
  if (!url) return { ok: false, server, note: "the server URL is not valid: it must be an http or https URL" };
  const stored = storage.getItem("caoToken") ?? undefined;
  if (!stored) return { ok: true, server, note: "" };
  if (LOOPBACK.has(url.hostname)) return { ok: true, server, token: stored, note: "" };
  const ok = confirm(`Send your stored CAO token to ${url.origin}? It is not on this machine.`);
  return ok ? { ok: true, server, token: stored, note: "" } : { ok: true, server, note: `token not sent to ${url.origin}` };
}
