# Design: First-class feature requests in the CAO project tracker

**Status:** Draft for maintainer review

**Scope:** Specification and reviewed migration inventory only; this branch does
not mutate the live tracker or retire source documents.

**Target repositories:** `cli-agent-orchestrator` (tracker, API, CLI, web UI)
and `cao-conductor` (roadmap-source retirement).

**Authoritative source heads inspected:**

- `cli-agent-orchestrator origin/main`: `f0f18ce4098ef1fc3ae1ab0999dd2d83006011cc`
- `cao-conductor origin/main`: `fa50cbfd38e017921c2d40e519cf94a1b4a0a247`
- Live tracker snapshot: project `cao-system`, 341 records, next key 342, captured 2026-08-08.

---

## Summary

Extend the existing project-scoped issue tracker with a first-class **feature
request** record kind. Issues and feature requests share the existing
`tracker_issues` table, project, monotonic key allocator, comments, links,
audit events, labels, scope resolution, and access controls. One additive
`kind` discriminator (`issue` or `feature`) separates the two lists. Existing
rows are backfilled as `issue`; no existing key, timestamp, status, severity,
comment, event, or link changes.

The CLI gains a top-level `cao feature` group. The Projects UI gains Issues,
Feature requests, and All tabs with feature-specific copy and complete CRUD,
filtering, history, comments, links, pagination, deep linking, and project
counts. Existing `cao issue` and `/tracker/issues` behavior remains issue-only
by default so a newly imported roadmap does not suddenly appear as defects to
old automation.

After the capability is deployed, migrate the CAO roadmap from
`FUTURE_IMPROVEMENTS.md` through a reviewed, digest-bound, idempotent manifest.
The inventory accompanying this design preserves 22 entries from authoritative
`cao-conductor origin/main`, five additional entries from the deliberately
untouched dirty root copy, and the dispositions of two historical title
variants found across worktrees. Every source candidate must have an explicit
create/map/terminal/skip disposition; title similarity never files or merges a
record automatically. Only after an exact tracker export and migration receipt
prove complete coverage may the Markdown intake be retired.

## Goals

1. Keep one project tracker and one key namespace while making defects and
   requested capabilities visibly distinct.
2. Give feature requests first-class CLI, REST, and web UI workflows rather
   than encoding them as labels that every caller must interpret independently.
3. Preserve all current issue behavior and all 341 existing records.
4. Make the roadmap migration reviewable, atomic, idempotent, and lossless at
   the source-entry/disposition level.
5. Prevent `FUTURE_IMPROVEMENTS.md` or historical worktree copies from becoming
   a second intake path after cutover.
6. Preserve cross-kind relationships: a feature can relate to or be blocked by
   an issue without pretending the two records are duplicates.

## Non-goals

- A public voting, reactions, customer-account, or entitlement system.
- A separate roadmap database or a second project/key allocator.
- GitHub Issues synchronization.
- Automatic implementation scheduling when a feature is accepted.
- Automatic semantic deduplication by title or embedding.
- Scraping every historical worktree into the live tracker.
- Reclassifying an existing record between issue and feature in v1. `kind` is
  immutable after creation; an audited reclassification command can be designed
  later if real mistakes justify it.
- Deleting or rewriting the current dirty `cao-conductor` root as part of this
  design-only branch.

---

## Current state

### Storage and services

`TrackerIssueModel` in `clients/database.py` already carries the fields needed
by both kinds: project/key identity, title/body, workflow status, P0-P4 value,
component, reporter/assignee, labels, evidence, resolution, source provenance,
duplicate pointer, origin, and timestamps. Comments, append-only events, and
directed links use the stable key and therefore require no new tables.

`services/issue_tracker.py` owns validation, scope resolution, compare-and-swap
key allocation, filtering, audit events, statistics, and Markdown export.
`ensure_tracker_schema()` is a direct-CLI schema path separate from server
`init_db()`; an additive column migration must run in both paths or an upgraded
`cao feature` command will fail against an old database before the server has
started.

The live `cao-system` project deliberately spans both repositories, their
worktree roots, and campaign aliases. Feature requests belong to that same
project rather than whichever repository contains the implementation.

### Existing API and UI

The REST surface is under `/tracker`, with strict request bodies. The dashboard
fetches vocabulary from `/tracker/vocabulary` and renders the issue tracker in
`web/src/components/ProjectsPanel.tsx`. It already provides project/scopes,
search, status and severity filters, create/edit/delete, comments, links,
history, pagination, and Markdown export. The feature UI should extend this
surface, not build a parallel page with divergent behavior.

### Roadmap sources

The committed `cao-conductor/FUTURE_IMPROVEMENTS.md` at the inspected
`origin/main` contains 22 top-level requests. The dirty root copy contains 27:
the committed set plus five additions (`Generation-bound harness session
operations`, `conduct deploy`, `Skill-harvest loop`, `Route validation via Prime
Lab`, and `multi-agent-pr-review depth-2 branch`). Its source text currently
contains an obsolete reference to `OPEN_ISSUES.md`; that wording is provenance,
not current authority.

A read-only scan found 307 copies under CAO worktrees, seven distinct file
hashes, and 29 normalized title strings. Worktree copies are historical evidence,
not 307 independent roadmap sources. Two extra strings require explicit lineage
handling:

- `Memory-candidate adjudication pipeline` is an older title/body version of the
  promoted canonical pre-chess entry.
- `Campaign timeline view` appears in one historical worktree and was later
  folded into the canonical durable supervisor-poke entry as an optional
  timeline surface.

The complete candidate payload and provenance are in
`future-improvements-migration-inventory.json` beside this design.

---

## D1 — One table, one key namespace, one immutable kind

Add one column to `tracker_issues`:

```python
kind = Column(String, nullable=False, default="issue", server_default="issue", index=True)
```

Allowed values:

```python
ITEM_KINDS = ("issue", "feature")
```

Fresh databases receive the column through SQLAlchemy metadata. Existing SQLite
databases receive an idempotent, fail-closed migration:

```sql
ALTER TABLE tracker_issues
ADD COLUMN kind TEXT NOT NULL DEFAULT 'issue';

CREATE INDEX IF NOT EXISTS ix_tracker_issues_project_kind_status
ON tracker_issues(project_id, kind, status);
```

The migration uses `PRAGMA table_info(tracker_issues)` as its gate. It runs:

1. after tracker table creation in server `init_db()`; and
2. after table creation inside `ensure_tracker_schema()` for direct CLI use.

Unlike best-effort cache migrations, failure is fatal and typed: the upgraded
ORM cannot safely query a table missing a mapped non-null column. A migration
test starts from the exact pre-feature schema with populated comments, links,
and events, upgrades it, and proves every old row is `kind=issue` byte-for-byte
apart from the new defaulted column/index.

Keys remain the project's existing monotonic sequence (`cond-0342`, ...). A
second `fr-` counter would let one external key mean two rows and would split
cross-kind chronology. Deleting either kind never decrements the shared counter.

`kind` is accepted only at creation and is recorded in the creation event. It
is returned by every row/detail/list/export response but is not in
`_EDITABLE_FIELDS`. V1 refuses PATCH attempts to change it. Incorrectly typed
records receive an explicit terminal disposition and a new correctly typed
record linked with `relates`; keys are never silently repointed.

### Why not encode kind only as a label?

A label cannot safely drive route validation, default list isolation, form copy,
counts, or migration invariants. Users can remove it, old callers can exceed the
label cap, and every client would invent its own interpretation. A one-column
discriminator keeps shared storage without making type an unenforced convention.

### Why not a `tracker_feature_requests` table?

It would duplicate allocation, comments, events, links, filtering, permissions,
and project resolution. Cross-kind links and project counts would need unions,
and moving the roadmap into it would create the second tracker the consolidation
is intended to eliminate.

---

## D2 — Reuse the proven workflow and priority columns

Feature requests reuse the existing status vocabulary and terminal semantics:

| Stored status | Issue meaning | Feature-request UI meaning |
|---|---|---|
| `open` | actionable defect | proposed/accepted backlog item |
| `triage` | under investigation | under product/technical evaluation |
| `in-progress` | fix underway | scheduled or being implemented |
| `blocked` | fix cannot advance | accepted request blocked on a dependency |
| `resolved` | fix landed, verification pending | delivered, confirmation/cutover pending |
| `closed` | verified and complete | shipped or archived |
| `wontfix` | consciously declined | declined or withdrawn |
| `duplicate` | canonical issue named | canonical request named |

This deliberate reuse preserves old-binary terminal behavior. Introducing
`planned`, `shipped`, or `declined` into the stored status column would make an
older binary treat terminal feature rows as open. Planning nuance is represented
by ordinary labels such as `accepted`, `planned`, `exploratory`, `watch`, and
`deferred`; they remain queryable and visible but do not redefine finality.

The existing `severity` column stores P0-P4/unset for both kinds. Feature CLI
and UI label it **Priority**, while JSON retains the canonical field name
`severity` to avoid dual sources of truth. `reporter` is displayed as Requester,
`assignee` as Owner, and `resolution` as Outcome. `failing_command` is omitted
from new feature forms and is always null for feature creation/import; the
shared detail renderer may display it only if non-null for forward/backward
compatibility.

`duplicate_of` may point across kinds, but migration/review should prefer
`relates` when a requested capability has associated defects. Duplicate means
same current request and acceptance criteria, not merely overlapping subject
matter.

---

## D3 — Service contract

Evolve the service without forking its invariants:

- `_issue_row()` returns `kind`.
- `create_issue(..., kind="issue")` validates `ITEM_KINDS`; add a thin
  `create_feature()` wrapper that supplies `kind="feature"`, rejects a failing
  command, and retains the same bounds/scope/key allocator/event transaction.
- `list_issues(..., kind="issue")` defaults to issues for compatibility.
  `kind=None` means all kinds and is used only by explicitly generic surfaces.
  `list_features()` is a thin `kind="feature"` wrapper.
- `get_issue(key)` remains key-universal internally because comments and links
  are key-universal. Kind-specific API/CLI wrappers assert the returned kind and
  issue a typed mismatch error.
- Update, delete, comment, and link operations keep their current atomic/audit
  behavior. Update validates that `failing_command` cannot become non-null for a
  feature.
- Search covers title, body, key, and evidence for features; it must not depend
  on a field hidden by their UI.
- `stats(project_id, kind="issue")` remains issue-only by default. Generic stats
  add `by_kind`, with per-kind totals/open/status/severity maps.
- Markdown rendering accepts an explicit kind. There is no mixed export unless
  the caller asks for `kind=None`, in which case every heading includes a Kind
  column/pill.

Project count compatibility is explicit:

```json
{
  "counts": {
    "total": 341,
    "open": 80,
    "by_status": {"open": 80, "closed": 186},
    "by_kind": {
      "issue": {"total": 341, "open": 80},
      "feature": {"total": 27, "open": 23}
    },
    "all_total": 368,
    "all_open": 103
  }
}
```

`total`, `open`, and `by_status` retain their legacy issue-only meaning. New UI
uses `by_kind`/`all_*`; old clients do not silently change counts.

All new validation raises the existing `TrackerError` classes so direct CLI and
HTTP callers receive the same refusal semantics.

---

## D4 — REST API

Keep existing routes stable and add typed feature aliases:

| Method/path | Contract |
|---|---|
| `GET /tracker/issues` | issue-only by default; optional `kind=issue|feature|all` for explicit generic callers |
| `POST /tracker/issues` | always creates `kind=issue`; body cannot smuggle `kind` |
| `GET /tracker/issues/{key}` | remains key-universal for backward-compatible links |
| `GET /tracker/features` | feature-only list; same filters/pagination/order |
| `POST /tracker/features` | creates `kind=feature`; feature-tailored strict body |
| `GET/PATCH/DELETE /tracker/features/{key}` | asserts the row is a feature, then reuses shared service logic |
| `/tracker/features/{key}/comments|links` | thin typed aliases over shared key resources |
| `GET /tracker/features/stats` | feature-only stats |
| `GET /tracker/projects/{id}/features/export` | feature Markdown export |

`IssueCreateBody` remains unchanged. `FeatureCreateBody` accepts title, project,
body, status, `severity`, component, reporter, assignee, labels, evidence,
session/source resolution, optional migration key, and origin; it excludes
`failing_command`. `FeatureUpdateBody` similarly excludes `kind` and
`failing_command`.

Vocabulary becomes additive:

```json
{
  "item_kinds": ["issue", "feature"],
  "statuses": ["open", "triage", "in-progress", "blocked", "resolved", "closed", "wontfix", "duplicate"],
  "statuses_by_kind": {"issue": [...], "feature": [...]},
  "terminal_statuses_by_kind": {
    "issue": ["closed", "duplicate", "wontfix"],
    "feature": ["closed", "duplicate", "wontfix"]
  }
}
```

The old `statuses` and `terminal_statuses` keys remain. Unknown request fields
continue to fail with 422 rather than disappearing.

---

## D5 — CLI

Add a top-level `cao feature` group backed by the shared tracker service:

```text
cao feature file
cao feature list
cao feature show
cao feature edit
cao feature close
cao feature comment
cao feature link
cao feature rm
cao feature stats
cao feature import-future-improvements
```

Feature-facing option names are ergonomic aliases over canonical storage:

- `--priority P0..P4|unset` -> `severity`
- `--requester` -> `reporter`
- `--owner` -> `assignee`
- `--outcome` in edit/close -> `resolution`

JSON always emits canonical field names plus `kind`, so scripts never receive a
different schema merely because they used the friendly command. Human output
uses “feature request”, “priority”, “requester”, and “owner”.

`cao issue` stays issue-only for file/list/stats/export. Key-universal show,
comment, and link may explain the mismatch and suggest `cao feature show` rather
than modifying the wrong kind. Add `--include-features` only to explicit generic
project export; do not make it the default.

CLI command implementations share private render/edit helpers. Copying the
current 600-line issue command file and changing nouns is rejected because fixes
to redaction, body-file handling, audit actors, or pagination would immediately
diverge.

---

## D6 — Projects UI

Extend the existing Projects panel rather than adding a second navigation area.
The project and scope rail remains unchanged.

### Main layout

A segmented control sits above search:

```text
[ Issues 80 ] [ Feature requests 23 ] [ All 103 ]
```

- Default is **Issues**, preserving the current page on first load.
- Feature requests queries `/tracker/features` and switches status/severity copy
  to Status/Priority.
- All explicitly requests both kinds and shows a Kind pill on every row.
- Search, open-only, filters, pagination, loading, empty, and error states are
  independent per tab; changing project or tab resets offset and stale selection.
- Project cards show compact issue and feature counts rather than redefining the
  old open/total numbers.

### Feature list and detail

The feature list keeps key, priority, status, title, component, and date, adding
a feature icon/pill. “Log issue” becomes “Request feature” in that tab. The new
modal collects title, structured body, priority, status, component, requester,
owner, labels, and evidence. Its starter body is editable Markdown:

```markdown
## Problem / opportunity

## Desired outcome

## Acceptance criteria

## Constraints / alternatives
```

The detail drawer reuses comments, links, event history, save/delete, and audit
behavior. It changes labels only:

- Severity -> Priority
- Reporter -> Requester
- Assignee -> Owner
- Resolution -> Outcome

It hides Failing command when null. Closing presents feature-oriented choices
while storing the existing statuses: Shipped (`closed`), Declined/Withdrawn
(`wontfix`), or Duplicate (`duplicate`, canonical key required).

### Full UI support requirements

- URL state supports direct links such as
  `?project=cao-system&kind=feature&key=cond-0342`; refresh/back/forward retains
  the selected project, tab, and row.
- Create/update/delete/comment/link/history operations show the same success and
  typed-error feedback as issues.
- Keyboard focus, labels, confirmation modals, narrow layouts, and empty states
  cover both kinds.
- Vocabulary remains server-driven. No status/priority value exists only in
  TypeScript.
- The dashboard never loads all rows to filter client-side; kind participates in
  the paginated server query.

### Frontend types

Add `kind: 'issue' | 'feature'` to `TrackerIssue` (renaming the exported type is
optional churn and not required in this PR), a `kind` filter, additive project
counts, and feature API methods. Factor the detail/create components around an
explicit kind/presentation descriptor rather than duplicate JSX trees.

---

## D7 — FUTURE_IMPROVEMENTS consolidation

### Source hierarchy

Migration uses a reviewed JSON manifest, not a live recursive worktree scan.
Authority order is:

1. committed `cao-conductor origin/main:FUTURE_IMPROVEMENTS.md` at its exact
   approved SHA;
2. explicitly preserved supplemental entries from the dirty root, accepted by
   the operator before apply;
3. historical Git/worktree variants only as lineage evidence.

This design's inventory records exact SHA-256 digests:

- committed source: `44c54374f71b66706756fbd95707af998e268ed6cf79630ce659a7002f96be96`
- dirty-root supplement snapshot: `2726b33acdc91acdb801b02a7ad83270ac28089e77a31b82fe386740f4de8bb0`

The dirty root is not overwritten or cleaned. If either source changes before
migration, plan generation refuses until the new content is reviewed and the
manifest regenerated.

### Manifest dispositions

Every discovered candidate has exactly one reviewed action:

- `create-feature`: allocate a new shared project key and create a feature row;
- `create-terminal-feature`: create historical provenance already marked
  `closed`, `wontfix`, or `duplicate` with an outcome;
- `map-existing`: create no row; name an existing feature key with the same
  desired outcome/acceptance and attach source provenance idempotently;
- `relate-existing`: create a feature and add explicit links to related defects;
- `skip-invalid`: only for content that is not a request, with a mandatory
  rationale.

No title match selects an action. The proposed dispositions in the inventory
are review prompts, not apply authority. Apply refuses any entry still carrying
`needs-current-source-adjudication`, missing an action, or naming a nonexistent
canonical key.

Known delivered candidates (`Session-env durability`, `Durable supervisor-poke
visibility`, `conduct deploy`, and `conduct policy lint`) are proposed as
closed/shipped history, but exact current-source ancestry must be rechecked at
apply time. A reference to a closed bug does not itself prove the broader
feature shipped.

### Import command

`cao feature import-future-improvements` has two modes:

```text
# Pure planning; no tracker writes or key reservation
cao feature import-future-improvements   --source FUTURE_IMPROVEMENTS.md   --supplement reviewed-supplement.md   --inventory-out plan.json   --project cao-system   --dry-run

# Apply an explicitly adjudicated plan
cao feature import-future-improvements   --manifest approved-plan.json   --project cao-system   --expected-source-sha256 ...   --expected-supplement-sha256 ...   --apply --yes
```

The Markdown parser recognizes only top-level bold bullets, supports bold titles
wrapped across lines, binds P0-P4 from the nearest section, and preserves
multiline body text. Headings, prose, and nested explanatory bullets never
become requests by accident.

Each candidate receives a stable migration ID derived from source digest and
ordinal. Apply stores it in provenance/audit metadata and a bounded migration
label, then performs one SQLite transaction for key allocation, rows, creation
events, comments/links, and the project counter. A retry either returns the
exact existing mapping or refuses conflicting bytes; it never allocates a
second key. Dry-run is read-only and does not advance `next_issue_number`.

Apply writes a receipt containing source/manifest hashes, before/after project
counter, candidate action, resulting key/mapping, row digest, and transaction
ID. The receipt contains no secrets and is written atomically. Backup the live
SQLite DB and export the full project immediately before and after apply.

### Cross-kind curation

Referenced `cond-NNNN` defects are imported as `relates`/`blocks` links only when
review confirms the relationship. A feature is marked duplicate of an issue
only if they have the same current desired outcome and acceptance criteria.
“Capability motivated by a fixed bug” normally remains a distinct feature with
a relation link.

### Retiring Markdown intake

After deployment, successful import, independent audit, and backup:

1. delete tracked `cao-conductor/FUTURE_IMPROVEMENTS.md` from a clean exact-head
   branch;
2. add `/FUTURE_IMPROVEMENTS.md` to `.gitignore` to prevent local fallback
   intake;
3. update active policy, contributor docs, profiles, and skills to use
   `cao feature file/list`;
4. preserve Git history, the approved manifest, tracker exports, and migration
   receipt as rollback evidence;
5. verify no runtime, test, package-data, or documentation path reads or writes
   the retired file.

Historical worktrees may still contain the file at old commits. They are not
mutated and are never scanned by normal tracker operation.

---

## D8 — Deployment and compatibility

Roll out in this order:

1. Land and test the additive tracker/UI implementation in
   `cli-agent-orchestrator`.
2. Deploy the exact merged fork and prove the executed source/schema version.
3. Verify existing issue-only API/CLI/UI behavior against a copy of the live DB.
4. Generate and independently review the final migration manifest against fresh
   source and tracker exports.
5. Apply the manifest and verify idempotent replay.
6. Land the `cao-conductor` intake-retirement PR.
7. Deploy/provision updated conductor guidance and audit all active campaigns.

Before feature rows exist, rollback is the old binary plus an ignored additive
column/index. After feature rows exist, an old binary can still read their shared
status/severity safely but cannot filter by kind and may display them among
issues. Therefore rollback after migration is read-only/emergency: restore the
pre-import DB backup or redeploy the feature-aware binary before permitting
tracker writes. Do not drop the column during rollback.

The feature migration must not share a deploy transaction with schema rollout.
A schema/UI failure can then be rolled back without touching roadmap data, and a
migration failure can roll back one database transaction without replacing the
binary.

---

## Security, integrity, and concurrency

- Existing tracker read/write scopes protect feature routes identically to issue
  routes; there is no weaker “roadmap” permission.
- Strict request bodies reject `kind`, `failing_command`, key, project, or origin
  smuggling where a surface does not authorize them.
- Existing title/body/label limits and label normalization apply.
- Source paths are provenance only and never become project identity.
- Migration accepts only explicit local files, validates regular-file/UTF-8
  input, hashes bytes before parsing, and never follows a worktree glob.
- The shared compare-and-swap counter remains the sole normal allocator.
  Concurrent issue and feature creation must prove unique monotonic keys.
- Migration holds one database write transaction and rechecks the project high
  watermark under that transaction. Concurrent allocation produces a typed
  conflict/replan, not shifted opaque mappings.
- Every create/update/link/comment/terminal disposition remains auditable.
- Deleting a feature follows the existing guarded semantics and never recycles
  its key. Migration-created records should normally be terminally dispositioned,
  not deleted.

---

## Test plan

### Database and service

- Fresh schema includes `kind` and the composite index.
- Upgrade a populated pre-feature SQLite fixture; all old rows become issues and
  every related row survives.
- Direct `cao issue`/`cao feature` on an old DB runs the additive migration even
  when no server lifespan has run.
- Concurrent issue/feature allocation yields unique monotonic keys and the exact
  final counter.
- Kind validation, immutability, default list isolation, generic all-kind list,
  open-only, status/priority/component/owner/label/search filters, order, stats,
  export, and deletion.
- Feature creation rejects failing-command input and over-limit bodies/labels.
- Cross-kind comments, links, duplicate validation, and audit events.
- Legacy issue counts and JSON snapshots remain unchanged when no features exist.

### REST API

- Strict issue and feature create/update bodies, type mismatch errors, status
  codes, scope resolution, pagination totals, vocabulary compatibility, and
  project count additions.
- Existing `/tracker/issues` contract tests run unchanged plus an explicit proof
  that newly created features are absent by default.
- Feature aliases and key-universal legacy detail routes agree on row bytes.

### CLI

- Full `cao feature` command matrix in human and JSON modes.
- Friendly aliases map to canonical fields without duplicate output keys.
- `cao issue list/stats/export` remain issue-only.
- Direct CLI schema bootstrapping and error exit mapping.
- Import planning is zero-mutation; apply is atomic; exact rerun is no-op;
  changed source/manifest/high-watermark and partial dispositions refuse.
- Parser fixtures cover wrapped bold titles, section changes, explanatory
  paragraphs, duplicate title variants, UTF-8, and malformed input.

### Web UI

Extend `projectsPanel.test.tsx` and add focused integration coverage for:

- Issues/Feature requests/All tabs and independent query state;
- counts, kind pills, feature-specific labels and structured create modal;
- create/edit/close/delete/comment/link/history flows;
- filtering, pagination, deep-link restoration, browser back/forward;
- typed API failures and retry; loading/empty states;
- keyboard and accessible labels; narrow viewport behavior;
- proof that issue-only behavior and snapshots remain unchanged.

Run the existing frontend typecheck/build/test suite and the full Python suite.
Update `docs/issue-tracker-test-matrix.md` and
`scripts/verify-issue-tracker.sh` so the isolated live-server matrix covers both
kinds and migration from a pre-feature DB.

### Migration acceptance

- Planning the supplied inventory yields 27 current candidates plus two explicit
  historical lineage dispositions and zero silent drops.
- Every approved candidate maps to exactly one feature key or one explicit
  existing-key/skip disposition.
- Exact apply rerun creates zero rows/events/links and returns the original map.
- Post-import export, SQLite backup, row counts, kind counts, terminal counts,
  and source digests agree with the receipt.
- A repository-wide search after conductor cutover finds no active intake
  reference to `FUTURE_IMPROVEMENTS.md`.

---

## Acceptance criteria

1. All pre-existing tracker tests pass without changing legacy issue behavior.
2. Existing rows are `kind=issue`; no historical field or related row changes.
3. `cao issue` and existing REST lists exclude features by default.
4. `cao feature` supports complete create/list/show/edit/close/comment/link/
   delete/stats workflows with human and JSON output.
5. The Projects UI provides complete, paginated, deep-linkable feature CRUD and
   history alongside Issues and All views.
6. Projects expose backward-compatible issue counts plus explicit feature/all
   counts.
7. Cross-kind links and audit history work; kind is mutable via PATCH with audit (switching to feature clears stale `failing_command`).
8. The migration plan is source-digest-bound, explicit for every candidate,
   dry-run safe, transactional, and idempotent.
9. The final migration accounts for the committed roadmap, the five preserved
   dirty-root additions, and both historical title variants without importing
   stale worktree copies independently.
10. Markdown intake is retired only after deployment, backup, import, replay,
    and independent audit succeed.
11. Exact source/deployment proof and the final tracker/export/migration receipt
    are retained for rollback.

---

## Implementation slices

### PR 1 — schema and service

Add the discriminator/migration, service filtering/creation/stats/export, and
full DB/service tests. No UI and no live migration.

### PR 2 — API, CLI, and UI

Add typed feature routes/commands, Projects UI tabs/forms/detail behavior,
deep links, docs, and the expanded isolated test matrix.

### PR 3 — importer and approved inventory

Land the parser/manifest validator/atomic importer and tests. Regenerate this
branch's candidate inventory against current sources and tracker, obtain explicit
adjudication, and deploy before apply.

### Operational migration

Back up/export, apply once, verify exact idempotent replay, independently audit,
and publish the mapping/receipt. This is an operation, not a code-review side
effect.

### PR 4 — conductor intake retirement

From clean current `cao-conductor origin/main`, remove the source document, add
the ignore guard, and update all active instructions and tests to the tracker.
Do not clean or overwrite unrelated dirty-root state.

---

## Resolved design decisions

- **Record type:** explicit immutable `kind`, not a label.
- **Storage:** existing `tracker_issues` table and shared related tables.
- **Identity:** one project key prefix/counter across both kinds.
- **Workflow:** existing stored statuses and terminal semantics; feature-oriented
  UI language and labels provide roadmap nuance.
- **Priority:** existing `severity` storage, displayed as Priority for features.
- **Compatibility:** issue-only defaults and legacy issue-only counts.
- **Migration authority:** reviewed digest-bound manifest; no recursive live scan
  and no automatic title dedupe.
- **Source retirement:** only after verified migration; historical worktrees are
  preserved evidence, not fallback intake.

## Open questions for maintainer approval

1. Should the All tab be visible by default, or behind a compact overflow menu
   until projects have at least one feature?
2. Should `cao feature rm` exist for symmetry, or should feature records be
   terminal-only after creation except under a separate admin command?
3. For the five dirty-root additions, should broader Prime Agent requests remain
   in `cao-system` with component/labels, or move to a separately declared
   project before import?
4. Should the four likely-delivered roadmap items be imported as closed/shipped
   history, or mapped to existing implementation issues with no new feature row?
5. Is the historical Campaign timeline candidate fully folded into supervisor-
   poke visibility, or should review promote it to an independent feature?
