# Releasing cli-agent-orchestrator

This project publishes to [PyPI](https://pypi.org/project/cli-agent-orchestrator/)
via GitHub Actions. The pipeline is designed so every release goes to
**TestPyPI first**, is smoke-tested, and then waits for a maintainer to approve
the production publish.

A release starts one of two ways: **every Monday at 09:17 UTC on its own**, when
something has merged since the last release, or **by hand**. Either way, nothing reaches
PyPI until a maintainer approves it.

## Pipeline overview

```
release.yml (weekly schedule, or manual)
  ├─► preflight      (scripts/release_preflight.py: on main, ci.yml green at the exact
  │                   commit, previous release complete, bump derived from the commits;
  │                   a scheduled run REFUSES a major and hands it to a person)
  ├─► release        (bump pyproject.toml + uv.lock + devcontainer feature + changelog,
  │                   run the same version check publish runs, then commit + tag + ONE
  │                   atomic push, then a GitHub Release)
  └─► start-publish  (schedule only: dispatch publish-to-pypi.yml `release` on the new tag)
        └─► publish-to-pypi.yml (a `release` dispatch, or a GitHub Release a person published)
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

## The weekly release

**Why it exists:** releases used to wait for someone to remember to cut one, and fixes,
some of them security-relevant, sat unreleased on `main` for weeks (#651).

**What it does, every Monday at 09:17 UTC** (UTC all year; cron does not follow
daylight saving):

1. Finds the previous release: the newest `vX.Y.Z` tag on `main`. Tags such as
   `v2.6.0rc1` are ignored.
2. If nothing has merged since, it stops. The run is green and nothing is released.
3. Works out the version bump from the commit messages
   ([Conventional Commits](https://www.conventionalcommits.org/)):

   | The commits since the last release contain | Bump |
   |---|---|
   | a subject with `!` (`feat!: …`, `build(deps)!: …`) or a `BREAKING CHANGE:` footer | **major**: refused, see below |
   | a `feat:` or `feat(scope):` subject | minor |
   | anything else | patch |

   Only the **subject** and the **footer** count, which is where the spec puts these
   markers. A `BREAKING CHANGE:` quoted in the middle of a description, in a code block
   or after `>`, does not make a release major.
4. Releases, then starts **Publish to PyPI** on the new tag. TestPyPI and the smoke test
   run, and the PyPI upload waits for a maintainer's approval as always.

**When it refuses** (the run is red and nothing is bumped, tagged or published):

| The run says | Why | What to do |
|---|---|---|
| `a scheduled run never cuts a major release` | A commit since the last release is marked breaking. A major is never cut with nobody watching. | If it really is breaking, run **Release** by hand with `bump=major`. Every scheduled run refuses until then. |
| `carry a breaking-change marker outside the subject and the footer` | A body line looks like a breaking marker. In a squash merge it can be a real break from one of the squashed commits, so the schedule cannot tell. | Read the line in the run summary. Run **Release** by hand: `bump=major` if it is breaking, otherwise the bump the summary shows. |
| `ci.yml is '…' at <sha>` | CI is missing, still running, or red for that exact commit. | Wait for CI or fix `main`. The next Monday tries again. |
| `the previous release vX.Y.Z is not a published GitHub Release` / `not verifiably on PyPI` | The last release stopped half way. The schedule never skips past it. | Finish that release (see the escape hatches below), or supersede it by hand. |

## Cutting a release by hand

1. Trigger **`Release`** workflow via Actions → "Run workflow" on `main`:
   - Pick `patch`, `minor`, or `major`. The run summary warns you if the commits call
     for a different bump, and if the previous release is incomplete.
   - It refuses to run from any branch but `main`, or on a commit whose `ci.yml` run is
     not green.
   - `release.yml` bumps `pyproject.toml`, `uv.lock` and the devcontainer feature, runs
     `git-cliff` to update `CHANGELOG.md`, checks the result the way
     `verify-release-version` will, then commits, tags `v<version>`, pushes both in one
     atomic push, and creates a GitHub Release.
   - If `uv lock` would change anything in `uv.lock` beyond this project's own version
     line, the bump refuses: the release commit goes to `main` without a PR, so it may
     carry the bump and nothing else. Relock `main` in a reviewed PR first.
2. A GitHub Release created by the workflow does not start **`Publish to PyPI`**: a
   release made with the workflow's own token triggers no workflows. Start it yourself:
   Actions → **Publish to PyPI** → "Run workflow", **use workflow from the tag**
   `v<version>`, `environment: release`. You are then that run's actor, so another
   maintainer approves the PyPI step. It runs:
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

### Full release chain from a tag (`environment: release`)

Runs exactly what a published GitHub Release runs: `verify-release-version` (tag, tree
and both indexes), the builds, `publish-testpypi`, `smoke-test`, then `publish-pypi`
behind the maintainer approval gate. The scheduled release dispatches this mode on the
tag it pushed. Dispatched from a branch instead of a `v*` tag, it fails in
`verify-release-version`.

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

**Release preflight refuses a major, or a marker outside a subject or footer:**
See the table under "The weekly release". The schedule will not guess; run **Release** by
hand with the bump you intend.

**Release fails at "Bump version and generate changelog" with "`uv lock` changed N
line(s) of uv.lock beyond the cli-agent-orchestrator version":**
The uv on the runner would rewrite more of the lock than the version line, usually
because the lock was written by a different uv. Nothing was committed. Relock `main` in
a reviewed PR with the uv CI uses (`uv lock`, then check the diff), then release again.
