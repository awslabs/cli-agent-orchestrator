# cao-contributing reference: the non-`ci.yml` PR gates

Moved verbatim out of `SKILL.md`. The table now lives here, and
`test/test_cao_contributing_skill_accuracy.py` reads `SKILL.md` plus this
directory, pinning every row to the workflow files themselves. Keep the rows
byte-identical to what the workflows say.

### Other workflows that gate a PR

`ci.yml` is not the only workflow on a pull request. These run alongside it, and none sets
job-level `continue-on-error`, so every check they run is blocking.
`test/test_cao_contributing_skill_accuracy.py` pins this table to the workflow files.

| Workflow | Checks on the PR | Runs on | Blocking? |
|----------|------------------|---------|-----------|
| **Secret Scan** (`secret-scan.yml`) | `gitleaks`, `gitleaks config tests` | every PR to `main` | **Yes** — the config tests run locally as `uv run pytest test/test_gitleaks_config.py`; the scan itself is `gitleaks detect --config .gitleaks.toml` over the PR's commits |
| **cargo-deny** (`cargo-deny.yml`) | `cargo-deny (advisories, licenses, bans, sources)` | every PR to `main` | **Yes** — locally, `cargo deny --manifest-path tui/Cargo.toml --locked check` (global flags before the subcommand, as the action passes them) |
| **Test Antigravity CLI Provider** (`test-antigravity-cli-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, **any** of its `test/providers/test_<provider>_*.py` tests (globbed since #1005 — it used to be one hardcoded filename) or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Test Claude Code Provider** (`test-claude-code-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, **any** of its `test/providers/test_<provider>_*.py` tests (globbed since #1005 — it used to be one hardcoded filename), `pyproject.toml`, or the workflow | **Yes** |
| **Test Codex CLI Provider** (`test-codex-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, **any** of its `test/providers/test_<provider>_*.py` tests (globbed since #1005 — it used to be one hardcoded filename) or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Test Kiro CLI Provider** (`test-kiro-cli-provider.yml`) | `Unit Tests`, `Code Quality` | only PRs touching that provider, **any** of its `test/providers/test_<provider>_*.py` tests (globbed since #1005 — it used to be one hardcoded filename) or fixtures, `pyproject.toml`, or the workflow | **Yes** |
| **Robotics transport example** (`robotics-transport.yml`) | `Transport simulator` | only PRs touching the cross-zone transport example, agent-profile schema, CAO provider list, or the workflow | **Yes** — real MuJoCo and authenticated MCP tests; locally, `uv --directory examples/robotics/cross-zone-transport run --locked pytest` |
| **Robotics factory example** (`robotics-factory.yml`) | `Factory simulator` | only PRs touching the hierarchical factory example, agent-profile schema, CAO provider list, or the workflow | **Yes** — real MuJoCo and authenticated MCP tests; locally, `uv --directory examples/robotics/hierarchical-factory run --locked pytest` |
| **Docs site** (`gh-pages.yml`) | `build` | only PRs touching `docusaurus/**`, `patches/**`, `vendor/**`, `scripts/test-braces-security.cjs`, `examples/robotics/cross-zone-transport/images/**`, `examples/robotics/hierarchical-factory/images/**`, or the workflow | **Yes** — `deploy` is push-only and never runs on a PR |
| **Actions Security** (`zizmor.yml`) | `zizmor` | every PR to `main` | **Yes** — zizmor 1.30.1 over `.github/` at Medium or higher; locally, `uvx zizmor==1.30.1 --collect=all --min-severity medium --offline --config .github/zizmor.yml .github/`. Accepted findings and their reasons live in `.github/zizmor.yml`; fix the workflow or add a reasoned entry there, never an inline ignore |

> **Two checks can share a name.** Each provider workflow has its own `Unit Tests` and
> `Code Quality`, so a PR that touches `pyproject.toml` shows those names more than once.
> Read the workflow name next to a failing check before assuming it is the CI one —
> `gh pr checks <number>` lists every workflow, whereas `gh run list --workflow CI` sees
> only `ci.yml`.
