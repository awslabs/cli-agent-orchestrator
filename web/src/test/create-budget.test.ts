/**
 * The web UI's create budgets must be sized from the server's
 * `provider_init_timeout`, not from a fixed literal (issues #710, #1002).
 *
 * #1002 routed every Python create path's client deadline through
 * `utils/init_allowance.synchronous_create_timeout`, and added an AST lint
 * (`test/test_synchronous_create_timeout_boundary.py`) that fails any new fixed
 * literal on a create call. The browser is outside that lint's reach: three
 * calls in `api.ts` carried a flat 90s, which is BELOW a successful Antigravity
 * init at stock settings (430s after #1002). The browser aborted while the
 * server went on building the worker, which is exactly the shape #710 reported
 * for the CLI.
 *
 * This file is the web-side equivalent of that lint plus the unit tests for the
 * helper it enforces:
 *
 * 1. `initAllowanceMs` reproduces the Python arithmetic exactly, and the
 *    constants it mirrors are read back out of the Python sources so the two
 *    computations cannot drift apart silently.
 * 2. `createBudgetMs` reads `/health` once, caches it, and falls back to the
 *    old 90s when the field or the server is not there.
 * 3. The guard: every create call site in `api.ts` is enumerated by name, must
 *    reach its budget through `createBudgetMs()`, and must carry no fixed
 *    `timeoutMs` literal. A create call that is NOT in the enumeration fails
 *    too, so a new one has to be registered here deliberately.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import { api, initAllowanceMs, FALLBACK_CREATE_BUDGET_MS } from '../api'

/**
 * The repository root, found by walking up from the working directory until
 * `pyproject.toml` appears. `import.meta.url` is not a `file:` URL under
 * Vitest's transform, and anchoring on the cwd alone would break the moment
 * someone runs Vitest from the repo root instead of `web/`.
 */
function repoRoot(): string {
  let dir = process.cwd()
  for (let i = 0; i < 10; i++) {
    if (existsSync(join(dir, 'pyproject.toml'))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new Error(`could not locate the repository root above ${process.cwd()}`)
}

const REPO_ROOT = repoRoot()
const API_TS = join(REPO_ROOT, 'web', 'src', 'api.ts')

/**
 * The create call sites in `api.ts`, by property name on the `api` object.
 *
 * A "create" is a POST the server answers only AFTER `provider.initialize()`
 * returns, so its client deadline is a function of `provider_init_timeout` and
 * of nothing else. The route shapes below are the same two the Python lint
 * matches (`POST /sessions`, `POST /sessions/{}/terminals`, and since #948 the UI's
 * `POST /sessions/{}/terminals:operator` form) plus the flow run,
 * which launches a session synchronously behind one POST.
 *
 * Registering a name here is a claim that it is a create. The reverse check —
 * every POST whose route LOOKS like a create is registered — is
 * `test_every_create_route_is_registered`, and that is the half that makes a
 * new, unregistered create call fail rather than quietly inherit a literal.
 */
const CREATE_CALL_SITES = ['createSession', 'addTerminalToSession', 'runFlow'] as const

/** Route shapes (interpolations reduced to `{}`, query string dropped) that initialize synchronously. */
const CREATE_ROUTE_SHAPES = [/\/sessions$/, /\/sessions\/\{\}\/terminals(?::operator)?$/, /\/flows\/\{\}\/run$/]

// ── api.ts source parsing ────────────────────────────────────────────────────

/** Body of the `export const api = { ... }` object literal. */
function apiObjectBody(source: string): string {
  const marker = 'export const api = {'
  const start = source.indexOf(marker)
  expect(start, `${API_TS} must declare \`${marker}\``).toBeGreaterThan(-1)
  const end = source.lastIndexOf('\n}')
  expect(end, `${API_TS} must close the api object at column 0`).toBeGreaterThan(start)
  return source.slice(start + marker.length, end)
}

/**
 * The `api` object's entries, keyed by property name, each value the raw source
 * of that entry. Entries are delimited by a property name at exactly two
 * spaces of indentation, which is how every entry in this file is written.
 */
function apiEntries(source: string): Map<string, string> {
  const body = apiObjectBody(source)
  const starts: Array<{ name: string; at: number }> = []
  const re = /^ {2}([A-Za-z_$][\w$]*):/gm
  for (let m = re.exec(body); m !== null; m = re.exec(body)) {
    starts.push({ name: m[1], at: m.index })
  }
  const entries = new Map<string, string>()
  starts.forEach(({ name, at }, i) => {
    const end = i + 1 < starts.length ? starts[i + 1].at : body.length
    entries.set(name, body.slice(at, end))
  })
  return entries
}

/**
 * The first request URL in an entry, reduced to a route SHAPE: every `${...}`
 * becomes `{}` and the query string is dropped. The scan tracks brace depth so
 * a nested template literal inside an interpolation (the optional
 * `&session_name=` tail, for instance) does not end the literal early.
 */
function routeShape(entry: string): string | null {
  const callAt = entry.indexOf('fetchJSON')
  if (callAt < 0) return null
  let i = callAt
  while (i < entry.length && entry[i] !== '`' && entry[i] !== "'") i++
  if (i >= entry.length) return null

  const quote = entry[i]
  i++
  if (quote === "'") {
    const end = entry.indexOf("'", i)
    return end < 0 ? null : entry.slice(i, end).split('?')[0]
  }

  let out = ''
  while (i < entry.length) {
    if (entry[i] === '`') break
    if (entry[i] === '$' && entry[i + 1] === '{') {
      let depth = 1
      i += 2
      while (i < entry.length && depth > 0) {
        if (entry[i] === '{') depth++
        else if (entry[i] === '}') depth--
        i++
      }
      out += '{}'
      continue
    }
    out += entry[i]
    i++
  }
  return out.split('?')[0]
}

function looksLikeCreate(entry: string): boolean {
  if (!/method:\s*'POST'/.test(entry)) return false
  const shape = routeShape(entry)
  return shape !== null && CREATE_ROUTE_SHAPES.some(r => r.test(shape))
}

// ── Python constant mirroring ────────────────────────────────────────────────

function pythonConstant(relPath: string, name: string): number {
  const text = readFileSync(join(REPO_ROOT, relPath), 'utf-8')
  const m = new RegExp(`^${name}\\s*=\\s*([0-9]+(?:\\.[0-9]+)?)`, 'm').exec(text)
  expect(m, `${relPath} must define ${name} at module level`).not.toBeNull()
  return Number(m![1])
}

describe('initAllowanceMs mirrors the Python init allowance', () => {
  // `init_allowance(T)` is
  //   int(max(4*T, antigravity(T), kimi(T))) + 60
  // with antigravity(T) = 10 + 2*max(180, T) and kimi(T) = T + 2*max(120, T).
  // Reimplemented here from the docstrings rather than imported, so a change to
  // either side has to be made in both places to keep this test green.
  const expectedSeconds = (t: number) =>
    Math.trunc(Math.max(4 * t, 10 + 2 * Math.max(180, t), t + 2 * Math.max(120, t))) + 60

  it('matches at the stock provider_init_timeout', () => {
    // 10 + 2*180 = 370 (the Antigravity shape dominates 4*60 = 240), plus the
    // 60s delivery-confirmation allowance. This is the 430s the #1002 Python
    // helper produces at stock settings, and it is what the old fixed 90s
    // budget was abandoning a successful init below.
    expect(initAllowanceMs(60)).toBe(430_000)
    expect(initAllowanceMs(60)).toBe(expectedSeconds(60) * 1000)
  })

  it('matches across the regimes where a different provider shape dominates', () => {
    for (const t of [1, 10, 30, 60, 119, 120, 121, 180, 181, 300, 600, 3600]) {
      expect(initAllowanceMs(t), `provider_init_timeout=${t}`).toBe(expectedSeconds(t) * 1000)
    }
  })

  it('never returns less than the fallback it replaces', () => {
    // The Antigravity floor dominates at every T, so the derived budget can
    // never regress a create below the 90s it had before.
    for (const t of [1, 10, 60, 600]) {
      expect(initAllowanceMs(t)).toBeGreaterThanOrEqual(FALLBACK_CREATE_BUDGET_MS)
    }
  })

  it('reads the same constants the Python helper does', () => {
    // The drift guard. Two implementations of one formula in two languages is
    // exactly the coupling that rots in silence (the same reasoning as
    // test/agent_plugins/test_web_timeout_drift.py), so the Python sources are
    // the authority and this test fails the moment one of them moves.
    const initAllowancePy = 'src/cli_agent_orchestrator/utils/init_allowance.py'
    expect(pythonConstant(initAllowancePy, '_INIT_PHASES')).toBe(4)
    expect(pythonConstant(initAllowancePy, '_DELIVERY_CONFIRM_ALLOWANCE')).toBe(60)
    expect(
      pythonConstant('src/cli_agent_orchestrator/providers/antigravity_cli.py', 'SHELL_WAIT_TIMEOUT_S'),
    ).toBe(10)
    expect(
      pythonConstant('src/cli_agent_orchestrator/providers/antigravity_cli.py', 'READY_TIMEOUT_FLOOR_S'),
    ).toBe(180)
    expect(
      pythonConstant('src/cli_agent_orchestrator/providers/kimi_cli.py', 'READY_TIMEOUT_FLOOR_S'),
    ).toBe(120)
  })

  it('reads the field name the server actually reports', () => {
    const main = readFileSync(join(REPO_ROOT, 'src/cli_agent_orchestrator/api/main.py'), 'utf-8')
    // GET /health gained this field in #1002 (P3-1); it is the only way the
    // browser can see the value, so a rename must fail here.
    expect(main).toContain('"provider_init_timeout": int(get_server_settings()["provider_init_timeout"])')
  })
})

// ── createBudgetMs behaviour ─────────────────────────────────────────────────

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

describe('createBudgetMs', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('derives the budget from the health value and reads health only once', async () => {
    const fetchMock = vi.fn(async (_url: string, _opts?: RequestInit) =>
      jsonResponse({ status: 'ok', provider_init_timeout: 180 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    vi.resetModules()
    const mod = await import('../api')

    // 4*180 = 720 dominates both provider shapes here, +60 = 780s.
    expect(await mod.createBudgetMs()).toBe(780_000)
    expect(await mod.createBudgetMs()).toBe(780_000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toBe('/health')
  })

  it('reads health once for concurrent callers', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: 'ok', provider_init_timeout: 60 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.resetModules()
    const mod = await import('../api')

    const budgets = await Promise.all([mod.createBudgetMs(), mod.createBudgetMs(), mod.createBudgetMs()])
    expect(budgets).toEqual([430_000, 430_000, 430_000])
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('falls back to the previous fixed 90s when health is unreachable', async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError('network down')
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.resetModules()
    const mod = await import('../api')

    expect(await mod.createBudgetMs()).toBe(90_000)
    expect(await mod.createBudgetMs()).toBe(mod.FALLBACK_CREATE_BUDGET_MS)
    // A failure is NOT cached: a transient blip must not pin the fallback for
    // the lifetime of the page.
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('falls back when an older server omits the field or reports nonsense', async () => {
    for (const payload of [{ status: 'ok' }, { provider_init_timeout: 'soon' }, { provider_init_timeout: 0 }]) {
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(payload)))
      vi.resetModules()
      const mod = await import('../api')
      expect(await mod.createBudgetMs(), JSON.stringify(payload)).toBe(90_000)
    }
  })
})

describe('a create call carries the derived budget end to end', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('does not abort createSession before the server-sized deadline', async () => {
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (String(url).includes('/health')) {
        return Promise.resolve(jsonResponse({ status: 'ok', provider_init_timeout: 180 }))
      }
      // A create the server has not answered yet: resolve only on abort, so the
      // client's own deadline is the only thing that can settle this promise.
      return new Promise<Response>((_resolve, reject) => {
        opts.signal!.addEventListener('abort', () =>
          reject(new DOMException('The operation was aborted.', 'AbortError')),
        )
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
    vi.resetModules()
    const mod = await import('../api')

    let settled: 'pending' | 'aborted' = 'pending'
    const pending = mod.api.createSession('antigravity', 'engineer').catch(() => {
      settled = 'aborted'
    })

    // Let the /health read resolve and the create POST start.
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/sessions'))).toBe(true)

    // 90_000 was the old literal; a successful Antigravity init runs well past
    // it, so the request must still be in flight here.
    await vi.advanceTimersByTimeAsync(90_000)
    expect(settled).toBe('pending')

    // Still in flight just shy of the derived 780s budget, gone just after it.
    await vi.advanceTimersByTimeAsync(780_000 - 90_000 - 1)
    expect(settled).toBe('pending')
    await vi.advanceTimersByTimeAsync(2)
    await pending
    expect(settled).toBe('aborted')
  })
})

// ── The guard ────────────────────────────────────────────────────────────────

describe('every create call in api.ts is sized from provider_init_timeout', () => {
  const source = readFileSync(API_TS, 'utf-8')
  const entries = apiEntries(source)

  it('parses the api object this test is guarding', () => {
    // A refactor that defeats the parser must fail here rather than silently
    // stop guarding anything.
    expect(entries.size).toBeGreaterThan(40)
    expect(entries.has('listSessions')).toBe(true)
    expect(entries.has('installPlugin')).toBe(true)
    for (const name of CREATE_CALL_SITES) {
      expect(entries.has(name), `api.ts no longer defines ${name}`).toBe(true)
    }
  })

  it('exposes every registered create call on the api object', () => {
    for (const name of CREATE_CALL_SITES) {
      expect(typeof (api as Record<string, unknown>)[name], name).toBe('function')
    }
  })

  it.each(CREATE_CALL_SITES)('%s takes its budget from createBudgetMs()', name => {
    const entry = entries.get(name)!
    expect(
      /timeoutMs:\s*await\s+createBudgetMs\(\)/.test(entry),
      `${name} must pass \`timeoutMs: await createBudgetMs()\` so raising the server's ` +
        'provider_init_timeout moves it too (issues #710, #1002). Found:\n' +
        entry,
    ).toBe(true)
  })

  it.each(CREATE_CALL_SITES)('%s carries no fixed timeout literal', name => {
    const entry = entries.get(name)!
    const literal = /timeoutMs:\s*[0-9]/.exec(entry)
    expect(
      literal,
      `${name} carries a fixed timeoutMs literal. A create's deadline is a function of the ` +
        "server's provider_init_timeout and of nothing else: a literal below it abandons a " +
        'successful init while the server goes on building the worker (issue #710). Use ' +
        '`timeoutMs: await createBudgetMs()`.',
    ).toBeNull()
  })

  it('registers every POST whose route initializes synchronously', () => {
    const unregistered = [...entries]
      .filter(([name, entry]) => looksLikeCreate(entry) && !CREATE_CALL_SITES.includes(name as never))
      .map(([name, entry]) => `${name} -> ${routeShape(entry)}`)
    expect(
      unregistered,
      'these api.ts calls POST to a route the server answers only after provider.initialize() ' +
        'but are not registered in CREATE_CALL_SITES, so nothing checks their budget. Add them ' +
        'to the list in this test and size them from createBudgetMs().',
    ).toEqual([])
  })

  it('finds every registered create call by its route shape', () => {
    // The other direction: a registered name whose route no longer looks like a
    // create means the registry is stale, not that the guard passed.
    for (const name of CREATE_CALL_SITES) {
      const entry = entries.get(name)!
      expect(looksLikeCreate(entry), `${name} -> ${routeShape(entry)}`).toBe(true)
    }
  })

  it('leaves the non-create budgets alone', () => {
    // Out of scope by design (issue text): reads, cancels and the authoring
    // round-trips are not init-bound, so their literals stay. Pinned so the
    // helper is not over-applied in a later sweep.
    expect(/timeoutMs:\s*330000/.test(entries.get('installPlugin')!)).toBe(true)
    expect(/timeoutMs:\s*120000/.test(entries.get('getGraph')!)).toBe(true)
    expect(/timeoutMs:\s*30000/.test(entries.get('createFlow')!)).toBe(true)
  })
})
