# Tasks: clear GHSA-5p4m-2wfm-xmqj and make the Security Scan log its findings

**Issue:** [#568](https://github.com/awslabs/cli-agent-orchestrator/issues/568)
**Requirements:** [requirements.md](requirements.md) · **Design:** [design.md](design.md)

---

## Parallelisation plan

Three tasks, no shared files, so all three run in one wave.

| Wave | Task | Owns (exclusively) | Depends on |
|---|---|---|---|
| 1 | **T1** — bump `js-yaml` to 4.3.1 | `docusaurus/package-lock.json` | — |
| 1 | **T2** — make the scan job log its findings | `.github/workflows/ci.yml` | — |
| 1 | **T3** — correct the documented scan condition | `scripts/security-scan.sh`, `SECURITY.md`, `DEVELOPMENT.md` | — |

No two tasks own a file. T1 is the fix; T2 and T3 are the observability and
documentation corrections that keep the next occurrence from costing another
manual investigation.

**One cross-task hazard:** T1 and T3 both *run* Trivy for verification, and T3's
verification runs the wrapper's `uv export`, which mutates the tracked `uv.lock`
(C-5, P-18). T3 removes the hazard at its source by passing `--frozen`; until
that edit lands, whoever runs the wrapper must `git checkout -- uv.lock`, and
nobody should `git add -A` at any point. T1's verification does not invoke
`uv export` and is unaffected.

---

## T1 — Bump `js-yaml` to 4.3.1 in the docs lockfile

**Status:** [x] Complete — implemented and verified
**Owns:** `docusaurus/package-lock.json`

### Description

Run `cd docusaurus && npm update js-yaml`. Expect an 8-line diff: `version`,
`resolved`, `integrity` on the single `node_modules/js-yaml` entry. `package.json`
must come out byte-identical.

**Likely defects to watch for:**

1. **Checking presence instead of count.** A grep for `4.3.1` passes while a
   nested vulnerable copy survives elsewhere in the lockfile — the exact #551
   failure, where `bun update` left `ajv`'s nested copy in place and needed a
   `package.json` `overrides` entry (PR #552). npm hoists all five `^4.1.0`
   dependents onto one node here, so `npm update` is sufficient — but assert the
   **count is 1**, do not assume it.
2. **Verifying at `CRITICAL,HIGH`.** The issue and `SECURITY.md` both say to.
   `trivy-action` discards the severity filter under `format: sarif`
   (`entrypoint.sh:76-83`), so the gate fires at *every* severity. Verify with
   `TRIVY_SEVERITY` unset or the check is weaker than the gate.
3. **Accepting `npm`'s exit code as proof.** `npm update` exits 0 while printing
   `up to date` and changing nothing. Read the resolved version out of the file.
4. **Committing extra lockfile churn.** More than three changed lines means npm
   resolved beyond the target; stop and read the diff before shipping it inside a
   security patch.

Do **not** add a `js-yaml` entry to `docusaurus/package.json`'s `overrides`
block. It produces a byte-identical lockfile (DR-1) and would be a permanent
no-op entry.

### Requirements addressed

FR-1, FR-2, FR-3, FR-4, FR-5, FR-7, NFR-1

### Acceptance criteria

- [x] `node -e "const l=require('./docusaurus/package-lock.json'); const k=Object.keys(l.packages).filter(x=>/(^|\/)js-yaml$/.test(x)); console.log(k.length, k.map(x=>l.packages[x].version))"` prints exactly `1 [ '4.3.1' ]`.
- [x] The `node_modules/js-yaml` entry's `resolved` ends with `js-yaml-4.3.1.tgz` and its `integrity` is `sha512-CY6crGq313MX8GkwvB7tzgp99vjQxY1++5y10/BKN/GUfHqWaOGQMNZkBvqSzsZKWk/ijwHlWzzkLulsGHhjWQ==`.
- [x] `git diff --numstat docusaurus/package-lock.json` prints `3	3	docusaurus/package-lock.json`.
- [x] `git diff --name-only` lists `docusaurus/package-lock.json` and nothing else.
- [x] `git diff --quiet HEAD -- docusaurus/package.json` exits 0 (package.json untouched).
- [x] All five dependents still declare `^4.1.0`: `node -e "const l=require('./docusaurus/package-lock.json'); const r=Object.entries(l.packages).filter(([,v])=>v.dependencies&&v.dependencies['js-yaml']).map(([k,v])=>[k,v.dependencies['js-yaml']]); console.log(r.length, new Set(r.map(x=>x[1])))"` prints `5 Set(1) { '^4.1.0' }`.
- [x] `cd docusaurus && npm ci && npm run typecheck && npm run build` all succeed, ending in `[SUCCESS] Generated static files`.
- [x] `node -e "console.log(require('./docusaurus/node_modules/js-yaml/package.json').version)"` prints `4.3.1`.
- [x] `TRIVY_IGNORE_UNFIXED=true TRIVY_PKG_TYPES=os,library trivy fs . --format json --output /tmp/t.json --quiet` then a count of `Results[].Vulnerabilities` prints **0**, with `TRIVY_SEVERITY` unset in the environment.
- [x] `git status --porcelain` shows no modification to `uv.lock`.

**Command that must pass:**
```bash
cd docusaurus && npm ci && npm run typecheck && npm run build
```

---

## T2 — Make the Security Scan job name what it failed on

**Status:** [x] Complete — implemented and verified
**Owns:** `.github/workflows/ci.yml`

### Description

Add the `if: failure()` table-format Trivy step given verbatim in
[design.md § Interfaces](design.md#interfaces) to the `security` job, between the
existing scanner step (`ci.yml:763-772`) and the SARIF upload (`ci.yml:774-781`).
Keep the comment block — it records why each field is what it is.

Today a failing `Security Scan` prints an exit code and no finding, because
`format: 'sarif'` redirects output to a file. Identifying #568 required an
out-of-band `gh api .../code-scanning/alerts` call.

**Likely defects to watch for:**

1. **Changing the existing step's `format` to `table` instead of adding a step.**
   That destroys the SARIF upload the Security tab depends on, and the
   `if: always()` comment at `ci.yml:776-778` explains why that upload is
   load-bearing. FR-9 forbids it.
2. **Passing `severity: 'CRITICAL,HIGH'` on the new step.** It would print a
   *narrower* set than the one that failed the build — and could print **nothing**
   while the job is red, which is worse than the current silence because it looks
   authoritative. Omit `severity:` entirely.
3. **Omitting `exit-code: '0'`.** Without it, the second step fails too and the
   log no longer shows which step is the gate.
4. **Using a different action SHA.** Both steps must pin `57a97c7e…` so the repo
   has exactly one Trivy version to reason about.

### Requirements addressed

FR-8, FR-9, NFR-2

### Acceptance criteria

- [x] `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` exits 0.
- [x] `grep -c 'aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1' .github/workflows/ci.yml` prints `2`.
- [x] `grep -c 'aquasecurity/trivy-action' .github/workflows/ci.yml` also prints `2` — i.e. no unpinned or differently-pinned reference was introduced.
- [x] The new step's block contains `if: failure()`, `format: 'table'`, and `exit-code: '0'`, and contains **no** `severity:` key.
- [x] The pre-existing scanner step still has `format: 'sarif'`, `output: 'trivy-results.sarif'`, `severity: 'CRITICAL,HIGH'`, `ignore-unfixed: true` and `exit-code: '1'`, unchanged.
- [x] The `upload-sarif` step still carries `if: always()`.
- [x] Within the `security` job, step order is: scanner (sarif) → table → upload-sarif. Verified by reading the parsed `jobs.security.steps` name list.
- [x] `git diff --name-only` for this task lists `.github/workflows/ci.yml` and nothing else.

**Command that must pass:**
```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci.yml')); \
names=[s.get('name') for s in d['jobs']['security']['steps']]; print(names); \
sys.exit(0 if len([s for s in d['jobs']['security']['steps'] if 'trivy-action' in str(s.get('uses',''))])==2 else 1)"
```

---

## T3 — Correct the documented local-scan condition

**Status:** [x] Complete — implemented and verified
**Owns:** `scripts/security-scan.sh`, `SECURITY.md`, `DEVELOPMENT.md`

### Description

`scripts/security-scan.sh:24` echoes "matching CI" and then at lines 31-35 passes
`--severity CRITICAL,HIGH` — which CI discards. `SECURITY.md:99-104` documents the
same narrow command, and additionally `--scanners vuln`, which disables the secret
scanning CI *does* run (both `[vuln]` and `[secret]` appear in the job log).
`DEVELOPMENT.md:259` repeats "(CRITICAL/HIGH)".

Change the wrapper to drop `--severity` and `--scanners` so its pass/fail
condition is no narrower than the gate's, update its echoed description, and
correct the two prose surfaces. Keep `--exit-code 1` and `--ignore-unfixed`.
State explicitly, where a reader will see it, that the CI gate fires at **all**
severities because `trivy-action` unsets `TRIVY_SEVERITY` under
`format: sarif` — that is the fact whose absence caused this.

**Likely defects to watch for:**

1. **Dropping `--exit-code 1`** while editing the flag list, which would turn the
   wrapper into a reporter that always succeeds — a check that cannot fail.
2. **Leaving `uv.lock` dirty.** The script's `uv export` rewrites the tracked
   `uv.lock` (reverting the version 2.4.1 → 2.4.0). It cleans up
   `requirements.txt` afterwards but not `uv.lock`. Fix the cause, not the
   symptom: add `--frozen` to the export so it reads the lockfile without
   rewriting it. Do not settle for documenting a `git checkout -- uv.lock`
   remedy, and do not `git add -A` at any point.
3. **"Fixing" the mismatch by narrowing CI instead.** Setting
   `limit-severities-for-sarif: true` would make the gate match the docs, but it
   narrows an existing security gate and loses MEDIUM/LOW SARIF history. Rejected
   as DR-5 / OQ-1; not this task.
4. **Editing `SECURITY.md` and forgetting `DEVELOPMENT.md:259`**, leaving the two
   inconsistent with each other.

### Requirements addressed

FR-6, NFR-3

### Acceptance criteria

- [x] `grep -n 'severity' scripts/security-scan.sh` returns no `--severity` flag on the `trivy fs` invocation.
- [x] `grep -n 'scanners' scripts/security-scan.sh` returns nothing.
- [x] `grep -n 'exit-code 1' scripts/security-scan.sh` still matches, and `--ignore-unfixed` is retained.
- [x] The script's echoed banner no longer claims `CRITICAL,HIGH` and states the gate runs at all severities.
- [x] `bash -n scripts/security-scan.sh` exits 0.
- [x] `scripts/security-scan.sh trivy` exits 0 on the current (post-T1) tree, and its output shows the scan ran.
- [x] `git status --porcelain` is **empty** immediately after `scripts/security-scan.sh trivy` — in particular `uv.lock` is not left modified. **Met by passing `--frozen` to the script's `uv export`**, not by a manual restore: a plain export rewrites the tracked lockfile (P-18), and `--frozen` reads it without rewriting. Measured: identical 1837-line `requirements.txt` either way. Do not "fix" this criterion with `git checkout -- uv.lock` — that leaves the hazard in place for the next contributor.
- [x] `SECURITY.md`'s "Running Security Scans Locally" block no longer shows a `--severity` narrower than the gate, no longer shows `--scanners vuln`, and names the `trivy-action` severity-unset behaviour.
- [x] `DEVELOPMENT.md:259` no longer states `(CRITICAL/HIGH)` as the scan's gate condition.
- [x] `git diff --name-only` for this task lists only `scripts/security-scan.sh`, `SECURITY.md`, `DEVELOPMENT.md`.

**Command that must pass:**
```bash
bash -n scripts/security-scan.sh && scripts/security-scan.sh trivy && \
  test -z "$(git status --porcelain)"
```

---

## Out of scope

| Item | Owned by |
|---|---|
| Narrowing the gate to `CRITICAL,HIGH` via `limit-severities-for-sarif: true` | OQ-1 — needs a maintainer policy decision; it *reduces* what CI blocks on. |
| Why Dependabot filed no PR and reports 0 open alerts for a HIGH Trivy finding; whether a `.github/dependabot.yml` should exist (there is none) | OQ-2 — a follow-up issue. A scanner covering less than believed outranks this bump in importance. |
| Making `uv export` stop mutating `uv.lock` in **`ci.yml:752`** | OQ-3 — harmless on an ephemeral CI checkout, so it stays out of this PR. The **wrapper** side was fixed here with `--frozen`, because T3's clean-tree criterion required it. |
| Pinning `js-yaml` in `docusaurus/package.json` `overrides` as a regression floor | OQ-4 / DR-1 — byte-identical lockfile today, so a no-op entry. |
| Upgrading `docusaurus` to `js-yaml@5.x` | DR-4 — satisfies none of the five `^4.1.0` constraints. |
| Any change to the shipped `cli-agent-orchestrator` package | Not affected: `js-yaml` appears in no other lockfile, and the unrestricted scan finds nothing elsewhere. |
| Trivy 0.73.0 upgrade (the job log notices one is available) | Unrelated to this advisory; the pinned 0.69.3 detects it correctly. |
