# Fix: Web UI packaging — serve the frontend regardless of cwd or install mode

## Problem

The production-mode Web UI (`cao-server` serving the built bundle at `http://localhost:9889`) only works when `cao-server` is launched from inside a cloned copy of the repository via `uv run cao-server`. Any other invocation — `cao-server` from `$PATH` after `uv tool install`, `uv run cao-server` from `/tmp`, a packaged wheel from PyPI — returns `404` at `/` while the REST API continues to work.

### Root cause

`src/cli_agent_orchestrator/api/main.py:869` resolves the static-file directory by walking four parents up from `__file__`:

```python
WEB_DIST = Path(__file__).parent.parent.parent.parent / "web" / "dist"
if WEB_DIST.exists():
    from starlette.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
```

This arithmetic only resolves to a real directory when the package is **editable-installed** in a clone that contains `web/dist/` as a sibling of `src/`:

```
<repo>/
├── src/cli_agent_orchestrator/api/main.py   ← __file__
└── web/dist/                                ← 4 parents up + "web/dist"
```

The hatch build config at `pyproject.toml:34` only packages `src/cli_agent_orchestrator`, so `web/dist/` is **never copied into the wheel**. In any non-editable install:

- `__file__` lives inside `site-packages/cli_agent_orchestrator/api/main.py`
- The 4-parent walk lands somewhere inside `site-packages/` or `lib/`
- `web/dist/` does not exist there
- `WEB_DIST.exists()` returns `False`
- The static mount is silently skipped
- `GET /` returns `404 Not Found`

The fix moves the frontend bundle **inside** the Python package, rewrites the resolver to anchor on the package itself, and configures hatch to ship the static files in the wheel.

## Fix overview

Three independent changes, all required:

| Part | Change | File |
|---|---|---|
| 1 | Point Vite's `outDir` at a directory inside the Python package | `web/vite.config.ts` |
| 2 | Rewrite `WEB_DIST` using `importlib.resources` | `src/cli_agent_orchestrator/api/main.py` |
| 3 | Force-include the built assets in the wheel via hatch | `pyproject.toml`, `.gitignore` |

After all three, `cao-server` finds the UI via `importlib.resources.files("cli_agent_orchestrator") / "web_ui"`, which resolves correctly for editable installs (points at the on-disk source tree) and wheel installs (points inside `site-packages/`) alike. Launching from any `cwd` works identically.

---

## Part 1 — Redirect the Vite build output into the Python package

Build artifacts should land where the package resolver will look for them: `src/cli_agent_orchestrator/web_ui/`.

**File:** `web/vite.config.ts`

**Current contents:**

```ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: { /* ... */ },
  server: {
    host: 'localhost',
    port: 5173,
    proxy: { /* ... */ },
  },
})
```

**Change:** add a `build.outDir` entry pointing at the package-internal location. Path is relative to `web/vite.config.ts`.

```ts
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/cli_agent_orchestrator/web_ui',
    emptyOutDir: true,
  },
  test: { /* ... */ },
  server: { /* ... */ },
})
```

`emptyOutDir: true` lets Vite clean the target directory on each build even though it sits outside `web/`. Without it, Vite refuses to delete files outside the project root and will log a warning.

**Delete the old output location** once builds write to the new path:

```bash
rm -rf web/dist/
```

**Acceptance criteria:**

- `cd web && npm run build` succeeds with no warnings.
- `src/cli_agent_orchestrator/web_ui/index.html` exists after the build.
- `web/dist/` no longer exists (or is empty and unused).
- Dev mode (`npm run dev`) still works unchanged — `outDir` only affects `vite build`.

---

## Part 2 — Rewrite the resolver in `main.py`

Replace the `__file__`-relative path arithmetic with a lookup anchored to the package itself.

**File:** `src/cli_agent_orchestrator/api/main.py`

**Current code (lines 868–873):**

```python
# Static file serving for built web UI
WEB_DIST = Path(__file__).parent.parent.parent.parent / "web" / "dist"
if WEB_DIST.exists():
    from starlette.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
```

**Replacement:**

```python
# Static file serving for built web UI.
# Anchored to the package via importlib.resources so it works for both
# editable installs (uv sync) and wheel installs (uv tool install, pip install).
from importlib.resources import files as _pkg_files

WEB_DIST = Path(str(_pkg_files("cli_agent_orchestrator") / "web_ui"))
if (WEB_DIST / "index.html").exists():
    from starlette.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
```

### Why `importlib.resources` instead of `Path(__file__).parent.parent / "web_ui"`

Both produce the same result for regular installs, but `importlib.resources.files()` is the documented API for "where did my package end up on disk", handles namespace packages correctly, and makes the intent explicit: this path is a package resource, not a repo-relative convention.

The `Path(str(...))` wrapper is there because `files()` returns a `Traversable`, not a `Path`. Starlette's `StaticFiles` needs a real filesystem path. For every install mode CAO actually supports (uv sync, uv tool install, pip install from wheel), the underlying `Traversable` is a `PosixPath`, so stringifying and re-wrapping is safe.

### Why check `index.html` instead of the directory

Checking `WEB_DIST.exists()` false-positives on an empty `web_ui/` directory, which can happen if the package ships the directory placeholder but the frontend was never built. Checking for `index.html` proves the build actually produced usable output.

**Acceptance criteria:**

- After `npm run build`, starting `cao-server` from the repo root serves the UI at `http://localhost:9889/` (unchanged behavior).
- Starting `cao-server` from `/tmp` after `uv sync && uv run cao-server` serves the UI.
- If `src/cli_agent_orchestrator/web_ui/index.html` is deleted, the `/` route returns `404` and the API routes still work (graceful degradation preserved).
- No references to `Path(__file__).parent.parent.parent.parent` remain in `main.py`.

---

## Part 3 — Ship the built assets in the wheel

Hatch's default wheel target picks up `*.py` files under the package directory. Static assets must be explicitly opted in.

### 3a. Add the force-include and artifacts rules

**File:** `pyproject.toml`

**Current block (lines 34–35):**

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/cli_agent_orchestrator"]
```

**Replacement:**

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/cli_agent_orchestrator"]

[tool.hatch.build.targets.wheel.force-include]
"src/cli_agent_orchestrator/web_ui" = "cli_agent_orchestrator/web_ui"

[tool.hatch.build]
artifacts = [
    "src/cli_agent_orchestrator/web_ui/**",
]
```

Two directives, each doing one job:

- **`force-include`** — copies the source tree at `src/cli_agent_orchestrator/web_ui` to the wheel path `cli_agent_orchestrator/web_ui`. Without this, hatch's default selector excludes non-Python files from the wheel.
- **`artifacts`** — whitelists files that are gitignored. Hatch treats `.gitignore` as the default exclude list, and the built frontend should stay gitignored (see 3b), so we must explicitly un-exclude it for the build.

### 3b. Gitignore the build output

**File:** `.gitignore`

Add:

```
# Built web UI (generated by `npm run build` in web/)
src/cli_agent_orchestrator/web_ui/
```

Built artifacts don't belong in git. The `artifacts` directive in 3a ensures they're still included in the wheel despite being gitignored.

### 3c. Verify the wheel contents

After the config changes, build a wheel and inspect it:

```bash
cd web && npm run build && cd ..
uv build --wheel
unzip -l dist/cli_agent_orchestrator-*.whl | grep web_ui
```

You should see entries like:

```
cli_agent_orchestrator/web_ui/index.html
cli_agent_orchestrator/web_ui/assets/index-XXXXXXXX.js
cli_agent_orchestrator/web_ui/assets/index-XXXXXXXX.css
```

If the `web_ui/` entries are missing, one of the hatch directives is misconfigured — check that `force-include` key and value paths are correct and that `artifacts` uses the `**` recursive glob.

**Acceptance criteria:**

- `uv build --wheel` produces a wheel containing `cli_agent_orchestrator/web_ui/index.html` and the full `assets/` subtree.
- `uv tool install dist/cli_agent_orchestrator-*.whl` installs the UI files into `site-packages/cli_agent_orchestrator/web_ui/`.
- After a tool install, running `cao-server` from any `cwd` (e.g., `cd /tmp && cao-server`) serves the UI at `http://localhost:9889/`.
- `git status` shows `src/cli_agent_orchestrator/web_ui/` as ignored (not tracked).

---

## Pre-flight checklist for maintainers

Before cutting a release after this change, verify:

1. `cd web && npm run build` — regenerates `src/cli_agent_orchestrator/web_ui/`.
2. `uv build --wheel` — produces a wheel.
3. `unzip -l dist/*.whl | grep web_ui` — confirms assets are bundled.
4. In a scratch directory: `uv tool install <wheel>` and `cao-server`, then `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9889/` → `200`.

CI should run steps 1–3 on every PR; step 4 is release-gate.

## Follow-ups (out of scope for this fix)

- **Automated frontend build during wheel packaging.** A hatch build hook (`hatch_build.py`) can run `npm run build` as part of `uv build`, so contributors who forget step 1 above still ship a working wheel. Cost: Node becomes a build-time dependency. Track separately.
- **README update.** The note in `README.md` under "Starting the Web UI" that says "The Web UI requires a cloned copy of the repository (the `web/` directory is not included in the `uv tool install` package)" becomes obsolete once this fix lands and should be deleted.
