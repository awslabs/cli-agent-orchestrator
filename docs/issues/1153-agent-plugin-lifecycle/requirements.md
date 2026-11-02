# Requirements Document

> **Feature:** `cao-agent-plugin-lifecycle`
> **Issue of record:** [awslabs/cli-agent-orchestrator#1153](https://github.com/awslabs/cli-agent-orchestrator/issues/1153) · **Parent tracker:** [#1154](https://github.com/awslabs/cli-agent-orchestrator/issues/1154) · **Sibling:** [#1152](https://github.com/awslabs/cli-agent-orchestrator/issues/1152) (`--keep-committed-pin`).
> **Base:** `2731179845f9bd332a22e72fa44cc55345561c99`, which is both this worktree's `HEAD` and `upstream/main` (fast-forwarded 2026-11-01, `P-66`). The previous base, `00611d119ce6e8c8a940929f4f59c58e34a8ea2e`, **is** the merge of #1121; the two commits since then touch no path this document cites. Follow-ups to #1121 are tracked in **#1168**.
> **Status: decided, not open.** The user elected on 2026-10-30 to drive #1153 without waiting for maintainer answers. See [`## Decisions, and what remains open`](#decisions-and-what-remains-open) for every decision and its authority. **No decision below is a maintainer answer**, and none should be presented as one.
> **This revisits #573 decision D4, deliberately.** D4 records a CAO agent-plugin authoring skill as out of scope. The decision is to **proceed as if D4 is reversed** and to say so up front in the pull-request body — [`## Relationship to #573 decision D4`](#relationship-to-573-decision-d4) is the source text for that paragraph. **Requirement 14 stays in force**, so a maintainer can still decline the whole thing at review; that is what it was written for.
> **In the pull request:** this document and [`design.md`](design.md), plus the code. **Not in the pull request:** `tasks.md`, `research.md`, `blog-outline.md` and the review files, which stay untracked. The consequence for citations is stated in [`## Provenance`](#provenance) — a `research.md` reference is a working-record cross-reference, not something a reviewer can follow.
> **Derived from:** `research.md` (318 lines, `kiro-researcher`), the factual authority for the measurements recorded here.
> **Reviewed:** six rounds. `review-1.md`, `review-2.md` and `review-6.md` record three of them; rounds 3–5 are recorded in the working status file. All three live outside the pull request. Review 6's one blocking finding, **R6-B-1**, is applied. No code is written until a round clears.

## Scope of this document

Four deliverables:

1. **`Creator_Skill`** — `cao-agent-plugin-creator`, for authoring an Agent Plugins 1.0.0 package from scratch.
2. **`Audit_Skill`** — `cao-agent-plugin-audit`, for auditing an existing package against the vendored schemas and the Agent Skills limits.
3. **`Dogfood_Package`** — an Agent Plugins 1.0.0 package shipping both skills, built by CAO's own builder and **audited by the skills it carries**.
4. ~~**`Blog_Post`**~~ — **deferred out of this pull request** (**OQ-8**–**OQ-10**, **OQ-14**). Requirement 12 is retained as deferred-with-reason; `blog-outline.md` stays untracked on disk.

**`Blog_Post` content structure is specified in `blog-outline.md`, not here.** This document specifies only its mechanically enforced properties (Requirement 12).

### Charter deviation: only the audit half is dogfooded (OQ-17 dropped)

Recorded explicitly for the user rather than left implicit, per `review-2.md`'s residual under **F-5**.

The charter's second goal names **both** new skills as dogfooded. **`Audit_Skill` is exercised on CAO's own packages** — Requirement 8.10 and `tasks.md` T6 run it against the `Dogfood_Package` and record the findings, so that half is demonstrated by a command with an exit status.

**`Creator_Skill` is not dogfooded on an external package, by decision.** **OQ-17** had chosen to dogfood it against an external package; **the user dropped that on 2026-11-02** (`decisions-oq17-n11.md` (the user's decision record, kept outside the repository; its "Update 2026-11-02" section)). No external work is planned, and nothing in this pull request promises or schedules it. **The deviation therefore stands**, and Requirement 15 forbids marking it resolved.

**No task was invented to paper over this**, and two routes were available and rejected:

- Claiming the package is "produced by" or "guided by" the skills it ships. `review-1.md` **F-5** caught the first as an overclaim, and the second would be the same error — `review-2.md` confirmed that rejecting "guided by" was right, because no task makes it true.
- Adding a task that authors a throwaway package with `Creator_Skill` purely to have dogfooded it. That manufactures evidence for a claim rather than earning it, and the artifact would have no consumer.

### Criterion kinds

`review-1.md` **N-3** observed that some criteria read like commands but can only be checked by reading. Each acceptance criterion below is one of:

- **Mechanical** — a command with an exact expected output. Default; unlabelled.
- **[review-checked]** — a property of a document, verifiable only by a reader. Labelled explicitly so it does not pose as a gate.

### What this document does not establish

- Whether `cao plugin validate` **admits** a skill breaking the Agent Skills **name** limits (the regex and the 64-character bound) is still **inferred from `models/skill.py:6-19`, not run**. **The two length limits are now OBSERVED** (`P-6`): `tasks.md` T3 found a 2000-character description and a 600-line body both admitted, `"loadable": true`, `"findings": []`, exit 0. No requirement depends on the remaining inference.
- The provenance of `cao-skill-creator` / `cao-skill-audit` is unknown (`P-14`).
- The Agent Plugins specification **prose** was not read; only the vendored schema bytes and CAO's code. No requirement cites a spec section number as authority, and **OQ-2** remains open for that reason.

## Relationship to #573 decision D4

**A recorded maintainer decision says the thing this document specifies is not wanted.** Stated first, verbatim, because a reviewer will find it in minutes.

`docs/issues/573-agent-plugins/design.md:151`, repeated at `:862` and `:1172`:

> A new `cao-agent-plugin` authoring skill is **out of scope** here; the ecosystem's `migrate-agent-plugin` skill already covers authoring, and CAO dogfoods it (**D4**).

D4 is deliberate, thrice-recorded, and load-bearing for #573's scope boundary. The case for revisiting rests on **exactly two grounds** (Requirement 14.5), and on no more than `research.md` and the repository support.

### Ground 1 — the skill D4 names has no greenfield trigger

Verbatim, `test/agent_plugins/fixtures/canonical-example/skills/migrate-agent-plugin/SKILL.md:3` — **a protected quotation. Do not edit the client names out of it.** It is a third-party artifact enumerating *clients a tool supports*, not a claim that any agent authored or reviewed work here, and the binding attribution directive carves it out explicitly (`review-1.md`, `## Attribution directive`). Editing a quotation to satisfy a naming rule would be an evidence-integrity failure of exactly the kind **F-1** and **N-A** exist to catch, and it would leave the whole D4 argument resting on a doctored quote:

> `description: Migrate an existing Claude, Copilot, Codex, Cursor, Kiro, VS Code, or other client-specific agent plugin to the portable Agent Plugins v1 structure while preserving platform-specific hooks, agents, commands, LSP, UI, and marketplace behavior. Use when auditing, converting, or modernizing an agent plugin.`

And its body at `:11`:

> Convert an existing plugin to the Agent Plugins v1 portable core without prematurely removing behavior required by its current clients.

**Every trigger presupposes an existing client-specific plugin.** "Migrate an existing …", "Convert an existing …", "auditing, converting, or modernizing". There is no `create`, no `scaffold`, no `new`. So D4's premise — that this skill "already covers authoring" — does not hold for authoring from scratch (`P-3`).

**Context, not a third ground:** CAO carries it only as a test fixture; it is not in `SHIPPED_SKILLS` or any `PackageConfig` (`P-57`). This is context under ground 1 rather than an argument of its own, for a reason worth recording: **it restates D4's premise instead of refuting it.** D4 never claimed CAO ships the skill — `573/design.md:1172` names the external `agent-plugins-example` package as the documented route, and D4's "CAO dogfoods it" refers to the conformance fixture. An earlier revision of this section was instructed to lead with the not-shipped point; `review-1.md` **N-2** refuted that, and the review's supervisor note records why.

**The residual limit, stated once.** The body defers to the specification prose at `:15` ("Use the current Agent Plugins specification as the normative source"), and that prose was not read (`research.md:300`). So whether the specification itself defines greenfield authoring is unknown. **OQ-2** carries this; it narrows the ground without closing it.

### Ground 2 — no artifact CAO ships mechanically enforces three of the limits

This ground does not depend on ground 1 and is the evidenced half. It is **narrower than an earlier revision claimed**, because `review-1.md` **F-1** showed the earlier wording could be rebutted with the very fixture this document cites.

CAO's package validation checks a skill only for non-blank `name` and `description` and for folder-equals-frontmatter-`name` (`P-4`). It does **not** check the Agent Skills name regex, the 1024-character description limit, or the 500-line body limit (`P-5`).

**What the D4-named skill does cover, stated against our own interest.** Its bundled `references/validation-checklist.md` covers naming manually: `:13` checks that `name` "satisfies the v1 length and character rules" for the manifest, and `:22` checks that "the frontmatter `name` matches the directory name and Agent Skills naming rules" (`P-58`). Its description also claims an auditing trigger. So "no existing skill audits anything" would be false.

**What remains genuinely uncovered, which is the ground:**

| Limit | Enforced by anything CAO ships? | In the D4 skill's checklist? |
|---|---|---|
| Agent Skills **name regex** | No (`P-5`) | Manually, as a human checkbox (`P-58`) |
| Description ≤ **1024** chars | No (`P-5`) | **Not mentioned at all** (`P-58`) |
| Body ≤ **500** lines | No (`P-5`) | **Not mentioned at all** (`P-58`) |

The checklist is **30 human checkboxes**, prose for a person to tick, not an enforced check (`P-58`). And the two limits most likely to be exceeded by a real skill — the ones measured in characters and lines rather than judged by eye — appear nowhere in it.

So the ground is: **no artifact CAO ships mechanically enforces the 1024-character description limit, the 500-line body limit, or the name regex.** The limits exist only in `validate_skills.py`, which is not repo-tracked (`P-7`, `P-11`), and in the harness-oss validator, which cannot be aimed at a CAO package (`P-8`).

### What follows

**OQ-1 splits in two, and conflating the halves is how a proposal starts reading as a fait accompli.** **OQ-1a** is the *user's* question — proceed now, or hold until the maintainer answers? — and it is **decided: proceed.** **OQ-1b** is the *maintainer's* question — reverse, narrow, or uphold D4? — and it is **open**. All three of its outcomes remain reachable, and the pull request must read as a proposal a maintainer can decline:

| OQ-1b outcome | Consequence |
|---|---|
| D4 reversed | Implement in full. |
| D4 **narrowed** to authoring only | Implement `Audit_Skill` and `Dogfood_Package` (the `Blog_Post` is deferred out of this pull request in any case); drop `Creator_Skill` per Requirement 14.2, which names every criterion and count that changes. |
| D4 stands | **The pull request is withdrawn unmerged**, recorded as not merged by maintainer decision (Requirement 14.1). The work exists; it does not land. |

A spec that cannot imagine its own rejection is marketing. Requirement 14 keeps this one honest.

## Provenance

One row per factual claim. `research.md:N` locates it in the research record; the right-hand column is the primary evidence.

**`research.md` IS NOT IN THE PULL REQUEST, and that changes how these citations must be read.** The pull request carries `requirements.md` and `design.md` plus the code; `tasks.md`, `research.md`, `blog-outline.md` and the review files stay **untracked and uncommitted**. So a citation of the form `research.md:N` points at a **working record the reviewer does not have**, and presenting it as though they could follow it would be the same "appearance of a citation without the substance" defect that `P-9` was corrected for.

The rule, therefore:

- **Where a row cites a repository path** — a `.py`, `.json`, `.md`, `.toml` or `.yml` file in this tree — **that path is the citation.** `research.md:N` beside it is a cross-reference into the working record, useful to us and ignorable by a reviewer.
- **Where `research.md` or an out-of-repository artifact is the *only* evidence, the row is a working-record claim** and a reviewer cannot verify it from the pull request. Those rows are: **P-6, P-7, P-8, P-9, P-10, P-13, P-14, P-31, P-32, P-34, P-35, P-36, P-37, P-38, P-41, P-56, P-64**, the **second half of P-17** (#1121's red-phase output, which lived in a planning artifact), and **P-54**. Several (**P-32**, **P-35**, **P-37**, **P-38**) additionally depend on scripts that live only in the installed skills store, outside any repository. **An earlier revision listed eleven rows and missed eight**; the list is longer than it looks because much of the measurement for this feature was done against artifacts that do not ship.
- **No criterion's *pass or fail* depends on a working-record-only row.** An earlier wording said "no criterion depends on" one, and that was **false**: Requirement 5's rationale cites P-38's measured figure, and Requirement 13's scope exclusions name a script that is not in the repository. The accurate claim is narrower — a criterion may *cite* such a row as rationale, but **no criterion's verdict turns on one.** Where a figure appeared inside criterion text it has been moved into rationale, and the two exclusions that named an absent script are now marked `[review-checked]`.

**How the `research.md` line references are checked, stated because two successive rounds found them drifted and that is a pattern rather than an accident.** `research.md` is another seat's living document, so a line number into it is the most fragile part of any row here. Round 4 corrected sixteen primary references per `review-1.md` **N-5**; `review-2.md` **N-C** then found about eighteen *secondary* references in the same rows still wrong, including some outside the first list. All of them are corrected in this revision, each by printing the cited line and reading it rather than by trusting the prior number.

Two durable measures, because correcting numbers again would not stop a third round:

1. **The claim text is the real anchor.** Every row states its claim in full, so a reader who finds a line number stale can locate the claim by its wording. Treat the line number as a convenience and the quoted claim as the citation.
2. **The file's length is recorded as a cheap version marker.** These references were verified against `research.md` at **318 lines** (2026-10-29). If `wc -l research.md` no longer prints 318, assume the numbers may have moved and re-locate by claim text. The file was in fact edited by its owner during this revision — its length did not change, so the references held, but that was luck and not a property of the design.

| # | Claim | Label | Evidence |
|---|---|---|---|
| P-1 | #573 D4 puts a CAO agent-plugin authoring skill out of scope, in favour of `migrate-agent-plugin`. | **OBSERVED** | `573/design.md:151`, repeated `:862`, `:1172` (`research.md:153`). |
| P-2 | `migrate-agent-plugin` exists in this repo only as a test fixture. | **OBSERVED** | `test/agent_plugins/fixtures/canonical-example/skills/migrate-agent-plugin/SKILL.md`, upstream `5f3f5084…` per its `NOTICE` (`research.md:153`). |
| P-3 | Its description and body have **no greenfield trigger**: every trigger presupposes an existing plugin. | **OBSERVED (direct)** | `…/migrate-agent-plugin/SKILL.md:3` and `:11`, quoted in full above. Upgraded from `OBSERVED (narrow)` per **N-2**. |
| P-4 | CAO's skill check enforces only non-blank `name`/`description` and folder == frontmatter `name`. An invalid skill is **skipped, not fatal**. | **OBSERVED** | `validation.py:580` → `utils/skills.py:66-69`, `models/skill.py:6-19` (`research.md:130-131`). |
| P-5 | CAO's skill check does **not** enforce the name regex, the 1024-char description limit, or the 500-line body limit. | **OBSERVED** | `models/skill.py:6-19` has only the two non-blank fields (`research.md:134`). |
| P-6 | `cao plugin validate` **admits** a skill whose `description` is 2000 characters and a skill whose body is 600 lines: `"loadable": true`, `"findings": []`, exit 0. The name regex and the 64-character name bound were **not tested** and stay inferred from `models/skill.py:6-19`. | **OBSERVED (run, `tasks.md` T3, 2026-11-01) for the two length limits; INFERRED for the name limits** | `t3-overlimit-experiment.md` § Verdict and § "What this does and does not settle". Previously INFERRED from `research.md:134, 301`. |
| P-7 | Those limits live only in `validate_skills.py` (not repo-tracked) and the harness-oss validator. | **OBSERVED** | `research.md:134`; inventory `research.md:46-48`. |
| P-8 | `harness-oss/tools/validate-plugin.py` hard-codes its own `ROOT` and ignores argv, so it cannot validate a CAO package. | **OBSERVED** | `validate-plugin.py:25`; `grep -n "sys.argv\|argparse"` exits 1 (`research.md:172-174`). |
| P-9 | #1121's audit brief claims that validator was run on CAO's packages; the claim is unsupported. | **OBSERVED** | Two halves, with different evidentiary standing. **The checkable half:** `validate-plugin.py:25`, where `ROOT = Path(__file__).resolve().parent.parent`, and `grep -n "sys.argv\|argparse"` exits 1 — so the script cannot validate a package it is pointed at (`research.md:295-296`). That is `P-8`, and it stands alone. **The reported half:** that #1121's audit brief asserted such a run. **Filename withheld under the attribution directive**, which forbids disclosing a foreign reviewer; the brief lives in `.agents/plans/pr1121/` and **is available to the user**, who can confirm the assertion. **Rationale corrected:** an earlier revision of this row argued the omission cost nothing because "no reader could open it either way". That was wrong — `research.md:254-291` cites that directory throughout, so the artifact *is* reachable by the user. The honest statement is that the filename is withheld for compliance and the path is given so the claim stays confirmable, not that the citation was worthless. |
| P-10 | harness-oss's schema bytes are byte-identical to CAO's vendored copies, but it runs no JSON Schema validation against them — it hand-codes an allowlist and compares property sets. | **OBSERVED** | `cmp` exit 0; `validate-plugin.py:31-35,112-120` (`research.md:175-177`). |
| P-11 | `cao-skill-creator` and `cao-skill-audit` are not repo-tracked on any reachable ref. | **OBSERVED** | `git log --all --oneline -- '*cao-skill-*'` no output exit 0; `find … -maxdepth 5` no output exit 0 (`research.md:102, 106-107, 111`). My own `find . -type d -name 'cao-skill-*'` exit 0, zero results. |
| P-12 | An `upstream/main` test already guards against routing to the non-existent `cao-skill-creator`. | **OBSERVED** | `test/test_cao_contributing_skill_accuracy.py:534-559`, docstring `:537` (`research.md:103-105`). |
| P-13 | Both source skills' bodies invoke `python skills/cao-skill-creator/scripts/…`, a path absent from this repo. | **OBSERVED** | creator `:40,128,175,183`; audit `:54,57,130,173` (`research.md:76`). |
| P-14 | The source skills' provenance is unknown; a shared mtime is the only clue. | **UNVERIFIED** | `research.md:106, 299`. |
| P-15 | `agent-plugin/` holds exactly two packages, both generated by `scripts/build_agent_plugin.py`. **`cao-contributor` now carries THREE skills** — `cao-provider`, `cao-plugin` and `cao-contributing` — with keywords extended by "contributing" and "ci". | **OBSERVED (read at `00611d11`)** | `PACKAGES = [OPERATOR, CONTRIBUTOR]` at `build_agent_plugin.py:188`; the `CONTRIBUTOR` block read at `:162-186`, where `cao-contributing` carries the comment "Added once PR #448 merged (#1120)" at `:179`. **#1121 changed this block's content, not merely its position**, so an earlier revision's two-skill description of `cao-contributor` is stale. `docs/agent-plugins.md:493` lists the three. |
| P-16 | Those packages **commit byte-identical skill copies**, including `references/` — **26** tracked files under `agent-plugin/`, **14** of them under a `skills/` path, across **7** `SKILL.md` files (4 in `cao`, **3** in `cao-contributor`). | **OBSERVED (run, re-measured at `00611d11`)** | `git ls-files agent-plugin/ \| wc -l` → `26`; `… \| grep -c 'skills/'` → `14`; `… \| grep 'SKILL.md'` lists seven. **Re-measured because #1121 added `cao-contributing` to the contributor package**; an earlier revision recorded 23 and 11 at `556726a4` and those numbers are stale, not wrong-at-the-time. Corroborates `research.md:140-144`. **Corrects an earlier claim of mine that no skill files are committed there, which was an artifact of `-maxdepth 3`.** |
| P-17 | The drift guard compares committed bytes against a fresh build and fails on a stale skill copy. | **OBSERVED** | `build_agent_plugin.py:731-866`, `:765-769`; #1121's red phase recorded `CONTENT DIFFERS … skills/cao-contributing/SKILL.md`, `exit=2` (`research.md:280`). |
| P-18 | A skill must exist under repo `skills/` before a package can ship it, or the build raises `BuildError`. | **OBSERVED** | `build_agent_plugin.py:666-674` (`research.md:144`). |
| P-19 | A new skill is not in the wheel unless added to `SHIPPED_SKILLS`; all 14 current directories are listed. | **OBSERVED** | `scripts/sync_skills.py:44-59` (`research.md:109-111`). |
| P-20 | A **rename** of a `SHIPPED_SKILLS` entry also requires `SKILL_RENAMES` in `cli/commands/init.py`; adding does not. | **OBSERVED** | `scripts/sync_skills.py:36-43` (`research.md:111`). |
| P-21 | **#1121 is MERGED** as `00611d119ce6e8c8a940929f4f59c58e34a8ea2e`, which was this worktree's `HEAD` and `upstream/main` on 2026-10-30; the base has since been fast-forwarded to `27311798` (`P-66`). Its follow-ups are tracked in **#1168**. | **OBSERVED (run)** | `git log --oneline -1` → `00611d11 docs(skills): encode the #1119 lessons in contributor skills and the cao-contributor plugin (#1121)`; `git rev-parse HEAD` = `git rev-parse upstream/main` = `00611d11…`. **Supersedes an earlier row** recording #1121 as open at head `a64bbc76` and treating its edit to the `CONTRIBUTOR` block as a conflict cost for one of OQ-7's options. **The conflict is resolved by the merge, not by deleting the history of the decision** — OQ-7 was decided on narrative grounds, and the removed cost is recorded here so the decision's reasoning stays legible. |
| P-22 | `CODEBASE.md:53` enumerates the two packages by name, so a third obligates that edit. | **OBSERVED (direct)** | My `grep -n` exit 0: the row reads `` `agent-plugin/` | CAO's own Agent Plugins 1.0.0 packages (`cao`, `cao-contributor`), generated and checked by `make check-agent-plugin` ``. |
| P-23 | Required manifest keys are `$schema` and `name` only, and the schema is closed (`additionalProperties: false`). **CAO does not treat every unknown key as FATAL:** an unknown **top-level** key is a **WARNING**, `manifest.unknown_field` (§5.2); the key is ignored and the package stays loadable, exit 0. An unknown key **nested** in an object, such as `author.twitter`, is **FATAL**, `manifest.invalid`, exit 1. **Corrected 2026-11-01 (`P-72`).** Earlier revisions said "an unknown key is FATAL", which the fixture this row cited contradicts. | **OBSERVED (run and read)** | `plugin.schema.json:63`, `:64`; fixture `test/agent_plugins/fixtures/corpus/manifest-unknown-field/`, whose expectation at `test/agent_plugins/fixtures/corpus/cases.json:61-69` is "unknown top-level manifest field → non-fatal, reported, ignored"; `_apply_manifest_tolerances` in `src/cli_agent_orchestrator/agent_plugins/validation.py` (`:462` onward, called at `:288`). `research.md:128` repeats the earlier error. |
| P-24 | `name` must match `^(?!.*(?:--\|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`, 1–64 chars. | **OBSERVED** | `plugin.schema.json:12-18` (`research.md:126`). |
| P-25 | README, CHANGELOG and LICENSE are **not** required by either vendored schema; CAO's builder generates them anyway. | **OBSERVED** | `build_agent_plugin.py:92, 239, 593, 623` (`research.md:132`). |
| P-26 | The vendored schemas are the only schemas CAO validates against; fetching at load time is forbidden. | **OBSERVED** | `PIN.json:6,7,13,18`, upstream `b78a4f16…` (`research.md:115-117`). |
| P-27 | The `/plugins*` surface and the `cao plugin` CLI are default-off behind `CAO_AGENT_PLUGINS_ENABLED`. The value is **stripped and lowercased**, so `TRUE` and ` yes ` work. | **OBSERVED (direct)** | `agent_plugins/gate.py:33` `_TRUTHY = ("1","true","yes")` and `:42` `os.environ.get(ENV_VAR,"").strip().lower() in _TRUTHY`; `cli/commands/agent_plugin.py:142,154-158`; `api/main.py:3548,3617,3666,3711,3742` (`research.md:163-167`). Normalisation added per **N-4**. |
| P-28 | With the gate off, `cao plugin validate ./agent-plugin/cao` exits **1** with a refusal and **emits no JSON**. | **OBSERVED (read: `cli/commands/agent_plugin.py:154-158`)** | The gate-off **exit 1** was run (`research.md:168, 192`), in text mode. The **no-JSON** half was not run: it is read from the group callback raising `ClickException` before any subcommand executes, so stdout is empty. Relabelled per `review-2.md` **N-B**, which records that the reviewer's own round-3 wording caused the earlier `OBSERVED (run)` label. |
| P-29 | With the gate on, the same command exits **0**; a FATAL fixture exits **1**. Under `--json`, stdout parses with a `loadable` key. | **OBSERVED (read: `cli/commands/agent_plugin.py:259-270`)** | The clean-package runs were observed, exit 0 / exit 1 (`research.md:189-191`) — but the `--json` run was on a **clean** package, so "a FATAL finding still emits parseable JSON" is read, not run: `validate --json` prints the report at `:258-259` and only then raises `SystemExit(1)` at `:269-270`. `tasks.md` T3 now runs the FATAL fixture with `--json` to close this. Relabelled per **N-B**. |
| P-30a | `build_agent_plugin.py --check` exits **0** today. | **OBSERVED (run)** | `Makefile:46-47`, CI `ci.yml:100-101` (`research.md:187`). |
| P-30b | The sibling flags `--skip-publish-check`, `--require-bounded-client`, `--require-current-pin` **exist**; none was run. | **OBSERVED (read, not run)** | `build_agent_plugin.py:988, 997, 1007`. Split from a single mislabelled `OBSERVED (run)` row per **N-6**; `research.md` does not mention these flags. |
| P-31 | `sync_skills.py --check` exits 0 with "OK: 14 shipped skills are in sync." | **OBSERVED (run)** | `research.md:193`. |
| P-32 | `validate_skills.py skills/` exits 0, "14 passed, 0 failed"; packaged copies "6 passed"; the three source skills "3 passed". | **OBSERVED (run)** | `research.md:194-196`. |
| P-33 | `validate_markdown_links.py` exits 0, and `docs/issues/` is **not** excluded from it. | **OBSERVED (run)** | `research.md:199`; exclusion list read at `src/cli_agent_orchestrator/utils/markdown_links.py:19-33` — `docs/issues` absent, `docusaurus/blog` present. |
| P-34 | Targeted pytest exits 0, "169 passed in 147.36s". | **OBSERVED (run)** | `research.md:200`. |
| P-35 | `run_trigger_evals.py --all skills/` **crashes**, exit 1, `AttributeError` at `:186`, on `agui-author`'s list-shaped evals. | **OBSERVED (run)** | `research.md:198`; two shapes at `research.md:91-99`. |
| P-36 | Trigger evals are a keyword heuristic (0.35 overlap + 0.25 Jaccard + 0.20 body + 0.20 bigram, thresholds 0.192/top-2 or 0.36), **not** activation, and negative boundaries are stripped before scoring. | **OBSERVED** | `run_trigger_evals.py:232, 295-307, 152` (`research.md:87`); no provider-level activation test found (`research.md:302`). |
| P-37 | `run_trigger_evals.py` declares `httpx>=0.27` but imports only the standard library. | **OBSERVED** | `:3`, `:29-30` (`research.md:87`). |
| P-38 | The installed `cao-skill-creator`'s own trigger evals **fail**, 7/10, exit 1, with false triggers including "write a Discord plugin…". Measured in `$S`, not in `skills/`. | **OBSERVED (run)** | `research.md:197`. |
| P-39 | `cao-plugin`'s description overlaps agent-plugin authoring phrasing. The **bigram** "agent plugin" is absent, but the tokens "agent" and "plugin" both occur (via "CLI Agent Orchestrator"). | **OBSERVED** | `skills/cao-plugin/SKILL.md:3` (443 chars) and `$S/cao-plugin/SKILL.md:3` (535 chars) (`research.md:34-40`). Token qualification added per **N-10**. |
| P-40 | #573's **M4** already records that `cao-plugin`'s description "will now compete for activation against agent-plugin requests", and proposes renaming it `cao-event-plugin`. M4 is open. | **OBSERVED** | `573/design.md:144-149` (`research.md:41`). |
| P-41 | Both source skills' descriptions exclude "plugins" as a negative boundary, so evolving them reverses it. | **OBSERVED** | `research.md:70-71`; descriptions `research.md:60-66`. |
| P-42 | `docusaurus.config.ts:64-66` sets `onInlineAuthors: 'throw'`, `onInlineTags: 'throw'` **and** `onUntruncatedBlogPosts: 'throw'`, so a truncate marker is mandatory. | **OBSERVED** | `research.md:205`. |
| P-43 | The truncate marker in use is the **MDX** form `{/* truncate */}`. | **OBSERVED (direct)** | All six posts: `2026-08-26…:18`, `2026-09-07…:10`, `2026-09-16…:10`, `2026-10-08…:29`, `2026-10-11…:27`, `2026-10-12…:21` (`grep`, exit 0). `research.md:205` says `<!-- truncate -->`; that is incorrect — **OQ-11**, informational. |
| P-44 | `authors.yml` has no `plauzy` key. Existing entries omit `title` entirely rather than leaving it blank. | **OBSERVED (direct)** | `docusaurus/blog/authors.yml:44-99`; the `haofeif`…`yinsong1986` entries have no `title` key. Refined per **N-15**. |
| P-45 | `tags.yml` keys are `tutorial, deep-dive, orchestration-patterns, providers, mcp, case-study, community, physical-ai`. | **OBSERVED (direct)** | `tags.yml:8-43`. |
| P-46 | Posts use the directory form `docusaurus/blog/YYYY-MM-DD-slug/index.md`. Front matter is `title, authors, tags, description` in **five** of six posts; `2026-08-26-welcome-to-the-cao-blog/index.md:2` additionally carries `slug:`. | **OBSERVED (direct)** | My per-post front-matter read of all six. **Corrected per N-7** — the earlier row claimed "exactly" four keys in all six, which my own Round 1 data contradicted. |
| P-47 | The Docusaurus build was **not** run by research. | **UNVERIFIED** | `research.md:205, 303`. |
| P-48 | `docusaurus/blog` is excluded from the repo link validator, and covered instead by `onBrokenLinks: 'throw'` at build time. | **OBSERVED (direct)** | `markdown_links.py:25-28`; `docusaurus.config.ts:20`. |
| P-49 | `pyproject.toml` is **not** an obligation for a new skill: `artifacts` has four globs, none matching `skills/`, and the mirror is git-tracked. | **OBSERVED (direct)** | `pyproject.toml:351-375`; `git check-ignore` exit 1; principle stated at `test/test_hatch_artifacts_guard.py:33-37`. |
| P-50 | Adding a `skills/**` entry to `artifacts` would **fail** CI. | **OBSERVED (direct)** | `test_artifacts_entries_are_all_guarded`, `test/test_hatch_artifacts_guard.py:123-128`. |
| P-51 | M1's status disagrees between files. | **OBSERVED** | `research.md:154`. |
| P-52 | **No longer true: #1121 fixed it.** `build_agent_plugin.py:38-40` now reads "`cao-contributing` ships in the contributor package. It was held back while PR #448 was a draft; **#448 merged, and adding it was the one allowlist line this design promised (#1120)**", and `:179` reads "Added once PR #448 merged (#1120)." | **OBSERVED (run)** | `grep -n '#448' scripts/build_agent_plugin.py` at `00611d11`. **An earlier row recorded those comments as stale and two requirements listed correcting them as out of scope; both are now moot** and say so. Recorded rather than deleted, because a reviewer who read the earlier revision would otherwise look for an unfixed defect that no longer exists. |
| P-53 | Both committed packages pin `2.5.3` while `pyproject.toml` is at `2.5.4`; the lag is deliberate. | **OBSERVED** | `docs/agent-plugins.md:11-23`; `--check` output (`research.md:142`). |
| P-54 | A real non-CAO Agent Plugin (`revive-oss-pr`) is installed through CAO's own projection as a symlink in the skills store. | **OBSERVED** | `ls -la $S` (`research.md:179`). |
| P-55 | Two `evals.json` shapes are in use, object and list. | **OBSERVED** | `research.md:92-95`. |
| P-56 | The source audit skill's evals reference three artifacts that do not exist. | **OBSERVED** | `evals.json:18, 42, 33` (`research.md:98-99`). |
| **P-57** | `migrate-agent-plugin` appears **nowhere outside `573/design.md` and `test/agent_plugins/`**. Not in `scripts/`, `skills/`, `src/`, `agent-plugin/`, `docs/agent-plugins.md`, `cli/commands/init.py`, or the installed store. | **OBSERVED (run)** | Four commands, all mine. **(1)** `grep -rn` over `*.py,*.md,*.json,*.yml,*.ts`, **excluding** `.git/`, `node_modules/`, `test/` and this spec directory → 3 hits, all `573/design.md:151,862,1172`. **(2)** `grep -rl` **including** `test/` → hits confined to the fixture plus `test/agent_plugins/{test_cli,test_conformance_corpus,test_installer,test_validation}.py`, all fixture consumers, **none** a `SHIPPED_SKILLS`, packaging, wheel-parity or allowlist test. **(3)** `find . -type d -name migrate-agent-plugin -not -path './test/*'` → no output, exit 0. **(4)** `find "$CAO_HOME" -maxdepth 4 -name '*migrate-agent-plugin*'`, where `$CAO_HOME` is the CAO state directory `~/.aws/cli-agent-orchestrator` (`find`, never `grep -r`, because that tree contains named pipes on which `grep -r` hangs) → no output, exit 0. **Classification rule, fixed before the runs:** a test consuming the canonical-example fixture is not falsifying; a hit in a `SHIPPED_SKILLS`, packaging, wheel-parity or allowlist expectation would be, and would withdraw the claim rather than qualify it. No hit of the second class was found. A narrower prior check over `scripts/`, `skills/` and `docs/agent-plugins.md` was run by the supervisor (terminal `9feec10a`) and agreed. |
| **P-58** | The D4-named skill's `references/validation-checklist.md` covers naming **manually** and mentions **neither** the 1024-character nor the 500-line limit. | **OBSERVED (run)** | `:13` "`name` satisfies the v1 length and character rules" (manifest section); `:22` "the frontmatter `name` matches the directory name and Agent Skills naming rules"; `:23` description says what and when. **Two greps, both re-run 2026-10-29 after `review-2.md` **N-A** caught a misreported command.** `grep -n '1024\|500\|regex'` → **0 matches, exit 1**: no numeric limit and no "regex" appears anywhere in the file. `grep -n '1024\|500\|regex\|charac\|length'` → **1 match, `:13`, exit 0**: the checklist speaks of "length and character rules" in prose without ever giving a number. `grep -c '^- \[ \]'` → **30** human checkboxes. **An earlier revision of this row reported the narrow pattern with the wide pattern's output** — the output was real but belonged to a command I did not write down. Found by `review-1.md` **F-1**; commands mine. |
| **P-59** | CAO's installer **projects** this fixture's skill into the store and the skill catalog, so a user following D4's documented route obtains it via `cao plugin add`. "No activation contest" holds only for a **default** install. | **OBSERVED** | `test_installer.py:82-83`, `test_conformance_corpus.py:144` (`review-1.md` **N-1**). |
| **P-60** | #573 Requirement 23.5 requires the installer to deliver the canonical example's skill to **at least one provider**; 23.4 requires that fixture as a known-good positive. | **OBSERVED (direct)** | `573/requirements.md:416` (23.4) and `:417` (23.5), located by `grep -n 'canonical example Agent Plugin'`, exit 0. This is a test-time **delivery** surface with no acquisition, which is why an earlier enumeration derived from shipping surfaces missed it. **Citation history, recorded because this row's purpose is that a cited line can be trusted:** an earlier revision said `:418-419`. That number reached me from the supervisor (terminal `9feec10a`) as independently verified; it had been read off a 14-line `sed` window rather than grepped, and it was wrong. The quoted *text* was correct throughout. `review-2.md` **N-C** caught it and the supervisor's verification section records the origin. |
| **P-61** | The canonical example ships its skill **with a `references/` directory**: `client-extensions.md`, `migration-guide.md`, `validation-checklist.md`. | **OBSERVED (run)** | `git ls-files test/agent_plugins/fixtures/canonical-example/skills/migrate-agent-plugin/` → 4 files, exit 0. First observed by the supervisor (terminal `9feec10a`); re-run by me. Evidence for **OQ-13**. |
| **P-62** | `gh-pages.yml` builds the docs site on **`pull_request`** for `docusaurus/**`, so gate 9 is enforced in CI before merge even though it was never run locally. | **OBSERVED (direct)** | `gh-pages.yml:30` `pull_request:`, paths `:31-38`, `npm run build` at `:69`. |
| **P-63** | The real-pipeline install smoke test is `TestPackagesInstallThroughTheRealPipeline` at `test/agent_plugins/test_packages.py:309` **at `27311798`; at `fa2240ef` it is at `:345`** (`P-77`). The other line numbers in this row are also as of `27311798`. It is parametrised over **only** `OPERATOR_DIR` and `CONTRIBUTOR_DIR` at `:312`, with `test_both_packages_install_side_by_side` asserting the installed set equals `{"cao", "cao-contributor"}` at `:336`. | **OBSERVED (direct)** | Read at `:305-345`. A third package is therefore never installed by the suite (**N-13**). |
| **P-65** | `GENERATIVE_UI` is not an AG-UI 1.0 event type. | **UNVERIFIED — advice, not a primary source** | Relayed from a devops review via the supervisor. **Not cited to the AG-UI specification**, which this feature has not read. Requirement 16.2's prohibition does not depend on it. Recorded so the claim cannot be promoted to fact without the citation. |
| **P-64** | This fleet records outcomes in the **`workflow_outcomes`** table. It held **0 rows table-wide** at the start of this work and now holds four for workflow `agent-plugin-lifecycle-spec`: `9124f65c`, `821e3521`, `c79a4f5a`, `fa67120e`. | **OBSERVED (supervisor, terminal `9feec10a`)** | The supervisor's own `report_outcome` calls, 2026-10-29. I re-read the table and confirm four rows with that `workflow_name`; the **rows are the supervisor's**, not mine, and the table name is `workflow_outcomes`, not `outcomes`. |
| **P-66** | **Base fast-forwarded to `2731179845f9bd332a22e72fa44cc55345561c99` on 2026-11-01.** The two new commits are `5892e8e3` (#1170, the Kiro identity header) and `27311798` (#1176, a `joserfc` bump). **Neither touches a path this specification cites**, so no citation moved. Neither commit is plauzy's, so Requirement 12.13's figure is unchanged at the new base: **16 of 19**. | **OBSERVED (run)** | `git merge --ff-only upstream/main`, exit 0. `git diff --name-only 00611d11 27311798 -- scripts Makefile .github agent-plugin skills test/agent_plugins docs/agent-plugins.md CODEBASE.md pyproject.toml` prints nothing, exit 0. Every path that commit range changes was grepped for in all four specification files: the only hits are mentions of a **package's own** generated `CHANGELOG.md` and the tracked `uv.lock`, none with a line number. `git log 27311798 --format='%an%x09%(trailers:key=Co-authored-by,valueonly)'` → 19 plauzy lines, 16 with the Kiro trailer. |
| **P-67** | **The `agent-plugins-dogfood` CI job covers `agent-plugin/cao` and no other package.** The job (`ci.yml:509-561`) runs `npm run record` (`ci.yml:551`), which is `node record-dogfood-demo.mjs` (`tools/package.json:8`), which drives `run.sh`. At this base `run.sh` validates, adds and removes only `agent-plugin/cao` (`:93-94`, `:114-115`, `:272-273`). So the third package is **gated in CI by `make check-agent-plugin`** and is **not dogfooded in CI**. | **OBSERVED (direct), scoped to this sha** | `grep -nE 'agent-plugin/\|cao-contributor' examples/agent-plugins/agent-plugins-dogfood/run.sh` lists only `agent-plugin/cao` lines. **Not read:** `record-dogfood-demo.mjs`, which may carry assertions of its own. This is a claim about the file at this sha, not about its history. |
| **P-68** | **`committed_package_version()` returns `None` once a third config is in `PACKAGES` and its directory does not exist yet.** It reads `plugin.json` from **every** entry in `PACKAGES` and returns `None` on a missing or unreadable manifest or on any disagreement (`build_agent_plugin.py:319-348`, missing file at `:338-341`, disagreement at `:346-347`). The caller then falls back to `pyproject.toml`'s version, so **every** package reports `CONTENT DIFFERS` (`:329-333`, `:758`). | **OBSERVED (run)** | In memory only, nothing written: `committed_package_version()` → `'2.5.3'`; after appending a third `PackageConfig` to `PACKAGES` with no directory on disk → `None`; `package_version()` → `2.5.4`. `git status --short` unchanged afterwards. |
| **P-69** | **`test/agent_plugins/test_packages.py` names exactly two packages at ten sites, and every one of them still passes when a third package exists, while covering only the first two.** Parametrised over `[OPERATOR_DIR, CONTRIBUTOR_DIR]`: `:42`, `:50`, `:54`, `:67`, `:83`, `:312`. Parametrised over `(config, package_dir)` pairs: `:216-219`. Two-package assertions that are not parametrised: `:223-225` (the allowlists are disjoint, compared pairwise), `:227-234` (`--check` names `cao:` and `cao-contributor:`), and `:326-340` (`test_both_packages_install_side_by_side`, which loops over two directories at `:328` and asserts the installed set equals `{"cao", "cao-contributor"}` at `:336`). | **OBSERVED (direct)** | `grep -nE 'parametrize\|OPERATOR_DIR, CONTRIBUTOR_DIR\|cao-contributor' test/agent_plugins/test_packages.py`, and each site read. **Corrects** the earlier "six parametrised sites", which omitted the `(config, package_dir)` site at `:216-219`, and the checkpoint's list, which omitted `:312`. **Two further sites outside this file**, found by review 6 (**N-9**) and read at `27311798`: `test/test_agent_plugin_pin_boundedness.py:484-493` (`test_packages_disagreeing_is_unknowable` writes only `cao` and `cao-contributor`, so with a third config it passes because a manifest is missing, not because two disagree) and `test/test_agent_plugins_docs.py:139-164` (`test_package_skill_lists_match_the_build_configuration` iterates `PACKAGES`, so it fails from T5 until T7). |
| **P-70** | **Three release-path questions are answered, and none needs action.** (1) `publish-to-pypi.yml`'s `regenerate-agent-plugin` job (`:503`) runs the builder, which iterates `PACKAGES`, so a third package is regenerated with no workflow edit. (2) `--require-bounded-client` (`:532`) is decided globally: `ships_mcp = any(config.ships_mcp for config in PACKAGES)` (`build_agent_plugin.py:875`, used at `:876` and `:891`), so a package with no `mcp.json` neither triggers nor escapes it. (3) `--require-current-pin` (`publish-to-pypi.yml:572`; flag at `build_agent_plugin.py:1007-1015`) is **not** a defect for a package never released at `2.5.3`: the manifest `version` means "the CAO release this package is pinned to", which the generated CHANGELOG states ("This package is pinned to CAO v2.5.3", `agent-plugin/cao-contributor/CHANGELOG.md:12`), and the release job repins every package in one pass. | **OBSERVED (direct)** | Read at this base; `scripts/` and `.github/` are unchanged since `00611d11` (`P-66`). Whether the published 2.5.4 sdist passes `verify_bounded_client` is **not** known, and needs a networked run nobody has authorised. |
| **P-71** | **A signed commit made on this machine reports `%G?` = `G`.** The user's git configuration signs with SSH (`gpg.format=ssh`, `user.signingkey=~/.ssh/id_ed25519.pub`, `commit.gpgsign=true`) and verifies against an allowed-signers file (`gpg.ssh.allowedSignersFile`). Upstream's commits by the same author report `E`, because they are squash merges this machine cannot verify. | **OBSERVED (run, 2026-11-01)** | In a throwaway `git clone --shared` of this worktree, never the worktree itself: `git commit -S` with the two trailers, exit 0; `git log -1 --format='%G? %GS %an %ae'` → `G 4451274+plauzy@users.noreply.github.com plauzy 4451274+plauzy@users.noreply.github.com`; `git verify-commit HEAD` → `Good "git" signature for 4451274+plauzy@users.noreply.github.com with ED25519 key SHA256:<redacted>`, exit 0. The clone was deleted afterwards. `git log 27311798 --author=plauzy --format=%G?` → `E` for every upstream commit read. |
| **P-72** | **Correction: an unknown manifest key is not always FATAL in CAO.** An unknown top-level key gives WARNING `manifest.unknown_field` (§5.2) and is stripped before schema validation, so the package stays loadable and the command exits 0. An unknown key nested in an object reaches the closed sub-schema and gives FATAL `manifest.invalid`, exit 1. Found by the T1 implementer, then re-verified by the author seat. **Requirement 1.4, `P-23`, T1's description and T5's defect 7 previously stated the opposite and are corrected; `research.md:128` still states it and is not edited (Requirement 13.7).** | **OBSERVED (run and read, 2026-11-01)** | Read: `_apply_manifest_tolerances`, `src/cli_agent_orchestrator/agent_plugins/validation.py:462` onward, whose docstring names "an unrecognized top-level field (§5.2) — reported, ignored, plugin still loads", called at `:288` before `_schema_findings`; `test/agent_plugins/fixtures/corpus/cases.json:61-69`. Run: `CAO_AGENT_PLUGINS_ENABLED=1 uv run cao plugin validate ./test/agent_plugins/fixtures/corpus/manifest-unknown-field --json` → `"loadable": true`, one finding `warning` `manifest.unknown_field` "Unrecognized top-level manifest field 'hooks' was ignored; the plugin still loaded", exit **0**. A temporary package whose manifest has `"author": {"name": "x", "twitter": "@x"}` → `"loadable": false`, `fatal` `manifest.invalid` "author: Additional properties are not allowed ('twitter' was unexpected)", exit **1**. |
| **P-73** | **A fresh `CAO_HOME_DIR` is not left empty by `cao plugin validate`.** Three empty directories appear: `fifos/`, `logs/` and `logs/terminal/`, and no files. The cause is **inferred** to be CAO's startup directory creation and has **not been traced**. | **OBSERVED (run) for the effect; INFERRED for the cause** | `t3-overlimit-experiment.md` § 9; reproduced by the author seat during `P-72`'s run, whose fresh `CAO_HOME_DIR` held `./fifos ./logs ./logs/terminal`. |
| **P-74** | **The `Audit_Skill` bundles its own byte-identical copy of the vendored schemas** at `skills/cao-agent-plugin-audit/schemas/1.0.0/` (`plugin.schema.json`, `mcp.schema.json`). Its validator **refuses to run, exit 2,** if a bundled file does not hash to its embedded pin, and also if a CAO `PIN.json` it finds above itself records different hashes. **Consequence:** T4's wheel mirror and T5's package each carry a further copy of those bytes, because both copy the skill directory whole. | **OBSERVED (read and compared)** | `cmp` of both bundled files against `src/cli_agent_orchestrator/schemas/agent_plugins/1.0.0/` → identical. `skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py`: embedded pin `:54-61`, `PIN.json` search paths `:63-65` and `_find_cao_pin` `:113-121`, refusals in `load_schemas` `:124-150`, `AuditError` → `return 2` in `main` `:420-426`. Both `scripts/sync_skills.py`'s `sync()` and `build_package`'s `copytree` copy every file in the skill directory. |
| **P-75** | **Two bad-path cases of `cao plugin validate` emit parseable JSON**, so neither is the unchecked "third exit-1 case" of Requirement 4.8. A nonexistent path and a regular file each give `"loadable": false` and `plugin.root_invalid`, exit 1. Other raise paths are unchecked. | **OBSERVED (run, 2026-11-01)** | Reported by the T2 implementer and re-run by the author seat: `CAO_AGENT_PLUGINS_ENABLED=1 uv run cao plugin validate /nonexistent/cao-apl-x --json` → exit 1, parses, `loadable False`, codes `['plugin.root_invalid']`; the same on a `mktemp` regular file → identical. |
| **P-76** | **The bundled validator exits 1 on a finding CAO rates WARNING.** On `manifest-unknown-field` it prints `WARNING  manifest.unknown_field [plugin.json]: top-level key 'hooks' is outside the closed schema; CAO ignores it and loads the package, another client may reject it` and `summary: 0 fatal, 0 skipped, 0 limit, 1 warning, 1 advisory; exit 1`, while `cao plugin validate` exits 0 on the same fixture (`P-72`). This follows T2's exit contract (1 = findings) and is not a disagreement about the finding. | **OBSERVED (run, 2026-11-01)** | `uv run --quiet --script skills/cao-agent-plugin-audit/scripts/validate_agent_plugin.py ./test/agent_plugins/fixtures/corpus/manifest-unknown-field` → exit 1, output as quoted. |
| **P-77** | **`TestPackagesInstallThroughTheRealPipeline` moved from `test/agent_plugins/test_packages.py:309` to `:345`** when T5 (`fa2240ef`) extended the file. **Updated:** `P-63`, `design.md`'s install-smoke-test paragraph, T5's list of two-package sites, and T6's description and criterion, each now naming both positions with their commits. **Kept as historical:** every line number in `P-63`, `P-69` and Requirement 8.11 that names a site T5 edited; those stay pinned to `27311798`, where T5 found them. | **OBSERVED (run, 2026-11-01)** | `git show fa2240ef:test/agent_plugins/test_packages.py \| grep -n 'class TestPackagesInstallThroughTheRealPipeline'` → `345:`; the same at `HEAD` → `345:`. Found by T6, and reported in its `audit-report.md`. |
| **P-78** | **The published per-token "unqualified plugin" check could never pass on correctly qualified prose.** It split text on whitespace, so the qualifier and the noun of "agent plugin" landed in different tokens. Run on T7's commit `a2953880`, it rejected "agent plugins," and "an agent plugin", and the `plugin` inside a backticked `cao plugin validate`. **Replaced** in Requirement 10.5 and T7's criterion by the repository's own phrase-level rule (`_UNQUALIFIED_PLUGIN`, `test/agent_plugins/test_naming_migration.py:74`, with `_prose_of`'s stripping at `:60-70`). | **OBSERVED (run, 2026-11-01)** | T7's report. The replacement check, run as published under zsh and bash, exits **0** on `a2953880`. In throwaway commits it exits **1** on a planted "This plugin is planted…" sentence, **1** on a line mixing "An agent plugin" with a bare "plugins", **0** on qualified-only prose, and **0** on an inline code span and a link that wrap across a line break. **Its first draft stripped code and links line by line, and that falsely flagged T7's own `cao plugin validate` span, which wraps across a line in `docs/agent-plugins.md`** (exit 1 on `a2953880`). It now strips the whole text in `_prose_of`'s order, keeping newlines, as the published block does. Recorded in `STATUS.md`, "Wave 4 note". |

## Requirements

### Requirement 1: `Creator_Skill` — authoring an Agent Plugin from scratch

**User Story:** As a contributor asked to package some skills as a portable Agent Plugin, I want a skill that walks me from an empty directory to a package CAO's own validator calls loadable, so that I do not have to reverse-engineer the manifest rules out of `plugin.schema.json` and `build_agent_plugin.py`.

#### Acceptance Criteria

1. THE `Creator_Skill` SHALL be named `cao-agent-plugin-creator`, SHALL live at `skills/cao-agent-plugin-creator/`, and its frontmatter `name` SHALL equal that folder name.
2. THE `Creator_Skill` frontmatter SHALL declare `name` and `description`, and its `description` SHALL be at most 1024 characters.
3. THE `Creator_Skill` body SHALL be at most 500 lines, measured as the text after the closing `---`, stripped.
4. THE `Creator_Skill` SHALL state the required manifest keys as `$schema` and `name` only; SHALL state that the schema is closed and how CAO treats a key outside it: an unknown **top-level** key is reported as a WARNING (`manifest.unknown_field`) and **ignored**, and the package still loads, while an unknown key **nested** in an object, such as `author.twitter`, is FATAL (`manifest.invalid`); SHALL NOT state that every unknown key is FATAL (corrected 2026-11-01, `P-72`); and SHALL state the `name` pattern and its 1–64 character bound. **[review-checked]**
5. THE `Creator_Skill` SHALL state that `README.md`, `CHANGELOG.md` and `LICENSE` are **not** required by the vendored schemas, while noting that CAO's builder generates them for CAO's own packages. **[review-checked]**
6. THE `Creator_Skill` SHALL state that a `skills/` directory is optional and that each immediate child is one Agent Skill, one level only. **[review-checked]**
7. THE `Creator_Skill` SHALL state that CAO validates against the **vendored** schema bytes recorded in `PIN.json` and never fetches a schema at load time. **[review-checked]**
8. WHERE the `Creator_Skill` instructs the reader to run `cao plugin validate`, THE `Creator_Skill` SHALL state that `CAO_AGENT_PLUGINS_ENABLED` must be set to a truthy value, that the value is stripped and lowercased before comparison against `1`, `true` and `yes`, and that with the gate unset the command exits 1 **and emits no JSON** rather than reporting a conformance result.
9. THE `Creator_Skill` SHALL NOT instruct the reader to run any script at a path that does not exist in this repository.
10. THE `Creator_Skill` SHALL satisfy the three Agent Skills limits — name regex, description ≤ 1024 characters, body ≤ 500 lines — and **the `Audit_Skill`'s bundled validator SHALL report it as passing**. THE limits SHALL remain measurable directly, so the criterion is testable before the bundled validator exists and does not depend on any script absent from a fresh clone.

**Traceability:** P-23, P-24, P-25, P-26, P-27, P-28, P-13; Criterion 10 rewritten per `review-1.md` **F-3**, which showed the earlier wording made this criterion untestable from the repository. This is the requirement **OQ-1b** may delete. Exercised by Property P2, P5, P6.

---

### Requirement 2: `Audit_Skill` — auditing a package against the limits nothing CAO ships enforces

**User Story:** As a reviewer asked whether a candidate Agent Plugin is conformant, I want a skill that mechanically checks the rules CAO's loader skips, so that a package which passes `cao plugin validate` is not mistaken for one that meets the Agent Skills limits.

#### Acceptance Criteria

1. THE `Audit_Skill` SHALL be named `cao-agent-plugin-audit`, SHALL live at `skills/cao-agent-plugin-audit/`, and its frontmatter `name` SHALL equal that folder name.
2. THE `Audit_Skill` frontmatter `description` SHALL be at most 1024 characters, and its body SHALL be at most 500 lines.
3. THE `Audit_Skill` SHALL state explicitly that CAO's package validation checks a skill only for non-blank `name` and `description` and for folder-equals-`name`, and that **no artifact CAO ships mechanically enforces** the Agent Skills name regex, the 1024-character description limit, or the 500-line body limit. **[review-checked]**
4. THE `Audit_Skill` SHALL **mechanically** check, for each skill in the audited package: the name regex, the 64-character name bound, folder-equals-`name`, the 1024-character description bound, and the 500-line body bound.
5. THE `Audit_Skill` SHALL classify each finding by severity, and SHALL distinguish a finding that makes a package unloadable from one that does not. **[review-checked]**
6. THE `Audit_Skill` SHALL report the manifest checks of Requirement 1, Criterion 4 against the vendored schema bytes rather than against a fetched schema. **[review-checked]**
7. THE `Audit_Skill` SHALL NOT claim that CAO rejects a skill which breaks the Agent Skills limits. WHERE it describes CAO's behaviour for an over-limit skill, it SHALL mark that behaviour as inferred from `models/skill.py` and not observed, UNLESS `tasks.md` T3 has settled it, in which case it SHALL cite T3's result. **T3 has settled it for the two length limits** (`P-6`, OBSERVED): for those the `Audit_Skill` SHALL cite T3's result, and for the name regex and the 64-character bound, which T3 did not test, it SHALL still mark the behaviour as inferred.
8. THE `Audit_Skill` SHALL NOT reference an artifact outside the repository, and SHALL NOT reference a baseline report or external CLI that the skill does not itself create.
9. THE `Audit_Skill` SHALL satisfy the three Agent Skills limits on the same terms as Requirement 1, Criterion 10, **including passing its own bundled validator** — a validator that cannot validate the skill shipping it is not a validator.
10. IF **OQ-1b** resolves such that `Creator_Skill` is dropped, THEN THE `Audit_Skill` SHALL remain implementable with no change to this requirement beyond removing its cross-reference to `Creator_Skill`.
11. THE `Audit_Skill` SHALL NOT claim that no existing skill audits an Agent Plugin. WHERE it describes prior art, IT SHALL record that the D4-named skill's `references/validation-checklist.md` covers naming as human checkboxes and mentions neither the 1024-character nor the 500-line limit. **[review-checked]**
12. THE `Audit_Skill` SHALL emit an **advisory** finding that lists the manifest's declared `description` beside the discovered skill names, so a reader can judge whether they correspond. THE finding SHALL be advisory, never fatal, and THE `Audit_Skill` SHALL NOT claim to **detect** semantic mismatch between a description and a skill set — that is a judgement about meaning, and a criterion asserting detection would have no input that makes it fail.

**Traceability:** P-4, P-5, P-6, P-7, P-56, P-58; Criteria 4 and 11 narrowed per `review-1.md` **F-1**; Criterion 12 added per review 4's **B-6**, with `tasks.md` T2 carrying it. Criteria 11 and 12 were written in the order 12, 11 until review 6 (**N-1**); they are now in numeric order, with their numbers unchanged. This is the half whose gap survives D4's premise, hence Criterion 10. Exercised by Property P2, P6.

---

### Requirement 3: Activation boundary against `cao-plugin`, the event-plugin system, and `migrate-agent-plugin`

**User Story:** As an agent choosing a skill for an incoming request, I want the agent-plugin, event-plugin and migration skills to have disjoint trigger surfaces, so that a request to package an Agent Plugin does not load the skill about `cao-server` event observers or the skill about migrating a client-specific plugin.

#### Acceptance Criteria

1. THE `Creator_Skill` and `Audit_Skill` descriptions SHALL each contain a negative boundary naming the event-plugin system explicitly.
2. THE `Creator_Skill` and `Audit_Skill` descriptions SHALL each use at least one term absent from both copies of `cao-plugin`'s description — from `plugin.json`, `mcp.json`, or `manifest`. THE phrase `agent plugin` SHALL NOT be relied on for this purpose, because the tokens "agent" and "plugin" both occur in `cao-plugin`'s description and only the bigram is absent.
3. THE `Creator_Skill` and `Audit_Skill` SHALL NOT use an unqualified "plugin" in their `description`, and SHALL qualify every occurrence in the body. **[review-checked]**
4. THE `Creator_Skill` and `Audit_Skill` SHALL NOT reuse the source skills' negative boundary excluding "plugins", because this feature reverses it.
5. WHERE #573's decision **M4** is unresolved, THE `Creator_Skill` and `Audit_Skill` SHALL name the event-plugin skill by its **current** name `cao-plugin`, and SHALL NOT assume the proposed rename. **[review-checked]**
6. WHEN the trigger-eval heuristic is run per-skill, THE result SHALL be recorded with the directory it was measured in named, and SHALL be described as a keyword-ranking measurement and NOT as evidence that either skill activates in a provider.
7. THE `Creator_Skill` and `Audit_Skill` eval files SHALL each include at least one should-not-trigger query drawn from event-plugin phrasing.
8. THE `Creator_Skill` description SHALL contain a negative boundary excluding migration of an existing client-specific plugin, naming `migrate-agent-plugin`; and THE `Audit_Skill` SHALL scope itself to auditing an existing v1 package's conformance and limits, NOT to auditing in preparation for a migration.
9. THE specification SHALL record that this third boundary is **precautionary rather than remedial**, and SHALL record the qualifier: CAO's installer **does** project that skill into the store and catalog, so a user following D4's documented route obtains it via `cao plugin add`; the absence of an activation contest holds only for a default install. **[review-checked]**
10. THE specification SHALL record that **no gate can check Criterion 8**: the trigger-eval heuristic ranks a skill only against the skills present in the scanned directory, and a fixture under `test/` is never in `skills/`. Criterion 8 is therefore **[review-checked]** by reading the two descriptions. **[review-checked]**

**Traceability:** P-39, P-40, P-41, P-36, P-59, P-60; Criteria 8–10 added per `review-1.md` **N-1**, Criterion 2 narrowed per **N-10**. **OQ-3** carries the M4 dependency. Exercised by Property P1, P6.

---

### Requirement 4: The bundled validator and its dependency posture

**User Story:** As someone auditing a package on a machine that may not have CAO's plugin surface enabled, I want to know exactly what the audit depends on, so that an audit does not silently become a no-op because an environment variable was unset.

#### Acceptance Criteria

1. THE `Audit_Skill` SHALL ship a **bundled validator pinned to the `PIN.json` schema hashes**, and SHALL **also** run `cao plugin validate --json` when `CAO_AGENT_PLUGINS_ENABLED` is set (**OQ-4**, decided: option C). IT SHALL name both and state each one's dependency on the CAO installation. **[review-checked]**
2. WHERE the `Audit_Skill` wraps `cao plugin validate`, THE `Audit_Skill` SHALL state that `CAO_AGENT_PLUGINS_ENABLED` is a precondition, that the value is stripped and lowercased before comparison, and that an unset gate yields exit 1 with no JSON and no conformance result.
3. THE bundled validator's **positional argument SHALL be an Agent Plugin package root** — a directory containing `plugin.json`. A bare skills directory SHALL be accepted **only through `--skills-dir`**. **This matters because `skills/` in this repository is not a package root:** it contains `skills/plugin.json`, an inert proto-manifest with `name: cao-skills` and **no `$schema`**, which a package-root validator would reject. WHERE the three Agent Skills limits must be checked across `skills/`, THAT is a **separate skills-directory mode** and the specification SHALL say which mode each gate invokes. THAT mode SHALL be `--skills-dir <dir>`: each **immediate child** of `<dir>` that contains a `SKILL.md` is one skill; a `plugin.json` at the top of `<dir>` and any child without a `SKILL.md`, such as `skills/vendor/`, SHALL be ignored, and the script SHALL say that it ignores them.
4. WHERE the `Audit_Skill` bundles its own checking script, THE script SHALL carry a PEP 723 inline dependency block declaring only dependencies the script imports.
5. WHERE the `Audit_Skill` bundles its own checking script, THE script SHALL validate against the vendored schema bytes identified by `PIN.json`, and SHALL NOT fetch a schema over the network.
6. THE `Audit_Skill` SHALL NOT depend on `harness-oss/tools/validate-plugin.py`, because it hard-codes its own root and ignores its arguments.
7. THE `Audit_Skill` SHALL distinguish, for any validator it names, between validating a manifest against a JSON Schema and comparing a hand-coded allowlist with a schema's property set. **[review-checked]**
8. THE `Audit_Skill` SHALL discriminate an unset-gate exit from a FATAL-finding exit **mechanically**, not by prose: under `--json` a FATAL finding still emits parseable JSON carrying a `loadable` key, whereas an unset gate emits the refusal text and no JSON. THE discriminator SHALL be "stdout parses as JSON", not the exit status, which is 1 in both cases. **WHERE a third exit-1 case exists, THE `Audit_Skill` SHALL NOT treat absence-of-JSON as proving an unset gate.** Whether `validate_plugin` can raise on a bad path — producing exit 1 with no JSON and no refusal text — was unchecked (`review-2.md` **N-B**). **It is now partly closed by observation** (`P-75`): a nonexistent path and a regular-file path each produce parseable JSON with `"loadable": false` and `plugin.root_invalid`, exit 1, so neither is a third case. **Other raise paths remain unchecked.** Until they are, "no JSON" distinguishes "not a FATAL finding" and nothing narrower, and a wrapper SHALL report the refusal text it did or did not see rather than inferring a cause from its absence. **A related exit difference is by design, not a disagreement with CAO** (`P-76`): the bundled validator exits **1** for any finding, including one CAO rates WARNING, so on `manifest-unknown-field` it exits 1 where `cao plugin validate` exits 0. Both report the same finding; the validator's exit contract (0 clean, 1 findings, anything else = the audit failed, `tasks.md` T2) counts a WARNING as a finding.
9. WHERE the `Audit_Skill` bundles a script, THAT script SHALL be the gate named in Requirement 11 in place of the non-repo-tracked `validate_skills.py`, once it exists.

**Traceability:** P-8, P-9, P-10, P-26, P-27, P-28, P-29, P-37; Criterion 8 made mechanical per `review-1.md` **N-4**, Criterion 9 added per **F-3**. Criteria 5–9 were numbered 4–8 until review 6 (**N-1**), when a duplicated "4." was found; Criterion 3's skills-directory mode was specified after review 6's **R6-B-1**. **OQ-4** selects among the options. Exercised by Property P5.

---

### Requirement 5: Evals format, and honesty about what trigger evals prove

**User Story:** As a contributor adding eval files, I want the required shape stated and the CI entry point chosen so that my addition does not crash the eval runner, and I want the runner's output described as what it measures.

#### Acceptance Criteria

1. THE `Creator_Skill` and `Audit_Skill` SHALL each ship an `evals/evals.json` in the **object** shape, with `skill_name`, `trigger_evals[]` of `{query, should_trigger}`, and `evals[]`.
2. THE eval files SHALL each contain at least five should-trigger and at least five should-not-trigger queries.
3. THE specification SHALL NOT adopt `run_trigger_evals.py --all` as a gate, because that invocation crashes on the existing list-shaped `agui-author` eval file.
4. WHERE trigger evals are run as a check, THEY SHALL be invoked per-skill with `--skill <name>`.
5. WHEN a trigger-eval result is reported in any document produced by this feature, THE report SHALL describe it as a keyword-ranking heuristic over the skills in one directory, and SHALL NOT describe it as evidence of activation in a provider. **[review-checked]**
6. THE specification SHALL NOT require a fix to `run_trigger_evals.py`, and SHALL record that the crash and the unused `httpx` dependency are pre-existing and out of scope. **[review-checked]**
7. WHERE a document produced by this feature states a trigger-eval score, THE document SHALL state the directory the score was measured in. **[review-checked]**
8. THE specification SHALL NOT set a numeric trigger-eval score as a pass threshold, because no criterion can enforce a score that depends on which other skills share the scanned directory.

**Rationale for Criterion 8, not part of it:** the installed `cao-skill-creator` scored 7/10 in its own store (`P-38`). That figure is a working-record measurement against a script that is not in this repository, so it is context only — it was previously written into the criterion text, which made a criterion appear to depend on evidence a reviewer cannot reach.

**Traceability:** P-35, P-36, P-37, P-38, P-55; Criterion 8 added per `review-1.md` **N-3**, which found a baseline asserted in `design.md` with no criterion behind it. **OQ-5** may change Criterion 1 or 6. Exercised by Property P6.

---

### Requirement 6: Both skills are net-new additions, with no rename path

**User Story:** As a maintainer upgrading an installation, I want to be sure this change cannot leave two copies of a skill in the store, so that no retirement step is silently owed.

#### Acceptance Criteria

1. THE `Creator_Skill` and `Audit_Skill` SHALL be **new** directories under `skills/`, and the specification SHALL NOT describe either as a rename, move, or supersession of `cao-skill-creator` or `cao-skill-audit`.
2. THE specification SHALL record that `cao-skill-creator` and `cao-skill-audit` are not repo-tracked on any reachable ref and exist only in an installed store, and SHALL treat them as design input only. **[review-checked]**
3. THE change SHALL NOT add any entry to `SKILL_RENAMES` in `cli/commands/init.py`.
4. THE change SHALL NOT remove or rename `cao-skill-creator` or `cao-skill-audit` from any installed store.
5. THE specification SHALL record that the provenance of the two source skills is unknown and SHALL NOT attribute them to this repository, to a merged pull request, or to an identified upstream. **[review-checked]**
6. WHERE the specification cites a behaviour of the source skills, IT SHALL cite the installed copy and SHALL mark it as not repo-tracked. **[review-checked]**
7. THE `Creator_Skill` and `Audit_Skill` SHALL NOT reference the path `skills/cao-skill-creator/scripts/…`, which does not exist in this repository and which an existing `upstream/main` test guards against.
8. WHERE Requirement 11 names the installed `validate_skills.py` as a bootstrap gate, THAT reference SHALL carry the not-repo-tracked and unknown-provenance caveats, and SHALL NOT appear inside either new skill's body. Requirement 6's "design input only" constraint governs the skills' **content**; it does not forbid a human running a script on their own machine to check a limit.

**Traceability:** P-11, P-12, P-13, P-14, P-20; Criterion 8 added per `review-1.md` **F-3**, which identified a contradiction between this requirement and the gate ladder. Exercised by Property P7.

---

### Requirement 7: Wheel shipping and mirror parity

**User Story:** As a maintainer, I want a new skill either shipped in the wheel or deliberately not shipped, with the generated mirror consistent either way, so that the packaging guard cannot fail after merge.

#### Acceptance Criteria

1. THE `Creator_Skill` and `Audit_Skill` **SHALL** be added to `SHIPPED_SKILLS` in `scripts/sync_skills.py` (**OQ-6**, decided), so the full obligation ladder applies.
2. WHEN a skill is added to `SHIPPED_SKILLS`, THE generated mirror under `src/cli_agent_orchestrator/skills/` SHALL be regenerated by running `python scripts/sync_skills.py`.
3. WHEN `scripts/sync_skills.py --check` is run after the change, IT SHALL exit 0 and report a shipped-skill count equal to the prior count plus the number of skills added.
4. WHEN the skill-packaging parity test is run after the change, IT SHALL pass.
5. THE change SHALL NOT modify `pyproject.toml`.
6. THE change SHALL NOT add a `skills/**` entry to `[tool.hatch.build] artifacts`, because every entry must have a corresponding wheel-contents guard and an unguarded entry fails the artifacts-guard test.
7. THE Agent-Plugin-only alternative is **not** taken (**OQ-6**). It is recorded here because the consequence is worth knowing: omitting the skills from `SHIPPED_SKILLS` would leave them absent from the wheel and unavailable to `seed_default_skills()`.
8. WHERE mirror presence is checked, IT SHALL be checked with a filesystem command rather than `git ls-files`. **OQ-16 now permits the implementation to commit, and this criterion is unaffected**, because the check runs **during** the task and any commit follows it — so at the moment the criterion runs the mirror is still unstaged. THE reasoning that made this criterion necessary therefore survives the decision that removed its original justification.

**Traceability:** P-19, P-31, P-49, P-50; Criterion 8 added per `review-1.md` **F-2**, which found a `git ls-files` check that could never pass. **OQ-6** selects between Criteria 1 and 7. Exercised by Property P3.

---

### Requirement 8: `Dogfood_Package` placement and manifest conformance

**User Story:** As a reader of the blog post, I want the package that ships these skills to have been built by CAO's own builder and audited by the skills it carries, so that the dogfood claim is a demonstrated fact rather than a description.

#### Acceptance Criteria

1. THE `Dogfood_Package` SHALL be produced by `scripts/build_agent_plugin.py` and SHALL live under `agent-plugin/`.
2. THE `Dogfood_Package` manifest SHALL declare `$schema` with the vendored 1.0.0 `plugin.schema.json` identifier and a `name` matching the schema's pattern within its 1–64 character bound.
3. THE `Dogfood_Package` manifest SHALL NOT contain a key outside the schema's property set.
4. THE `Dogfood_Package` SHALL include the `Creator_Skill` and the `Audit_Skill` among its skills.
5. WHERE the `Dogfood_Package` is a **third** `PackageConfig`, THE `CODEBASE.md` row for `agent-plugin/` SHALL be updated in the same change.
6. THE `Dogfood_Package` SHALL be a **third `PackageConfig` named `cao-agent-plugin-lifecycle`** (**OQ-7**, **OQ-12**). THE `cao-contributor`-allowlist alternative is **not** taken; its in-flight-conflict cost is now gone because #1121 is merged (`P-21`), so the decision rests on the narrative ground that the package is the deliverable.
7. THE `Dogfood_Package` SHALL NOT be placed in the `harness-oss` repository.
8. WHEN `CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate` is run against the `Dogfood_Package`, IT SHALL exit 0 and report the package loadable with both new skills named.
9. WHEN the same command is run with the gate unset, IT SHALL exit 1 and emit no JSON; and the specification SHALL NOT treat that exit as a conformance failure of the package.
10. THE `Audit_Skill` SHALL be run against the `Dogfood_Package` and its findings recorded, so that the package is **audited by a skill it carries**. THE specification SHALL NOT claim the package is "produced by" its own skills, because it is produced by `build_agent_plugin.py`.
11. WHERE a third `PackageConfig` is added, **EVERY site in `test/agent_plugins/test_packages.py` that names exactly the two existing packages SHALL be extended to cover it** (`P-69`). Each of these passes with a third package present while not covering it, so none of them fails on its own: the six sites parametrised over `[OPERATOR_DIR, CONTRIBUTOR_DIR]` at `:42`, `:50`, `:54`, `:67`, `:83` and `:312`; the `(config, package_dir)` parametrisation at `:216-219`; the pairwise disjointness assertion at `:223-225`; the `--check` both-packages assertion at `:227-234`; and `test_both_packages_install_side_by_side` at `:326-340`, whose loop at `:328` and installed-set equality `{"cao", "cao-contributor"}` at `:336` both name two packages. **Outside that file, two more sites behave the same way:** `test/test_agent_plugin_pin_boundedness.py:484-493` builds a two-package disagreement fixture and still passes with a third config, because the missing third manifest alone makes `committed_package_version()` return `None`; it SHALL be extended so the disagreement is what produces `None`, by making its fixture write all three manifests; `tasks.md` T5 owns that file and carries the edit (review 7 **R7-B-1**). `test/test_agent_plugins_docs.py:139-164` reads `PACKAGES` and requires every packaged skill to be named in `docs/agent-plugins.md`, so it **turns red** when T5 adds the third config and stays red until T7 documents it; that is an expected intermediate state, not a defect (`tasks.md` hazard 1). **This, plus the `PACKAGES` data line and `CODEBASE.md:53`, is the full edit list.** An earlier revision described the change as one line of data plus `CODEBASE.md:53`, which was incomplete.
12. THE specification and the pull request SHALL NOT claim that the `Dogfood_Package` is **"dogfooded in CI"**. The claim they MAY make is that it is **gated in CI by `make check-agent-plugin`**. The CI dogfood job reaches only `agent-plugin/cao` through `run.sh` (`P-67`). **Whether to extend `run.sh` to cover the third package is an open user question** (see [`## Decisions, and what remains open`](#decisions-and-what-remains-open)); this requirement adds no such scope.
13. THE `Dogfood_Package` SHALL first be built **at the held pin, `2.5.3`**, by calling the builder in-process, `build_package(config, committed_package_version(), PACKAGES_DIR)`, and then verified with `make check-agent-plugin`. IT SHALL NOT be built with `make agent-plugin`, which rewrites **all** packages at `pyproject.toml`'s `2.5.4` and so moves the pin. The `cao-contributing` skill's checklist item 7 (`skills/cao-contributing/SKILL.md:233-242`) says a contributor pull request should not move the pin, and its remedy, restoring the version strings by hand after the rebuild, is the step the in-process build makes unnecessary. **Failure mode, measured (`P-68`):** once the third config is in `PACKAGES` and before its directory exists, `committed_package_version()` returns **`None`**. Passing that through writes a package at no valid version, and the fallback in `--check` then reports **every** package as `CONTENT DIFFERS`. THE build step SHALL therefore assert that the version it passes is `2.5.3` and not `None`, taking it from `committed_package_version()` evaluated **before** the third config is appended, or from the two existing manifests, which agree. After the build, `committed_package_version()` SHALL return `2.5.3`, which shows that all three packages agree.


**The three release-path questions recorded here are answered** (`P-67`, `P-70`), and two of the answers changed what this requirement may claim:

1. **`publish-to-pypi.yml`'s `regenerate-agent-plugin` job and `--require-bounded-client`:** handle a third package with no edit. The job runs the builder, which iterates `PACKAGES`, and the MCP gate is global. **No action.**
2. **`--require-current-pin` on a package never released at `2.5.3`:** not a defect. The manifest `version` records the CAO release a package is pinned to, and the release job repins every package in one pass. **No action.**
3. **Does the `agent-plugins-dogfood` CI job list packages by name?** In effect, yes: `run.sh` names only `agent-plugin/cao`. **Criterion 12 is the consequence.**

What remains unverified: whether the published 2.5.4 sdist passes `verify_bounded_client`, which decides when the held pin can move and needs a networked run nobody has authorised; and `record-dogfood-demo.mjs`'s own assertions, which matter only if CI dogfooding is accepted.

**Traceability:** P-8, P-15, P-16, P-21, P-22, P-23, P-24, P-27, P-28, P-29, P-63, P-67, P-68, P-69, P-70; Criterion 10 added and the "produced by" claim softened per `review-1.md` **F-5**, Criterion 11 added per **N-13** and widened to every two-package site after review 5; Criteria 12 and 13 added from the release-path findings. **OQ-7** selects among the three placements. Exercised by Property P4, P5.

---

### Requirement 9: Package build, committed skill copies, and the drift guard

**User Story:** As a maintainer merging this change, I want the committed package bytes to equal a fresh build, so that a stale skill copy inside a package cannot reach `main`.

#### Acceptance Criteria

1. THE `Creator_Skill` and `Audit_Skill` SHALL exist under repo `skills/` before any package references them.
2. THE `Dogfood_Package` SHALL commit a byte-identical copy of each skill it ships, including any `references/` subdirectory.
3. WHEN `uv run python scripts/build_agent_plugin.py --check` is run after the change, IT SHALL exit 0.
4. THE change SHALL NOT leave a committed package skill copy differing from its canonical source in `skills/`.
5. THE set of skills discovered in the `Dogfood_Package` SHALL equal its declared allowlist.
6. THE `Dogfood_Package` SHALL be built at the version the builder holds for committed packages, and the specification SHALL NOT treat the lag between that pinned version and `pyproject.toml`'s version as a defect. Requirement 8.13 gives the build procedure and its `None` failure mode.
7. WHEN `uv run python scripts/vendor_agent_plugins_schemas.py --check` is run after the change, IT SHALL exit 0.
8. THE change SHALL NOT alter the vendored schema bytes or `PIN.json`.

**Traceability:** P-16, P-17, P-18, P-26, P-30a, P-53. Exercised by Property P4.

---

### Requirement 10: Documentation obligations

**User Story:** As a reviewer, I want every canonical document the repository's own maintenance rule names to be updated in the same change, so that no document contradicts the shipped behaviour after merge.

#### Acceptance Criteria

1. WHERE a third `PackageConfig` is added, THE `agent-plugin/` row in `CODEBASE.md` SHALL be updated to enumerate the new package.
2. THE `docs/skills.md` document SHALL be updated to describe the two new skills.
3. THE `docs/agent-plugins.md` document SHALL be updated in its "CAO's own packages" section, and in its "Verified by dog-fooding" section.
4. THE change SHALL keep every local Markdown link resolvable, and `uv run python scripts/validate_markdown_links.py` SHALL exit 0.
5. THE change SHALL NOT introduce an unqualified "plugin" into any document it touches. THIS SHALL be checked **at the phrase level with the repository's own rule**, `_UNQUALIFIED_PLUGIN` (`test/agent_plugins/test_naming_migration.py:74`, `(?<![\w-])(?<!agent )(?<!event )plugins?\b` with `re.I`). It is applied after the stripping `_prose_of` performs (`:60-70`: fenced code, inline code and links removed), and it counts **occurrences**, not lines, so one qualified occurrence cannot hide an unqualified one on the same line. A change passes when it adds no occurrence that the parent commit did not already have. The runnable form is `tasks.md` T7's check block. **An earlier wording split text into whitespace tokens (`grep -o '\S*plugin\S*'`), and that check could never pass on correct prose** (`P-78`): it cannot accept "agent plugin" with a space, it rejects "agent plugins," and "an agent plugin", and it rejects the token inside a backticked `cao plugin validate`. That wording is withdrawn.
6. THE change SHALL NOT state that #573's decision **M1** is settled.
7. THE change SHALL NOT correct the split severity table in `docs/agent-plugins.md`. THE stale #448 comments this criterion also used to name are **no longer stale** — #1121 corrected them (`P-52`) — so that half of the exclusion is moot and is retained only so a reader of an earlier revision is not left hunting a defect that no longer exists.
8. THE documents **in this specification directory** SHALL cite `research.md` for every factual claim they inherit from it. THIS obligation SHALL NOT extend to shipped documentation such as `docs/skills.md` or `docs/agent-plugins.md`, which must not cite a spec-phase research note. **[review-checked]**
9. **EXECUTABLE CONTENT SHALL appear only in fenced code blocks, never in a markdown table cell.** THIS applies to any command, regex, glob or pattern **intended to be copied and run**; a prose *mention* of a command name or flag is not executable content and is unaffected.

   **The reason, recorded because the rule looks like style and is not:** a table cell requires `|` to be escaped as `\|`, and this platform's grep reads `\|` as a **literal pipe** rather than alternation — so the escaped form is not the runnable form. Measured on BSD grep 2.6.0-FreeBSD: `echo a | grep -E 'a\|b'` exits **1** while `echo a | grep -E 'a|b'` exits **0**. A scope check published that way could neither fail on a stray write nor pass on a clean tree, and **the defect was invisible to review because the logic was correct** — only the transcription was wrong.

   **The test that catches this is coarser than reasoning about the predicate:** run the command **as it sits in the source file, under the shell a reader will paste it into**. Copying from the *rendered* page is not that test: the renderer un-escapes `\|` inside a table cell, so the rendered text works while the source text, which is what a reader opening the file copies, does not. A fail/pass analysis of the logic cannot detect it, because the published text is not the text that was reasoned about.

   WHERE a table is the right presentation — the gate ladder, for instance — THE commands SHALL live in a fenced block that the table references, and the fenced block SHALL be authoritative.

**Traceability:** P-22, P-33, P-51, P-52; Criterion 5 made mechanical and Criterion 8 scoped per `review-1.md` **N-3**. Exercised by Property P6.

---

### Requirement 11: The shift-left gate ladder

**User Story:** As the contributor implementing this, I want the exact commands that can fail before review, each honestly marked as observed or not, so that I learn about a defect from a gate rather than from a maintainer.

#### Acceptance Criteria

1. THE specification SHALL define a gate ladder in which every command is marked either **observed**, with its exit status, or **to execute (not observed)**. THE ladder SHALL NOT claim every command has been run.
2. THE ladder SHALL include `build_agent_plugin.py --check`, `vendor_agent_plugins_schemas.py --check`, `sync_skills.py --check`, a skill-limits check, `validate_markdown_links.py`, the targeted agent-plugin pytest selection with `-p no:cacheprovider` as observed, and `CAO_AGENT_PLUGINS_ENABLED=1 cao plugin validate` against each package.
3. THE ladder SHALL NOT include `harness-oss/tools/validate-plugin.py`.
4. THE ladder SHALL NOT include `run_trigger_evals.py --all`.
5. EVERY gate invoking `cao plugin validate` SHALL set `CAO_AGENT_PLUGINS_ENABLED` inline in the command, and the specification SHALL state that an unset gate makes that command exit 1 without evaluating conformance.
6. THE specification SHALL include a negative control demonstrating that a FATAL finding produces a non-zero exit, and a gate-off control demonstrating the refusal. THE gate-off control SHALL be described as **demonstrating** the gate-off behaviour, NOT as catching a defect, because it exits 1 whether or not anything is wrong.
7. WHERE a gate's result is cited as evidence, THE citation SHALL state the command and its exit status.
8. THE specification SHALL NOT claim that a gate proves more than its observed behaviour supports.
9. THE skill-limits gate **SHALL be the `Audit_Skill`'s bundled validator** (**OQ-4**, decided), which is repo-tracked and runs on a fresh clone. THE installed `validate_skills.py` **bootstrap framing is retired**: it was a placeholder for exactly this, and the repo-reproducible limits measurement folds into the bundled validator rather than standing as a separate gate. THE specification SHALL NOT list a gate whose script is absent from the repository.
10. WHERE a gate was not run locally but is enforced in continuous integration, THE specification SHALL say so and cite the workflow. THE Docusaurus build is such a gate: `gh-pages.yml` builds the site on `pull_request` for `docusaurus/**`.

**Traceability:** P-28, P-29, P-30a, P-30b, P-31, P-32, P-33, P-34, P-35, P-8, P-6, P-62; Criterion 1 amended per `review-1.md` **F-4**, Criterion 6 corrected per **N-4**, Criterion 9 added per **F-3**, Criterion 10 added per **F-4**. Exercised by Property P5, P6.

---

### Requirement 12: `Blog_Post` mechanical conformance — **DEFERRED, NOT DELETED**

> **The `Blog_Post` is out of this pull request** (**OQ-8**, **OQ-9**, **OQ-10**, **OQ-14** deferred). This requirement is retained rather than deleted so the work already done on it is not lost, and a separate post plan follows from the AG-UI work. `tasks.md` has **no blog task**, and no acceptance criterion here is on the critical path.
>
> **Two parts remain in force now.** Criterion 14 governs **quotations generally**, not the blog, and is load-bearing for the D4 argument's protected quote. Criterion 12's prohibition on foreign-agent attribution restates the binding attribution directive, which applies to every document regardless of this deferral.

**User Story:** As the person publishing the post, I want its build-breaking properties specified as checks, so that the Docs site job does not fail on a front-matter detail.

#### Acceptance Criteria

1. THE `Blog_Post` SHALL live at `docusaurus/blog/YYYY-MM-DD-<kebab-slug>/index.md`.
2. THE `Blog_Post` front matter SHALL contain `title`, `authors`, `tags` and `description`, in that order, and SHALL NOT contain a key outside that set together with the optional `slug` that one existing post carries.
3. THE `Blog_Post` SHALL contain a truncate marker in the **MDX** form `{/* truncate */}`. It SHALL NOT use the HTML comment form, which MDX renders as visible text.
4. THE truncate marker SHALL be present, because the site configuration throws on an untruncated blog post.
5. THE `Blog_Post` `authors` SHALL name `plauzy`, and an entry keyed `plauzy` SHALL be appended to `docusaurus/blog/authors.yml` after the final existing entry, with `name` and `socials.github` set and **no `title` key at all**, matching the practice of the existing entries.
6. THE `Blog_Post` `tags` SHALL be `[deep-dive, case-study]`, both already present in `docusaurus/blog/tags.yml`.
7. WHERE a new diagram is added, IT SHALL be SVG.
8. WHEN `npm run build` is run in `docusaurus/`, IT SHALL succeed. **This has not been observed locally**; it is enforced in CI by `gh-pages.yml` on `pull_request`.
9. THE `Blog_Post` SHALL describe PR **#1121 as merged** — it is, as `00611d11`, and it is this feature's base. **An earlier revision forbade calling it merged**, which was correct while it was open and inverted the moment it landed; this was the last surviving open-#1121 claim in the document.
10. THE `Blog_Post` content structure SHALL be governed by `blog-outline.md`; this requirement SHALL NOT duplicate it.
11. **EVERY** count the `Blog_Post` states SHALL be one of exactly **four** kinds, and the charter requires the first two: (a) a **dated trailer recount with its denominator**, (b) a **reconciled count stated with the query that produced it and its `total_count`**, such as the merged-pull-request count, (c) a **composition count cited to its source** — for example a package's skill count cited to its `PackageConfig` allowlist, or a seat count cited **by role** — and (d) a **gate's verbatim output**. **No count outside these four is permitted.** THE purpose is to exclude a count used as a measure of effort or scale; the list is made **exhaustive** rather than adding "not a measure of effort or scale" as a separate standalone test, because that phrase is a judgement call and an earlier revision's version of it let `tasks.md`'s aligned criterion be **stricter than this one while declaring that it must not be** (`review-3.md` F-6 residual). An exhaustive list removes the judgement and makes the two criteria textually identical. **Kind (c) SHALL NOT be satisfied by a count of seats by provider, nor cited to CAO's `terminals` rows.** That form was briefly listed as the example for (c) and is **withdrawn**, because the evidence behind it supports a claim the attribution directive forbids; a composition count must be citable without naming any agent. THE permissions here SHALL NOT be justified by what `blog-outline.md` currently contains, because that document is being rewritten; Criterion 10 defers content structure to it, which is a different matter from deriving a permission from its present text.
12. THE `Blog_Post`, and every document produced by this feature, SHALL NOT state that any coding agent other than Kiro wrote, reviewed, or co-developed anything in this repository. THIS SHALL extend to foreign commit trailers, session lines, and any description of a fleet seat by its provider. THE permitted story is **kiro-cli authoring and CAO orchestration only**. (Binding attribution directive, which overrides the charter's blog goal.) **[review-checked]** — no gate can detect an attribution claim; only a reader can.
13. THE `Blog_Post` SHALL NOT substitute an **"exclusively kiro-cli"** claim for the claim Criterion 12 forbids. WHERE authorship is described, IT SHALL state what the Kiro trailers show: **16 of 19** of plauzy's commits on `main` at **`00611d119ce6e8c8a940929f4f59c58e34a8ea2e`** (2026-10-30), date-stamped, with the recount command given so a reader can reproduce it rather than take the figure on trust. **Measured:** `git log 00611d11 --format='%an%x09%(trailers:key=Co-authored-by,valueonly)'`, exit 0 → **19** lines for plauzy, of which **16** carry the Kiro trailer; 16 case-insensitive "kiro" lines overall, so every Kiro-trailer commit on `main` is still his. **THE figure SHALL NOT be hardcoded from an earlier revision. It has moved twice during this feature:** 15 of 18 at `9d029f9d`, and **16 of 19** at `00611d11` — because `00611d11` (#1121) is plauzy's and **carries the trailer**, while the two other new commits are not his. An earlier revision of this criterion did more than state 15 of 18: it **asserted a reconciliation** — that the figure was identical across two shas because the only commit between them was not the author's — and the very next base commit **falsified it**, because `00611d11` *is* the author's and carries the trailer. **So this criterion demonstrated its own necessity rather than merely asserting it:** the clause below forbids inheriting a figure on trust "including a figure inherited from this document", and the document then inherited one and reasoned from it to a conclusion a commit undid. That is the worked example, and it is worth more than the rule stated without it. AND the requirement that the post re-run the command **at the sha it publishes against** SHALL stand regardless, because a figure inherited on trust is precisely what this criterion exists to prevent — including a figure inherited from this document. **AND the figure measures what it claims only if the trailer is applied where a Kiro session actually co-authored the commit.** Directive item 1 requires every commit to carry exactly two trailers, `plauzy` and `Kiro Agent`; if that trailer were applied as a matter of policy rather than of fact, "16 of 19" would stop being a measurement and become a restatement of the policy, and a reader could not tell the difference from the number alone. THE post SHALL therefore present the figure as *what the trailers record*, not as independent evidence of how each commit was produced.
14. WHERE a quotation of a third-party artifact enumerates client or product names, THAT quotation SHALL be reproduced **verbatim** and SHALL NOT be edited to satisfy Criterion 12. A naming rule applied to a quotation corrupts the evidence it was quoted for.

**Traceability:** P-42, P-43, P-44, P-45, P-46, P-47, P-48, P-62, P-21; Criterion 2 corrected per `review-1.md` **N-7**, Criterion 5 per **N-15**, Criterion 8 per **F-4**, Criterion 11 per **F-6**, which found 12.10 and 12.11 in direct contradiction. **OQ-8**, **OQ-9**, **OQ-11** bear on Criteria 5, 6 and 3.

---

### Requirement 13: Scope boundary

**User Story:** As a reviewer, I want the adjacent problems this change deliberately leaves alone to be named, so that I do not read an untouched defect as an oversight.

#### Acceptance Criteria

1. THE change SHALL NOT fix the `run_trigger_evals.py` crash on list-shaped eval files. **[review-checked]** — that script is **not in this repository**, so no gate here can detect a modification to it and this exclusion cannot mechanically fail.
2. THE change SHALL NOT remove the unused `httpx` PEP 723 dependency from `run_trigger_evals.py`. **[review-checked]**, for the same reason as Criterion 1.
3. THE change SHALL NOT generalise `harness-oss/tools/validate-plugin.py` to accept a path argument.
4. THE change SHALL NOT implement #573's **M4** rename.
5. THE change SHALL NOT resolve #573's **M1** disagreement.
6. THE change SHALL NOT alter the two existing packages' skill allowlists other than as required by the placement chosen under **OQ-7**.
7. THE change SHALL NOT modify `research.md`, `blog-outline.md` or `review-1.md`, which are owned by other seats.
8. THE specification phase SHALL NOT commit or push. THE implementation phase **SHALL commit** (**OQ-16**, decided): commits are **signed** (`-S`), **authored** by `plauzy` — which is the `%an`/`%ae` author field, **not** a trailer — and carry **exactly two `Co-authored-by:` trailers**. **Testable as:** **every** commit in `upstream/main..HEAD`, not only the last, is authored as `%an` = `plauzy` with `%ae` = `4451274+plauzy@users.noreply.github.com`; has exactly two `Co-authored-by` values, **one** of them `Kiro Agent <244629292+kiro-agent@users.noreply.github.com>` and **the other the commit's own author** (`%an <%ae>`), so a duplicated Agent trailer fails; has **exactly two trailer lines under any key** (`%(trailers:unfold,only)`), so a third trailer under another key, such as a session link that tooling may append, also fails (review 8 **R8-N-1**); attribution in the commit **body** stays **[review-checked]**; and has a `%G?` of **`G`**. **`G` is the measured value, not an assumption** (`P-71`): a signed commit made on this machine with the user's normal configuration (SSH signing, an allowed-signers file) reports `G`. Upstream's squash-merged commits report `E`, which is why an earlier round saw `E`. An empty range fails, so the check cannot pass before anything is committed. Checking `git log -1` alone, as an earlier revision did, lets every commit but the last go unchecked (review 6, **N-12**). The runnable form is the block at the end of this criterion.

   **AND mandating the trailer does not make it a measurement:** if the `Kiro Agent` trailer is applied as a matter of policy rather than because a Kiro session co-authored that commit, then any count derived from it — including Requirement 12.13's **16 of 19** — restates the policy rather than measuring anything, and a reader cannot tell the two apart from the number. THE trailer SHALL be applied where it is true. THE implementation phase **SHALL NOT push**. IT SHALL stop at "ready to push"; the push and the draft pull request are the supervisor's, after the user's approval. **No automated step may push**, and no criterion in `tasks.md` may require one.

```bash
bash -euo pipefail <<'EOF'
TOP=$(git rev-parse --show-toplevel) || exit 2; cd "$TOP"
AGENT='Kiro Agent <244629292+kiro-agent@users.noreply.github.com>'
COMMITS=$(git rev-list upstream/main..HEAD)
test -n "$COMMITS" || { echo "no commits in upstream/main..HEAD" >&2; exit 1; }
for c in $COMMITS; do
  test "$(git log -1 --format='%G?' "$c")" = G || { echo "$c: signature is not G" >&2; exit 1; }
  test "$(git log -1 --format='%an' "$c")" = plauzy || { echo "$c: author name is not plauzy" >&2; exit 1; }
  test "$(git log -1 --format='%ae' "$c")" = 4451274+plauzy@users.noreply.github.com || { echo "$c: author email is not plauzy's" >&2; exit 1; }
  SELF=$(git log -1 --format='%an <%ae>' "$c")
  T=$(git log -1 --format='%(trailers:key=Co-authored-by,valueonly)' "$c" | sed '/^$/d')
  test "$(printf '%s\n' "$T" | wc -l | tr -d ' ')" = 2 || { echo "$c: not exactly two Co-authored-by values" >&2; exit 1; }
  test "$(printf '%s\n' "$T" | grep -cxF "$AGENT")" = 1 || { echo "$c: Kiro Agent trailer missing or duplicated" >&2; exit 1; }
  test "$(printf '%s\n' "$T" | grep -cxF "$SELF")" = 1 || { echo "$c: the other trailer is not the author" >&2; exit 1; }
  # Every trailer, under any key: exactly two lines, and no key but Co-authored-by.
  ALL=$(git log -1 --format='%(trailers:unfold,only)' "$c" | sed '/^$/d')
  test "$(printf '%s\n' "$ALL" | wc -l | tr -d ' ')" = 2 || { echo "$c: not exactly two trailer lines in total" >&2; exit 1; }
  if grep -qv '^Co-authored-by: ' <<<"$ALL"; then echo "$c: a trailer key other than Co-authored-by" >&2; exit 1; fi
done
echo "all commits in upstream/main..HEAD signed (G) by plauzy with exactly the two trailers"
EOF
```

9. THE change SHALL NOT correct the pre-existing `docs/agent-plugins.md` severity-table defect recorded at the end of `research.md`. THE second defect recorded there, the stale #448 comments, was fixed by #1121 (`P-52`).

**Traceability:** P-35, P-37, P-8, P-40, P-51; Criterion 8 re-scoped per `review-1.md` **F-2**, whose root-cause finding is escalated as **OQ-16** rather than decided here.

---

### Requirement 14: Behaviour if decision D4 stands

**User Story:** As a maintainer who recorded D4, I want this specification to state what happens if I decline to reverse it, so that declining is a supported outcome rather than an argument.

#### Acceptance Criteria

1. IF **OQ-1b** resolves that **D4** stands unchanged, THEN THE pull request SHALL be **withdrawn unmerged**, and the outcome SHALL be recorded as **"not merged by maintainer decision"** rather than as a failure. **[review-checked]** — **an earlier wording said "not implemented", which became unreachable** the moment **OQ-1a** authorised implementation and signed commits ahead of any maintainer answer. A requirement whose stated outcome cannot occur is not a safeguard; the reachable outcome is non-merge, and that is what this criterion now names.
2. IF **OQ-1b** resolves that **D4** is narrowed to authoring only, THEN: Requirement 1 SHALL be dropped; every reference to `Creator_Skill` in Requirements 3, 5, 6 and 7 SHALL be void; Requirement 8, Criterion 4 SHALL require only the `Audit_Skill`; the `SHIPPED_SKILLS` count in `tasks.md` T4 SHALL become **15** rather than 16 with a single presence assertion; and `tasks.md` T5's package-content check and Requirement 8, Criterion 8 SHALL name one skill. Requirements 2 through 13 SHALL otherwise remain in force. **[review-checked]**
3. THE specification SHALL NOT present D4 as an oversight, an omission, or a decision made without the information now available. **[review-checked]**
4. THE pull request SHALL NOT be **merged** while **OQ-1b** is open. **[review-checked]** — implementation proceeding is exactly what **OQ-1a** decided, so forbidding it here would contradict the decision this document records. What must not precede the maintainer is the **merge**.
5. WHERE this specification argues for revisiting D4, IT SHALL rest on **exactly two** grounds: the absence of a greenfield trigger in the skill D4 names, and the absence of mechanical enforcement of three Agent Skills limits by anything CAO ships. THE not-shipped observation SHALL appear as context under ground 1 and SHALL NOT be presented as a third ground, because it restates D4's premise rather than refuting it. **[review-checked]**
6. THE specification SHALL record that ground 2 stands regardless of how **OQ-1b** resolves the authoring question, and SHALL state ground 2 in its narrowed form: **mechanical** enforcement of the 1024-character limit, the 500-line limit and the name regex, not auditing in general, because the D4-named skill's checklist covers naming manually. **[review-checked]**

**Traceability:** P-1, P-2, P-3, P-4, P-5, P-7, P-58; Criterion 2 expanded per `review-1.md` **N-9**, Criterion 5 restored to two grounds per **N-2**, Criterion 6 narrowed per **F-1**.

---

### Requirement 15: The `Creator_Skill` is not dogfooded on an external package (OQ-17 dropped)

**User Story:** As the reader of this specification, I want to know whether the creator skill was dogfooded, so that "dogfooded" names an event that happened rather than an intention.

**OQ-17 is dropped, by the user, 2026-11-02** (`decisions-oq17-n11.md` (the user's decision record, kept outside the repository; its "Update 2026-11-02" section)). The decision record says: "Dogfooding the creator skill on harness-oss is **dropped**, not deferred. In the user's words: working on a package other than CAO spreads the effort too thin, and the blog should present the cumulative CAO work (MCP Apps, AG-UI, Agent Plugins). Taking up work in an empty external package defeats that purpose." It continues: "No harness-oss work is planned. #1200 must not promise it. The creator skill is simply not dogfooded on an external package. The audit skill is still exercised on CAO's own packages."

**What this replaces.** Earlier revisions sized a dogfood of the `Creator_Skill` on an external package and, on 2026-11-01, deferred it out of this pull request. Those revisions carried a table of facts measured in that package, a sizing, a floor, and nine criteria governing the work. **All of that is retired**, because there is no such work. The criteria are not renumbered into new obligations; the three below replace them.

#### Acceptance Criteria

1. THE `Creator_Skill` SHALL NOT be described as dogfooded on an external package, and THE specification and the pull request SHALL NOT promise, schedule or imply external dogfooding work. **No external work is planned** (OQ-17, dropped, the user, 2026-11-02).
2. THE charter deviation SHALL stand, and SHALL NOT be marked resolved: the creator half is not dogfooded, by decision, and the audit half is exercised on CAO's own packages (Requirement 8.10).
3. THE specification SHALL NOT substitute another target to claim the creator half dogfooded unless that substitution is recorded as a new decision with its authority.

**Traceability:** OQ-17, dropped on 2026-11-02 (`decisions-oq17-n11.md` (the user's decision record, kept outside the repository; its "Update 2026-11-02" section)); the charter's second goal; `review-1.md` **F-5**, whose rejection of "guided by" is why this requirement insists a dogfood be real or be declared absent.

---

### Requirement 16: Vocabulary constraints carried from the AG-UI reconciliation

**User Story:** As a reader of anything this feature produces, I want its vocabulary to match what the code actually does, so that a word does not promise a capability the repository does not have.

**The AG-UI reconciliation report contains NO Agent Plugins items.** Nothing in it touches package shape, the manifest, the vendored schemas, `PIN.json`, `cao plugin validate`, the feature gate, or skills packaging, so **no requirement of this feature changes**. What it carries is vocabulary, and these are forward constraints. Our documents have **one** occurrence of a forbidden form today: the verbatim #436 title at `research.md:241`, which Criterion 3 carves out. The point is to keep it at that.

#### Acceptance Criteria

1. WHERE the six-component allow-list is named, THE documents SHALL say **"controlled generative UI"**, never bare "generative UI". THE terms "A2UI" and "declarative" SHALL be reserved for A2UI itself.
2. THE documents SHALL NOT claim that allow-listed output "renders in any AG-UI-compatible client", nor that it renders in every client built on a third-party UI framework. THE allow-list is a **gate**, and A2UI is only a candidate target. **THE supporting claim that `GENERATIVE_UI` is not an AG-UI 1.0 event type is carried as `P-65` and is NOT yet cited to a primary source** — it reached this document through a devops review, which is advice rather than specification. Criterion 2 stands on the gate-and-candidate-target reasoning alone; the event-type claim SHALL NOT be stated as fact until `P-65` cites the AG-UI specification.
3. WHERE a quoted third-party artifact uses vocabulary Criteria 1 or 2 forbid, THAT quotation SHALL be reproduced **verbatim** under Requirement 12.14 and SHALL NOT be edited to comply. **One such carve-out exists, and it is named rather than asserted away:** `research.md:241` reproduces PR **#436's title**, "feat(agui): AG-UI protocol adapter + **generative UI** (L1, default-off)", which is a bare "generative UI" in a verbatim commit title. **An earlier revision named a second carve-out, a `research.md` quotation of an installed skill's description, and it does not exist:** a search of `research.md` for every form Criteria 1, 2 and 4 forbid finds only the #436 title. "renders in any" appears only in our own prohibitions. `research.md:288` uses "declarative" in a different sense, for a lint finding about a skill description; it is neither a quotation nor a description of A2UI, and `research.md` is outside the pull request.
4. THE `Creator_Skill`, the `Audit_Skill` and the learn step SHALL NOT use a third-party UI framework's product language: **"self-improving"**, **"AI coworkers"**, **"one checkpoint"**, **"governed"**. THIS is a live risk rather than a theoretical one, because that product's "Automatic Learning … proposes each one as a skill. Every skill is a SKILL.md" describes adjacent behaviour to the `Creator_Skill`, and borrowed phrasing would imply a shared design this feature does not have.
5. THE AG-UI fixes themselves are **out of scope** and SHALL NOT be specified here.

**Traceability:** the AG-UI reconciliation report, folded in vocabulary-only; Requirement 12.14 governs Criterion 3.

## Decisions, and what remains open

**The user has decided to drive #1153 without waiting for maintainer answers.** The decisions below are
settled and this document reads as a plan rather than a questionnaire. Each original question keeps its
`OQ-n` id so the traceability matrix still resolves and a maintainer can see **what was decided and on
whose authority**. Authority for every row marked *decided* is the user, relayed through the supervisor
on 2026-10-30; none of them is a maintainer answer, and none should be presented as one.

### Decided

| Id | Decision | Authority |
|---|---|---|
| **OQ-1a** | **Proceed as if D4 is reversed** — build it, commit it, present it. **This is the user's question and the only half that is decided.** The pull-request body states up front that this revisits #573's D4 and why; [`## Relationship to #573 decision D4`](#relationship-to-573-decision-d4) is the source text for that paragraph. **OQ-1b, the maintainer's question, stays open and gates the merge.** | User, 2026-10-30 |
| **OQ-2** | Not a blocker. Ground 1 holds on the skill's own triggers regardless of the unread specification prose; the residual limit stays recorded in the D4 section. | Author, unchanged |
| **OQ-3** | Keep the current name **`cao-plugin`**. M4 remains open upstream; this specification does not assume its rename. | User |
| **OQ-4** | The `Audit_Skill` ships a **bundled validator pinned to the `PIN.json` schema hashes**, and it **also** runs `cao plugin validate --json` when `CAO_AGENT_PLUGINS_ENABLED` is set. This is option C. It **retires the bootstrap-gate framing**: the bundled validator is the real gate, and the repo-reproducible limits measurement folds into it. | User |
| **OQ-5** | **Object-shaped** `evals.json`. | User, adopting the recommendation |
| **OQ-6** | **Both skills join `SHIPPED_SKILLS`**, so the full obligation ladder applies: repo-root `skills/`, `scripts/sync_skills.py`, the regenerated mirror, and `test_skill_packaging_parity.py`. | User |
| **OQ-7** | A **third `PackageConfig`**. The `cao-contributor` route's cost is gone — #1121 is **merged** — but the decision stands on the narrative ground: the package is the deliverable. | User |
| **OQ-12** | The package is named **`cao-agent-plugin-lifecycle`**. It matches this specification's directory and issue #1153, so package, spec and issue share one string — the traceability argument that recommended it. `CODEBASE.md`'s `agent-plugin/` row must name it **exactly**. | User, adopting the recommendation |
| **OQ-13** | A **`references/` directory for the `Audit_Skill`**; optional for the `Creator_Skill`. | User, adopting the recommendation |
| **OQ-16** | **The implementation commits.** Signed (`-S`), author `plauzy`, **exactly two trailers** (`plauzy`, `Kiro Agent`). **It never pushes.** Work stops at "ready to push"; the push and the draft pull request are the supervisor's after the user's approval. Requirements 7.8 and 13.8 are rewritten accordingly — the bar is no longer "no commit", it is **commit yes, push no**. | User |
| **OQ-17** | **Dropped.** The `Creator_Skill` is not dogfooded on an external package, and no external work is planned (Requirement 15). This replaces the 2026-10-30 decision to dogfood it and the 2026-11-01 decision to defer it. | User, 2026-11-02 |

### Deferred

| Id | Status |
|---|---|
| **OQ-8**, **OQ-9**, **OQ-10**, **OQ-14** | **Deferred — the `Blog_Post` is out of this pull request.** Requirement 12 is marked deferred with its reason rather than deleted, so the work already done on it survives; a separate post plan follows from the AG-UI work. `blog-outline.md` stays on disk, untracked. **Requirement 12.14 remains in force**, because it governs quotations generally rather than the blog specifically. |
| **OQ-11** | Informational, unchanged: `research.md` gives the truncate marker as `<!-- truncate -->`; the six existing posts use `{/* truncate */}`. Carried for whoever writes the post. |

### Still genuinely open

| Id | Question | Status |
|---|---|---|
| **OQ-1b** | **Does the maintainer reverse, narrow, or uphold #573 decision D4?** This is the question D4's author must answer, and **nothing in this document settles it.** **OQ-1a** decided only that the work proceeds and is presented. Requirement 14 states the outcome under each resolution; 14.1's is **withdrawal unmerged**. | **OPEN — gates the merge** |
| **OQ-17 scope** | **Was:** is the dogfood accepted at full scope, at the M2+M4 floor, or as its own feature? **Now dropped by the user, 2026-11-02** (`decisions-oq17-n11.md` (the user's decision record, kept outside the repository; its "Update 2026-11-02" section)): no external dogfood, and no external work planned. The charter deviation stands (Requirement 15). Kept in this table so the earlier open state stays legible. | **Dropped** |
| **OQ-15** | Issue of record. **Resolved:** #1153, sub-issue of tracker #1154, sibling #1152. | Resolved |
| **CI dogfood coverage** (no `OQ` id yet) | **Should `run.sh` be extended so the CI dogfood job covers the third package?** Today it reaches only `agent-plugin/cao` (`P-67`), so Requirement 8.12 limits the claim to "gated in CI by `make check-agent-plugin`". Extending it would be new scope, and `record-dogfood-demo.mjs` has not been read. Recorded, not added. | **Open — needs a user decision** |
| **M1 / M2 / M4** (upstream, not ours) | #573's own open maintainer decisions. Requirement 10.6 declines to settle M1; Requirement 3.5 declines to assume M4; **OQ-12**'s chosen name sits in the namespace M2 may yet settle, which is recorded as a known risk rather than resolved. | Open upstream |

Requirement 14 is the one place where **OQ-1b can still change the outcome**: proceeding as if D4 is reversed is a decision to *propose*, not a finding that the maintainer agreed.

`research.md:307-317` raises eight open questions. Mapping: its 1 → **OQ-1a** and **OQ-1b**, **OQ-2**; its 2 → **OQ-6**, **OQ-7**; its 3 → **OQ-3**; its 4 → **OQ-4**; its 5 → **OQ-5**; its 6 → **OQ-8** (byline resolved as `plauzy`, now deferred with the post); its 7 → Requirement 10.6 declines to settle M1; its 8 → **OQ-10**, deferred.

## Traceability Matrix

Correctness properties are defined in [`design.md`](design.md).

| Correctness Property | Validated by |
|---|---|
| P1 — Vocabulary disjointness from the event-plugin and migration skills | Requirement 3 |
| P2 — Audit rule superset over CAO's loader checks | Requirements 1, 2 |
| P3 — Mirror parity between `skills/` and the packaged copy | Requirement 7 |
| P4 — Package byte-identity with a fresh build | Requirements 8, 9 |
| P5 — Gate-precondition soundness, no silently vacuous gate | Requirements 1, 4, 8, 11 |
| P6 — Claim honesty, no assertion exceeding its evidence | Requirements 2, 3, 5, 10, 11, 12 |
| P7 — Net-new safety, no dual-folder install | Requirement 6 |
| P8 — Reversibility under decision D4 | Requirement 14 |

| Deliverable | Specified by |
|---|---|
| `Creator_Skill` | Requirements 1, 3, 5, 6, 7 |
| `Audit_Skill` | Requirements 2, 3, 4, 5, 6, 7 |
| `Dogfood_Package` | Requirements 8, 9, 10 |
| ~~`Blog_Post`~~ — deferred | Requirement 12, retained deferred-with-reason |
| Cross-cutting gates and boundaries | Requirements 11, 13, 14 |
| ~~`Creator_Skill` dogfood on an external package~~ — dropped | Requirement 15 records the decision |
| Vocabulary carried from the AG-UI reconciliation | Requirement 16 |

| Open question | Referenced by (not resolved by) |
|---|---|
| OQ-1a (decided), OQ-1b (open), OQ-2 — decision D4 | Requirements 1, 2, 14 |
| OQ-3 — M4 rename | Requirement 3 |
| OQ-4 — validator selection | Requirements 4, 11 |
| OQ-5 — evals shape and runner fix | Requirements 5, 13 |
| OQ-6 — wheel shipping, **decided** | Requirement 7 |
| OQ-7, OQ-12 — package placement and name, **both decided** | Requirements 8, 10 |
| OQ-8, OQ-9, OQ-10, OQ-11, OQ-14 — blog details | Requirement 12 |
| OQ-13 — `references/` layout | Requirements 1, 2, 9 |
| OQ-16 — commit scope | Requirement 13 |
| OQ-17 — dogfooding the creator skill, **dropped (the user, 2026-11-02)** | Requirements 1, 8, 15; the charter-deviation section |
