# Requirements: clear GHSA-5p4m-2wfm-xmqj (js-yaml) from `docusaurus/package-lock.json`

**Issue:** [#568](https://github.com/awslabs/cli-agent-orchestrator/issues/568)
**Scope:** `security-patch` — lockfile-only dependency bump plus one CI-observability
change.
**Status:** Specified, not implemented.

---

## Context

Trivy code-scanning alert **#201** flags `js-yaml@4.3.0` in
`docusaurus/package-lock.json:11016` as vulnerable to **GHSA-5p4m-2wfm-xmqj**
(HIGH, CVSS 7.5, CWE-407). It is the **only** open code-scanning alert on the
repo and the `Security Scan` job is the only failing job on `main`.

Nothing in the repository changed to cause this. The advisory was published
2026-08-06T20:27:32Z against an unchanged pin; the alert was created
2026-08-07T04:24:47Z. `main`'s CI went red on the first run after that.

**The one hard constraint someone must internalise:** the `Security Scan` gate
does **not** run at `CRITICAL,HIGH`. `trivy-action`'s `entrypoint.sh:76-83`
executes `unset TRIVY_SEVERITY` whenever `format` is `sarif` and
`limit-severities-for-sarif` is not `true` — which is the case in
`.github/workflows/ci.yml:763-772`. The job therefore fails on a finding of
**any** severity, including LOW and UNKNOWN. Verifying a fix with
`--severity CRITICAL,HIGH` (as `SECURITY.md:102` and `scripts/security-scan.sh:31-35`
both instruct) checks a **narrower** condition than the gate and can report a
false all-clear. Every acceptance check in this spec runs at the gate's real,
unrestricted severity.

## Dependency status

| Dependency | What it provides | Verified state |
|---|---|---|
| `js-yaml@4.3.1` | Patched `resolveYamlOmap` (O(n) key set) | **Published on npm.** `npm view js-yaml versions` lists `4.3.1`; `dist-tags.v4-legacy = 4.3.1`. Tarball downloaded and read. |
| GHSA-5p4m-2wfm-xmqj | The advisory being cleared | **Open, reviewed.** `gh api advisories/GHSA-5p4m-2wfm-xmqj`: severity `high`, CVSS 7.5, no CVE assigned, ranges `>=4.0.0 <4.3.1` → `4.3.1` and `>=3.0.0 <3.15.1` → `3.15.1`. |
| Code-scanning alert #201 | The CI-blocking signal | **Open.** Sole open alert; `gh api .../code-scanning/alerts?state=open` returns exactly 1. |
| Dependabot | Would otherwise auto-fix | **Enabled but has NOT filed a PR.** `dependabot_security_updates: enabled`, `automated-security-fixes: {enabled: true, paused: false}`, yet `dependabot/alerts?state=open` returns **0** and no js-yaml PR exists. Manual fix required; see FR-7. |
| `#551` / PR #552 | Prior art for this shape | **Merged** (`dd32f15`). Same shape, but its `bun update` step was **insufficient** and needed a `package.json` `overrides` entry — a difference this spec resolves for npm (see DR-1). |
| Docs site build | Must survive the bump | **Green with 4.3.1.** `npm ci && npm run typecheck && npm run build` all pass locally on the patched lockfile. |

## Provenance

One row per claim this spec relies on.

| # | Claim | Label | Evidence |
|---|---|---|---|
| P-1 | The vulnerable code is `resolveYamlOmap`'s `objectKeys.indexOf(pairKey)` inside the per-element loop, making resolution O(n²). | **OBSERVED** | Read `lib/type/omap.js:11-31` from the extracted `js-yaml-4.3.0.tgz`. |
| P-2 | 4.3.1 fixes it by replacing the array with an object and `_hasOwnProperty.call` + `Object.defineProperty`. | **OBSERVED** | `diff -u p430/lib/type/omap.js p431/lib/type/omap.js` — a 4-line change. |
| P-3 | 4.3.1 changes **nothing else**. | **OBSERVED** | `diff -rq` of the two tarballs: only `lib/type/omap.js`, the 6 `dist/` build artifacts, and `package.json`'s `version` field differ. No API, dependency, or engine change. |
| P-4 | The quadratic path requires an **explicit `!!omap` tag**; untagged YAML is unaffected. | **OBSERVED** | `omap` is in the `explicit:` list of `lib/schema/default.js`, not `implicit:`. Measured: 32 000 tagged entries = 655.9 ms; the same 32 000 entries **untagged** = 49.2 ms (no quadratic term). |
| P-5 | Growth is quadratic and material at realistic sizes. | **OBSERVED (measured)** | 4.3.0, `yaml.load` of an `!!omap`: n=4 000 → 17.7 ms; 8 000 → 50.9 ms; 16 000 → 172.1 ms; 32 000 → 655.9 ms (≈3.8× per doubling). Same inputs on 4.3.1: 8.6 / 16.6 / 26.5 / 74.0 ms. A 490 KB document costs 656 ms on one core. |
| P-6 | A nested `!!omap` under a mapping key also triggers it — the tag need not be the document root. | **OBSERVED (measured)** | `title: x\nitems: !!omap\n  - …` × 32 000 → 631.0 ms on 4.3.0. This is the shape reachable through markdown frontmatter. |
| P-7 | `!!omap` semantics are byte-identical across the bump, including for prototype-polluting key names. | **OBSERVED** | 6 cases (`valid`, `dup`, `__proto__`, `__proto__` dup, `hasOwnProperty`, `toString` dup) produce identical results on 4.3.0 and 4.3.1: same values, same throws. The `Object.defineProperty` form is what keeps `__proto__` from being swallowed. |
| P-8 | The `Security Scan` gate runs at **all** severities, not `CRITICAL,HIGH`. | **OBSERVED** | `entrypoint.sh:76-83` at the pinned SHA `57a97c7e` (= tag `v0.35.0`) does `unset TRIVY_SEVERITY`. Confirmed in the live log of run 31177773018: `Building SARIF report with all severities` / `Running Trivy with options: trivy fs .`. |
| P-9 | With severity unrestricted, `js-yaml` is the repo's **only** finding — of any severity. | **OBSERVED (measured)** | `TRIVY_IGNORE_UNFIXED=true TRIVY_PKG_TYPES=os,library trivy fs .` (severity unset, trivy 0.69.0, DB 2026-08-07) → `{'HIGH': 1}`, zero MEDIUM/LOW/UNKNOWN, zero secrets, zero misconfigurations. |
| P-10 | Bumping only `js-yaml` makes the scan clean. | **OBSERVED (measured)** | Same unrestricted scan against the patched lockfile → **0 findings**. |
| P-11 | `npm update js-yaml` produces a minimal, correct lockfile edit and does **not** add a direct dependency. | **OBSERVED (measured)** | Ran it (both with and without `--package-lock-only`). Diff = exactly 8 lines: `version`, `resolved`, `integrity` at `node_modules/js-yaml`. `package.json` byte-identical; `packages[""].dependencies` unchanged. |
| P-12 | There is exactly **one** `js-yaml` node in the tree — no nested copies to miss. | **OBSERVED** | Enumerated `Object.keys(lock.packages)`: the only match is `node_modules/js-yaml`. This is why npm's flat hoist suffices where bun's did not for #551. |
| P-13 | All five dependents declare `^4.1.0`, which already admits 4.3.1. | **OBSERVED** | Walked every `dependencies`/`devDependencies`/`peerDependencies` block in the lockfile: `@11ty/gray-matter`, `@docusaurus/plugin-content-docs`, `@docusaurus/utils`, `@docusaurus/utils-validation`, `cosmiconfig` — all `^4.1.0`, all under `dependencies`. |
| P-14 | The docs site builds and typechecks on 4.3.1. | **OBSERVED (measured)** | `npm ci` → `npm run typecheck` (exit 0) → `npm run build` → `[SUCCESS] Generated static files`. Installed tree confirmed at `4.3.1`. |
| P-15 | The failing job's log names no finding, so the failure reads as a phantom. | **OBSERVED** | Full log of the `Security Scan` job in run 31177773018: SARIF goes to `trivy-results.sarif`; the log ends after the scanner notices with no findings table. |
| P-16 | `Security Scan` is the only failing job on `main`. | **OBSERVED** | Run 31177773018 (`e592b21`, current `main` and this branch's HEAD): 13 success, 1 failure (`Security Scan`), 1 skipped (`Dependency Review`). |
| P-17 | The docs site build is gated on PRs, so a lockfile regression is caught pre-merge. | **OBSERVED** | `.github/workflows/gh-pages.yml:10-18` triggers on `pull_request` with `paths: docusaurus/**`; the `build` job runs `npm ci`, `typecheck`, `build`. Deploy is gated on `github.event_name == 'push'`. |
| P-18 | `uv export` — run by both CI and the local wrapper — **mutates the tracked `uv.lock`**. | **OBSERVED (measured)** | Ran `uv export --format requirements-txt` twice with uv 0.9.4; each time `git status` showed ` M uv.lock` (16 insertions / 16 deletions: the `cli-agent-orchestrator` version reverting 2.4.1 → 2.4.0, plus marker churn). Restored both times. |
| P-19 | The YAML `js-yaml` actually parses here is repo-authored frontmatter and config. | *source-read inference* | Read the call sites: `@11ty/gray-matter/lib/engines.js:15` (`parse: yaml.load.bind(yaml)`), reached from `@docusaurus/utils/lib/markdownUtils.js:18`; `cosmiconfig/dist/loaders.js:47-50`; `@docusaurus/utils/lib/dataFileUtils.js:18`; `@docusaurus/utils-validation/lib/{tagsFile,validationUtils}.js`; `@docusaurus/plugin-content-docs/lib/sidebars/index.js:19`. All are build-time. Inference: no untrusted input reaches them, because the site is built from this repo's own tree. |
| P-20 | No file in the repo contains an `!!omap` tag today. | **OBSERVED** | `grep -rn '!!omap' .` excluding `node_modules` → zero hits. So the quadratic path is not currently exercised at all. |
| P-21 | The advisory is a non-backport of CVE-2026-59870 / GHSA-724g-mxrg-4qvm. | **OBSERVED** | `gh api advisories/GHSA-724g-mxrg-4qvm`: CVE-2026-59870, **medium**, range `>=5.0.0 <=5.2.0` → patched `5.2.1`. Note the severity differs from this advisory's `high`. |
| P-22 | Dependabot did not open a PR for this despite being enabled. | **OBSERVED** | Settings show enabled + unpaused; `dependabot/alerts?state=open` returns 0 while Trivy reports the finding. The two scanners disagree. |

**The design is load-bearing on P-8, P-10, P-11, P-12 and P-14.** P-8 sets the
acceptance bar (get it wrong and a passing local check still ships a red gate).
P-10 says the one-package bump is sufficient. P-11 and P-12 together are why no
`package.json` change is needed here even though #551 needed one. P-14 says the
bump is safe for the only consumer that matters.

## Functional requirements

### FR-1 — Resolve `js-yaml` to a patched version

The lockfile at `docusaurus/package-lock.json` SHALL resolve
`node_modules/js-yaml` to `4.3.1` or later within the 4.x line, with matching
`resolved` URL and `integrity` hash.

*Rationale:* 4.3.1 is the first 4.x release carrying the O(n) `resolveYamlOmap`
(P-2), and per P-3 it changes nothing else, so it is the smallest sufficient
move. Staying in 4.x rather than jumping to 5.x keeps every `^4.1.0` constraint
(P-13) satisfiable without touching a single dependent.

### FR-2 — Do not add a direct dependency

`docusaurus/package.json`'s `dependencies` and `devDependencies` SHALL be
byte-identical before and after the change.

*Rationale:* `js-yaml` is transitive-only (P-13). Promoting a build-time
transitive to a declared dependency of the docs site would make the repo
responsible for a package it does not import, and would survive long after the
advisory stops mattering. #551 hit exactly this trap: `bun update` "adds a
direct dependency" (PR #552's own commit message).

### FR-3 — Change no file other than the lockfile in the fix commit

The dependency-fix change SHALL touch exactly one file:
`docusaurus/package-lock.json`, and within it only the three lines identified in
P-11.

*Rationale:* per P-11 and P-12 the minimal edit is also the complete one. Any
additional lockfile churn indicates `npm` resolved something beyond the target
and needs review before it ships inside a security patch.

### FR-4 — Clear the gate at the gate's real severity

After the change, `trivy fs .` run with `TRIVY_IGNORE_UNFIXED=true`,
`TRIVY_PKG_TYPES=os,library` and **`TRIVY_SEVERITY` unset** SHALL report zero
vulnerabilities.

*Rationale:* this is the exact condition `Security Scan` evaluates (P-8), not
the `CRITICAL,HIGH` condition the repo's own docs describe. P-9 establishes the
baseline is already clean at every severity, so this requirement is currently
satisfiable — but only because nothing else is outstanding. **This FR
deliberately contradicts `SECURITY.md:102` and `scripts/security-scan.sh:31-35`;
FR-6 reconciles them.**

### FR-5 — Keep the docs site building

`npm ci`, `npm run typecheck` and `npm run build` in `docusaurus/` SHALL all
succeed on the patched lockfile.

*Rationale:* the docs site is the only consumer of this lockfile, and per P-17
its build is a PR gate — so a regression blocks the merge rather than reaching
`main`. P-14 records that this already passes.

### FR-6 — Make the documented local scan match the real gate

`scripts/security-scan.sh` and `SECURITY.md`'s "Running Security Scans Locally"
section SHALL describe an invocation whose pass/fail condition is no narrower
than the CI gate's.

*Rationale:* today both instruct `--severity CRITICAL,HIGH`, which per P-8 is
strictly narrower than what CI enforces. A contributor who follows the
documentation, sees green, and pushes can still turn `main` red on a MEDIUM
finding. The documentation is wrong about the tool it documents; that is a
defect independent of this advisory. **This is the one requirement here that the
issue does not ask for.**

### FR-7 — Land the fix manually rather than waiting for Dependabot

The fix SHALL be committed by this change and SHALL NOT be deferred to an
automated dependency PR.

*Rationale:* Dependabot security updates are enabled and unpaused, yet per P-22
it reports **0** open alerts for a package Trivy calls HIGH — GitHub's advisory
database has the advisory (it answered `gh api advisories/…`) but Dependabot has
not raised it against this lockfile. Waiting on a bot that has not fired leaves
`main` red indefinitely.

### FR-8 — Surface the finding in the job log

The `Security Scan` job SHALL print the identity of any finding that fails it —
at minimum package, installed version, severity, and advisory ID — to the job's
own log.

*Rationale:* per P-15, `format: 'sarif'` sends findings to a file, so the job
that failed the build names nothing. The current log shows a scanner starting,
some notices, and a non-zero exit. Diagnosing #568 required an out-of-band
`gh api` call to the code-scanning endpoint to learn what the failure even was.
Covers the issue's "worth considering separately" paragraph, which this spec
promotes to in-scope: it is the reason this issue took a manual investigation to
open. *Corresponds to AC-6.*

### FR-9 — Preserve the SARIF upload

The SARIF report SHALL continue to be produced and uploaded to the Security tab
on both passing and failing runs.

*Rationale:* the `if: always()` at `ci.yml:779` and its comment are load-bearing
— without it, a finding fails the scan step and skips the upload, hiding the very
finding that failed the build. FR-8 must not be implemented in a way that
displaces the SARIF output; hence the design's second-step approach (DR-2).

## Non-functional requirements

### NFR-1 — No new network or build-time cost in the docs build

The change SHALL NOT increase `docusaurus/`'s installed dependency count.

*Rationale:* a version bump of an existing single node (P-12) with no dependency
change (P-3) alters no edge in the graph.

### NFR-2 — The added Trivy step SHALL NOT extend the job by more than one scan

Any step added for FR-8 SHALL reuse the already-populated Trivy cache rather
than re-downloading the vulnerability DB.

*Rationale:* the DB download is 103.74 MiB and took 3.7 s of the job's 28 s in
run 31177773018. `TRIVY_CACHE_DIR` is set by the action on every invocation
(`action.yaml:248`), so a second step in the same job hits the warm cache.

### NFR-3 — Leave `uv.lock` unmodified

The working tree SHALL contain no modification to `uv.lock` after any
verification step in this change.

*Rationale:* P-18 — `uv export`, which both CI's scan job and
`scripts/security-scan.sh` run, rewrites the tracked `uv.lock` as a side effect
(it reverts the package version 2.4.1 → 2.4.0). Anyone reproducing this fix
locally will find a dirty `uv.lock` that has nothing to do with the fix, and
could easily commit it inside a security patch. Encountered and reverted twice
during this spec's verification. Satisfied for the wrapper by passing `--frozen`
to its `uv export` (C-5), which removes the side effect rather than documenting a
cleanup step; `ci.yml:752` keeps the plain call, where an ephemeral checkout makes
it harmless (OQ-3).

## Acceptance criteria → requirements

The issue states its fix as prose, not a numbered list. Its checks are
enumerated here as AC-1…AC-6 and mapped.

| AC | From the issue | Covered by |
|---|---|---|
| AC-1 | "Verify `docusaurus/package-lock.json` resolves `js-yaml@4.3.1`" | FR-1 |
| AC-2 | "no `package.json` change is needed" | FR-2, FR-3 |
| AC-3 | "confirm the docs site still builds (`npm run build`)" | FR-5 |
| AC-4 | "commit the lockfile" | FR-3, FR-7 |
| AC-5 | "confirm alert #201 auto-closes on the next Trivy scan" | FR-4 |
| AC-6 | "Worth considering separately: adding `format: 'table'` … so the job log names what it failed on" | FR-8, FR-9 |

AC-6 is the only one the issue defers. It is pulled in-scope: it is a
five-line workflow change in the same file family, and per P-15 it is the reason
this alert needed a manual investigation to identify at all.

## Contradiction in the issue, resolved

The issue's §"Reproducing locally" prescribes
`trivy fs --scanners vuln --severity CRITICAL,HIGH --ignore-unfixed docusaurus/`.
That command does reproduce **this** finding (verified — it prints the js-yaml
row). But per P-8 it does **not** reproduce the **gate**: CI discards the
severity filter, and the issue's own §Summary asserts a conclusion about the
gate ("the only failing job in CI"). Using the narrow command to confirm the fix
would be checking a weaker condition than the one that must hold.

| Option | Verdict |
|---|---|
| Verify at `CRITICAL,HIGH`, as the issue and `SECURITY.md` say | **Rejected.** Narrower than the gate (P-8). Passes while a MEDIUM finding keeps `main` red. This is precisely the trap PR #552's commit message documents hitting. |
| Verify with severity unset, matching the gate | **Chosen** (FR-4). P-9 shows the unrestricted baseline is a single HIGH, so this is not a wider net in practice — it is the same net, correctly described. |
| Change `ci.yml` to pass `limit-severities-for-sarif: true` so the gate honours `CRITICAL,HIGH` | **Rejected here.** It would make the gate match the docs, but it narrows an existing security gate — a policy change, not a patch. It also shrinks the SARIF uploaded to the Security tab. Out of scope; noted as OQ-1. |

The reverse-facing half of the contradiction — that the repo's documentation
misdescribes its own gate — is fixed by FR-6 rather than left in place.
