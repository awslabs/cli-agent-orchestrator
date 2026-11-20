# CAO Shelf front-end: developer notes (workstream B)

Run everything from `examples/kiro-crew-cao-shelf/ui/`. Node 22.12 or later.

| Task | Command |
|---|---|
| Install exact dependencies (needs the npm registry once; `node_modules` is git-ignored) | `npm ci` |
| Type check | `npm run typecheck` |
| Lint (`react/no-danger` is an error) | `npm run lint` |
| Component tests | `npm test -- --run` (one file: `npm test -- --run useShelf`, `dialogs`, `a11y`) |
| Regenerate `src/types.ts` from `../schemas/*.json` (every response body has a schema: no hand-typed shapes in `src/api.ts`) | `npm run gen:types` (`test/types.test.ts` fails on drift) |
| Build the committed bundle `bundle/index.mjs` | `npm run build` |
| Bundle guards (no HTML sinks, no direct fetch, no status "running", under 120 KB) | `npm run check:bundle` |
| Drift check for C | `npm run build && git diff --exit-code bundle` |
| Everything | `npm run check` |

The done-check is `npm ci && npm run typecheck && npm test -- --run && npm run build`.

## How the page reaches Kiro Crew

* Entry: `src/App.tsx`, default export, a React component. `app.json` should set
  `ui.entry = "bundle/index.mjs"`.
* The host provides `react`, `react-dom`, `react/jsx-runtime`, `@kirocrew/app-sdk`,
  `@kirocrew/app-sdk/ui`, `lucide-react` and `@tanstack/react-query` through its import map; they are
  Vite externals and are never installed for the build. `src/sdk.d.ts` declares the SDK pieces used:
  `useAppApi`, `useAppInfo`, `useNavBadge` and `useChatLauncher` from `@kirocrew/app-sdk`, and from the
  host's App Kit (`@kirocrew/app-sdk/ui`) `Badge`, `Btn`, `Card`, `CardTitle`, `EmptyState`,
  `ErrorNotice`, `Input`, `Modal`, `PageHeader`, `Skeleton` and `Toggle`, which may be imported by name,
  plus `Select` and `guideCaution`, which an older host may lack and so are read only through the
  namespace import in `src/components/kit.tsx`, with a native `<select>` fallback.
  `scripts/check-bundle.mjs` fails the build on a named import outside that list.
* Every page button goes through `src/components/Button.tsx` (the host `Btn`, `type="button"` by
  default, the `cs-btn` class for the 44 px touch rule, the guide's caution marker on danger).
* The page calls only `/apps/cao-shelf/api/*` through `useAppApi()`; `permissions.api` needs
  `/apps/cao-shelf/api` and `/apps/cao-shelf/api/*`. No `fetch` appears in `src/`.
* Tests alias the two SDK module names to `test/sdk-stub/`: `index.tsx` is an in-memory double that
  enforces the declared path and throws `AppApiError`-shaped errors, and `ui.tsx` copies the host App
  Kit's DOM contract at Kiro Crew 0.12.2 (roles, ARIA, test ids, variant classes; pinned by
  `test/sdk-stub.test.tsx`). The same `ui.tsx` is what `make dev` serves, so there is one stub.
  The tests also replace `globalThis.fetch` with a router (`test/helpers.tsx`) fed by `../fixtures/expected/*`. No gateway is started.

## Looking at the page without Kiro Crew

Run `make dev` (from the example folder): a fake CAO, the real backend, a signing proxy and the
built page with the SDK stub, on `http://127.0.0.1:8765/`, with the host's light and dark token
blocks and an approximation of its component look (`e2e/rig/dev_host/host-kit.css`). The stub renders
Profile as a native list (the host's touch path), so the desktop Radix picker, the dashboard chrome
and the exact host look are only on the real host: `make e2e` (`e2e/tests/test_screens_restyle.py`)
takes the screenshots of record. See `e2e/rig/README.md`.

## Conventions

* Every CAO string is a React text node. No `dangerouslySetInnerHTML`, no markdown, no `href` or `src`
  from data (the source rules test greps for them).
* Colours come from host tokens only: `src/styles.ts` reads an allowlisted set of `var(--x)` with no
  fallback value, defines no variables, has no dark-mode branch and no colour literal, and
  `test/styles.test.ts` and `test/kit-imports.test.ts` enforce it. Dark mode is whatever the host
  paints. No inline `style` props (ESLint `react/forbid-dom-props`).
* Session-level wording follows PROPOSAL 4.3: idle age, never "running"; "last message N ago" for closed
  helpers; "waiting on helper" instead of "handoff".
