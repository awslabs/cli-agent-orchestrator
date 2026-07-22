# CAO View Guardrails

The complete set of CAO-specific invariants enforced on `ui://cao/*` views. These go beyond the base MCP Apps spec requirements — CAO's views are hand-rolled single-file React bundles with stricter constraints than the `create-mcp-app` starter templates.

## 1. Default-Off (`CAO_MCP_APPS_ENABLED`)

The entire MCP Apps surface is gated behind an environment variable:

```bash
export CAO_MCP_APPS_ENABLED=true
```

When unset or `false`, no resources are registered, no app tools are mounted, and the server behaves identically to a build without the MCP Apps feature. This is an **end-to-end** gate — it applies to resource registration in `ext_apps/apps.py` and tool registration in `mcp_server/app_tools.py`.

**Why:** A default-off posture means MCP Apps never affects users who haven't opted in. It simplifies testing (baseline behavior is unmodified) and ensures the feature doesn't add attack surface by default.

## 2. JIT-Free Bundles

CAO bundles must contain zero JIT (Just-In-Time compilation) constructs:

- No `eval()`
- No `new Function()`
- No `Function.prototype.constructor` tricks
- No template-compiled code at runtime

**Why:** The MCP App host's Content Security Policy (CSP) sets `script-src` to `'self' 'unsafe-inline'` with **no** `'unsafe-eval'`. Any JIT construct causes a runtime CSP violation and the view silently fails.

**What to avoid:**
- CSS-in-JS libraries that use `eval` (e.g., older styled-components versions)
- Template engines that compile to `new Function`
- Dynamic import expressions that fall back to `eval` in some bundlers

**CI gate:**
```bash
cd cao_mcp_apps
npm run scan:jit
```

This runs `scripts/scan-jit.mjs`, which scans all built artifacts in `apps_static/` for JIT tokens. Any match fails the build.

## 3. HTTP-Only Boundary

All code in `mcp_server/*` must reach Backplane state **only** through HTTP calls to the FastAPI server at `API_BASE_URL` (default `http://127.0.0.1:9889`). Direct imports from `clients.tmux`, `clients.database`, or any other state-holding module are forbidden.

**Why:** The HTTP boundary is the governance layer — it enforces auth scopes, validates payloads, emits plugin events, and provides a single audit surface. Bypassing it means mutations escape governance.

**CI gate:**
```bash
uv run pytest test/test_http_only_boundary.py -v
```

This is an AST-based guard that statically analyzes imports in `mcp_server/` and `ext_apps/` to ensure no direct state access.

## 4. Bundle-Size Budget

Each single-file HTML artifact has a gzipped size ceiling. The budget prevents bundle bloat from new dependencies or excessive inlining.

**CI gate:**
```bash
cd cao_mcp_apps
npm run check:size
```

This runs `scripts/check-bundle-size.mjs`, which gzips each artifact in `apps_static/` and compares against the budget defined in `package.json` or a config file.

**Tips to stay within budget:**
- Share code via `src/shared/` (it's tree-shaken per entry point)
- Avoid large utility libraries; use focused imports
- Check the size impact of new dependencies before adding them

## 5. Coverage Ratchet

Test coverage (line %) must never decrease relative to the recorded baseline in `.coverage-baseline.json` at the repo root.

**CI gate:**
```bash
cd cao_mcp_apps
npm run coverage:ratchet
```

This runs `scripts/coverage-ratchet.mjs`, which:
1. Reads the floor from `.coverage-baseline.json`
2. Compares against `coverage/coverage-summary.json` (produced by Vitest with V8 provider)
3. Fails if the measured coverage drops below the floor

**If your changes lower coverage:** Add tests for the new code before merging. If a tooling upgrade causes a measurement change, re-baseline with documented rationale.

## Running All Gates Locally

The complete pre-merge check sequence:

```bash
cd cao_mcp_apps

# Build all views
npm run build:all

# Gate 1: JIT-free
npm run scan:jit

# Gate 2: Bundle size
npm run check:size

# Gate 3: Tests + coverage
npm test -- --coverage

# Gate 4: Coverage ratchet
npm run coverage:ratchet

# Gate 5: HTTP-only boundary (from repo root)
cd ..
uv run pytest test/test_http_only_boundary.py -v

# Gate 6: Type check
cd cao_mcp_apps
npm run typecheck
```

All six must pass before CI will merge a PR touching `cao_mcp_apps/` or `ext_apps/`.

## Summary Table

| Gate | Command | What It Checks |
|------|---------|---------------|
| Default-off | (design invariant) | Feature only active when env var is set |
| JIT-free | `npm run scan:jit` | No eval/new Function in built bundles |
| HTTP-only | `pytest test/test_http_only_boundary.py` | No direct state imports in mcp_server/ |
| Bundle size | `npm run check:size` | Gzipped artifact within budget |
| Coverage | `npm run coverage:ratchet` | Line coverage ≥ baseline floor |
| Type check | `npm run typecheck` | TypeScript compiles cleanly |
