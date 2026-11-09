/**
 * The only URLs this app requests. A test asserts that no other path is ever
 * fetched, which is what keeps the app off every write route except the
 * run plane's resume and off every route that returns terminal output.
 */

export const ROUTES = {
  stream: "/agui/v1/stream",
  run: "/agui/v1/run",
} as const;

export type RouteName = keyof typeof ROUTES;

const ALLOWED_PATHS: ReadonlySet<string> = new Set(Object.values(ROUTES));

/** Join a base URL and a route, refusing anything outside {@link ROUTES}. */
export function urlFor(base: string, route: RouteName, query?: Record<string, string>): string {
  const url = new URL(ROUTES[route], base.endsWith("/") ? base : `${base}/`);
  for (const [key, value] of Object.entries(query ?? {})) {
    url.searchParams.set(key, value);
  }
  assertAllowed(url.toString());
  return url.toString();
}

/** Throw unless `url`'s path is one of the allowed routes. */
export function assertAllowed(url: string): void {
  const path = new URL(url).pathname;
  if (!ALLOWED_PATHS.has(path)) {
    throw new Error(`route not allowed: ${path}`);
  }
}
