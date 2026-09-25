---
name: cao-contributing
description: Contribute changes to the CAO (CLI Agent Orchestrator) codebase — the local
  dev loop, the CI gate map, and the pre-PR checklist. Use when the user says "open a PR",
  "why did CI fail", "run the checks before I push", "the mypy/Code Quality job is red",
  "add a test and verify coverage", or when making any code change intended to land on a
  branch/PR. Covers uv-based build/test/lint, the ci.yml jobs and their pass/fail
  semantics, and the golden rules that stop a green-locally / red-in-CI surprise. Not for
  authoring agent skills, building providers/plugins/MCP-apps, or operating running
  sessions.
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
   alone.** Poll it: `gh pr checks <number>` for every workflow on the PR, or
   `gh run list --branch <branch> --workflow CI` for `ci.yml` alone, then
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
implement until it passes. New features and bug fixes ship with tests.

Keeping patch coverage at 100% for changed lines is a **team convention, not a gate**.
Nothing fails your build over it: the repo has no `codecov.yml`, so there is no configured
status check or target, and the Unit Tests job uploads coverage with
`fail_ci_if_error: false` — even a broken upload is tolerated. Treat the Codecov comment as
a review signal to justify, not a red gate to chase.

## The CI gate map (`.github/workflows/ci.yml`)

Know which jobs are **blocking** vs **tolerated** so you can tell a real failure from
noise. `test/test_cao_contributing_skill_accuracy.py` fails if this table drifts from
`ci.yml`, so trust it — and if you rename a job, update it here.

| Job | Runs | Blocking? |
|-----|------|-----------|
| **Unit Tests** (3.10 / 3.11 / 3.12) | `uv run pytest test/ examples/workflow/tests/ --ignore=test/providers/test_kiro_cli_integration.py --ignore=test/e2e -m "not e2e" --cov=src/cli_agent_orchestrator --cov-report=term-missing` | **Yes** |
| ↳ step: **Validate Markdown links** | `uv run python scripts/validate_markdown_links.py` — every relative link in every tracked `.md`, including `skills/` | **Yes** |
| **Code Quality** | black `--check`, isort `--check-only`, then `uv run mypy src/` | black/isort **yes**; **mypy is non-blocking** (`continue-on-error: true`) |
| **AG-UI demo (shift-left recording)** | boots a `CAO_AGUI_ENABLED` server, drives the viewer, records a GIF artifact | **Yes** |
| **AG-UI construct demos (shift-left recordings)** | same pattern for the L2 construct library | **Yes** |
| **AG-UI stock-client live (AC3)** | drives a real third-party AG-UI client against the surface | **Yes** |
| **Agent Plugins dog-food (shift-left recording)** | records the plugin pipeline from `examples/agent-plugins/agent-plugins-dogfood/tools` and gates on drift | **Yes** |
| **CAO MCP Apps** | MCP Apps build + backend coverage ratchet floor | **Yes** |
| **CAO MCP Apps E2E (Playwright)** | browser E2E over the `ui://cao/*` views | **Yes** |
| **Rust TUI** (Linux x86_64 / macOS arm64) | `cargo test` for the `tui/` crate | **Yes** |
| **Web UI Build** | frontend build | **Yes** |
| **AI-DLC Portfolio Example** | example project builds | **Yes** |
| **Security Scan** | Trivy | **Yes** |
| **Dependency Review** | `actions/dependency-review-action` over the PR's dependency delta: `fail-on-severity: high` plus denied licences `GPL-3.0`/`AGPL-3.0` | **Yes** — CI-only; there is nothing to run locally, and it is skipped on forks (`if: github.repository == 'awslabs/cli-agent-orchestrator'`), so a green run on your fork has not exercised it |

> **The `-m "not e2e"` on the CI command replaces your local `addopts` — it does not
> compose with it.** So a local run that *also* deselects `integration` is a strict subset
> of CI's selection and can be green while CI is red. Compare deselected counts, not just
> pass counts.
>
> **CI's selection is narrower than the marker alone implies.** The same command passes
> `--ignore=test/providers/test_kiro_cli_integration.py` and `--ignore=test/e2e`, and
> `--ignore` wins over `-m`: that Kiro provider test needs an authenticated external CLI
> and **never runs in CI**. So *most* `integration`-marked tests do run in CI — but not
> that one, and nothing in CI covers it. If you change it, you have to run it yourself.

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

### Other workflows that gate a PR

`ci.yml` is not the only workflow on a pull request. These run alongside it, and none sets
job-level `continue-on-error`, so every check they run is blocking. The same accuracy test
pins this table to the workflow files.

| Workflow | Checks on the PR | Runs on | Blocking? |
|----------|------------------|---------|-----------|
| **Secret Scan** (`secret-scan.yml`) | `gitleaks`, `gitleaks config tests` | every PR to `main` | **Yes** — the config tests run locally as `uv run pytest test/test_gitleaks_config.py`; the scan itself is `gitleaks detect --config .gitleaks.toml` over the PR's commits |
| **cargo-deny** (`cargo-deny.yml`) | `cargo-deny (advisories, licenses, bans, sources)` | every PR to `main` | **Yes** — locally, `cargo deny --manifest-path tui/Cargo.toml --locked check` (global flags before the subcommand, as the action passes them) |
| **Test Antigravity CLI Provider** (`test-antigravity-cli-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, its unit test or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Test Claude Code Provider** (`test-claude-code-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, its unit test, `pyproject.toml`, or the workflow | **Yes** |
| **Test Codex CLI Provider** (`test-codex-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, its unit test or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Test Kiro CLI Provider** (`test-kiro-cli-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, its unit test or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Docs site** (`gh-pages.yml`) | `build` | only PRs touching `docusaurus/**` or the workflow | **Yes** — `deploy` is push-only and never runs on a PR |

> **Two checks can share a name.** Each provider workflow has its own `Unit Tests` and
> `Code Quality`, so a PR that touches `pyproject.toml` shows those names more than once.
> Read the workflow name next to a failing check before assuming it is the CI one —
> `gh pr checks <number>` lists every workflow, whereas `gh run list --workflow CI` sees
> only `ci.yml`.

## Testing gotchas

- **The full `uv run pytest test/` is flaky locally** — it needs a running server, tmux,
  and real CLI binaries, and can hit a flaky OTel/gRPC abort. Run **targeted test files**
  while iterating and lean on CI (the Unit Tests job) for breadth; get authoritative
  missing-coverage lines from that job's `term-missing` output ∩ your diff. **CI is not the
  full suite, though** — it excludes `test/e2e` and the Kiro provider integration test by
  path (see the callout above), so those two are only ever covered by someone running them
  deliberately.
- **A test that touches CAO's own state can pass only on your machine.** If a code path
  reaches the real database, config dir, or a live session, it will be green on a
  developer box that has an initialised CAO install and red on a clean runner with
  `sqlite3.OperationalError: no such table: terminals`. Mock the store/DB seam explicitly;
  when a test exercises a service function, check what that function calls *today* — a
  rebase can introduce a new unmocked DB write into a path your test already covered.
  Verify by running under a throwaway `HOME` **and** a throwaway `CAO_HOME_DIR` —
  `HOME` alone is not enough, because `constants.py` prefers an exported
  `CAO_HOME_DIR` and derives the database and every other state path from it, so an
  absolute value you already export keeps the initialised store this is meant to
  exclude:
  ```bash
  TMPH=$(mktemp -d)
  ( trap 'rm -rf "$TMPH"' EXIT              # cleans up on every path
    HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" \
      uv run pytest test/path/to/test_x.py
    rc=$?                                   # not `status`: read-only in zsh
    echo "exit=$rc"                         # pytest's status, not rm's
    exit "$rc" )                            # ...and the block returns it too
  ```
  Keep the trap-in-a-subshell shape, and keep the diagnostic *inside* it. Cleaning up
  with `; rm -rf "$TMPH"` returns `rm`'s status instead of pytest's, so a failing run
  reports success; switching to `&&` fixes the status but leaks the temp directory on
  exactly the failures you wanted isolated. A trailing `echo "exit=$?"` *after* the
  subshell prints the right number but is itself the block's last command, so the block
  returns 0 and a script or agent checking `$?` still reads a failing run as a pass.
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

- Authoring a *new agent skill* (`SKILL.md`, frontmatter, evals) → no shipped skill covers this
  yet; follow the [Agent Skills specification](https://agentskills.io/specification) directly.
- Building a provider / plugin / MCP-apps view → **cao-provider** / **cao-plugin** /
  **cao-mcp-apps**.
- Launching or steering running agent sessions → **cao-session-management**.
