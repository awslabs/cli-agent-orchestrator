# Design: clear GHSA-5p4m-2wfm-xmqj (js-yaml) and make the Security Scan log its findings

**Issue:** [#568](https://github.com/awslabs/cli-agent-orchestrator/issues/568)
**Requirements:** [requirements.md](requirements.md)
**Status:** Specified, not implemented.

---

## Contents

1. [Overview](#overview)
2. [Corrections to the issue's sketch](#corrections-to-the-issues-sketch)
3. [Position in the system](#position-in-the-system)
4. [File layout](#file-layout)
5. [Interfaces](#interfaces)
6. [Algorithms](#algorithms)
7. [Error handling](#error-handling)
8. [Decisions and trade-offs](#decisions-and-trade-offs)
9. [Testing strategy](#testing-strategy)
10. [Manual verification](#manual-verification)
11. [Open questions](#open-questions)

---

## Overview

Three lines of `docusaurus/package-lock.json` move `js-yaml` from `4.3.0` to
`4.3.1`, clearing the repo's only open code-scanning alert and unblocking
`Security Scan` on `main` and every open PR. Alongside it, two documentation
surfaces that misdescribe the gate's severity condition are corrected, and the
scan job gains a table-format step so the next failure names itself in the log
instead of requiring an API call to identify.

## Corrections to the issue's sketch

### Changes what the code does

**C-1 — `npm update js-yaml` works here, but the issue's reasoning for why is
incomplete, and the reason matters.**

The issue argues the fix is safe because "every declared constraint is `^4.1.0`,
which already admits the patched `4.3.1`." True (P-13) but not sufficient: #551
had the identical argument (`ajv` declared `fast-uri: ^3.0.1`, which admitted
`3.1.5`) and `bun update` still left a **nested** vulnerable copy in place,
forcing a `package.json` `overrides` entry in PR #552.

*Evidence:* enumerating `Object.keys(lock.packages)` yields exactly one match,
`node_modules/js-yaml` (P-12) — npm hoisted all five `^4.1.0` dependents onto a
single node, so there is no nested copy for `npm update` to miss. Verified by
running it: an 8-line diff, `package.json` untouched (P-11). I also built the
`overrides`-based alternative and diffed it — **byte-identical lockfile** (DR-1).

*Consequence:* the issue's command is correct as written; adopt it. But the
acceptance criterion must be "exactly one `js-yaml` node, at 4.3.1" (Task 1's
AC), not "the constraint permits it" — otherwise this spec would sanction the
same check that passed for #551 and shipped a vulnerable tree.

**C-2 — The Security Scan gate does not run at `CRITICAL,HIGH`, so the issue's
verification command checks a weaker condition than the gate.**

*Evidence:* `entrypoint.sh:76-83` at the pinned SHA `57a97c7e` (tag `v0.35.0`)
runs `unset TRIVY_SEVERITY` when `format` is `sarif`. The live log of run
31177773018 prints `Building SARIF report with all severities` then
`Running Trivy with options: trivy fs .` — no `--severity` (P-8).

*Consequence:* every acceptance check in `tasks.md` runs with severity unset. A
MEDIUM or LOW finding fails this job, and the issue's repro command would not
have shown it. Note that PR #552's commit message already recorded this exact
behaviour for the postcss MEDIUM — so this is a rediscovery of a known trap that
never made it into the repo's documentation, which is what C-3 fixes.

**C-3 — The repo's own documentation instructs the wrong scan command.**

`SECURITY.md:102` documents `trivy fs --scanners vuln --severity HIGH,CRITICAL .`
and `scripts/security-scan.sh:31-35` — advertised as "matching CI" in its own
echo string at line 24 — passes `--severity CRITICAL,HIGH`.

*Evidence:* same as C-2. The wrapper's claim to mirror CI is false in the one
dimension that decides pass/fail.

*Consequence:* FR-6. This is a change the issue did not request; it is the
mechanism that made the issue's own repro section wrong, so fixing the instance
without fixing the source would leave the next contributor to rediscover it.

**C-4 — `--scanners vuln` in the documented command silently narrows the gate a
second time.**

CI passes no `scanners` input, so Trivy runs its default set. The run-31177773018
log shows both `[vuln] Vulnerability scanning is enabled` **and**
`[secret] Secret scanning is enabled`. `SECURITY.md:102`'s `--scanners vuln`
disables secret scanning, so a Trivy-detectable secret would fail CI and pass the
documented local check.

*Evidence:* job log, lines beginning `INFO [secret]`. My unrestricted scan
confirmed zero secret findings today (P-9), so nothing is currently hidden.

*Consequence:* folded into FR-6 — the corrected wrapper must not pass
`--scanners`.

**C-5 — Running the documented local scan dirties a tracked lockfile.**

`scripts/security-scan.sh:30` runs `uv export --format requirements-txt`. With uv
0.9.4 this **rewrites `uv.lock`**: 16 insertions / 16 deletions, including
reverting the `cli-agent-orchestrator` version from `2.4.1` (matching
`pyproject.toml:3`) back to `2.4.0`.

*Evidence:* P-18 — reproduced twice; `git status` showed ` M uv.lock` each time.
The script removes `requirements.txt` on the way out (line 36) but never restores
`uv.lock`.

*Consequence:* NFR-3, and a `git status` assertion in Task 3's acceptance
criteria. A contributor running the repo's own security wrapper before committing
a security patch is one `git add -A` away from shipping an unrelated version
downgrade.

*Resolution:* the wrapper's export now passes **`--frozen`**, which reads the
lockfile without rewriting it — measured identical 1837-line output, tree clean.
This turned out to be required rather than optional: T3's "tree clean after
running the wrapper" criterion cannot be met while the export mutates a tracked
file, and documenting `git checkout -- uv.lock` as the remedy leaves the trap
armed for whoever forgets it. The identical call at **`ci.yml:752`** is left
alone — harmless on an ephemeral checkout; see OQ-3.

### Changes only documentation

**C-6 — The issue's "every open PR inherits the red" is not currently true, and
the reason is a timing artefact.**

*Evidence:* `gh pr checks 567` and `gh pr checks 566` both show
`Security Scan  pass`. Their runs were created 2026-08-06T23:59:58Z and
2026-08-07T01:43:04Z — **before** the alert (2026-08-07T04:24:47Z) and before
Trivy's DB picked the advisory up.

*Consequence:* documentation only. The claim is right about the future and wrong
about the present: those PRs carry a stale green and will turn red on their next
run. Worth stating precisely because "every PR is red" invites someone to check
one, see green, and conclude the issue is stale.

**C-7 — The advisory has no CVE, and the upstream one it derives from is MEDIUM,
not HIGH.**

*Evidence:* `gh api advisories/GHSA-5p4m-2wfm-xmqj` → `cve_id: None`,
`severity: high`, CVSS `3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H` = 7.5, CWE-407.
`gh api advisories/GHSA-724g-mxrg-4qvm` → CVE-2026-59870, **`severity: medium`**.

*Consequence:* documentation only. The issue calls it "the same weakness as
CVE-2026-59870", which is accurate about the code and misleading about the
rating — the same defect is scored MEDIUM in 5.x and HIGH in 3.x/4.x. Do not
write "CVE-2026-59870" as this alert's identifier in the commit message; the
advisory has no CVE.

**C-8 — Exposure is narrower than "any consumer parsing untrusted YAML": the
`!!omap` tag must be present explicitly.**

*Evidence:* `omap` sits in the `explicit:` list of `lib/schema/default.js`, not
`implicit:` (P-4). Measured: 32 000 entries tagged `!!omap` = 655.9 ms;
byte-for-byte the same entries untagged = 49.2 ms — no quadratic term. And
`grep -rn '!!omap'` across the repo returns **zero** hits (P-20).

*Consequence:* documentation only, but it sharpens the issue's claim. The
issue's "a plain `yaml.load(input)` with no options is affected; no custom schema
is needed" is right that the tag is *available* by default, and easy to read as
"any YAML is affected", which is false. The tag is reachable through markdown
frontmatter — a nested `items: !!omap` under a mapping key still costs 631 ms at
n=32 000 (P-6) — so an untrusted-docs-contribution path would be real. It is not
real today.

### Checked and found correct

- The vulnerable function, the mechanism, and the O(n²) claim: exact (P-1, P-5).
- `4.3.1` (4.x) and `3.15.1` (3.x) as the fixed versions: exact, and the
  advisory's ranges confirm them.
- The five dependents and their `^4.1.0` constraints: all five verified
  individually, and the list is complete.
- "Docusaurus docs site, not the shipped package": confirmed — the other ten
  lockfiles carry no `js-yaml`, and the unrestricted scan finds nothing in them.
- "The only open code-scanning alert": exact — the API returns one.
- "`Security Scan` is the only failing job": exact (P-16).
- The `format: 'sarif'` phantom-failure diagnosis: exact (P-15).
- The repro command does print the finding: verified.

## Position in the system

```
                    ┌───────────────── in scope ──────────────────┐
                    │                                             │
  advisory DB       │  docusaurus/package-lock.json               │
  (GitHub/Trivy)    │    node_modules/js-yaml  4.3.0 → 4.3.1      │   ← Task 1
        │           │                                             │
        │ publishes │        ▲ hoisted, single node (P-12)         │
        ▼           │        │                                     │
  ┌───────────┐     │  ┌─────┴──────────────────────────────┐      │
  │  Trivy    │─────┼─▶│ 5 declared dependents, all ^4.1.0  │      │
  │ fs scan   │     │  │  @11ty/gray-matter                 │      │
  └─────┬─────┘     │  │  @docusaurus/plugin-content-docs   │      │
        │           │  │  @docusaurus/utils                 │      │
        │ SARIF     │  │  @docusaurus/utils-validation      │      │
        ▼           │  │  cosmiconfig                       │      │
  Security tab      │  └────────────────────────────────────┘      │
  + alert #201      │                                             │
        ▲           │  .github/workflows/ci.yml  (security job)   │   ← Task 2
        │           │    + table step, log-visible findings       │
        │           │                                             │
        │           │  scripts/security-scan.sh + SECURITY.md     │   ← Task 3
        └───────────┼──── severity condition corrected ───────────┘
                    │                                             │
                    └─────────────────────────────────────────────┘

  ══════════════ trust boundary ══════════════
  All five consumers run at STATIC-SITE-BUILD time, parsing repo-authored
  markdown frontmatter and Docusaurus config. No request path; no untrusted
  input (P-19). And no `!!omap` tag exists in the tree (P-20), so the
  quadratic branch is not reached at all today.

  NOT in scope: the shipped cli-agent-orchestrator package. uv.lock,
  tui/Cargo.lock, web/, cao_mcp_apps/, examples/** carry no js-yaml.
```

## File layout

| File | State | Why |
|---|---|---|
| `docusaurus/package-lock.json` | amended | FR-1. The three lines in P-11. The entire dependency fix. |
| `.github/workflows/ci.yml` | amended | FR-8, FR-9. A second Trivy step in the existing `security` job. |
| `scripts/security-scan.sh` | amended | FR-6, C-3, C-4. The wrapper claims to match CI and does not. |
| `SECURITY.md` | amended | FR-6, C-3, C-4. Same defect in prose. |
| `docs/issues/568-js-yaml-omap-dos/{requirements,design,tasks}.md` | NEW | This spec. Follows the `docs/issues/<n>-<slug>/` convention established by `docs/issues/345-okf-export-import/`. |

**Not changed, and deliberately:** `docusaurus/package.json` (FR-2 — an
`overrides` entry produces a byte-identical lockfile, so it would be pure
liability; see DR-1). `DEVELOPMENT.md:259` says "Trivy vulnerability scanner
(CRITICAL/HIGH)" — the same misdescription as C-3, but it is a one-line CI
summary table, not an instruction someone runs; correcting it is folded into
Task 3 as a one-word edit rather than left inconsistent with `SECURITY.md`.

## Interfaces

No code interfaces change. The behavioural contract that does change is the
workflow step, given as the actual YAML to be added to the `security` job in
`.github/workflows/ci.yml`, after the existing scanner step and before the SARIF
upload:

```yaml
      # FR-8: the SARIF step above writes findings to a FILE, so a failing scan
      # logs an exit code and names nothing — diagnosing #568 took an out-of-band
      # `gh api .../code-scanning/alerts` call to learn what had failed. This step
      # re-runs the same scan in table format so the finding appears in the log.
      #
      # `if: failure()` — runs only when the gate above fails, so a green run pays
      # nothing. `exit-code: '0'` — this step must not be the one that fails the
      # job; the gate above already did, and a second failure would obscure which
      # step is authoritative.
      #
      # No `severity:` input, matching the effective gate: trivy-action's
      # entrypoint.sh unsets TRIVY_SEVERITY whenever format is sarif, so the step
      # above scans at ALL severities. Passing CRITICAL,HIGH here would print a
      # narrower set than the one that failed the build — and could print NOTHING
      # while the job is red.
      - name: Show Trivy findings in the log
        if: failure()
        uses: aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1  # v0.35.0
        with:
          scan-type: 'fs'
          scan-ref: '.'
          format: 'table'
          ignore-unfixed: true
          exit-code: '0'
```

The action SHA must be the same pin as the existing step (`57a97c7e…`), so both
steps observe identical resolution semantics and the repo keeps one Trivy version
to reason about.

## Algorithms

The severity-resolution order inside `trivy-action`, which is the whole reason
the added step omits `severity:` and every acceptance check unsets it:

```
# action.yaml:218 — the input is always exported, defaulted if absent
TRIVY_SEVERITY := INPUT_SEVERITY or "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL"

# entrypoint.sh:76-83 — THEN, after the export, format is consulted
if TRIVY_FORMAT == "sarif":
    if INPUT_LIMIT_SEVERITIES_FOR_SARIF != "true":
        unset TRIVY_SEVERITY          # ← the CI job's path; the input is DISCARDED
        log "Building SARIF report with all severities"

# entrypoint.sh:87-90
run: trivy <scanType> <scanRef>       # no --severity flag ever appears in argv
exit with returnCode                  # non-zero iff TRIVY_EXIT_CODE and findings
```

The order is load-bearing: the unset happens **after** the export, so a
`severity:` input that is visibly present in the job's `with:` block (and echoed
into the log — run 31177773018 prints `severity: CRITICAL,HIGH`) has no effect on
the scan. Reading the workflow file, or even the job log's parameter echo, gives
the wrong answer; only the ordering above gives the right one.

Consequence for the fix, as pseudocode for the reproduction:

```
# WRONG — what the issue and SECURITY.md say. Narrower than the gate.
trivy fs --scanners vuln --severity CRITICAL,HIGH --ignore-unfixed docusaurus/

# RIGHT — what the gate evaluates.
TRIVY_IGNORE_UNFIXED=true TRIVY_PKG_TYPES=os,library trivy fs .
#   severity: unset          → all severities, per the unset above
#   scanners: unset          → vuln AND secret, per the job log
#   scan-ref: "."            → whole repo, not docusaurus/ alone
#   pkg-types: os,library    → action.yaml:217 default
```

## Error handling

| Failure | Handling | Requirement |
|---|---|---|
| `npm update js-yaml` resolves a version other than 4.3.1+ | Stop. Assert the resolved version from the lockfile before proceeding; do not accept `npm`'s exit code as proof. | FR-1 |
| `npm update` touches more than the three expected lines | Stop and read the diff. Extra churn means npm resolved beyond the target; a security patch must not carry unreviewed resolution changes. | FR-3 |
| A nested `node_modules/**/js-yaml` appears | Stop and add a `package.json` `overrides` entry, as #551/PR #552 had to. Assert the node **count** is 1, not merely that the top-level one is patched. | FR-1, C-1 |
| `npm ci` or `npm run build` fails on the patched lockfile | Stop; do not proceed to CI. Per P-17 the PR's docs-site gate would catch it, but locally is cheaper. | FR-5 |
| Unrestricted scan still reports a finding after the bump | Do not narrow the severity to make it pass. A finding at any severity fails the real gate (P-8); report it and treat it as in scope for this issue. | FR-4 |
| `uv.lock` is modified by a verification step | Prevented at the source: the wrapper's export passes `--frozen` (C-5). If a *different* command still dirties it, `git checkout -- uv.lock` and never stage it. | NFR-3, C-5 |
| The added table step fails the job itself | `exit-code: '0'` prevents it. If it somehow errors, `if: failure()` means the job was already red — the gate's verdict is unchanged. | FR-8 |
| Alert #201 does not auto-close after merge | Expected latency, not a failure: the alert closes on the next Trivy run against `main`. Confirm via the code-scanning API, not by assumption. | FR-4 |

## Decisions and trade-offs

| # | Decision | Alternative | Why |
|---|---|---|---|
| DR-1 | Lockfile-only bump via `npm update js-yaml`, no `package.json` change. | Add `"js-yaml": "^4.3.1"` to `docusaurus/package.json`'s existing `overrides` block, as #551/PR #552 did for `fast-uri`. **Fairly: this is the repo's most recent precedent for exactly this situation, it pins the floor durably so a future `npm install` cannot regress below 4.3.1, and the block already carries five such entries — consistency alone is a real argument.** | I built both and diffed the results: **byte-identical lockfiles** (8 changed lines each). The override buys nothing here because there is a single hoisted node (P-12), which is precisely what was *not* true for bun in #551 — there the override was load-bearing. An entry that changes no resolution is a permanent claim someone must later re-evaluate ("is this still needed?") for a package the docs site does not import. FR-2's cost of *not* having it is bounded: the docs build is a PR gate (P-17) and Trivy is a merge gate, so a regression below 4.3.1 is caught twice before it reaches `main`. |
| DR-2 | Add a second, `if: failure()` Trivy step in table format. | Echo `trivy-results.sarif` (or `jq` it) in a failure step — cheaper, no second scan. | Reusing the SARIF means parsing SARIF in shell to get a readable line, and the raw file is thousands of lines of JSON, which is not "the log names what it failed on". A second Trivy run hits the warm cache (NFR-2) and costs one scan on **failing runs only**. Considered and rejected: adding `format: 'table'` to the *existing* step — that would destroy the SARIF upload the Security tab depends on, violating FR-9 and the load-bearing `if: always()` comment at `ci.yml:779`. |
| DR-3 | Fix `scripts/security-scan.sh` + `SECURITY.md` in this issue, though #568 does not ask. | Leave them; file a follow-up. | The wrong command is *why* the issue's own repro section is wrong, and PR #552's commit message shows the trap was already hit once and never written down. Leaving it means a third rediscovery. Cost is a two-file, few-line docs change reviewed alongside a one-line lockfile bump — and it is what makes the acceptance criteria in `tasks.md` runnable by a contributor. |
| DR-4 | Stay on the 4.x line (4.3.1). | Move `docusaurus` to `js-yaml@5.2.3` (`latest`). | Every dependent declares `^4.1.0` (P-13); 5.x satisfies none of them, so it would require an `overrides` force across five packages whose YAML behaviour would then be untested. 4.3.1's diff is 4 lines in one function with identical observable semantics (P-3, P-7). 5.x is a larger, unneeded blast radius for a build-time transitive. |
| DR-5 | Do not set `limit-severities-for-sarif: true` to make the gate honour `CRITICAL,HIGH`. | Set it; the workflow would then mean what it says and match the docs. | It **narrows a security gate** — MEDIUM findings stop failing CI — which is a policy decision for the maintainers, not a security patch's business. It also shrinks the SARIF uploaded to the Security tab, losing MEDIUM/LOW history. Instead the docs are corrected to match the code (DR-3). Recorded as OQ-1 so the maintainers can take the other branch deliberately. |
| DR-6 | Assert the `js-yaml` node **count**, not just the top-level version. | `grep '"js-yaml"' package-lock.json \| grep 4.3.1` — simpler. | A grep for the patched version passes while a *nested* vulnerable copy sits elsewhere in the file. That is the #551 failure mode verbatim. The count assertion is the only check that can fail for the right reason. |
| DR-7 | Verify with `TRIVY_SEVERITY` unset and no `--scanners`. | Use the issue's documented command. | C-2, C-4. The documented command is strictly weaker than the gate on two independent axes. Trade-off: the unrestricted command is noisier and could surface pre-existing MEDIUMs unrelated to this issue — measured, it does not (P-9: exactly one finding, repo-wide, all severities). |

## Testing strategy

There is no application code here, so "tests" are executable assertions on
artefacts. Per the team's same-commit rule, they ship with the change. Each is
tagged with the requirement it proves.

### `docusaurus/package-lock.json` — assertions run in Task 1

| # | Case | Proves |
|---|---|---|
| 1 | Exactly **one** key in `lock.packages` matches `js-yaml`, and its `version` is `4.3.1`. | FR-1, C-1, DR-6 |
| 2 | That entry's `resolved` ends `js-yaml-4.3.1.tgz` and `integrity` is `sha512-CY6crGq313MX8GkwvB7tzgp99vjQxY1++5y10/BKN/GUfHqWaOGQMNZkBvqSzsZKWk/ijwHlWzzkLulsGHhjWQ==`. | FR-1 |
| 3 | `git diff --numstat docusaurus/package-lock.json` = `3  3` (three lines changed, no additions or deletions of entries). | FR-3 |
| 4 | `git diff --name-only` lists **only** `docusaurus/package-lock.json`. | FR-2, FR-3 |
| 5 | `lock.packages[""].dependencies` and `.devDependencies` are unchanged from `HEAD`. | FR-2 |
| 6 | All five dependents still declare `^4.1.0` — i.e. no dependent's constraint was rewritten. | FR-2, NFR-1 |

### `.github/workflows/ci.yml` — assertions run in Task 2

| # | Case | Proves |
|---|---|---|
| 7 | The `security` job contains exactly two `aquasecurity/trivy-action` steps, both pinned to `57a97c7e…`. | FR-8 |
| 8 | The new step has `if: failure()`, `format: 'table'`, `exit-code: '0'`, and **no** `severity:` key. | FR-8, C-2 |
| 9 | The pre-existing step is unmodified — still `format: 'sarif'`, `exit-code: '1'`, `output: trivy-results.sarif`. | FR-9 |
| 10 | The `upload-sarif` step retains `if: always()`. | FR-9 |
| 11 | The file parses as YAML and the `security` job's step list is ordered scan → table → upload. | FR-8, FR-9 |

### `scripts/security-scan.sh` + `SECURITY.md` — assertions run in Task 3

| # | Case | Proves |
|---|---|---|
| 12 | `scripts/security-scan.sh` no longer passes `--severity` and no longer passes `--scanners`. | FR-6, C-3, C-4 |
| 13 | Its echoed description no longer claims `CRITICAL,HIGH`, and states that the CI gate fires at all severities. | FR-6 |
| 14 | `scripts/security-scan.sh trivy` still exits non-zero on a finding and zero on a clean tree (`--exit-code 1` retained). | FR-6 |
| 15 | `git status --porcelain` is empty after `scripts/security-scan.sh trivy` — in particular `uv.lock` is not left modified. | NFR-3, C-5 |
| 16 | `SECURITY.md`'s local-scan block and `DEVELOPMENT.md:259` no longer state a severity narrower than the gate. | FR-6 |
| 17 | `bash -n scripts/security-scan.sh` passes. | FR-6 |

### The three defects most likely to occur, and their coverage

1. **A nested vulnerable `js-yaml` survives the bump** — the #551 failure mode,
   and the one a version grep cannot see. Covered by case 1 (count, not
   presence).
2. **Verification passes at `CRITICAL,HIGH` while the real gate stays red** —
   the trap PR #552 hit and never documented. Covered by cases 8 and 12 (the
   severity filter is absent from both the new CI step and the wrapper) and by
   Task 1's unrestricted scan.
3. **`uv.lock` gets swept into the commit** by a contributor who ran the repo's
   own security wrapper and then `git add -A`. Covered by case 15, and by
   Task 3 making the wrapper leave a clean tree.

### Commands that must pass

```bash
# Task 1
cd docusaurus && npm ci && npm run typecheck && npm run build
TRIVY_IGNORE_UNFIXED=true TRIVY_PKG_TYPES=os,library trivy fs .   # → 0 findings

# Task 2
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"

# Task 3
bash -n scripts/security-scan.sh && scripts/security-scan.sh trivy
git status --porcelain      # → empty
```

## Manual verification

Automation cannot confirm the alert closes, because that happens on GitHub's side
after merge.

1. Before the change, capture the baseline:
   `gh api "repos/awslabs/cli-agent-orchestrator/code-scanning/alerts?state=open"`
   → expect exactly alert #201.
2. After the fix, run the gate's real condition locally:
   `TRIVY_IGNORE_UNFIXED=true TRIVY_PKG_TYPES=os,library trivy fs .` → 0
   findings. Do **not** substitute the issue's `--severity CRITICAL,HIGH`
   command (C-2).
3. Confirm the working tree is clean apart from the intended files —
   specifically that `uv.lock` is untouched (C-5).
4. On the PR, confirm `Security Scan` is green **and** that the `Docs site`
   workflow ran (it is path-triggered on `docusaurus/**`, so this change fires
   it) and passed.
5. Deliberately red-run the log-visibility change: on a scratch branch,
   temporarily revert the lockfile line, push, and confirm the failing
   `Security Scan` job's log now prints a table naming `js-yaml`, `4.3.0`,
   `HIGH`, `GHSA-5p4m-2wfm-xmqj`. This is the only way to prove FR-8 — a green
   run never executes the `if: failure()` step. Discard the scratch branch.
6. After merge to `main`, re-query the alerts endpoint and confirm #201 is
   `closed`/`fixed`, and that the count of open alerts is 0.

## Open questions

| # | Question | Blocks | Leaning |
|---|---|---|---|
| OQ-1 | Should the gate be narrowed to `CRITICAL,HIGH` via `limit-severities-for-sarif: true`, so the workflow means what its `severity:` input says? | Nothing in this issue — the docs fix (DR-5) resolves the inconsistency in the safe direction. Blocks any future decision to treat MEDIUMs as non-blocking. | **No.** The gate is currently stricter than documented; making the documentation honest costs nothing, while narrowing the gate loses MEDIUM/LOW SARIF history and is a policy change. If the maintainers do want it, it should be its own PR with that rationale stated. |
| OQ-2 | Why did Dependabot file no PR when security updates are enabled and unpaused, and why does it report **0** open alerts for a package Trivy calls HIGH (P-22)? | Nothing here (FR-7 lands the fix manually). Blocks relying on Dependabot for the *next* advisory of this shape. | Most likely the advisory is too new for Dependabot's alert pipeline, or `docusaurus/` is not in a Dependabot ecosystem config — there is **no `.github/dependabot.yml` in this repo** (verified: absent locally and 404 from the contents API), yet dependabot has previously opened PRs against `/docusaurus` (#549, #550), so the ecosystem is discovered some other way. Worth a follow-up issue; a scanner that silently covers less than believed is the more expensive problem than this bump. |
| OQ-3 | Should `uv export` stop mutating `uv.lock` (C-5) — e.g. via `--frozen`, or restoring the file in the wrapper's trap? | Nothing. **Resolved during implementation for the local wrapper.** | **Done for `scripts/security-scan.sh`:** the export now passes `--frozen`. Measured: identical 1837-line output, and `git status` stays clean where a plain export left ` M uv.lock`. This was necessary, not optional — T3's acceptance criterion "tree clean after running the wrapper" could not otherwise be met, and documenting a manual `git checkout -- uv.lock` as the remedy left the trap armed for anyone who forgot it. **Still open for `ci.yml:752`**, which has the same call; harmless there (ephemeral checkout) so it stays out of this PR's scope. |
| OQ-4 | Should the docs site pin `js-yaml` in `overrides` anyway, as a floor against future regression (the DR-1 alternative)? | Nothing. | **No**, per DR-1 — it changes no bytes today and adds a permanent entry to re-evaluate. Revisit if npm ever resolves this node non-deterministically, or if a second `js-yaml` node appears in the tree. |
