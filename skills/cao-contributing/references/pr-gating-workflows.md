# cao-contributing reference: the non-`ci.yml` PR gates

Moved out of `SKILL.md`; the table now lives here, and
`test/test_cao_contributing_skill_accuracy.py` reads `SKILL.md` plus this
directory, pinning every row to the workflow files themselves. Keep the rows
accurate to what the workflows say.

Pull requests path-select filtered jobs; pushes to `main` and merge queue entries
run every filtered job. **Only merge non-failing pull requests** is not required
for these filtered jobs: no later queue entry can skip a previously failing job.

## Other workflows that gate a PR

`ci.yml` is not the only workflow on a pull request. These run alongside it, and none sets
job-level `continue-on-error`, so every check they run is blocking.
`test/test_cao_contributing_skill_accuracy.py` pins this table to the workflow files.

| Workflow | Checks on the PR | Runs on | Blocking? |
|----------|------------------|---------|-----------|
| **Secret Scan** (`secret-scan.yml`) | `gitleaks`, `gitleaks config tests` | every PR to `main` | **Yes** — the config tests run locally as `uv run pytest test/test_gitleaks_config.py`; the scan itself is `gitleaks detect --config .gitleaks.toml` over the PR's commits |
| **cargo-deny** (`cargo-deny.yml`) | `cargo-deny (advisories, licenses, bans, sources)` | every PR to `main` | **Yes** — locally, `cargo deny --manifest-path tui/Cargo.toml --locked check` (global flags before the subcommand, as the action passes them) |
| **Actions Security** (`zizmor.yml`) | `zizmor` | every PR to `main` | **Yes** — zizmor 1.30.1 over `.github/` at Medium or higher; locally, `uvx zizmor==1.30.1 --collect=all --min-severity medium --offline --config .github/zizmor.yml .github/`. Accepted findings and their reasons live in `.github/zizmor.yml`; fix the workflow or add a reasoned entry there, never an inline ignore |
| **Docs site** (`gh-pages.yml`) | `build` | only PRs to non-main branches touching `docusaurus/**`, `patches/**`, `vendor/**`, `scripts/test-braces-security.cjs`, `examples/robotics/cross-zone-transport/images/**`, `examples/robotics/hierarchical-factory/images/**`, or the workflow | **Yes** — deploy is push-only; PRs to `main` use CI's docs-build |

> **Provider checks now run inside `ci.yml`.** The four path-selected provider test/lint
> pairs are checked by `filtered-gate` and the aggregate `ci-gate` on PRs to `main`.
