# Releasing cli-agent-orchestrator

This project publishes to [PyPI](https://pypi.org/project/cli-agent-orchestrator/)
via GitHub Actions. The pipeline is designed so every release goes to
**TestPyPI first**, is smoke-tested, and then waits for a maintainer to approve
the production publish.

## Pipeline overview

```
release.yml (manual)
  └─► bump version + changelog + tag + GitHub Release
        └─► publish-to-pypi.yml (triggered by Release publish)
              ├─► build            (build wheel + sdist once, bundle Web UI)
              ├─► publish-testpypi (environment: testpypi, OIDC)
              ├─► smoke-test       (install from TestPyPI, verify entry points)
              └─► publish-pypi     (environment: pypi — MAINTAINER APPROVAL GATE)
```

Authentication uses [PyPI Trusted Publishing (OIDC)](https://docs.pypi.org/trusted-publishers/) —
no API token secrets are stored in GitHub.

---

## One-time setup (maintainer, per project)

### 1. Configure Trusted Publishers

**PyPI:**
1. Go to https://pypi.org/manage/account/publishing/
2. "Add a new pending publisher" with:
   - PyPI project name: `cli-agent-orchestrator`
   - Owner: `awslabs`
   - Repository: `cli-agent-orchestrator`
   - Workflow: `publish-to-pypi.yml`
   - Environment: `pypi`

**TestPyPI:**
1. Go to https://test.pypi.org/manage/account/publishing/
2. Same values, but environment: `testpypi`

### 2. Configure GitHub Environments

In GitHub: **Settings → Environments**.

**`testpypi`** — no restrictions. (Smoke-test runs here; no approval needed.)

**`pypi`** — this is the approval gate:
- **Required reviewers:** add the maintainer team / usernames who can approve prod releases.
- **Deployment branches and tags:** restrict to tags matching `v*` so only tagged releases can promote to prod.

### 3. Verify Web UI build works in CI

Nothing to configure — the `build` job runs `npm ci && npm run build` in `web/`
and copies `web/dist/` into `src/cli_agent_orchestrator/web_ui/` before `uv build`.
The wheel artifact includes this per `[tool.hatch.build].artifacts` in
`pyproject.toml`.

---

## Cutting a release

1. Trigger **`Release`** workflow via Actions → "Run workflow":
   - Pick `patch`, `minor`, or `major`
   - `release.yml` bumps `pyproject.toml`, runs `git-cliff` to update `CHANGELOG.md`,
     commits, tags `v<version>`, pushes, and creates a GitHub Release.
2. GitHub Release publish automatically triggers **`Publish to PyPI`**:
   - `build` runs (wheel + sdist with Web UI bundled).
   - `publish-testpypi` publishes to TestPyPI.
   - `smoke-test` installs from TestPyPI and runs `cao --help`, `cao-server --help`,
     `cao-mcp-server --help`.
   - `publish-pypi` **pauses** waiting for a maintainer to approve in the `pypi`
     environment. A required reviewer clicks **Review deployments → Approve**
     on the Actions run to ship to prod.

### Manual TestPyPI-only publish (no release)

Sometimes you want to sanity-check a build without cutting a real release:

1. Actions → **Publish to PyPI** → "Run workflow"
2. Select `environment: testpypi`
3. Only `build` + `publish-testpypi` run. Smoke-test and prod steps are skipped.

### Manual PyPI publish (escape hatch)

If a release was cut but the auto-pipeline failed partway through and you need
to retry the prod step:

1. Actions → **Publish to PyPI** → "Run workflow"
2. Select `environment: pypi`
3. `build` runs, `publish-testpypi` + `smoke-test` are skipped (they already
   ran), and `publish-pypi` hits the maintainer approval gate.

---

## Troubleshooting

**"pending publisher" error on first publish:**
Expected. PyPI marks the publisher pending until the first successful run. After
that it becomes permanent.

**Smoke-test fails with "No matching distribution":**
TestPyPI index can take up to a minute to propagate. The workflow sleeps 30s;
if that's not enough, increase it or retry.

**Approval required but button missing:**
Check `Settings → Environments → pypi → Required reviewers`. You must be listed
there.

**Version mismatch between wheel and tag:**
`scripts/bump_version.py` updates `pyproject.toml` and `release.yml` tags on the
bumped version. If drift occurs, fix `pyproject.toml` manually and re-run the
release workflow.
