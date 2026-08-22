# Implementation boundaries

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Planning record. No unit has been implemented.
**Revision:** 3, amended — U2 gains three migration corrections, U5 gains a security review and the N1 predicate, U6 gains a static assertion, and the predicate site list grows from thirteen to seventeen. Amended after the supervisor corrected the test-baseline premise: **CI on `main` at `6c890ca` is GREEN**, so U5's contract is written against a **working** lint subsystem. Last pass before implementation.
**Related:** [design.md](design.md), [test-strategy.md](test-strategy.md), [traceability.md](traceability.md)

---

## Contents

1. [Sizing rule](#sizing-rule)
2. [Revision 3 changes](#revision-3-changes)
3. [Unit list](#unit-list)
4. [Dependency graph](#dependency-graph)
5. [Ordering constraints that are policy not convenience](#ordering-constraints-that-are-policy-not-convenience)
6. [Units in detail](#units-in-detail)
7. [Security-sensitive units](#security-sensitive-units)
8. [Cross-language and enumerated-set obligations](#cross-language-and-enumerated-set-obligations)
9. [Suggested pull-request sequence](#suggested-pull-request-sequence)
10. [What deliberately is not in release one](#what-deliberately-is-not-in-release-one)

---

## Sizing rule

Each unit is one pull request, reviewable on its own, green on its own, and mergeable to
`main` without the units after it. Two consequences shape the split:

- **Every unit before `vault-recall` is inert in production.** The feature is gated by
  `memory.vault.enabled`, default false, and by requiring a configured vault. Units 1
  through 5 can land in any release without changing behavior for a single existing user
  — with one exception noted in U2, which touches a shared table.
- **No unit leaves the tree in a state where a half-built path is reachable.** In
  particular no exposure surface lands before the gate it depends on.

Unit names are the branch-name stems.

## Revision 3 changes

| Change | Unit | Driver |
| --- | --- | --- |
| `promotion_service.plan()` gains `source_kind == 'native'` — it read a row's `file_path` with **no confinement** and promoted the content into a persistent agent system prompt | **U5** (earlier than required) | N1 [HIGH] |
| A 16th predicate site, `memory_service.py:1388` `_candidate_keys_for_topic`, whose absence lets native and vault keys contaminate each other's `related_keys` | U6 | N3 [MED] |
| `ck_related_keys_length` **omitted** from the rebuilt table | U2 | ruling R12 |
| The idempotence gate becomes a **column-list comparison**, because a named table-level UNIQUE discards its name and a name check can never match | U2 | N5 |
| Index-recreation **ordering** relative to `_migrate_memory_indexes` pinned and asserted | U2 | N6 |
| Site list corrected: **17**, not 13, with `compact()`'s scope filter at **1271** not 1275 | U2 | reviewer |
| Secret gate runs at **index time**, quarantining under `reject` and reporting under `warn` | U3, U4 | ruling R5 |
| **U5 gets a security review** with its own reviewer-confirm list and teeth test | U5 | ruling R13 |
| The five-consumer enumeration is replaced, as enforcement, by a **static assertion** | U6 | reviewer framing |
| `GraphView.meta` distinguishes three causes for absent lint enrichment | U9 | self-identified |
| Every new refusal guard gets **its own test that passes on the current tree** | U5 | self-identified |
| U5's identity-diff becomes a **gate**, with the baseline identities **and the exact invocation** recorded — the local failure set is invocation-dependent, so comparing across invocations manufactures phantom regressions | U5 | supervisor baseline, amended |

### Revision 2 changes

| Change | Driver |
| --- | --- |
| U2 grows substantially: the R2 constraint widening is a **SQLite table rebuild**, `source_kind` must be **NOT NULL DEFAULT 'native'**, and a list of query sites gains a predicate (thirteen as of revision 2; seventeen as of revision 3). U2 is the highest-risk unit in the plan and ships alone | F3, ruling R2 |
| U5 gains the three F9 **refusal guards** (`wiki_lint`, `wiki_healer`, `cleanup_service`), because U5 is the unit that ships `binding.py` | F9 |
| U6 gains the **vault `Memory` builder** — without it AC3 and AC4 are unreachable — plus the OKF export refusal, and its byte-identical claim is restated honestly | F2, F6, F7, F9 |
| U7 gains the **curator recall gate** and becomes the unit that closes F1 | F1, ruling R1 |
| U8 gains the **`ForgetResult`** return shape and both `SKILL.md` copies | F11, F10, ruling R3 |
| U9 joins the **security-review list**, must land **after U7**, and projects only `status='indexed'` notes | F12, directed fix D3 |
| U12's targets corrected: `docs/settings.md` and `docs/configuration.md` **already exist** | F10 correction |
| F16 CodeQL discipline added to the U4, U6 and U8 review checklists | F16 |

## Unit list

| # | Unit | Depends on | Security review | Rough size |
| --- | --- | --- | --- | --- |
| U1 | `vault-config` | — | **Yes** | Medium |
| U2 | `vault-schema` | — | **Yes** | **Large** |
| U3 | `vault-parse` | U1 | **Yes** | Medium |
| U4 | `vault-scan` | U1, U3 | **Yes** | Medium |
| U5 | `vault-reconcile` | U2, U4 | **Yes** | Large |
| U6 | `vault-recall` | U5 | **Yes** | Large |
| U7 | `vault-inject-gate` | U6 | **Yes** | Medium |
| U8 | `vault-write` | U5, U6 | **Yes** | Large |
| U9 | `vault-graph` | U5, **U7** | **Yes** | Small |
| U10 | `vault-migrate` | U8 | No | Medium |
| U11 | `vault-cli` | U5, U7, U8, U10 | No | Medium |
| U12 | `vault-docs` | U11 | No | Small |

**Eight of twelve** carry a security review. Revision 2 added U2 and U9; revision 3 adds
U5 (ruling R13). The reasoning is the same class in all three cases: U2 because a
constraint widening on a shared table can silently destroy an invariant; U9 because
`Node.label` is the key and a key encodes structure; U5 because its four maintenance
refusals are the only thing standing between an unattended job — `cleanup_service` at
90-day project retention, `wiki_healer --apply` on an LLM's judgement — and the silent
de-indexing of a user's vault notes. A refusal is a security control even though it is
three lines of predicate.

## Dependency graph

```
U1 vault-config --+--> U3 vault-parse --> U4 vault-scan --+
                  |                                       |
U2 vault-schema --+---------------------------------------+--> U5 vault-reconcile
                                                                     |
                          +------------------------------------------+
                          v
                  U6 vault-recall
                          |
              +-----------+-----------+
              v                       v
    U7 vault-inject-gate      U8 vault-write
              |     |                 |
              |     +-----+           v
              |           |   U10 vault-migrate
              v           v           |
      U9 vault-graph      +-----------+
              |                       |
              +-----------+-----------+
                          v
                   U11 vault-cli --> U12 vault-docs
```

U9's edge from U7 is new in revision 2 and is a policy constraint, not a technical one.

## Ordering constraints that are policy not convenience

Two edges must not be relaxed for scheduling reasons.

1. **U7 immediately after U6.** There must be no release in which vault content is
   recallable and the injection policy is not yet enforced. U6 is the first unit that
   changes behavior for a configured user; U7 is what makes the second of the issue's two
   mandated policies real.
2. **U9 after U7.** `Node.label` is the key, and a path-derived key encodes folder path
   and filename, so the graph is an exposure surface reaching `GraphView`, the MCP Apps
   `ui://cao` views and the browser dashboard. U9 must project through the chokepoint U7
   establishes, with the same `status='indexed'` predicate, so graph exposure equals recall
   exposure rather than exceeding it. Landing U9 first would publish quarantined notes'
   keys — that is, their paths and titles — for notes deliberately excluded from recall.

## Units in detail

### U1 — `vault-config`

**Ships.** `services/vault/config.py` with `VaultConfig`, `VaultSpec`, `FolderMapping`
and all **twenty** validation rules from
[adr-007-configuration-surface.md](adr-007-configuration-surface.md).
`settings_service.get_vault_config()`. `vault` on `config_service.MemoryConfig`.
`CAO_MEMORY_VAULT_ENABLED` in `ENV_REGISTRY`. `services/vault/findings.py` with the closed
finding vocabulary and the supported-boundary table from
[adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md) as data.

**Does not ship.** Any read of a note. Any database access. Any CLI command.

**Revision 2 note.** Rules 7 and 8 are the D1 correction: a mapping's `folder` is
validated for shape and containment but **not** against a character allowlist, while
`managed_folder` is charset-validated because it feeds `safe_join_under_base`. Getting
this backwards — as revision 1 did — makes the feature unconfigurable against any vault
containing a folder with a space or a non-ASCII character.

**Reviewable in isolation because** its whole surface is "given this JSON, does it load
and with what error". Tests are table-driven over valid and invalid documents.

### U2 — `vault-schema`

**Ships.** Three tables (`vault_note`, `vault_finding`, `vault_note_alias`), one column
(`memory_metadata.source_kind`), one **widened unique constraint**, and the
`source_kind` predicate on **thirteen** existing queries.

**Why this is now the largest-risk unit.** Three things compound:

1. **`source_kind` must be `TEXT NOT NULL DEFAULT 'native'`, not nullable.** SQLite treats
   `NULL != NULL` inside a UNIQUE index, so a nullable discriminator makes the widened
   constraint **inert for every native row** — native duplicate keys become insertable and
   an invariant that has held since Phase 2 silently disappears. `RELATIONSHIP_SCOPE_ID_SENTINEL`
   (`database.py:139`) exists for exactly this reason and its comment is the precedent;
   `access_count`'s `nullable=False, default=0, server_default="0"` is the precedent for the
   no-backfill add.
2. **Widening a UNIQUE constraint in SQLite is a table rebuild.** There is no
   `ALTER ... ADD CONSTRAINT`. Create `memory_metadata_new`, `INSERT ... SELECT ...,
   'native'`, drop, rename, recreate the three indexes — one transaction, gated on
   `PRAGMA index_list`/`index_info` so a second `init_db()` is a no-op.
3. **There is no drift guard on this table.** Verified: the `_REQUIRED_*_COLUMNS` equality
   pattern exists only for `workflow_run` and `workflow_run_step` in
   `services/workflow_journal.py`. Nothing fails loudly if a mirror is missed, so the
   thirteen-site list in [design.md](design.md) must be worked through mechanically.

**The seventeen sites**, by path and line: `memory_service.py` 341, 395, **1271**, 1388,
1480, 2089, 2126; `memory_reconciliation.py` 608, 948; `memory_relationship_service.py`
286, 805; `wiki_healer.py` 270, 317, 453, 681; `promotion_service.py` 136;
`wiki_lint.py` 1002-1004. The full table with each site's consequence is in
[design.md](design.md).

Three corrections from revision 2, each of which would have left a real predicate
unpatched:

- **`compact()`'s scope filter is at 1271, not 1275.** Line 1275 is only the optional
  `key ==` clause. Since this unit's spec says the list is to be worked through
  mechanically, a wrong line number *is* a missed predicate — the mechanical process would
  have patched a line that needed nothing.
- **`memory_reconciliation.py:608` has no filter at all** — `db.query(MemoryMetadataModel).all()`,
  feeding `_load_rows()` for `cao memory repair`.
- **`wiki_lint.py:1002-1004`** was named in design.md's component table but absent from the
  enumeration, which is exactly the failure mode a mechanical list is supposed to prevent.

Where the correct behavior is "native-only code must not see vault rows", the predicate is
`source_kind = 'native'` and is simultaneously the F9 guard.
`memory_service.py:1388` is the one site that is neither native-only nor binding-aware: it
must filter to the **same** backend as the topic it is computing candidates for.

**Ships alone.** No other content in the pull request. This is the one unit that is *not*
inert for existing users: it rewrites a shared table on the next `init_db()`.

**Three migration corrections from the revision-2 review**, each of which would have made
U2 misbehave permanently rather than loudly:

- **`ck_related_keys_length` is OMITTED from the rebuilt table** (ruling R12). The
  existing comment in `database.py` states that CHECK applies to fresh databases only and
  that existing databases rely on the parse-side cap in `_parse_related_keys`, so omitting
  it makes the rebuilt table behave identically to the one it replaces. U2 then changes
  uniqueness and nothing else, which is what a constraint-widening migration should do.
  `substr` truncation was rejected outright — silently destroying data during a uniqueness
  migration is the one thing it must never do — and pre-scan-and-refuse was rejected as
  still blocking startup.
- **The idempotence gate is a column-list comparison, not a name check** (N5). A named
  table-level `UNIQUE` in SQLite produces `sqlite_autoindex_<table>_1` with `origin='u'`,
  and **the name `uq_memory_key_scope` is discarded** — it never appears in
  `PRAGMA index_list`. A name-based gate can therefore never match, and the migration would
  rebuild a shared table on **every** `init_db()`, forever, with no error to notice. The
  gate must enumerate `PRAGMA index_list(memory_metadata)`, keep `origin='u'` rows, and
  compare each one's `PRAGMA index_info` column list against
  `('key','scope','scope_id','source_kind')`.
- **Index-recreation ordering must be pinned** (N6). `idx_memory_scope`,
  `idx_memory_updated` and `idx_memory_type` are created at `database.py:386`, `:389`,
  `:392` by the **separate** idempotent `_migrate_memory_indexes()` using
  `CREATE INDEX IF NOT EXISTS`. If the rebuild runs **before** that function, it need not
  recreate them; if **after**, it must recreate all three or they are silently lost until
  the next process start. Pin the position explicitly.

**Tests.** Fresh database; a database created before the change; `init_db()` twice is a
no-op **and performs no second rebuild** (the N5 assertion — count rebuilds, do not just
check the end state); the widened constraint rejects a duplicate
`(key, scope, scope_id, 'native')`; a duplicate native key is **still** rejected after the
widening (the assertion that catches the nullable mistake); all three secondary indexes
exist after migration on both a fresh and a pre-existing database (N6); the rebuilt table
carries no `ck_related_keys_length` and an over-long `related_keys` value survives the
rebuild unmodified (R12); and every one of the seventeen sites carries its predicate.

### U3 — `vault-parse`

**Ships.** `parser.py`, `identity.py`, `links.py`. All pure: no filesystem, no database.
Bounded frontmatter parse with the byte cap, safe loader, anchor/alias rejection. `cao`
block validation **importing** `VALID_TYPES`, `VALID_STATUSES`, `VALID_ORIGINS` from
`memory_relationship_service` rather than re-declaring them. `cao_key` extraction and the
mapping-relative path-derived fallback **with its stable digest suffix** (F14 — without it
`_sanitize_key`'s 60-character truncation silently collides long-titled notes in nested
folders). `note_uid` derivation. Wikilink resolution to the five outcomes. The
frontmatter-region preservation map the writer later uses.

**Also ships (ruling R5).** The `secret_detected` finding code and the pure
`scan_for_secrets`-based classification, so the index-time gate's decision is a testable
function of content with no I/O. The severity is **mapping-dependent** — `error` under
`secret_gate: "reject"`, `warn` under `"warn"` — which no other finding in the vocabulary
is, so `findings.py` must model severity as a function of configuration for this one code
rather than a constant.

**Does not ship.** Reading a file from disk. Writing anything.

### U4 — `vault-scan`

**Ships.** `scan.py`. Sorted traversal, exclusion application, always-excluded paths,
symlink refusal via `memory_reconciliation._first_symlink_component`, **hardlink refusal**
(`st_nlink > 1`, with the per-mapping `allow_hardlinks` escape hatch), `max_note_bytes` and
`max_notes`, the two-stat stability check, sync-conflict filename refusal, UTF-8 strictness,
line-ending normalization before hashing, `content_sha256` and `frontmatter_sha256`. A
frozen deterministic `ScanReport`. **And the index-time secret gate** (ruling R5): each
candidate's body is passed through `scan_for_secrets`, and a match quarantines the note
under `reject` or records the finding under `warn`. It runs here rather than at read time
because it is a pure function of content — free per read, re-run only when the content hash
changes, and deterministic under `rebuild`.

**Does not ship.** Any database write. Any vault write.

**F16 obligation.** `scan.py` owns its own `open()` sinks and re-asserts
`real_path.startswith(root + os.sep)` inline beside each one, in the single positive form,
taking and returning bare `str`.

### U5 — `vault-reconcile`

**Ships.** `reconcile.py`, `binding.py`, `status.py`. Plan and apply. Rename absorption.
Upserts into `vault_note`, `vault_finding`, `vault_note_alias`, `memory_metadata`
(`source_kind='vault'`), and `replace_set` of `origin="vault"` edges. The full-rebuild
path with scoped deletes. The frozen report. Two audit events added to
`NOWAIT_AUDIT_EVENTS`. `origin = "vault"` in `VALID_ORIGINS` and its two other enumerating
sites. **Alias-aware `scope_id` canonicalisation** in `binding.resolve()` plus the
orphaned-mapping and `unmapped_project_write` warnings (F13).

**Plus the three F9 refusal guards**, which belong here because this is the unit that
ships `binding.py`:

- `wiki_lint.py` — its metadata row load gains `source_kind = 'native'`. Today vault rows
  are dropped silently by the `base_resolved` containment check at lines 1047-1053, so
  every detector sees an empty corpus for that scope.
- `wiki_healer.py` — refuses vault-bound scopes early. Otherwise
  `cao memory heal --apply` reaches `_delete_row` (line 306) for a note whose native path
  does not exist: no file is unlinked, but the **metadata row is deleted** — a silent
  de-index of a user's vault note on an LLM's judgement. Unreachable today only by
  accident.
- `cleanup_service.py` — refuses vault-bound scopes. Otherwise the retention sweep parses
  a stale native `index.md` and calls `forget()` at line 195, de-indexing vault rows
  unattended at 90-day retention for a scope that used to be native.
- **`promotion_service.py` — `plan()` (`:136`) gains `source_kind = 'native'` (N1).** This
  is the fourth guard and the most consequential. `plan()` filtered only on
  `scope=='agent'`, `scope_id`, `memory_type.in_(PROMOTABLE_TYPES)` and
  `access_count >= min`; took `file_path` straight off the row; `_lesson_text` then read it
  with **no confinement at all**; and the section parser is `if line.startswith("## ")`, so
  **any** h2 matches and an ordinary Obsidian note parses cleanly rather than failing safe
  on the absent `## <ISO8601Z>` heading. The destination is `apply_deltas` into a profile's
  learned-patterns section — a **persistent system prompt on disk**. Native-only by intent
  (a learned lesson is CAO-authored), so the predicate is the correct fix, not a stopgap.

**Placement note.** The review said "fix N1 before U7"; it lands in U5 instead, which is
strictly earlier. U5 is where vault `memory_metadata` rows first exist, so any later
placement leaves a window; and the edit is the same class as the other three guards, so it
belongs on the same reviewer checklist. The *exploitable* window opens slightly later —
`DEFAULT_MIN_ACCESS_COUNT` is 3 and a vault row cannot reach 3 until recall exists in U6 —
so U5 placement closes the hole before it opens. **Mitigating factor, which lowers
likelihood and not severity:** the vault `Memory` builder defaults an absent `cao.type` to
`reference`, and `PROMOTABLE_TYPES` is `("feedback","project")`, so exploitation needs a
note carrying an explicit `cao.type: project` or `cao.type: feedback`.

**Does not ship.** Any change to `recall()`, `store()` or injection. After this unit derived
state builds and rebuilds, and nothing reads it.

**U5 may assume the lint subsystem works.** CI on `main` at `6c890ca` — this unit's exact
base — is **green across three consecutive runs**. An earlier brief described the lint, heal
and graph-cache subsystem as "already red on `main`"; that was a measurement of one
developer machine, not of the tree, and it has been retracted. Write U5's contract against a
functioning subsystem: there is no "best-effort-and-possibly-dead" allowance.

**Every guard still gets its own new test that passes on the current tree.** The reason is
not the subsystem's health, it is where the assertion lives. `TestMemoryHeal` and
`test_wiki_lint.py` may be red **on the implementer's machine** for local-environment
reasons, and an assertion bolted into a class that errors before reaching it can appear
covered while never executing. So each of the four guards needs a fresh test function in a
new file, green locally and in CI. This requirement is independent of the baseline question
and survives its correction intact.

**The identity diff is a GATE, not advice — and the invocation is part of the gate.** The
local failure set is **invocation-dependent**: running the four affected files in isolation
produces 25 failures, while the same files inside the full run produce 18. A set measured
one way and compared against a set measured the other way would manufacture phantom
regressions, which is worse than no baseline at all. So U5 records **the identities and the
exact command that produced them**, takes the baseline with the **CI-exact invocation**, and
compares against a run using that same invocation.

**CI is the arbiter.** A red local run on a developer machine is a **local-environment
artifact to be diagnosed as such** — two of the observed failures are timestamp-rendering
tests (`TestT3DetectedAtVisible::test_cli_json_uses_iso8601_z_suffix`,
`test_cli_table_renders_detected_at`), consistent with a timezone or library-version
difference rather than broken logic. It is never to be "fixed" inside a vault pull request:
repairing an unrelated subsystem there makes the diff unreviewable, and if the failure is
environmental there is nothing to repair.

**Watch items.** `AUDIT_EVENT_WHITELIST` is closed — an unlisted event is dropped silently,
which would make a content-free-audit assertion pass vacuously; add all three events
(`vault_reconcile_completed`, `vault_note_quarantined`, `vault_secret_quarantined`) in the
same commit as their emit sites. The rebuild determinism test lands here, with the two-group
dump split from [test-strategy.md](test-strategy.md).

### U6 — `vault-recall`

**Ships.** `services/vault/reader.py` — **the candidate chokepoint**:
`resolve_candidates()` with its no-default `require_injectable` parameter,
`load_candidate()`, and **the vault `Memory` builder**.

The builder is not optional polish. `_parse_wiki_file` (line 2601) does
`timestamps = re.findall(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", ...)` then
`if not timestamps: return None`, and **both** readbacks drop falsy results — line 2237 in
`_metadata_recall` and line 2470 in `_bm25_search`. An ordinary Obsidian note would be
discovered, ranked, and then thrown away, making AC3 and AC4 unreachable. The builder must
be wired into **both** sites, with the field mapping in [design.md](design.md), and the
vault arms must never call `_parse_wiki_file`.

Also ships: `MemoryService` consults `binding.py` in `recall()`; a new `_resolve_sources()`
**added beside** an unchanged `_get_search_dirs()`; the vault arm of `_metadata_recall`;
corpus-provider parameterization of `_bm25_search` and `_bm25_relevance` **plus the
trailing keyword parameter on `_apply_sort_and_increment`**, which is `_bm25_relevance`'s
only caller (line 1997) and is therefore unavoidable — revision 1 claimed both that the
corpus was parameterized and that this method did not change, and both cannot be true;
one-read-per-candidate on the vault BM25 arm (T2-minor); vault related-expansion routed
through the chokepoint (F8); read-time re-confinement inline at the sink; the body budget
and `token_estimate` (F15); freshness stamping; `source_kind`, `source_path`,
`indexed_at`, `index_freshness`, `content_truncated` on `Memory`, on `MemorySummary` and
`MemoryDetail`, and in the MCP `memory_recall` mapping; and the **OKF export refusal** for
vault-bound scopes in `export_memories()` (today `_collect_topics` walks native paths and
silently produces an empty bundle).

**Honest byte-identical claim (F7).** `_get_search_dirs` is retained **unchanged**. Every
new parameter on an existing method is a **trailing keyword argument with a
native-preserving default**, and a call that does not use it keeps its current form rather
than passing an explicit `None`. That keeps the existing recall tests passing unmodified,
which is stronger evidence than an edited assertion.

**Watch items.** `_bm25_relevance`'s docstring explains why the corpus must be the full
document population — a candidate-only corpus collapses IDF when every candidate contains
the query term; the vault provider must yield the whole indexed scope. Before changing any
injection-adjacent signature, grep for code that **replaces** the function rather than
calling it: a module-level monkeypatch in this suite assigns a two-parameter lambda over
`terminal_service.inject_memory_context`.

**F16 obligation.** `reader.py` owns every taint-reachable read sink with the guard inline
and returns a `Memory` or bytes, never a path.

**The chokepoint invariant is enforced by a static assertion, not by a consumer list
(N1/N3).** Revision 2 enumerated five consumers and claimed the gate was total; the review
then found two more read paths the sweep had missed — `promotion_service` and
`_candidate_keys_for_topic`. An enumeration is only as good as the sweep behind it, so U6
adds to `test_no_enumeration_outside_scan.py`:

> No module outside `reader.py` may call `open` or `read_text` on a value originating from
> `memory_metadata.file_path`.

The consumer list survives in [design.md](design.md) as **non-normative orientation** —
what exists today — and the assertion is what a future author's new read path fails against.
This is the only form in which "provable rather than asserted" is actually provable.

**N3 — the 16th site.** `_candidate_keys_for_topic` (`memory_service.py:1388`) filters scope
plus scope_id with no `source_kind` and `.limit(200)`, feeding `find_related` and the LLM
compiler's candidate set. Without the predicate, a **native** memory's compile can receive
**vault** keys as related candidates and write a vault key into a native row's
`related_keys` — and the reverse — after which the contamination flows into
`_expand_related`'s legacy fallback at `:1691-1693`. Unlike the other sites this one is
neither native-only nor binding-aware: it must filter to the **same** backend as the topic
it is computing candidates for, because its purpose is intra-corpus relatedness.

### U7 — `vault-inject-gate`

**Ships.** The `require_injectable` derivation at both injection entry points, and the
curator gate that closes F1:

- `get_memory_context_for_terminal` passes `require_injectable=True` into the chokepoint,
  including for the related fan-out at line 2854.
- **A recall whose resolved terminal context is the injection curator also passes
  `require_injectable=True`** (ruling R1). `get_curated_memory_context` already locates the
  curator via `_find_context_manager_terminal(session_name)`; a curator is identified by
  its `agent_profile`.
- **Fail closed when the curator is unidentifiable**: do not dispatch, fall back to the
  gated deterministic builder.
- The `status` warning line naming injectable mappings.

**Why this is no longer a tiny unit.** Revision 1 sized it small because it believed a
filter in the builder was sufficient. It is not:
`terminal_service.inject_memory_context` (line 118) calls `get_curated_memory_context`
(line 135), whose **success path returns the curator's raw block at line 2972** and which
reaches the gated builder **only on its fallback at line 2976**.

**Watch items.** `_is_memory_enabled()` is the first line of `get_curated_memory_context`;
no new path may bypass it. Any future frozen-memory injection arm must route through the
chokepoint with `require_injectable=True`.

### U8 — `vault-write`

**Ships.** `writer.py`: `store()` branches on binding; `safe_join_under_base` composition
under `managed_folder`; the vault secret gate; `locked_atomic_rewrite` with lock and temp
files in CAO-owned locations; frontmatter merge that re-emits the original frontmatter
**text region** verbatim except the `cao` block; the `content_sha256` conflict check inside
the lock, failing visibly; post-write single-note refresh raising the existing
`MemoryPartialWriteError` shape.

**Plus the `forget()` contract change (F11, ruling R3).** `forget()` returns a frozen
`ForgetResult` whose `__bool__` returns the previous boolean, so all seven existing call
sites — `cli/commands/memory.py:215` and `:256`, `api/main.py:6849` and `:6904`,
`mcp_server/server.py:2066`, `cleanup_service.py:195`, `memory_archive/okf.py:366` — keep
behaving identically with no edit. It adds `action` (`deleted` | `deindexed` | `absent`),
`source_kind` and `path`. MCP `memory_forget` keeps `deleted`, adds `action` and `path`,
and **its docstring stops claiming "Deletes the wiki topic file"** — today it returns
`{"deleted": true}` under that docstring, so an agent is told a file was deleted when it
was not. **Both `skills/cao-memory/SKILL.md` copies** get their Forget section corrected,
edited via `python scripts/sync_skills.py`.

**Constraint.** The native `store()` arm stays byte-identical.

**F16 obligation.** `writer.py` owns every write sink with the guard inline.

### U9 — `vault-graph`

**Ships.** `MemoryGraphProvider` takes its node set from **the chokepoint**, with the same
`status='indexed'` predicate recall uses. `is_vault` node attribute. And `GraphView.meta`
reports absent lint enrichment with a **discriminated cause**, not a single "unavailable":
`disabled_by_setting` (the existing `is_memory_lint_enabled` path), `unavailable_vault`
(this scope is vault-backed, so `wiki_lint`'s `MEMORY_BASE_DIR` containment check drops its
rows by construction — a permanent design boundary in release one), or `failed` (the
existing `meta["lint_error"]` path). Revision 2 conflated the second with the third, so a
reader could not tell a deliberate boundary from a live fault. The `disabled_enrichments`
shape at `graph/providers/memory.py:82-95` is the precedent for how to report it.

Note what this is **not** fixing. `_build`'s broad `except Exception` →
`meta["lint_error"]` is genuine graceful degradation and is **not** currently firing on
`main` (CI is green at `6c890ca`), so there is no pre-existing silent-masking problem for
this change to extend. The three-cause split is worth making on its own merit: a vault user
should see a permanent design boundary and a native user should see a fault, and a single
undifferentiated "unavailable" cannot say which.

**Does not ship.** Any new `EdgeType`, so `cao_mcp_apps/src/graph/types.ts` and the web
graph components are untouched. **And no `quarantined` node attribute** — revision 1
proposed one, and it was the leak: `Node.label` is the key, a path-derived key encodes
folder structure, so a quarantined node publishes the path and title of a note that is
deliberately not recallable. No `vault_relpath` or path-shaped value appears in any node or
edge attribute.

**Security review, and it must land after U7.** See
[Ordering constraints that are policy not convenience](#ordering-constraints-that-are-policy-not-convenience).

**Watch item.** Preserve the existing constraint that the relationship read happens
**after** `wiki_lint.run_lint` — the comment above that call documents a real bug that was
fixed once.

### U10 — `vault-migrate`

**Ships.** `migrate.py`. Dry-run-first migration of a native scope into the managed folder.
Representable-metadata mapping. A lossy-field report shaped like the OKF `ImportReport`.
`--delete-source` behind `--apply` plus a second confirmation.

**Lossy fields, reported by name:** `access_count`, `last_accessed_at`,
`last_compiled_at`, `source_provider`, `source_terminal_id`, the legacy `related_keys` text
column, and append-only section history beyond what fits the managed note. Typed
`memory_relationships` rows **are** representable and migrate into `cao.links`.

**Why R2 exists.** Without the widened constraint, `migrate` without `--delete-source`
keeps the key by design, so every migrated memory would collide with its native original
and the default migration mode would be unusable — which is why refusing colliding keys was
rejected.

### U11 — `vault-cli`

**Ships.** `cao memory vault {status,scan,reconcile,rebuild,migrate}`. Read-only
`GET /memory/vault/status`. **And the `tui/src/catalog.rs` changes.**

**Does not ship.** Any MCP tool. An agent able to trigger a vault rescan can make CAO read
files on a schedule the operator did not choose.

### U12 — `vault-docs`

**Ships.** A new `docs/obsidian-vault.md` carrying the supported-boundary table verbatim,
the two-policy explanation, the full annotated settings example, the `project_id` pinning
recommendation, and the documented behavioral differences (`forget()` de-indexes;
`rebuild` resets vault access counts; export refuses vault scopes). Edits to
**existing** files: `docs/memory.md` (the `vault` origin, a pointer to the new page),
`docs/settings.md` (the `memory.vault` block), `docs/configuration.md` (a
`CAO_MEMORY_VAULT_ENABLED` row in the env-var table at lines 266-276). A vault section in
**both** `SKILL.md` copies via `scripts/sync_skills.py`.

**Correction to the review:** `docs/settings.md` and `docs/configuration.md` were verified
present in this worktree. Neither is created.

**Watch item.** `scripts/validate_markdown_links.py` validates local links and heading
fragments across every **git-tracked** `.md`, so every relative link and anchor must
resolve once committed.

## Security-sensitive units

**Eight** units get an independent security review, separate from the functional review.

| Unit | What the review must confirm |
| --- | --- |
| U1 `vault-config` | `root` passes `resolve_and_validate_path`; no overlap with `MEMORY_BASE_DIR`, `CAO_HOME_DIR` or the graph export root; mappings cannot overlap; `federated`/`session` unmappable; `inject` without `index` refused; **`secret_gate` defaults to `reject` when absent, and `warn` + `inject: true` warns rather than passing silently** (R14); no path env-settable; every rule rejects rather than sanitizes; bounds not disableable; **and that read-mapping folders are NOT charset-restricted while `managed_folder` is** |
| U2 `vault-schema` | `source_kind` is NOT NULL with a server default, so the widened UNIQUE index is total; a duplicate **native** key is still rejected after the widening; the table rebuild is transactional; **the idempotence gate compares index COLUMN LISTS, not constraint names** (N5), and a second `init_db()` performs no second rebuild; all three secondary indexes survive (N6); `ck_related_keys_length` is absent and no data was truncated (R12); all **seventeen** sites carry the predicate; native-only consumers filter `source_kind = 'native'` |
| U5 `vault-reconcile` | **The four refusal guards are present and effective** — `wiki_lint` row load, `wiki_healer` early refusal, `cleanup_service` refusal, and `promotion_service.plan()`'s `source_kind = 'native'` (N1); each has its **own** test that is green on the current tree rather than an assertion inside an already-red class; the rebuild's deletes are scoped by `source_kind`/`origin`; all three audit events are in `NOWAIT_AUDIT_EVENTS`; the index-time secret gate quarantines under `reject` and its finding carries the pattern name only |
| U3 `vault-parse` | Frontmatter size-capped **before** parsing; safe loader; YAML anchors and aliases refused; `cao.links` capped; taxonomies imported not re-declared; invalid values quarantine rather than default; `cao.key` failing the shipped charset refused; the derived key is length-bounded by construction so truncation cannot decide identity; no parsed value interpolated into a prompt |
| U4 `vault-scan` | Symlinked components refused; **hardlinks refused**; always-excluded paths unconditional; exclusions applied **before** any open; the stability check not defeatable by a same-size write; non-UTF-8 quarantined; caps enforced; **F16 — this module owns its open sinks with the guard inline in the single positive form, passing bare `str`** |
| U6 `vault-recall` | Confinement re-asserted **inline at the sink**, independent of the row; a failing row is skipped and recorded, not raised; quarantined notes cannot become candidates; scope filtering unchanged; the API carries the vault-relative path only; `require_injectable` has no default; the body budget is enforced in the builder; **F16 as above** |
| U7 `vault-inject-gate` | Injection defaults deny; **the curator's recall input is gated, not its output**; an unidentifiable curator falls back rather than dispatching; unresolvable bindings exclude the candidate; the related fan-out at line 2854 is gated; `_is_memory_enabled()` not bypassed |
| U8 `vault-write` | Every target composed under `managed_folder`; a non-writable mapping fails rather than falling back; the secret gate runs; lock and temp files outside the vault; the conflict check inside the lock; only the `cao` frontmatter key rewritten; `forget()` never unlinks; **F16 as above** |
| U9 `vault-graph` | Node set uses the chokepoint with `status='indexed'`; **no quarantined node**; no path-shaped attribute; no new `EdgeType`; lands after U7 |

Each security review includes a **teeth test**: remove the guard, plant the attack, confirm
the leak, restore, confirm the leak is gone, and confirm `git diff` is empty afterwards.
The specific teeth tests are in [test-strategy.md](test-strategy.md).

## Cross-language and enumerated-set obligations

| Set | Where | Obligation |
| --- | --- | --- |
| CLI command catalog | `tui/src/catalog.rs` — `CommandId`, `COMMAND_COUNT` (currently 69), `DISPLAY_ORDER` (length is the type), two exhaustive matches, hard-coded counts in the module doc comment | Five new leaves means `COMMAND_COUNT` 74 and every site changes, in U11's pull request. `test/test_command_catalog_matches_click.py` asserts parity in **both** directions |
| Rust TUI build | `tui/` | U11 runs `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, `cargo test --locked` |
| **Agent-facing memory contract** | **`skills/cao-memory/SKILL.md` AND `src/cli_agent_orchestrator/skills/cao-memory/SKILL.md`** | Both copies exist and are held **byte-identical** by `test/test_skill_packaging_parity.py` via `filecmp.cmp`; sync with `python scripts/sync_skills.py`. Editing one copy turns the suite red. Its Forget section documents `memory_forget` as removing the topic file, which ruling R3 makes wrong — U8 corrects it, U12 adds the vault section. Revision 1 never mentioned skills at all |
| Relationship origins | `memory_relationship_service.py:55`; the comment at `database.py:169`; `docs/memory.md:301` | U5 adds `vault` to all three. It must **not** touch `_backfill_legacy_related_keys`, whose source text is hashed at `test/clients/test_memory_relationships_migration.py:119` |
| Edge types | `graph/models.py` `EdgeType`; `cao_mcp_apps/src/graph/types.ts:13`; web and MCP-apps graph tests | **No change.** An origin, not a type. Stated so nobody adds one helpfully |
| Audit events | `audit_log.py` `SYNC_AUDIT_EVENTS` / `NOWAIT_AUDIT_EVENTS`, unioned into the closed `AUDIT_EVENT_WHITELIST` | U5 adds two. An unlisted event is dropped silently, so an audit assertion would pass vacuously |
| `memory_metadata` columns and constraints | The model; the three `PRAGMA table_info(memory_metadata)` gates; `docs/memory.md`; **and the seventeen key-plus-scope query sites** across `memory_service.py`, `memory_reconciliation.py`, `memory_relationship_service.py`, `wiki_healer.py`, `promotion_service.py` and `wiki_lint.py` | U2. Verified: **no** `_REQUIRED_*_COLUMNS` equality guard on this table, so nothing fails loudly if a site is missed. Revision 2's list was 13 and mis-cited one line; four sites were added and one corrected in revision 3 |
| Secret-gate enforcement points | `store()`'s federated branch (existing); OKF export (existing); **and now reconcile** (ruling R5). One `mappings[].secret_gate` key governs the write and index points | U3 defines the finding, U4 applies it during the scan, U1 validates the enum |
| `forget()` return contract | `memory_service.py:2658`; seven call sites; `mcp_server/server.py:2047-2076` docstring and response; both `SKILL.md` copies | U8. `__bool__` is what keeps the seven call sites unedited |
| Memory settings keys | `get_memory_settings()` defaults; `set_memory_setting()`'s closed whitelist; `config_service.MemoryConfig`; `ENV_REGISTRY`; `docs/settings.md`; `docs/configuration.md` | U1 adds the nested `vault` object and one env var. `set_memory_setting()` deliberately gains no vault key |
| Findings vocabulary | `services/vault/findings.py` only | One module, so parser, resolver, status view and documentation cannot drift |

## Suggested pull-request sequence

1. **U2 `vault-schema`** — first, alone, and reviewed hardest. It is the only unit that
   touches a shared table, and the nullable-discriminator mistake is the kind that passes
   every test while removing an invariant.
2. **U1 `vault-config`** — the security boundary, nothing else in the diff.
3. **U3 `vault-parse`** — untrusted parsing, no I/O in the diff.
4. **U4 `vault-scan`** — traversal, symlink and hardlink review.
5. **U5 `vault-reconcile`** — after this, derived state builds and rebuilds
   deterministically and nothing reads it. Carries the three F9 refusal guards, so the
   native maintenance paths are safe before any vault row is readable. First
   demonstration-of-value checkpoint.
6. **U6 `vault-recall`** — the first unit that changes behavior for a configured user. AC3
   and AC4 become true here, and only because of the vault `Memory` builder.
7. **U7 `vault-inject-gate`** — immediately after U6. Policy constraint.
8. **U9 `vault-graph`** — after U7. Policy constraint.
9. **U8 `vault-write`** — AC7, AC8, and the `forget()` contract.
10. **U10 `vault-migrate`** — AC12.
11. **U11 `vault-cli`** — AC5's observability half, plus the Rust catalog.
12. **U12 `vault-docs`**.

U9 has moved from "any time after U5" to a fixed position. That is the visible consequence
of taking `Node.label` seriously as an exposure surface.

## What deliberately is not in release one

- **A file watcher.** Non-goal.
- **Multiple vaults.** [adr-002-vault-cardinality.md](adr-002-vault-cardinality.md).
- **Writes to any writable mapping rather than one managed folder.**
  [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md).
- **`cao memory vault adopt`**, a single audited write of `cao.key` into one unmanaged
  note. [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md).
  The strongest release-two candidate.
- **Per-note injection opt-out in frontmatter.**
  [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md).
- **Block-reference and heading-anchor resolution; embed inlining; Dataview
  interpretation.** [adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md).
- **A CLI or HTTP setter for vault configuration.**
  [adr-007-configuration-surface.md](adr-007-configuration-surface.md).
- **An MCP tool for reconcile.** U11's note.
- **Per-project vault configuration in a repository file.**
  [adr-007-configuration-surface.md](adr-007-configuration-surface.md) alternative D.
- **OKF export of a vault-bound scope.** Refused rather than taught the binding, because
  teaching it would create a second read path into the vault and therefore a second
  chokepoint consumer. Open question 3 in [design.md](design.md).
- **Lint enrichment for vault-bound scopes.** Reported as unavailable in `GraphView.meta`
  rather than made to work, because `wiki_lint`'s detectors read bodies through a
  `MEMORY_BASE_DIR` containment check. Reflected in
  [traceability.md](traceability.md).
