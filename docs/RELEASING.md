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
              ├─► verify-release-version (seconds: tag == pyproject.toml == uv.lock ==
              │                           devcontainer feature, and the version is on
              │                           neither TestPyPI nor PyPI; fails BEFORE the builds)
              ├─► build-wheels / build-sdist (per-platform wheels + one sdist, Web UI bundled)
              ├─► publish-testpypi (environment: testpypi, OIDC)
              ├─► smoke-test       (install from TestPyPI, verify entry points, run `cao tui`)
              └─► publish-pypi     (environment: pypi — MAINTAINER APPROVAL GATE)
```

`verify-release-version` exists because v2.5.1, v2.5.2 and v2.5.4 were each tagged on
a commit that had not been bumped, built already-published filenames for ~80 minutes,
and then failed at TestPyPI with `400 File already exists`. It runs
`scripts/assert_release_version.py`, whose failure text names the remedy for the
situation it found.

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
   - `verify-release-version` refuses, in seconds, a tag whose version
     `pyproject.toml`, `uv.lock` and the devcontainer feature do not all carry, or
     that TestPyPI or PyPI already serves.
   - `build-wheels` and `build-sdist` run (per-platform wheels + sdist with Web UI bundled).
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
3. `verify-release-version` checks that `pyproject.toml`, `uv.lock` and the
   devcontainer feature agree, and that this version is **not already on TestPyPI**
   (only TestPyPI is asked; PyPI state does not matter for this path). TestPyPI never
   accepts a filename twice, so a sanity-check build needs a version that has not
   been uploaded there before. If you dispatch from a `v*` tag, the tag is compared
   with the tree as well.
4. Then `build-wheels` + `build-sdist` + `publish-testpypi` run. Smoke-test and prod
   steps are skipped.

### Manual PyPI publish (escape hatch)

If a release was cut but the auto-pipeline failed partway through and you need
to retry the prod step:

1. Actions → **Publish to PyPI** → "Run workflow"
2. Select `environment: pypi`
3. `verify-release-version` compares the tag with the tree (the `pypi` environment
   only accepts `v*` tags) and checks the version is not already on PyPI. Then the
   builds run, `publish-testpypi` + `smoke-test` are skipped (they already ran), and
   `publish-pypi` hits the maintainer approval gate.

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

**`verify-release-version` fails ("tag vX.Y.Z names X.Y.Z but pyproject.toml says ..."):**
The tag was placed on a commit that was not bumped. Re-running the workflow on the
same tag fails the same way, because it checks out the same tree. Merge a
`chore: release vX.Y.Z` PR that bumps `pyproject.toml`, `uv.lock` and
`.devcontainer/features/cao/devcontainer-feature.json` together (the shape of #899),
then delete the tag and its GitHub Release and re-create both on that merge commit.

**`verify-release-version` fails ("... is already on https://test.pypi.org"):**
An earlier run of this version reached TestPyPI before failing later in the
pipeline. Neither index accepts a filename twice, so the version is spent: bump to
the next patch version through a `chore: release` PR and re-create the tag and
Release on it.
