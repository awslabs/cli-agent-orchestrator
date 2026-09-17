---
name: cao-contributing
description: Contribute changes to the CAO (CLI Agent Orchestrator) codebase — the local
  dev loop, the CI gate map, and the pre-PR checklist. Use when the user says "open a PR",
  "why did CI fail", "run the checks before I push", "the mypy/Code Quality job is red",
  "add a test and verify coverage", or when making any code change intended to land on a
  branch/PR. Covers uv-based build/test/lint, the ci.yml jobs and their pass/fail
  semantics, and the golden rules that stop a green-locally / red-in-CI surprise. Not for
  authoring agent skills (cao-skill-creator), building providers/plugins/MCP-apps, or
  operating running sessions.
---

# Contributing to CAO

How to make a change to the **cli-agent-orchestrator** codebase and get it through CI
cleanly. Read this before pushing a branch or opening a PR. The canonical human docs are
[`DEVELOPMENT.md`](../../DEVELOPMENT.md), [`CONTRIBUTING.md`](../../CONTRIBUTING.md), and
[`CODEBASE.md`](../../CODEBASE.md) — this skill is the operational checklist that mirrors what
CI actually enforces.

## Golden rules (read these first)

1. **Run Python tooling through `uv`.** `uv sync --all-extras --dev`, `uv run pytest …`,
   `uv run mypy src/`, `uv run cao …`. There is no bare `pip` workflow, and the venv CI
   builds is the one `uv` manages. Repo scripts are documented in their own text as plain
   `python scripts/<name>.py` (for example `scripts/sync_skills.py`, whose fix-up message
   and `test_skill_packaging_parity.py` both quote that form) — run them as
   `uv run python scripts/<name>.py`, which satisfies both.
2. **Verify the *actual* CI run after every push — never declare "done" on local tests
   alone.** Poll it: `gh run list --branch <branch> --workflow CI` then
   `gh run view <id>` / `gh run view <id> --log-failed`.
3. **When a required check fails unexpectedly, diff EVERYTHING your commit changed —
   including CI/workflow/config files** (`.github/workflows/*.yml`, `pyproject.toml`,
   `mypy.ini`) — before concluding the cause is pre-existing or external. The signal is
   often in your own diff (`git diff <base>..HEAD -- .github/`). A displaced one-line
   workflow key (see the mypy note below) can turn a tolerated warning into a hard failure.
4. **Never mark a task complete while a required CI gate is red.** A red gate means *not
   done*; investigate, don't rationalize.
5. **Match the repo, don't reshape it.** Don't bundle unrelated fixes (e.g. repo-wide type
   errors) into a feature PR, and don't tighten a CI policy as a side effect of an
   unrelated change.

## Local dev loop

```bash
uv sync --all-extras --dev          # install (mirrors what CI does)
uv run pytest test/path/to/test_x.py   # run targeted tests while iterating
uv run black src/ test/             # format (CI checks --check)
uv run isort src/ test/             # import order (CI checks --check-only)
uv run mypy src/                    # type check (see the mypy note below)
```

Write tests **RED-first**: add a test that reproduces the bug/behavior and fails, then
implement until it passes. New features and bug fixes ship with tests; patch coverage is
expected to stay at 100% for changed lines.

## The CI gate map (`.github/workflows/ci.yml`)

Know which jobs are **blocking** vs **tolerated** so you can tell a real failure from
noise. `test/test_cao_contributing_skill_accuracy.py` fails if this table drifts from
`ci.yml`, so trust it — and if you rename a job, update it here.

| Job | Runs | Blocking? |
|-----|------|-----------|
| **Unit Tests** (3.10 / 3.11 / 3.12) | `uv run pytest test/ -m "not e2e" --cov=src/cli_agent_orchestrator --cov-report=term-missing` | **Yes** |
| ↳ step: **Validate Markdown links** | `uv run python scripts/validate_markdown_links.py` — every relative link in every tracked `.md`, including `skills/` | **Yes** |
| **Code Quality** | black `--check`, isort `--check-only`, then `uv run mypy src/` | black/isort **yes**; **mypy is non-blocking** (`continue-on-error: true`) |
| **AG-UI demo (shift-left recording)** | boots a `CAO_AGUI_ENABLED` server, drives the viewer, records a GIF artifact | **Yes** |
| **AG-UI construct demos (shift-left recordings)** | same pattern for the L2 construct library | **Yes** |
| **AG-UI stock-client live (AC3)** | drives a real third-party AG-UI client against the surface | **Yes** |
| **CAO MCP Apps** | MCP Apps build + backend coverage ratchet floor | **Yes** |
| **CAO MCP Apps E2E (Playwright)** | browser E2E over the `ui://cao/*` views | **Yes** |
| **Rust TUI** (Linux x86_64 / macOS arm64) | `cargo test` for the `tui/` crate | **Yes** |
| **Web UI Build** | frontend build | **Yes** |
| **AI-DLC Portfolio Example** | example project builds | **Yes** |
| **Security Scan** | Trivy | **Yes** |

> **The `-m "not e2e"` on the CI command overrides your local `addopts`.** CI deselects
> *only* `e2e`, so **integration tests run in CI**. If your local config also deselects
> `integration`, your local run is a strict subset of CI's and can be green while CI is
> red. Compare deselected counts, not just pass counts.

> **mypy is intentionally non-blocking.** The repo has **known, pre-existing, repo-wide
> mypy errors** (historically in `services/agent_scaffold.py`, `cli/commands/profile.py`,
> `services/memory_service.py` / `api/main.py` `MemoryArchiveBackend` call-arg, and a
> `jsonschema` stub). CI tolerates them via `continue-on-error: true` on the mypy step.
> Therefore:
> - **Do not** make mypy blocking, and **do not** bundle those unrelated type-fixes into a
>   feature PR (they belong in a dedicated cleanup PR).
> - **Your change must add zero *new* mypy errors** — check the delta, not the raw count.
> - **When you insert a new job/step near the `lint` job, keep `continue-on-error: true`
>   attached to the mypy step.** A mis-insertion once displaced that line onto the next
>   job's step, silently turning mypy into a hard gate and failing the build for
>   unrelated, pre-existing errors.

## Testing gotchas

- **The full `uv run pytest test/` is flaky locally** — it needs a running server, tmux,
  and real CLI binaries, and can hit a flaky OTel/gRPC abort. Run **targeted test files**
  while iterating and **trust CI** (the Unit Tests job) for the full suite; get
  authoritative missing-coverage lines from that job's `term-missing` output ∩ your diff.
- **A test that touches CAO's own state can pass only on your machine.** If a code path
  reaches the real database, config dir, or a live session, it will be green on a
  developer box that has an initialised CAO install and red on a clean runner with
  `sqlite3.OperationalError: no such table: terminals`. Mock the store/DB seam explicitly;
  when a test exercises a service function, check what that function calls *today* — a
  rebase can introduce a new unmocked DB write into a path your test already covered.
  Verify by running with an isolated `HOME`:
  ```bash
  TMPH=$(mktemp -d); HOME="$TMPH" uv run pytest test/path/to/test_x.py; rm -rf "$TMPH"
  ```
- **Local green and CI green are different claims, in both directions.** A local suite can
  hide real failures (see above) *and* invent ones CI never sees (macOS-only, missing
  optional binaries). When they disagree, CI is authoritative — read the job log rather
  than reasoning from the local result.
- **FastAPI `TestClient` must use `base_url="http://localhost"`** — the Host-header /
  DNS-rebinding guard returns `400` otherwise.
- **Provider status detection is screen-scraping** — provider tests are fixture-driven
  state machines; when a CLI tool changes its TUI, update the regexes **and** add a fixture.
- **The AG-UI demo recorder** (`examples/ag-ui/ag-ui-eventsource-viewer/tools`) needs a
  Chromium `headless_shell` matching the pinned `@playwright/test` version (`npm run
  playwright:install`) plus `ffmpeg`, and boots its own `CAO_AGUI_ENABLED` server. It gates
  in CI, so you don't have to run it locally to land a change. The construct-demo recorder
  is the sibling at `examples/ag-ui/ag-ui-construct-demos/tools`.

## Pre-PR checklist

1. `uv run black src/ test/ && uv run isort src/ test/` (or `--check` to verify).
2. `uv run mypy src/` — confirm **no *new* errors** vs the base (pre-existing ones are OK).
3. `uv run pytest <targeted files>` green; add/keep tests for changed behavior.
4. If you touched **any** `.md`, run `uv run python scripts/validate_markdown_links.py` —
   a dead relative link fails the Unit Tests job, and `skills/` is in scope. CAO has no
   root `AGENTS.md`; the contributor map is `CODEBASE.md`.
5. If you touched `skills/`, run `uv run python scripts/sync_skills.py` so the packaged mirror
   stays in lockstep (`test/test_skill_packaging_parity.py` enforces it).
6. **Commits:** only when asked; sign if the repo expects it; keep the subject concise and
   Conventional-Commits style; never force-push to `main`.
7. **Open the PR, then watch its CI run to completion** and fix any red gate before calling
   it done (rule #2 and #4). Use `gh pr create` / `gh pr checks`.

## Not what you want?

- Authoring a *new agent skill* (SKILL.md, frontmatter, evals) → use **cao-skill-creator**.
- Building a provider / plugin / MCP-apps view → **cao-provider** / **cao-plugin** /
  **cao-mcp-apps**.
- Launching or steering running agent sessions → **cao-session-management**.
