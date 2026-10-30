---
name: cao-contributing
description: Contribute changes to the CAO (CLI Agent Orchestrator) codebase — the local
  dev loop, the CI gate map, and the pre-PR checklist. Use when the user says "open a PR",
  "why did CI fail", "run the checks before I push", "the mypy/Code Quality job is red",
  "add a test and verify coverage", "bump a dependency", "the uv.lock diff is noisy",
  "a CVE blocks the dependency gate", or when making any code change intended to land on a
  branch/PR. Covers uv-based build/test/lint, lockfile and CVE-floor rules, the ci.yml jobs
  and their pass/fail semantics, and the golden rules that stop a green-locally / red-in-CI
  surprise. Not for authoring agent skills, building providers/plugins/MCP-apps, or
  operating running sessions.
---

# Contributing to CAO

How to make a change to the **cli-agent-orchestrator** codebase and get it through CI
cleanly. Read this before pushing a branch or opening a PR. The canonical human docs are
[`DEVELOPMENT.md`](https://github.com/awslabs/cli-agent-orchestrator/blob/main/DEVELOPMENT.md),
[`CONTRIBUTING.md`](https://github.com/awslabs/cli-agent-orchestrator/blob/main/CONTRIBUTING.md), and
[`CODEBASE.md`](https://github.com/awslabs/cli-agent-orchestrator/blob/main/CODEBASE.md) —
this skill is the operational checklist that mirrors what CI actually enforces.

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
   workflow key can turn a tolerated warning into a hard failure, or the reverse.
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
uv run ruff check src/ test/ examples/   # dead code -- BLOCKING, and green on main
uv run python scripts/mypy_ratchet.py  # type check -- the gate CI runs (see note below)
uv run mypy src/                    # the raw mypy output, if you want to read it
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
| **Unit Tests** (3.11 / 3.12) | `uv run pytest test/ examples/workflow/tests/ --ignore=test/providers/test_kiro_cli_integration.py --ignore=test/e2e -m "not e2e" --cov=src/cli_agent_orchestrator --cov-report=term-missing` | **Yes** |
| ↳ step: **Validate Markdown links** | `uv run python scripts/validate_markdown_links.py` — every relative link in every tracked `.md`, including `skills/` | **Yes** |
| **Code Quality** | black `--check`, isort `--check-only`, `uv run ruff check src/ test/ examples/`, then `uv run python scripts/mypy_ratchet.py` | **Yes** (all four; the mypy step's `continue-on-error` was removed in issue #1005) |
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
| **CodeQL** | GitHub Actions, JavaScript/TypeScript, Python, and Rust analysis and upload in the same CI run, including forks after required workflow approval | **Yes** — no project build; the hosted scan/status rules remain as documented in `SECURITY.md` |
| **Dependency Security** | Full locked npm, Bun, uv, and Cargo inventories, including development dependencies and unfixed advisories; independent clean-install mitigation checks | **Yes** — every HIGH/CRITICAL finding or scan error fails; locally, `scripts/security-scan.sh dependencies` with Trivy installed |
| **Dependency Review** | `actions/dependency-review-action` over the PR's dependency delta: `fail-on-severity: high` plus denied licences `GPL-3.0`/`AGPL-3.0` | **Yes** — CI-only; there is nothing to run locally, and it is skipped on forks (`if: github.repository == 'awslabs/cli-agent-orchestrator'`), so a green run on your fork has not exercised it |

CodeQL's four language jobs are part of `ci.yml`, so **Re-run all jobs** includes
them. The standalone `codeql.yml` is only for weekly and manual scans; both use
the same maintainer-owned scan action. Existing PR branches must pick up the
current `main` workflow before their new CI runs use this wiring. Resolve merge
conflicts, update the branch, and approve fork workflows when required rather
than merely rerunning a CI run created before the change. An **Expected**
required check without an actual job is not a running scan.

Dependency Security also runs weekly and on manual dispatch through
`dependency-security.yml`, using the same action as CI. A passing local-patch
check does not exempt an open HIGH/CRITICAL advisory. See
[`SECURITY.md`](https://github.com/awslabs/cli-agent-orchestrator/blob/main/SECURITY.md#full-dependency-gate) for inventory artifacts,
failure states, and the required-status rollout after the workflow lands.

> **The `-m "not e2e"` on the CI command replaces your local `addopts` — it does not
> compose with it.** Since #1005 the two agree: `addopts` is `-m 'not e2e'`, so a default
> local run uses the same marker expression CI does — the path differences below are what
> still separates the two populations. Before that change, `addopts` read
> `-m 'not e2e and not integration'`, a strictly narrower marker than CI's, and a local
> run could be green while CI was red. If you override `-m` yourself, compare deselected
> counts, not just pass counts.
>
> **CI's selection is narrower than the marker alone implies.** The same command passes
> `--ignore=test/providers/test_kiro_cli_integration.py` and `--ignore=test/e2e`, and
> `--ignore` wins over `-m`: that Kiro provider test needs an authenticated external CLI
> and **never runs in CI**. So *most* `integration`-marked tests do run in CI — but not
> that one, and nothing in CI covers it. If you change it, you have to run it yourself.

> **mypy is blocking, via a ratchet** (issue #1005). The repo has **known, pre-existing,
> repo-wide mypy errors** — 177 of them in 22 files when the ratchet landed, concentrated in
> `services/agui/run_plane.py`, `utils/agent_profiles.py`, `utils/orchestration.py`,
> `mcp_server/server.py`, and `services/vault/reader.py`.
> They are frozen in [`mypy-baseline.json`](https://github.com/awslabs/cli-agent-orchestrator/blob/main/mypy-baseline.json) per file and per error
> code, and `scripts/mypy_ratchet.py` fails only when a count **rises**. Therefore:
> - **Your change must add zero *new* mypy errors.** Check the delta, not the raw count —
>   which is exactly what `uv run python scripts/mypy_ratchet.py` reports. A new error in one
>   file does not cancel out a fix in another; the comparison is per file per code.
> - **Do not** bundle unrelated repo-wide type-fixes into a feature PR (they belong in a
>   dedicated cleanup PR) — but if you *do* fix some, run
>   `uv run python scripts/mypy_ratchet.py --update` and commit the lowered baseline.
> - **Raising the baseline needs a reviewer's explicit OK.** `--update` will write a higher
>   number without complaint, so nothing but the review stops you. Prefer a
>   `# type: ignore[code]` with a reason on the offending line, which is visible where the
>   problem is.
> - **Do not add `continue-on-error` to the mypy step.** That key is the whole difference
>   between a gate and a decoration, and `test/scripts/test_mypy_ratchet.py` asserts it stays
>   absent from the Code Quality job.
> - `mypy.ini` at the repo root is the config mypy actually loads. The `[tool.mypy]` section
>   in `pyproject.toml` is shadowed by it and has no effect — including its `strict = true`.

### Other workflows that gate a PR

`ci.yml` is not the only workflow on a pull request: secret scanning, cargo-deny, the
per-provider suites, the two robotics examples, the docs site and zizmor gate too, and none of
them tolerates a failure. The full table, with the local command for each and the warning that
two checks can share a name, is in
[`references/pr-gating-workflows.md`](references/pr-gating-workflows.md).

## Testing gotchas

- **The full `uv run pytest test/` is flaky locally** — it needs a running server, tmux,
  and real CLI binaries, and can hit a flaky OTel/gRPC abort. Run **targeted test files**
  while iterating and lean on CI (the Unit Tests job) for breadth; get authoritative
  missing-coverage lines from that job's `term-missing` output ∩ your diff. **CI is not the
  full suite, though** — it excludes `test/e2e` and the Kiro provider integration test by
  path (see the callout above), so those two are only ever covered by someone running them
  deliberately.
- **A test that touches CAO's own state can pass only on your machine.** Run it under a
  throwaway `HOME` **and** a throwaway `CAO_HOME_DIR` — `HOME` alone is not enough. The recipe,
  and why its exact shape decides whether a failing run reports success, is in
  [`references/test-isolation.md`](references/test-isolation.md).
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

## Dependency and lockfile changes

A lockfile change should contain only the change you meant. These rules come from
[#1119](https://github.com/awslabs/cli-agent-orchestrator/pull/1119), where each one cost time
to rediscover.

1. **Relock with the uv CI uses, not an older local one.** CI's `setup-uv` installs the latest
   release and nothing pins a version, so run `uvx uv@latest lock` in every project whose
   lockfile you change — `git ls-files '*uv.lock'` lists them, and it is more than the root one.
   If `git diff uv.lock` changes the `revision =` header or the markers of packages you did not
   touch, suspect your uv first: a local 0.12.20 once rewrote `revision = 5` to `3`, where
   0.13.6 changed only the 4 intended lines. Confirm `uvx uv@latest --version` is really the
   newest uv release. If that uv still rewrites the header, keep the change and say so in the
   PR — an older uv (Dependabot's, say) wrote the committed lock, and no uv version gives a
   quieter diff.
2. **A lockfile pin does not protect `pip install` — for the base install.** `uv.lock` governs
   `uv sync`, not what pip resolves from the published metadata. The gate scans far more than
   the base install, though, so find out where the package sits before you declare anything:
   `uv export --frozen --no-dev --no-hashes | grep -i '^<name>=='` (grep the PEP 503 normalised
   name — lowercase, `_` and `.` as `-` — which is how `uv export` prints it). A match means the
   base install gets it, and a CVE fixed only by bumping it **transitively** in the lock also
   needs a floor in `[project].dependencies`, with a comment naming the CVE and the dependency
   path, as `cryptography` and `dnspython` do in `pyproject.toml`. No match? Re-run the export
   with `--all-extras`: a match there means an extra, and the floor belongs in that extra's
   `[project.optional-dependencies]` list, not in the base dependencies, which stay lean. Still
   no match means dev-only, and the lock bump is the whole fix.
3. **Check the floor in the built wheel**, not the source: `uv build --wheel --clear`, then
   `unzip -p dist/*.whl '*.dist-info/METADATA' | grep Requires-Dist`. Keep the `--clear` (or
   build into a fresh `--out-dir`): a plain `uv build` leaves earlier wheels in place, the glob
   then expands to several, and `unzip` reads the first while treating the rest as member names.
   It exits 11, but the pipe reports `grep`'s status, so a floor read off a stale wheel looks
   like a pass.
4. **Run the dependency gate locally** with the Trivy version CI pins (see `SECURITY.md`):
   `uv run python scripts/dependency_security.py`. Every HIGH or CRITICAL finding blocks, and
   local mitigations are not exemptions.

## Pre-PR checklist

1. `uv run black src/ test/ && uv run isort src/ test/` (or `--check` to verify).
2. `uv run ruff check src/ test/ examples/` — must be **clean**. Blocking, like the mypy
   ratchet, but with no tolerated backlog at all, so any finding is yours. Do not reach for
   `--fix` on a pytest fixture re-import: ruff cannot see that pytest injects the name,
   and removing it breaks collection (it turned `test/decisions` into 113 errors once).
   Use `# noqa: F401  # <reason>` there. Selected rules and the deferred ones (F811, ARG,
   B) are documented in `pyproject.toml`.
3. `uv run python scripts/mypy_ratchet.py` — must print `PASS`. This is the blocking gate;
   pre-existing errors are fine, new ones are not. If it reports improvements, run it again
   with `--update` and commit `mypy-baseline.json`.
4. `uv run pytest <targeted files>` green; add/keep tests for changed behavior.
5. If you touched **any** `.md`, run `uv run python scripts/validate_markdown_links.py` —
   a dead relative link fails the Unit Tests job, and `skills/` is in scope. CAO has no
   root `AGENTS.md`; the contributor map is `CODEBASE.md`.
6. If you touched `pyproject.toml` or a uv lockfile, follow
   [Dependency and lockfile changes](#dependency-and-lockfile-changes) first. For npm, Bun or
   Cargo lockfiles only rule 4, the dependency gate, applies — the rest of that section is
   uv-specific.
7. If you touched `skills/`, run `uv run python scripts/sync_skills.py` so the packaged mirror
   stays in lockstep (`test/test_skill_packaging_parity.py` enforces it). A skill that also
   ships in a plugin package under `agent-plugin/` has a *third* copy: rebuild it with
   `make agent-plugin` and commit `agent-plugin/`, or the Unit Tests job's
   `make check-agent-plugin` step goes red. The rebuild pins the version in `pyproject.toml`,
   which can run ahead of the pin the committed packages carry; the release workflow's
   `regenerate-agent-plugin` job moves that pin after publishing, so a contributor PR should
   not. No flag keeps it, so read `git diff agent-plugin/` — if the only change beyond your
   skill edit is the version moving, restore the version strings — and finish with
   `make check-agent-plugin`.
8. **Commits:** only when asked; sign if the repo expects it; keep the subject concise and
   Conventional-Commits style; never force-push to `main`.
9. **Open the PR, then watch its CI run to completion** and fix any red gate before calling
   it done (Golden rules #2 and #4). Use `gh pr create` / `gh pr checks`.

## Not what you want?

- Authoring a *new agent skill* (`SKILL.md`, frontmatter, evals) → no shipped skill covers this
  yet; follow the [Agent Skills specification](https://agentskills.io/specification) directly.
- Building a provider / plugin / MCP-apps view → **cao-provider** / **cao-plugin** /
  **cao-mcp-apps**.
- Launching or steering running agent sessions → **cao-session-management**.
