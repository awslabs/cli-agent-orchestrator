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
[`AGENTS.md`](../../AGENTS.md) — this skill is the operational checklist that mirrors what
CI actually enforces.

## Golden rules (read these first)

1. **`uv` is mandatory.** Run everything through it: `uv sync --all-extras --dev`,
   `uv run pytest …`, `uv run mypy src/`, `uv run cao …`. There is no bare
   `pip`/`python` workflow.
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

Know which jobs are **blocking** vs **tolerated** so you can tell a real failure from noise.

| Job | Runs | Blocking? |
|-----|------|-----------|
| **Unit Tests** (3.10 / 3.11 / 3.12) | `uv run pytest` (`--cov=src`, `-m 'not e2e'`) | **Yes** |
| **Code Quality** | black `--check`, isort `--check-only`, then `uv run mypy src/` | black/isort **yes**; **mypy is non-blocking** (`continue-on-error: true`) |
| **AG-UI demo (shift-left recording)** | boots a `CAO_AGUI_ENABLED` server, drives the viewer, asserts components render / off-list refused | **Yes** |
| **CAO MCP Apps** + **E2E (Playwright)** | MCP Apps build/coverage + browser E2E | **Yes** |
| **Web UI Build**, **Security Scan** | frontend build, Trivy | **Yes** |

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
- **FastAPI `TestClient` must use `base_url="http://localhost"`** — the Host-header /
  DNS-rebinding guard returns `400` otherwise.
- **Provider status detection is screen-scraping** — provider tests are fixture-driven
  state machines; when a CLI tool changes its TUI, update the regexes **and** add a fixture.
- **The AG-UI demo recorder** (`examples/agui-eventsource-viewer/tools/`) needs a Chromium
  `headless_shell` matching the pinned `@playwright/test` version (`npm run
  playwright:install`) plus `ffmpeg`; it boots its own `CAO_AGUI_ENABLED` server with
  `CAO_CORS_ORIGINS=http://localhost:8123`. It gates in CI, so you don't have to run it
  locally to land a change.

## Pre-PR checklist

1. `uv run black src/ test/ && uv run isort src/ test/` (or `--check` to verify).
2. `uv run mypy src/` — confirm **no *new* errors** vs the base (pre-existing ones are OK).
3. `uv run pytest <targeted files>` green; add/keep tests for changed behavior.
4. If you touched `skills/`, run `python scripts/sync_skills.py` so the packaged mirror
   stays in lockstep (`test/test_skill_packaging_parity.py` enforces it).
5. **Commits:** only when asked; sign if the repo expects it; keep the subject concise and
   Conventional-Commits style; never force-push to `main`.
6. **Open the PR, then watch its CI run to completion** and fix any red gate before calling
   it done (rule #2 and #4). Use `gh pr create` / `gh pr checks`.

## Not what you want?

- Authoring a *new agent skill* (SKILL.md, frontmatter, evals) → use **cao-skill-creator**.
- Building a provider / plugin / MCP-apps view → **cao-provider** / **cao-plugin** /
  **cao-mcp-apps**.
- Launching or steering running agent sessions → **cao-session-management**.
