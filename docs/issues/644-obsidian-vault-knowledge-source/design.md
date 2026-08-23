# Design: Obsidian vault as a canonical CAO knowledge source

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Specified, not implemented. No code, no migration, no schema change has been applied.
**Revision:** 3, amended — folds in the revision-2 review's findings N1-N6, rulings R5-R13, and three self-identified weaknesses. See [Revision 3 changes](#revision-3-changes). Amended after the supervisor corrected the test-baseline premise: **CI on `main` at `6c890ca` is GREEN**; the 20 local failures are a local-environment artifact. This is the last design pass before implementation.
**Companion records:** seven ADRs and three planning notes in this directory.

---

## Contents

1. [Overview](#overview)
2. [Revision 3 changes](#revision-3-changes)
3. [Document set](#document-set)
4. [What exists today](#what-exists-today)
5. [Corrections to the issue](#corrections-to-the-issue)
6. [The seam](#the-seam)
7. [Module boundaries](#module-boundaries)
8. [Derived state](#derived-state)
9. [The candidate chokepoint](#the-candidate-chokepoint)
10. [Read path](#read-path)
11. [Write path](#write-path)
12. [Injection path](#injection-path)
13. [Reconcile status and rebuild](#reconcile-status-and-rebuild)
14. [Where it plugs into each existing component](#where-it-plugs-into-each-existing-component)
15. [Security posture](#security-posture)
16. [Failure modes](#failure-modes)
17. [Operational surface](#operational-surface)
18. [Non-goals honored](#non-goals-honored)
19. [Settled rulings](#settled-rulings)
20. [Open questions for a human](#open-questions-for-a-human)

---

## Overview

CAO's memory subsystem owns three layers today: Markdown topic files hold content,
SQLite holds metadata for filtered query, and an in-process BM25 pass ranks bodies.
This design keeps all three and changes only **which files the content layer points
at** for scopes the operator explicitly maps to an Obsidian vault.

The mechanism is a **vault source and reconciliation adapter** behind the existing
`MemoryService` contract. It introduces two new authorities — a `ScopeBinding`
resolver that answers whether a `(scope, scope_id)` pair is served by the native wiki
or by a mapped vault folder, and a single **candidate chokepoint** through which every
vault read must pass — and one new obligation: the vault is walked only by the
reconciler, never by a query.

Four properties are load-bearing and every decision defers to them:

- **Vault Markdown is canonical.** SQLite rows, BM25 corpora, graph projections,
  access counters and caches are disposable projections that must rebuild
  deterministically from the vault alone.
- **Exactly one content backend per scope.** An unmapped scope keeps the native wiki;
  a mapped scope is served by the vault. There is never a moment where both are
  writable, and divergence is never silent.
- **Two independent policies.** Whether a note may be *indexed* and whether a note may
  be *injected into agent context* are separate and separately defaulted.
- **One chokepoint per exposure class.** Every path that turns a key into vault bytes —
  primary recall, BM25 readback, related-memory expansion, the curator's own recall,
  the graph projection — goes through one function, so a gate cannot be bypassed by a
  caller that forgot it.

## Revision 3 changes

The revision-2 review returned a verdict of **safe to build on, with fixes gated to
specific units** — not a rethink. It could not break the candidate chokepoint from the
recall side. What it did find was a set of read paths that never go through the chokepoint
at all, and two migration details that would have made U2 misbehave forever.

| Change | Driver |
| --- | --- |
| **`promotion_service.plan()` gains `source_kind == 'native'`.** It filtered only on scope, scope_id, `memory_type` and `access_count`; took `file_path` straight off the row; read it with **no confinement**; and its section parser accepts **any** `## ` heading, so an ordinary Obsidian note parses cleanly rather than failing safe. Destination is a persistent system prompt on disk. Placed in **U5**, earlier than the review required — see the note below | N1 [HIGH] |
| **A 16th predicate site: `memory_service.py:1388` `_candidate_keys_for_topic`.** Scope plus scope_id, no `source_kind`, `.limit(200)`, feeding `find_related` and the compiler's candidate set — so a native memory's compile can receive vault keys and write one into a native row's `related_keys`, and vice versa | N3 [MED] |
| **The five-consumer enumeration is demoted to non-normative orientation and replaced, as the enforcement mechanism, by a static assertion**: no module outside `reader.py` may call `open`/`read_text` on a value originating from `memory_metadata.file_path` | reviewer framing, adopted |
| **`ck_related_keys_length` is OMITTED from the rebuilt table**, so U2 changes uniqueness and nothing else | ruling R12 |
| **The U2 idempotence gate is specified as a column-list comparison, not a name check.** A named table-level UNIQUE yields `sqlite_autoindex_<table>_1` and the constraint name is **discarded**, so a name-based gate can never match and would rebuild a shared table on every `init_db()` forever | N5 |
| **Index-recreation ordering relative to `_migrate_memory_indexes` is stated** | N6 |
| **The secret gate now runs at INDEX time (reconcile)**, quarantining a secret-bearing note with a reported finding code. This reverses my revision-2 recommendation | ruling R5 |
| **`mappings[].secret_gate` governs both boundaries**, `reject` by default, `warn` a deliberate per-mapping election, finding reported in either mode; `warn` + `inject: true` warns at config load and is named in `status` | ruling R14 |
| **U5 gets a security review** — it carries the four maintenance-refusal guards that are the only thing preventing unattended de-indexing of a user's vault notes | ruling R13 |
| **U9's `GraphView.meta` distinguishes three causes** for absent lint enrichment rather than conflating a design boundary with a live failure | self-identified |
| **Each new refusal guard gets its own test that passes on the current tree**, because an assertion bolted into a test class that is red **on the implementer's machine** can appear covered while never executing | self-identified |
| **U5's identity-diff becomes a gate, not advice**, with the baseline identities **and the exact invocation that produced them** recorded as a precondition — the local failure set is invocation-dependent (25 from four files in isolation versus 18 from the same files inside the full run), so comparing across invocations would manufacture phantom regressions | supervisor baseline, amended |
| AC10 re-marked after N1; AC9's completeness assertion required to be a hard failure | rulings R5, R6 |

**One placement correction, offered rather than assumed.** The review said "fix N1 before
U7". N1 is placed in **U5** instead, which is strictly earlier, for two reasons. U5 is where
vault `memory_metadata` rows first exist, so any later placement leaves a window in which
`promotion_service` can see them. And the edit is the same class as U5's other four
native-only predicates, so it belongs on the same reviewer checklist rather than in a
different unit's diff. The *exploitable* window opens slightly later — `plan()` requires
`access_count >= 3` (`DEFAULT_MIN_ACCESS_COUNT`), and a vault row cannot reach 3 until
recall exists in U6 — so U5 placement closes the hole before it opens, with margin.

**Mitigating factor on N1, stated because severity and likelihood are not the same
thing.** The vault `Memory` builder defaults an absent `cao.type` to `reference`, and
`PROMOTABLE_TYPES` is `("feedback", "project")`, so an ordinary vault note is not
promotable. Exploitation needs a note carrying an explicit `cao.type: project` or
`cao.type: feedback`. That lowers likelihood and changes nothing about severity: the
destination is a persistent agent system prompt, and `_lesson_text` reads the file with no
confinement at all.

### Revision 2 changes

An independent adversarial review returned five foundation-level defects. What changed:

| Change | Driver |
| --- | --- |
| The injection gate is enforced on the **curator's recall input**, not on the injected block. Revision 1 asserted the gate was total in three places; all three were wrong, because `get_curated_memory_context`'s success path returns the curator's raw block and only its fallback path reaches the gated builder | F1, ruling R1 |
| U6 now names a **vault Memory builder** wired into both readback sites. `_parse_wiki_file` returns `None` for any note without a `## <ISO8601Z>` heading and both readbacks drop falsy results, so revision 1 would have ranked ordinary Obsidian notes and then discarded them — AC3 and AC4 were unreachable | F2 |
| `uq_memory_key_scope` is **widened** with `source_kind`, and `source_kind` is **NOT NULL DEFAULT 'native'** rather than nullable. A nullable discriminator would have made the widened constraint inert for native rows, silently destroying native key uniqueness | F3, ruling R2, plus a new finding of my own |
| Note-path confinement is **inline `realpath` + `startswith`** colocated with each sink, not `safe_join_under_base`, whose `[A-Za-z0-9._-]` charset rejects real folder names. `safe_join_under_base` is retained for the write path only | F4, directed fix D1 |
| The determinism dump is **split** into byte-equal and structural column groups, and the fixture factory gains fixed mtimes | F5, directed fix D2 |
| U9 joins the security-review list, must land after U7, and projects **only `status='indexed'`** notes. Revision 1's `quarantined` node attribute was itself the leak | F12, directed fix D3 |
| Related-memory expansion is **gated**, not declared out of scope, by routing it through the chokepoint | F8 |
| `wiki_lint`, `wiki_healer`, `cleanup_service` and OKF export are named as vault-scope interactions, three of them requiring refusal guards | F9 |
| `forget()` returns a `ForgetResult` with `__bool__`, and both `skills/cao-memory/SKILL.md` copies are named | F11, F10 |
| `ScopeBinding` resolves through the project-alias table; divergence is reported, never silent | F13 |
| Path-derived keys are mapping-relative with a stable path digest, eliminating truncation as a collision source | F14 |
| Vault `Memory.content` has a body budget and populates `token_estimate` | F15 |
| `reader.py` and `writer.py` own every taint-reachable filesystem sink, guard re-asserted inline, bare `str` across boundaries | F16 |
| Hardlinks are refused with an escape hatch | T5-gap |
| The BM25 vault arm reads each candidate once | T2-minor |
| Rulings R3 (`forget()` de-indexes) and R4 (`session`/`federated` unmappable) are closed as settled | R3, R4 |

## Document set

| File | Purpose |
| --- | --- |
| `design.md` | This document — architecture, module boundaries, chokepoint, read/write/inject/reconcile paths. |
| [adr-001-typed-relationship-representation.md](adr-001-typed-relationship-representation.md) | Canonical representation for typed edges, confidence, provenance, proposal status. |
| [adr-002-vault-cardinality.md](adr-002-vault-cardinality.md) | One vault or many in release one. |
| [adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md) | Whole-vault reads versus mapped-folder reads; managed-folder writes. |
| [adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md) | Aliases, embeds, heading links, attachments, duplicate titles, plugin metadata. |
| [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md) | Index eligibility versus automatic context injection, including the curator. |
| [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md) | Stable identity across rename; deterministic rebuild. |
| [adr-007-configuration-surface.md](adr-007-configuration-surface.md) | Where vault config lives and how it validates. |
| [implementation-boundaries.md](implementation-boundaries.md) | Twelve independently shippable units, dependencies, security review flags. |
| [test-strategy.md](test-strategy.md) | Fixture-vault design, per-criterion coverage, exact verification commands. |
| [traceability.md](traceability.md) | Each of the issue's thirteen acceptance criteria mapped to a unit, honestly marked. |

## What exists today

Every claim below was read from source in this worktree, not inferred from the issue.

### Memory service, store and recall contract, scope model, authorization

`src/cli_agent_orchestrator/services/memory_service.py` (3055 lines).

| Concern | Location |
| --- | --- |
| Master switch | `_is_memory_enabled()` line 76 |
| Project identity resolver | `resolve_project_id()` line 210 — override, then normalized `git remote.origin.url`, then `sha256(realpath(cwd))[:12]` |
| Service construction | `MemoryService.__init__` line 268 — `base_dir` and `db_engine` are the only injection points |
| Metadata upsert / delete | `_upsert_metadata()` line 299, `_delete_metadata()` line 389 |
| Caller scope | `resolve_caller_scope()` line 411 |
| Scope id resolution | `resolve_scope_id()` line 438; `_sanitize_scope_id()` line 479 |
| Key sanitization | `auto_generate_key()` line 536, `_sanitize_key()` line 548 — lowercase slug `[a-z0-9-]`, **truncated at 60** |
| Path composition | `_get_project_dir()` line 570, `get_wiki_path()` line 591, `get_index_path()` line 633 |
| Store | `store()` line 662 |
| Human index rewrite | `_update_index()` line 1709 |
| Related-keys lookup | `_related_keys_lookup()` line 1465 |
| Related-memory load | `_load_related_memory()` line 1492 — native-only, re-confines against the native scope dir at line 1528 |
| Recall | `recall()` line 1814; `_apply_sort_and_increment()` line 1952 |
| Metadata recall | `_metadata_recall()` line 2156; readback drops falsy at line 2237 |
| BM25 | `_bm25_tokenize()` 2261, `_bm25_relevance()` 2265 (sole caller is line 1997), `_bm25_search()` 2359; readback drops falsy at line 2470 |
| Corpus discovery | `_get_search_dirs()` line 2474 — includes `cwd_hash` alias dirs at lines 2523-2546 |
| Parsers | `_parse_index()` line 2553, `_parse_wiki_file()` line 2601 |
| Forget | `forget()` line 2658 returning `bool`, `_purge_relationships()` line 2717 |
| Context injection | `get_memory_context_for_terminal()` line 2744, related fan-out at line 2854, render at line 2863, over-cap `break` at line 2870 |
| Curated injection | `get_curated_memory_context()` line 2906 — **success path returns the curator's raw block at line 2972; only the fallback at line 2976 reaches the gated builder** |
| Archive delegators | `export_memories()` line 2982, `import_memories()` line 3002 |

`store()` runs, in order: enabled check, scope and type validation, secret gate for
`federated` only, cross-scope write guard via
`memory_scoring.scope_write_allowed(caller_scope, scope)`, scope-id resolution with a
hard failure when a non-global scope cannot resolve one, key sanitization, tag
normalization, a per-topic `fcntl.flock` on `.<stem>.lock` around the whole
read-modify-write, a temp-file plus `os.replace` atomic publish, an optional deferred
LLM compile, `_update_index()`, then `_upsert_metadata()`. A metadata failure after the
durable writes raises `MemoryPartialWriteError`
(`repair_command = "cao memory repair --apply"`).

`_parse_wiki_file()` is the constraint that shapes the whole read path: it does
`timestamps = re.findall(r"## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", file_content)`
and then `if not timestamps: return None`. An ordinary Obsidian note has no such
heading.

`models/memory.py` defines `Memory`, `MemoryScope` (`global`, `project`, `session`,
`agent`, `federated`), `MemoryType` (`user`, `feedback`, `project`, `reference`), and
the reject-first constrained types `MemoryKey` (`^[a-z0-9-]{1,60}$`) and
`MemoryScopeId` (`^[a-zA-Z0-9._-]{1,128}$`).

`memory_scoring.py` holds `scope_write_allowed`, `SCOPE_PRECEDENCE`,
`validate_sort_by`, `score_memory`, `normalise_bm25_scores`. `memory_format.py` holds
`parse_index_entry`, `normalize_memory_tags`, `TOPIC_HEADER_RE`.

### Terminal injection wiring

`services/terminal_service.py:118` `inject_memory_context(first_message, terminal_id)`
calls `get_curated_memory_context(terminal_id,
task_description=first_message[:200])` at line 135 and prepends the result; line 1233
applies it to the outgoing message. This is the actual injection entry point, and it
reaches the curator's raw output, not the gated builder.

### SQLite metadata schema and migration mechanism

`src/cli_agent_orchestrator/clients/database.py`.

- `MemoryMetadataModel` line 77 — `UniqueConstraint("key", "scope", "scope_id",
  name="uq_memory_key_scope")` at **line 118**, plus `ck_related_keys_length`.
- `MemoryRelationshipModel` line 141, with `RELATIONSHIP_SCOPE_ID_SENTINEL = ""` at
  line 139 and a comment explaining why: SQLite treats `NULL != NULL` in a UNIQUE
  index, so a nullable column makes the dedup index inert. **That comment is the
  precedent this revision applies to `source_kind`.**
- `access_count = Column(Integer, nullable=False, default=0, server_default="0")` is
  the precedent for adding a NOT NULL column with a server default so existing rows
  read correctly with no backfill.
- There is **no `schema_version` table**. Migration is a series of zero-argument,
  self-connecting, idempotent functions invoked from `init_db()` (line 296):
  `PRAGMA table_info(<table>)` gate plus `ALTER TABLE ADD COLUMN`
  (`_migrate_add_access_count` 398, `_migrate_add_last_compiled_at` 426,
  `_migrate_add_related_keys` 448), `CREATE TABLE IF NOT EXISTS`
  (`_migrate_memory_relationships` 472), `CREATE INDEX IF NOT EXISTS`
  (`_migrate_memory_indexes` 377).
- Verified: `memory_metadata` has **no** `_REQUIRED_*_COLUMNS` equality drift-guard
  (that pattern exists only for `workflow_run` and `workflow_run_step` in
  `services/workflow_journal.py`). The only mirrors of its column set are the model,
  the three `PRAGMA table_info(memory_metadata)` gates in `database.py`, and
  `docs/memory.md`.
- SQLite cannot `ALTER` a constraint, so widening `uq_memory_key_scope` is a
  table-rebuild migration, not an `ADD COLUMN`.

### BM25 index and ranking path

There is **no persisted BM25 index**. `rank-bm25>=0.2.2` is a direct dependency and
`_bm25_search()` constructs a `BM25Okapi` **per query** from documents discovered by
`wiki_root.rglob("*.md")` under every directory `_get_search_dirs()` returned.
`_bm25_relevance()` builds the same corpus a second way for `sort_by="score"`,
deliberately over the full document population so IDF is correct; its sole caller is
`_apply_sort_and_increment` at line 1997, which is itself called from four places in
`recall()` (lines 1879, 1900, 1915, 1941). `_bm25_search` reads each candidate file
**twice** — a `memory_type` header peek at line 2422 and the body read at line 2444.

### Typed memory relationships, shipped via #511

`memory_relationship_service.py` is the single authoritative boundary for the
`memory_relationships` table. Closed taxonomies: `VALID_TYPES` line 47,
`VALID_STATUSES` line 48, `CURATION_TERMINAL_STATUSES` line 54, `VALID_ORIGINS` line
55. Bounds `MAX_EDGES_PER_MUTATION = 64`, `MAX_ATTRIBUTES_BYTES = 2048`. Audit event
`relationship_mutation`. Both endpoints must resolve inside the same
`(scope, scope_id)`; `_assert_endpoint_exists` queries `MemoryMetadataModel` at line
286; `_source_updated_map` at line 805. `MemoryRelationshipModel.id` defaults to
`str(uuid.uuid4())` — relationship rows are therefore **not** byte-deterministic, which
constrains the rebuild proof.

### Read-only graph layer and Obsidian export sink, shipped via #348

- `graph/models.py` — `Node`, `Edge`, `GraphView`, `NodeStatus`, `EdgeType`. **`Node.label`
  is the key**, and `GraphView` validates edge endpoints and node-id uniqueness.
- `graph/providers/memory.py` — `MemoryGraphProvider`. Node set from
  `MemoryService._parse_index()`; edges from
  `MemoryRelationshipService.list_relationships(status="active", source_keys=keys)`,
  read **after** `wiki_lint.run_lint`. Lines 82-95 already carry the
  `lint_enabled: False` / `disabled_enrichments` meta precedent for reporting a
  disabled enrichment.
- `graph/cache.py` — `GraphViewCache`, `make_meta`, single-flight with a TTL.
- `graph/sinks/` — `base.py` with `confine_under_export_root`; `obsidian.py`, `okf.py`,
  `graphml.py`. `CAO_GRAPH_EXPORT_ROOT` in `constants.py` lines 308-324.
- `cao_mcp_apps/src/graph/types.ts` line 13 mirrors the edge-type union. This design
  adds no edge type.

### OKF export and import, shipped via #345

`services/memory_archive/` — `base.py`, `okf.py`, `__init__.py` registry.
`_collect_topics` at lines 537-560 enumerates topics through `get_wiki_path` (line
343), i.e. native paths only. Frontmatter key order is fixed (`type`, `title`,
`description`, `tags`, `timestamp`, `created`); `_RESERVED_FILES = {index.md,
manifest.md}`; `history/` is frontmatter-free. `_strip_leading_h1` and
`_escape_structural_markers` are reusable precedents. `okf.py:366` calls
`MemoryService.forget()` on the replace conflict policy.
`python-frontmatter>=1.1.0` and `pyyaml>=6.0` are already direct dependencies.

### Native-only maintenance code that touches memory scopes

Four modules assume the native layout and are reached by ordinary commands:

- `services/wiki_lint.py` — `run_lint` loads metadata rows then reads bodies in the
  loop at lines 1044-1056, where a `base_resolved` containment check
  (`str(resolved).startswith(str(base_resolved) + os.sep)`) `continue`s on any
  `file_path` outside `MEMORY_BASE_DIR`. A vault row is silently skipped, so every lint
  detector sees an empty corpus for that scope.
- `services/wiki_healer.py` — contradiction resolution at lines 502-511 resolves the
  loser through `get_wiki_path`, unlinks it if it exists, rewrites `index.md`, then
  calls `_delete_row(db, loser_key_s, scope, scope_id)` (line 306). For a vault-bound
  scope the native path does not exist so **no file is unlinked**, but the metadata row
  **is** deleted — a silent de-index of a user's vault note on an LLM's judgement.
- `services/cleanup_service.py` — the retention sweep parses `index.md` (line 145
  onward) and calls `forget()` through `_forget_sync` at line 195. For a scope that
  *was* native and is now vault-mapped, the stale native `index.md` still lists the old
  keys, so unattended 90-day retention would de-index vault-backed rows.
- `services/memory_archive/okf.py` — `cao memory export --scope project` on a
  vault-bound scope walks native paths and silently produces an **empty bundle**.

### Base directory, confinement, symlinks, secrets, atomic writes, audit

| Concern | Location and behavior |
| --- | --- |
| Base directory | `constants.py:610` `MEMORY_BASE_DIR = CAO_HOME_DIR / "memory"`; `CAO_HOME_DIR` from the env var at import, chmod `0o700` |
| Absolute-path policy | `resolve_and_validate_path` — expanduser, `realpath`, absolute guard, `BLOCKED_SYSTEM_DIRECTORIES`, existence policy |
| Segment policy | `validate_path_component` — rejects empty, `.`, `..`, NUL, separators, and **anything outside `_SAFE_PATH_COMPONENT_RE = \A[A-Za-z0-9._-]+\Z` at line 138** |
| Containment | `safe_join_under_base` — per-segment validation then `realpath` containment |
| Read-time traversal guard | `_metadata_recall` line 2218 skips an index entry whose realpath is not under `wiki_resolved + os.sep` rather than raising. **This inline shape is the primitive the vault read path adopts** |
| Symlink detection | `memory_reconciliation.py::_first_symlink_component` line 205, `_under` line 197 |
| Secret gate | `services/secret_gate.py` — six ordered named patterns; `scan_for_secrets` returns a pattern **name**; `redact_secrets` for export |
| Atomic write and locking | `utils/atomic_file.py` — `locked_atomic_write`, `locked_atomic_rewrite`, `_file_lock`, `LockTimeoutError`, umask-respecting mode, unique temp plus `os.replace` |
| Audit | `services/audit_log.py` — append-only daily markdown, `O_NOFOLLOW`/`O_CLOEXEC`, `fstat` `0o600` check, day cap, injection sanitization. `AUDIT_EVENT_WHITELIST` is **closed**: an unlisted event is dropped silently |
| CodeQL path-injection precedent | `services/workflow_spec_service.py:162-200` documents that `str.startswith` is a **flow-sensitive, function-local** barrier, so a checked path does not survive a `return`; alerts 166/167/168 were caused by exactly that. `_read_contained_spec_bytes` (line 198) is the fix shape: resolve, re-assert containment **inline**, then own the `isfile`/`open` sinks in the same function. Lines 238-243 add that the guard must be a **single positive** `startswith(base + os.sep)`, not a compound `!= base and not startswith(...)`, whose equal-to-base branch reaches the sink unguarded |
| Reconciliation precedent | `services/memory_reconciliation.py` — `RepairAction` (including `MALFORMED`, `CONFLICT`, `UNSAFE_PATH`), frozen `RepairRecord`/`RepairReport` with stable counts, `discover_canonical_scope_dirs` line 220, dry-run-then-apply |

### Configuration

`~/.aws/cli-agent-orchestrator/settings.json` is the single file.
`settings_service.get_memory_settings()` line 287 layers defaults, the persisted
`memory` object and `CAO_MEMORY_*` env overlays, and does `result.update(saved)` so an
unrecognized nested key passes through. `set_memory_setting()` line 520 has a closed key
whitelist. `config_service.py` exposes `CAOConfig` with a `MemoryConfig` section, an
`ENV_REGISTRY`, and the chain `CLI flag > CAO_* env var > config file > built-in
default`. `docs/configuration.md` owns the Memory configuration surface and U1 updates that
section. `docs/settings.md` is a three-line "this document has moved" stub, not a vault
documentation target; U12 owns the full user-facing vault document.

### Enumerated sets that live outside Python

`tui/src/catalog.rs` holds one row per CLI leaf, with `COMMAND_COUNT: usize = 69`, a
`DISPLAY_ORDER` array whose length is the type, a closed `CommandId` enum, and
exhaustive matches. `test/test_command_catalog_matches_click.py` asserts parity in
**both** directions.

**`skills/cao-memory/SKILL.md` exists twice** — `./skills/cao-memory/SKILL.md` and
`./src/cli_agent_orchestrator/skills/cao-memory/SKILL.md` — held byte-identical by
`test/test_skill_packaging_parity.py` using `filecmp.cmp`, synced by
`python scripts/sync_skills.py`. It is the **agent-facing memory contract**, and its
Forget section states that `memory_forget` removes the topic file.

## Corrections to the issue

**C-1 — "reuse SQLite and BM25 rather than filesystem scanning on every query"
describes a change, not the status quo.** There is no persisted BM25 index;
`_bm25_search()` globs and builds a fresh `BM25Okapi` per call, and `_metadata_recall()`
reads every `index.md` and then every referenced topic file. Satisfying the intent
requires new work: candidates from `memory_metadata`, and only those candidates' files
opened.

**C-2 — the "whole-vault read with managed-folder writes" alternative contradicts
AC2.** A whole-vault read must give a scope to a note in no mapped folder, which means
either a default scope (silent exposure, which AC2 forbids) or no indexing (which is
mapped-folder reads). See
[adr-003-read-and-write-surface.md](adr-003-read-and-write-surface.md).

**C-3 — "typed relationships in CAO-namespaced frontmatter" cannot be symmetrical,**
because CAO must never write an unmanaged note. An edge CAO derives between two
unmanaged notes has no canonical home and stays derived. See
[adr-001-typed-relationship-representation.md](adr-001-typed-relationship-representation.md).

**C-4 (revision 2) — an ordinary Obsidian note cannot become a `Memory` through the
existing parser.** `_parse_wiki_file` requires a `## <ISO8601Z>` heading and returns
`None` without one, and both readback sites drop falsy results at lines 2237 and 2470.
Any design that reuses that parser for vault notes silently returns nothing: a note
would be discovered, ranked, and then dropped. A dedicated vault Memory builder is
mandatory, not an optimisation.

## The seam

The existing code offers one clean insertion point, and it is not `MEMORY_BASE_DIR`.
`_get_search_dirs()` returns *project container directories*, and both read paths then
assume `<container>/wiki/<scope>[/<scope_id>]/<key>.md` plus a sibling `index.md`. A
vault has none of that structure.

The seam is a binding resolver consulted before any path is composed, plus a candidate
chokepoint through which every vault read passes.

```
   memory_store / memory_recall / memory_forget    terminal_service.inject_memory_context
   (MCP, CLI, API)                                              |
              |                                                 v
              v                                    get_curated_memory_context
   +------------------------------+                   |                |
   |        MemoryService         |                   | success        | fallback
   |  store() recall() forget()   |<--- curator ------+                v
   +---------------+--------------+     recall,               get_memory_context_
                   |                    gated at the          for_terminal
                   v                    chokepoint                    |
   +------------------------------+                                   |
   |  vault/binding.py            |  resolves through project_aliases |
   |  resolve(scope, scope_id)    |<----------------------------------+
   |   -> NativeBinding           |
   |   -> VaultBinding(vault_id,  |
   |        root, mapping,        |
   |        index, inject,        |
   |        writable)             |
   +-------+--------------+-------+
   native  |              |  vault
           v              v
  <MEMORY_BASE_DIR>   +-------------------------------------------+
  /.../wiki/          |  vault/reader.py  --  THE CHOKEPOINT      |
  (unchanged)         |  resolve_candidates(binding, ...,         |
                      |      require_injectable: bool)            |
                      |  load_candidate(row) -> Memory            |
                      |  owns every taint-reachable open/isfile,  |
                      |  re-asserts startswith(root + os.sep)     |
                      |  INLINE beside each sink                  |
                      +-------------------+-----------------------+
                                          ^
                                          | populates
              +---------------------------+-----------------------+
              |  vault/reconcile.py  --  the ONLY vault walker    |
              |  scan -> parse -> identity -> links -> upsert     |
              +---------------------------+-----------------------+
                                          v
          memory_metadata(source_kind='vault') . vault_note .
          vault_finding . vault_note_alias .
          memory_relationships(origin='vault')     all derived, all rebuildable
```

Three invariants follow, and all three are testable:

- **Only `vault/scan.py`, called by `vault/reconcile.py`, enumerates vault
  directories.** A test asserts no `rglob`, `glob`, `iterdir`, `walk` or `scandir`
  appears in `reader.py`, `writer.py` or `binding.py`.
- **Every vault read goes through `reader.py`.** A test asserts no module outside
  `services/vault/` opens a path derived from `memory_metadata.file_path` for a
  vault-backed row.
- **`ScopeBinding` is resolved once per public call and threaded down**, so a mid-call
  configuration change cannot split a store across two backends.

## Module boundaries

New package `src/cli_agent_orchestrator/services/vault/`. Nothing lower imports
anything higher.

| Module | Owns | Never |
| --- | --- | --- |
| `config.py` | `VaultConfig`, `VaultSpec`, `FolderMapping`; load-time validation; `root` through `resolve_and_validate_path`; mapping overlap, scope allow-list, inject-requires-index rules | reads a note, touches SQLite, **hands a caller a path it will open** |
| `findings.py` | The closed finding-code vocabulary, `VaultFinding`, and the supported-boundary table as data | I/O |
| `identity.py` | `cao_key` extraction, mapping-relative path-derived fallback with a stable digest, `note_uid` derivation, collision detection | filesystem, SQLite |
| `parser.py` | Bounded frontmatter and body parse; `cao:` validation against the imported taxonomies; user-key preservation region | filesystem, SQLite |
| `links.py` | Wikilink extraction and resolution to `resolved`/`ambiguous`/`dangling`/`excluded`/`unsupported` | filesystem, SQLite |
| `scan.py` | The vault walker: sorted traversal, exclusions, symlink and hardlink refusal, caps, stability check, hashing. **Owns its own `open()` sinks with the containment guard inline** | SQLite, any write |
| `binding.py` | `ScopeBinding` resolution, alias-aware; the single authority on native versus vault | filesystem walk |
| `reader.py` | **The candidate chokepoint.** `resolve_candidates()`, `load_candidate()`, the vault `Memory` builder, the body budget. **Owns every taint-reachable read sink, guard inline, returns bytes or a `Memory`, never a path** | directory enumeration |
| `reconcile.py` | Plan and apply; rename absorption; the full-rebuild path; the report; audit emission | writing to the vault |
| `writer.py` | Managed-folder writes: `safe_join_under_base` composition, frontmatter merge, secret gate, atomic publish, conflict detection, post-write refresh. **Owns every write sink, guard inline** | writing outside `managed_folder` |
| `status.py` | The read-only status projection: counts, freshness, findings, injectable-mapping and orphaned-mapping warnings | any write |
| `migrate.py` | Dry-run-first migration; representable-metadata mapping; lossy report | deleting the native source without an explicit second flag |

**F16 rule, stated once and applied in three modules.** `config.py` validates and
`binding.py` carries configuration, but neither hands a path to a caller that opens it.
`scan.py`, `reader.py` and `writer.py` each own their filesystem sinks, re-assert
`real_path.startswith(root + os.sep)` **inline immediately before the sink**, use the
**single positive** form rather than a compound `!= root and not startswith(...)`, and
pass **bare `str`** across module boundaries rather than `Path`, mirroring
`_read_contained_spec_bytes`. This is not stylistic: `startswith` is a flow-sensitive,
function-local CodeQL barrier and `Path(...)`-wrapping is unrecognised at the sink,
which is precisely what produced alerts 166/167/168 in `workflow_spec_service.py`. The
module split proposed here reproduces that exact shape unless the rule is followed.

Modules outside the package that change: `memory_service.py`,
`graph/providers/memory.py`, `wiki_lint.py`, `wiki_healer.py`, `cleanup_service.py`,
`memory_archive/okf.py`, `clients/database.py`, `memory_relationship_service.py`,
`memory_reconciliation.py`, `settings_service.py`, `config_service.py`,
`cli/commands/memory.py`, `mcp_server/server.py`, `api/main.py`, `tui/src/catalog.rs`,
and both `skills/cao-memory/SKILL.md` copies.

## Derived state

Three new tables, one widened constraint and one new column, all through the existing
idempotent migration idiom invoked from `init_db()`.

**`vault_note`** — one row per indexed or quarantined note.

| Column | Notes |
| --- | --- |
| `note_uid` TEXT PK | Derived digest, not a uuid4 |
| `vault_id` TEXT NOT NULL | Present from release one though only one vault is allowed |
| `scope`, `scope_id` TEXT | Same nullable convention as `memory_metadata` |
| `cao_key` TEXT NOT NULL | The canonical CAO key |
| `vault_relpath` TEXT NOT NULL | POSIX path relative to the vault root |
| `managed` BOOLEAN NOT NULL | Inside `managed_folder` |
| `content_sha256`, `frontmatter_sha256` TEXT | Change detection and rebuild determinism |
| `size_bytes` INTEGER, `mtime_ns` INTEGER | Cheap change gate |
| `status` TEXT NOT NULL | `indexed`, `quarantined`, `excluded`, `unsupported` |
| `last_reconciled_at` DATETIME | The run's single captured timestamp |
| Unique | `(vault_id, scope, scope_id, cao_key)` and `(vault_id, vault_relpath)` |

**`vault_finding`** — `id`, `vault_id`, `vault_relpath`, `code`, `severity`
(`info`/`warn`/`error`), bounded content-free `detail`, `reconcile_run_id`,
`created_at`.

**`vault_note_alias`** — `vault_id`, `former_relpath` (PK with `vault_id`), `cao_key`,
`scope`, `scope_id`, `content_sha256`, `created_at`. Modelled on `ProjectAliasModel`.

### The source_kind column and the widened unique constraint

Ruling R2 widens `uq_memory_key_scope` to `(key, scope, scope_id, source_kind)` so a
native row and a vault row may share a key. Two things this design must get right, the
second of which the ruling did not state and which would otherwise be a silent
regression:

1. **`source_kind` is `TEXT NOT NULL DEFAULT 'native'`, not nullable.** SQLite treats
   `NULL != NULL` inside a UNIQUE index, so a nullable discriminator would make the
   widened constraint **inert for every native row** — native duplicate keys would
   become insertable and an invariant that has held since Phase 2 would silently
   disappear. This is exactly the hazard `RELATIONSHIP_SCOPE_ID_SENTINEL`
   (`database.py:139`) was introduced to avoid, and `access_count`'s
   `nullable=False, default=0, server_default="0"` is the precedent for adding such a
   column without a backfill pass. Revision 1's "NULL or absent means native" was
   wrong and is corrected here.
2. **Widening a UNIQUE constraint in SQLite is a table rebuild.** There is no
   `ALTER ... ADD CONSTRAINT`. The migration creates `memory_metadata_new` with the final
   schema, `INSERT INTO ... SELECT ..., 'native'`, drops the old table, renames, and
   recreates the indexes — inside one transaction. This is the most invasive change in
   the plan, it runs against a table with **no drift guard**, and it is why U2 ships on
   its own with no other content.
3. **`ck_related_keys_length` is OMITTED from the rebuilt table** (ruling R12).
   `database.py`'s own comment states that CHECK applies to **fresh** databases only and
   that existing databases rely on the parse-side cap in `_parse_related_keys`. Omitting
   it therefore makes the rebuilt table behave identically to the one it replaces, so U2
   changes uniqueness and nothing else — which is what a constraint-widening migration
   should do. Two alternatives were rejected: truncating over-long values via `substr`
   (silently destroying data during a uniqueness migration is the one thing it must never
   do) and pre-scanning then refusing (still blocks startup).
4. **The idempotence gate is a column-list comparison, not a name check** (N5). A named
   table-level `UNIQUE` in SQLite yields an index called `sqlite_autoindex_<table>_1` with
   `origin='u'`, and **the constraint name `uq_memory_key_scope` is discarded** — it does
   not survive into `PRAGMA index_list`. A name-based gate can therefore never match, and
   the migration would rebuild a shared table on **every** `init_db()`, forever. The gate
   must instead enumerate `PRAGMA index_list(memory_metadata)`, keep the rows with
   `origin='u'`, and compare each one's `PRAGMA index_info` column list against
   `('key', 'scope', 'scope_id', 'source_kind')`. Present and matching means already
   migrated.
5. **Index-recreation ordering must be stated** (N6). The three secondary indexes are
   `idx_memory_scope`, `idx_memory_updated` and `idx_memory_type`, created at
   `database.py:386`, `:389` and `:392` by the **separate** idempotent
   `_migrate_memory_indexes()` using `CREATE INDEX IF NOT EXISTS`. Whether the rebuild
   must recreate them depends on its position inside `init_db()`: if the rebuild runs
   **before** `_migrate_memory_indexes()`, that function recreates them and the rebuild
   need not; if it runs **after**, the rebuild must recreate all three itself or they are
   silently lost until the next process start. U2 must pin the order explicitly and assert
   the three indexes exist after the migration, on both a fresh and a pre-existing
   database.

**Every query that filters `memory_metadata` on key plus scope must gain the
`source_kind` predicate**, or `.first()` will match an arbitrary one of two rows.
Enumerated by path and line so none is missed:

| Site | What it does |
| --- | --- |
| `memory_service.py:341` (`_upsert_metadata`) | Existence probe before insert-or-update |
| `memory_service.py:395` (`_delete_metadata`) | Row delete |
| `memory_service.py:1271` (`compact`) | Scope filter. **Not 1275** — that line is only the optional `key ==` clause, so a revision-2 mis-citation would have left the real predicate unpatched |
| `memory_service.py:1480` (`_related_keys_lookup`) | `key.in_(...)` related lookup |
| `memory_service.py:2089` (`_enrich_access_counts`) | `key.in_(...)` usage enrichment |
| `memory_service.py:2126` (`_increment_access_count`) | Per-key access bump |
| `memory_reconciliation.py:948` (`_repair_metadata`) | Native repair upsert |
| `memory_relationship_service.py:286` (`_assert_endpoint_exists`) | Endpoint existence check |
| `memory_relationship_service.py:805` (`_source_updated_map`) | `key.in_(...)` staleness basis |
| `wiki_healer.py:270` | Heal read |
| `wiki_healer.py:317` | Heal read |
| `wiki_healer.py:453` | `key == k` heal read |
| `wiki_healer.py:681` | Heal read |
| `memory_service.py:1388` (`_candidate_keys_for_topic`) | **N3.** Scope plus scope_id, no `source_kind`, `.limit(200)`. Feeds `find_related` and the LLM compiler's candidate set, so a **native** memory's compile can receive **vault** keys as related candidates and write a vault key into a native row's `related_keys` — and the reverse. That contamination then flows into `_expand_related`'s legacy fallback at `:1691-1693` |
| `promotion_service.py:136` (`plan`) | **N1 [HIGH].** Filters only on `scope=='agent'`, `scope_id`, `memory_type.in_(PROMOTABLE_TYPES)` and `access_count >= min`. Takes `file_path` straight off the row; `_lesson_text` then does `wiki_path.read_text()` with **no confinement**; the section parser is `if line.startswith("## ")`, so **any** h2 matches and an ordinary Obsidian note parses cleanly instead of failing safe on the absent `## <ISO8601Z>` heading. Destination is `apply_deltas` into a profile's learned-patterns section — a **persistent system prompt on disk** |
| `memory_reconciliation.py:608` (`_load_rows`) | `db.query(MemoryMetadataModel).all()` — **no filter at all**, feeding `cao memory repair` |
| `wiki_lint.py:1002-1004` | Metadata row load for every detector |

**Sixteen sites, seventeen counting `wiki_lint`.** Revision 2 said thirteen and
mis-cited one line; since U2's spec requires the list to be worked through mechanically, a
wrong line number is a missed predicate.

Where the correct behavior is "this native-only code must not see vault rows at all" —
`memory_reconciliation` (both sites), all four `wiki_healer` sites, `wiki_lint`'s row load
and `promotion_service` — the predicate is `source_kind = 'native'`. **R15 assigns all
seventeen predicates to U2**, including those overlapping the later U5 maintenance work.
Where the caller is binding-aware, the predicate is the binding's kind.
`_candidate_keys_for_topic` is the one site that is neither: it must filter to the **same**
backend as the topic it is computing candidates for, because its purpose is intra-corpus
relatedness.

**`promotion_service` is native-only by intent** — a learned lesson is CAO-authored, not
user-authored — so `source_kind = 'native'` is the correct fix rather than a temporary
guard. If vault-backed agent lessons are ever wanted, they must arrive through
`resolve_candidates(require_injectable=True)` like every other vault read, and that is a
deliberate future decision rather than something a missing predicate grants by accident.

**`memory_metadata` mirrors to update:** the model, a new migration function, and
`docs/memory.md`. Verified there is no `_REQUIRED_*_COLUMNS` equality guard on this
table, so nothing else fails loudly if a mirror is missed — which is why the list above
is exhaustive rather than indicative.

`memory_relationships` gains no column and one **origin value** `vault` — added to
`VALID_ORIGINS` (`memory_relationship_service.py:55`), the enumerating comment
(`database.py:169`) and `docs/memory.md:301`, and **not** to
`_backfill_legacy_related_keys`, whose source text is hashed by a drift guard at
`test/clients/test_memory_relationships_migration.py:119`. No new `EdgeType`, so
`cao_mcp_apps/src/graph/types.ts` is untouched.

## The candidate chokepoint

Revision 1 spread vault reads across the primary recall arm, the BM25 readback, the
related-expansion helper and the graph provider. Three separate gate-bypass findings
(F1, F8, F12) were all instances of one structural problem: several paths turned a key
into vault bytes, and only one had the gate.

Revision 2 collapses them into one function in `reader.py`:

```
resolve_candidates(
    binding,                     # VaultBinding, already alias-resolved
    *, keys=None, scope, scope_id,
    require_injectable: bool,    # derived, no default
) -> list[VaultCandidate]
```

Rules:

1. The query is always `memory_metadata` joined to `vault_note`, filtered on
   `source_kind = 'vault'`, the binding's `(scope, scope_id)`, and
   `vault_note.status = 'indexed'`. Quarantined, excluded and unsupported notes can
   never be candidates, by construction.
2. `require_injectable` is **derived from the caller's context, never optional**: it is
   `True` for the deterministic injection builder, `True` for a recall whose terminal
   context resolves to the injection curator, and `False` for an ordinary recall. It
   has no default, so a call site cannot omit it.
3. `load_candidate(candidate)` re-asserts `real_path.startswith(root + os.sep)` inline,
   opens the file, and returns a built `Memory` — never a path.
4. **The invariant is enforced statically, not by a list.** Revision 2 enumerated five
   consumers — `_metadata_recall`'s vault arm, `_bm25_search`'s vault arm,
   `_bm25_relevance`'s vault corpus, related-memory expansion in recall and injection, and
   the graph provider's node set. That enumeration was the weak part of the claim: it is
   only as good as the sweep behind it, and the revision-2 review found a sixth and
   seventh read path (`promotion_service`, `_candidate_keys_for_topic`) the sweep had
   missed. The list is therefore retained as **non-normative orientation only** — the
   consumers that exist today — and enforcement moves to a static assertion:

   > No module outside `reader.py` may call `open` or `read_text` on a value originating
   > from `memory_metadata.file_path`.

   It sits beside the existing no-`rglob`/`glob`/`iterdir`/`walk`/`scandir` invariant in
   `test_no_enumeration_outside_scan.py`. This is the form in which "provable rather than
   asserted" is actually provable: a new read path added by a future author fails the
   assertion instead of quietly joining an out-of-date list.

This is what makes the ADR-005 gate total rather than asserted, and it is why F8 is gated
rather than declared out of scope. N1 and N3 are the evidence that the static form was
necessary: both are pre-existing read paths, and neither would have been caught by
re-reading the enumeration.

## Read path

Native-bound scopes are byte-identical to today. For a vault-bound scope:

1. `recall()` resolves `ScopeBinding` once per scope in play.
2. **Candidates come from SQLite through the chokepoint**, never the filesystem.
3. **Each candidate is re-confined at its sink.** `reader.load_candidate` computes
   `os.path.realpath` and applies the single positive
   `startswith(vault_root_real + os.sep)` guard immediately before `open()` — the
   `_metadata_recall:2218` shape, not `safe_join_under_base`. A row that fails is
   skipped, a `warn` finding is recorded, and recall continues. `safe_join_under_base`
   is **wrong for the read path**: `_SAFE_PATH_COMPONENT_RE` is `\A[A-Za-z0-9._-]+\Z`,
   which rejects a folder named `CAO Design`, a note named `Don't Panic.md`, and
   anything non-ASCII, so using it would fail at config load on any real vault. It
   remains correct for the write path, where every segment is a config-validated
   `managed_folder` component or a `[a-z0-9-]` key.
4. **The vault `Memory` builder replaces `_parse_wiki_file` on the vault arms.** This is
   C-4. Mapping:

   | `Memory` field | Vault source |
   | --- | --- |
   | `id` | The derived `note_uid` digest |
   | `key` | `cao_key` |
   | `memory_type` | `cao.type` if present, else `reference` |
   | `scope`, `scope_id` | The mapping's, never parsed from the note |
   | `file_path` | Absolute, internal only, never returned by the API |
   | `tags` | Frontmatter `tags` through `normalize_memory_tags` |
   | `created_at` | `cao.created`, else frontmatter `created`, else the note's mtime — never `now()` |
   | `updated_at` | The note's mtime |
   | `content` | Body prose after the frontmatter, leading H1 stripped (the OKF `_strip_leading_h1` precedent), **capped per the body budget below** |
   | `access_count` | From the metadata row, unchanged |
   | `source_kind`, `source_path`, `indexed_at`, `index_freshness`, `content_truncated` | New fields |

   No `## <ISO8601Z>` heading is required or expected. The builder is wired into
   **both** readback sites — `_metadata_recall`'s vault arm and `_bm25_search`'s vault
   arm — so neither reaches the `if memory:` drop at lines 2237 and 2470 with a `None`.
5. **Body budget (F15).** `memory.vault.max_recall_body_chars`, default 4096. A longer
   body is truncated at a character boundary with a trailing marker and
   `content_truncated=True`. `token_estimate` is populated as `len(content) // 4`,
   matching `store()`'s convention. Without this, `max_note_bytes` of 262144 times a
   `limit` of 10 is roughly 2.5 MB returned to an agent; and the injection renderer at
   line 2863 formats `- [scope] key: content` and `break`s at line 2870 once over cap,
   so one large note would silently truncate the entire scope's block. The cap is
   applied in the builder so the renderer never sees an oversized line and the native
   `break` semantics stay unchanged.
6. **Freshness is compared, not assumed.** The note's `(size_bytes, mtime_ns)` is
   stat-checked; a mismatch stamps `index_freshness = "stale"` and increments a counter
   surfaced by `status`. No implicit reconcile — release one has no watcher.
7. **Related-memory expansion is gated, not dropped (F8).** `_load_related_memory`
   (line 1492) is native-only: it composes a native path through `get_wiki_path` and
   re-confines against the native scope directory at line 1528, so for a vault scope it
   always returns `None` — silently, and in contradiction to the issue's Query Behavior
   item 2, which lists related-memory expansion as part of the retained contract. The
   fix routes vault related-expansion through `resolve_candidates(keys=[...])`. Because
   the chokepoint applies the same `status='indexed'` and `require_injectable`
   predicates as the primary path, the injection-side fan-out at line 2854 — which
   loads by key and would otherwise bypass the gate entirely — is closed by the same
   change rather than by a second patch.
8. **`_apply_sort_and_increment` does change (F6).** Revision 1 claimed it did not
   while also parameterizing `_bm25_relevance`'s corpus discovery. Both cannot be true:
   `_apply_sort_and_increment` (line 1952) is `_bm25_relevance`'s **only** caller, at
   line 1997. It therefore gains a trailing keyword parameter carrying the resolved
   sources, defaulted so the four call sites at lines 1879, 1900, 1915 and 1941 remain
   valid unchanged, and forwards it. The **ranking mathematics** in `memory_scoring` is
   untouched; that is the narrower and accurate claim.
9. **The BM25 vault arm reads each file once (T2-minor).** The native arm reads twice —
   a `memory_type` header peek at line 2422 and the body at line 2444. The vault arm
   takes `memory_type` from the metadata row, so one read per candidate.
10. **IDF correctness is preserved.** The vault corpus provider yields the whole indexed
    scope, not just the matches, because a candidate-only corpus collapses IDF when
    every candidate contains the query term — the property `_bm25_relevance`'s
    docstring already calls out.
11. `Memory`, `MemorySummary`, `MemoryDetail` and the MCP `memory_recall` mapping gain
    `source_kind`, `source_path`, `indexed_at`, `index_freshness` and
    `content_truncated`. `file_path` stays excluded from the API summary, so the
    vault-relative path is what leaves the process.

**Honest statement of what stays byte-identical (F7).** Revision 1 said
`_get_search_dirs` was "superseded", which is not what should happen.
`_get_search_dirs` is **retained unchanged**; a new `_resolve_sources()` is added
beside it and returns native container directories (by calling `_get_search_dirs`) plus
vault candidate providers. Every new parameter on an existing method is a **trailing
keyword argument with a native-preserving default**, and a call that does not use it
keeps its current form rather than passing an explicit `None`. That is what keeps the
existing recall tests passing unmodified, which is stronger evidence than an edited
assertion. Before changing any injection-adjacent signature, grep for code that
**replaces** the function rather than calling it: there is a module-level monkeypatch
precedent in this test suite that assigns a two-parameter lambda over
`terminal_service.inject_memory_context`.

## Write path

`memory_store` for a vault-bound scope:

1. Resolve `ScopeBinding`. If the mapping is not `writable`, **fail** naming the
   mapping — never fall back to the native wiki, which would be the two-replicas
   outcome the issue forbids.
2. Run the existing checks unchanged and in order: enabled, scope and type validation,
   `scope_write_allowed(caller_scope, scope)`, scope-id resolution, `_sanitize_key`.
3. Run the secret gate. `mappings[].secret_gate` defaults to `reject`. The log carries
   the pattern **name** only.
4. Compose the target as
   `safe_join_under_base(vault_root, *managed_folder_segments, f"{key}.md")`. This is
   the one place `safe_join_under_base` is correct: `managed_folder` segments are
   config-validated against its charset and the key is already `[a-z0-9-]`.
5. Take the lock and publish through `locked_atomic_rewrite`, whose lock file lives in a
   CAO-owned location — no `.lock` or `.tmp` debris in the user's vault for Obsidian to
   display and cloud sync to replicate.
6. **Frontmatter merge, not overwrite.** Every top-level key other than `cao` is
   re-emitted from the original frontmatter **text region** verbatim rather than
   round-tripped through YAML, so comments, key order and plugin inline fields survive.
   Body content is appended in the same `## <ISO8601Z>` section form `store()` already
   uses, preserving the append-only ordering rule and `_occurred_at_would_clamp`.
7. **Conflicts fail visibly.** The note's `content_sha256` is compared under the lock
   against the `vault_note` row. A mismatch is refused with a distinct error naming the
   path and `cao memory vault reconcile`. Never resolved by overwriting.
8. Refresh derived state for that one note in the same call — `vault_note`,
   `memory_metadata`, and a `replace_set` of its `origin="vault"` edges. A refresh
   failure after the durable write raises the existing `MemoryPartialWriteError` shape
   so `cao memory repair` and the MCP `partial_write` response keep working.
9. Emit `memory_stored`; vault specifics ride as content-free fields.

### forget() de-indexes, and says so

Settled by ruling R3. `forget()` on a vault-bound scope **does not delete a vault
file**. It drops the `memory_metadata` row, sets `vault_note.status = 'excluded'`,
purges the note's `origin="vault"` edges, and reports the path the human may delete in
Obsidian.

Revision 1 claimed `forget()` "keeps its meaning", which was wrong in a way that
matters (F11): it returns `bool` (line 2658) across seven call sites, so there was no
channel to tell the caller what happened, and `mcp_server/server.py:2047-2076` returns
`{"deleted": true}` under a docstring that says "Deletes the wiki topic file" — the
agent would be told a file was deleted when it was not.

The contract therefore changes, minimally:

- `forget()` returns a frozen `ForgetResult` whose `__bool__` returns the previous
  boolean, so all seven existing call sites — `cli/commands/memory.py:215` and `:256`,
  `api/main.py:6849` and `:6904`, `mcp_server/server.py:2066`,
  `cleanup_service.py:195`, `memory_archive/okf.py:366` — keep behaving identically
  with no edit.
- It adds `action` (`"deleted"` | `"deindexed"` | `"absent"`), `source_kind`, and
  `path` (vault-relative for a vault note).
- MCP `memory_forget` keeps `deleted` for compatibility, adds `action` and `path` as
  authoritative, and **its docstring stops claiming the file is deleted**.
- **Both `skills/cao-memory/SKILL.md` copies** change their Forget section, edited via
  `python scripts/sync_skills.py` so `test/test_skill_packaging_parity.py`'s
  `filecmp.cmp` stays green. This file is the agent-facing contract; leaving it saying
  the topic file is removed would make every agent's mental model wrong.

## Injection path

This is the section revision 1 got wrong, in three places. The correction is ruling R1.

**What revision 1 asserted:** that filtering inside `get_memory_context_for_terminal`
made the injection gate total.

**What the code does.** `terminal_service.inject_memory_context` (line 118) calls
`get_curated_memory_context` (line 135) and prepends its result. That function's
**success** path — when a `memory_manager` curator terminal exists in the session and is
idle — dispatches the task description to the curator and returns the curator's own
`<cao-memory>` block verbatim (line 2972). It reaches
`get_memory_context_for_terminal` **only on its fallback path** (line 2976). So a gate
placed in the builder is bypassed on every successful curated injection.

**The fix: gate the curator's recall input.** The curator, when producing an injection
block, may only recall `inject:true` notes. Whatever it emits is then injectable by
construction, regardless of how it paraphrases.

Mechanism, reusing existing discriminators:

1. `get_curated_memory_context` already locates the curator with
   `_find_context_manager_terminal(session_name)`; a curator terminal is identified by
   its `agent_profile`.
2. A recall whose resolved terminal context is that curator passes
   `require_injectable=True` into `resolve_candidates`. Because the flag has no default
   and the chokepoint is the only way to reach vault bytes, the curator physically
   cannot recall a non-injectable vault note.
3. **Fail closed when the curator is unidentifiable.** If the curator terminal cannot be
   resolved to a profile, the injection path does not dispatch to it and falls back to
   the deterministic builder, which is gated.
4. The deterministic builder keeps its own filter, including on the related fan-out at
   line 2854, now via the chokepoint.

**Why not post-filter the curator's returned block.** Explicitly rejected: the curator
is an LLM that paraphrases and synthesises. A key-based or path-based filter over
generated text cannot catch confidential content restated in the model's own words, so
it would look like a control while providing none. Gating the input is the only
placement where the guarantee is structural.

**Forward risk, not on this base.** A frozen-memory injection arm designed elsewhere
deliberately bypasses `MemoryService` for determinism. Any such arm must route through
the same chokepoint with `require_injectable=True`, and must not skip
`_is_memory_enabled()` — the first line of `get_curated_memory_context`. Recorded here
so the next author of that arm inherits the constraint.

## Reconcile status and rebuild

Three explicit operations. No watcher; the only implicit refresh is the single note a
store just wrote.

**`reconcile`** — dry-run by default, `--apply` to write, mirroring `cao memory repair`.

1. Capture one `reconcile_run_id` and one `run_started_at`. Every timestamp the run
   writes is that value or comes from the file.
2. `scan.py` walks each mapping's folder in sorted relative-path order, applying
   exclusions, refusing symlinked components and hardlinks, enforcing `max_note_bytes`
   and `max_notes`, and running the two-stat stability check.
3. `parser.py` and `identity.py` derive `cao_key`, `note_uid` and the `cao:` block.
   Collisions quarantine **both** notes.
4. **The secret gate runs here, at index time** (ruling R5). `scan_for_secrets` is applied
   to the note body. Under the mapping's `secret_gate` setting: `reject` (the default)
   quarantines the note with a `secret_detected` finding carrying the matched **pattern
   name only**, never the matched bytes; `warn` indexes the note and records the same
   finding without quarantining. This reverses my revision-2 recommendation that reads not
   be gated at all. Index time is the right placement because the gate is a pure function
   of content: it costs nothing per read, re-runs only when the content hash changes, and
   stays deterministic under `rebuild`. My original objection — gating reads is invisible
   data loss — is answered by the finding being reported in `status`, not by leaving the
   note exposed.
5. `links.py` resolves wikilinks within the scope only.
6. Diff against `vault_note`: unchanged, created, updated, renamed, deleted,
   quarantined, excluded, unstable-skipped.
7. Apply: upsert `vault_note` and `memory_metadata` (`source_kind='vault'`), insert
   `vault_finding` rows, `replace_set` the `origin="vault"` edges per source note, and
   delete rows for notes that disappeared. **U5 also retracts a note's projection whenever
   it leaves `indexed`**: delete its vault `memory_metadata` row and clear its
   `origin="vault"` edges; the reverse transition restores both.
8. Emit `vault_reconcile_completed`, `vault_note_quarantined` and
   `vault_secret_quarantined`, all of which **must** be added to `NOWAIT_AUDIT_EVENTS` —
   the whitelist is closed and an unlisted event is dropped silently, which would make a
   content-free-audit assertion pass vacuously.
9. Return a frozen report with stable counts, shaped like
   `memory_reconciliation.RepairReport`, with a `has_unresolved` property.

**`status`** — pure read. Per mapping: note count by status, finding count by code,
oldest and newest `last_reconciled_at`, count of rows whose stat no longer matches the
recorded hash inputs, an explicit warning naming every mapping with `inject: true`, an
**orphaned-mapping** warning for any mapping whose `scope_id` resolves to no known
project id or alias, and a warning when a mapped `project` `scope_id` was resolved from
a cwd hash rather than pinned or git-derived.

**`rebuild`** — the AC9 proof. Delete `vault_note`, `vault_finding`,
`vault_note_alias`, `memory_metadata WHERE source_kind = 'vault'` and
`memory_relationships WHERE origin = 'vault'`, then a full `reconcile --apply`. The
deletes are scoped by `source_kind` and `origin` so native rows and other producers'
edges cannot be touched. Rebuilding also resets `access_count` and `last_accessed_at`
for vault rows — correct, they are derived — which is stated in the command's help.

Determinism conditions are in
[adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md);
the proof, including which columns compare byte-equal and which compare structurally,
is in [test-strategy.md](test-strategy.md).

## Where it plugs into each existing component

| Existing component | Change |
| --- | --- |
| `memory_service.store()` | Branch on `ScopeBinding` after `_sanitize_key`; vault branch delegates to `writer.py`. Native branch **byte-identical** |
| `memory_service.recall()` | `_get_search_dirs()` **retained unchanged**; new `_resolve_sources()` added beside it |
| `memory_service._metadata_recall()` | Vault arm iterates chokepoint candidates and uses the vault `Memory` builder. No `index.md` is written for vault scopes |
| `memory_service._bm25_search()` | Vault arm takes its corpus from the chokepoint and reads each file once |
| `memory_service._apply_sort_and_increment()` | **Does change** — gains a trailing keyword parameter carrying resolved sources and forwards it to `_bm25_relevance`, whose sole caller it is (line 1997) |
| `memory_service._load_related_memory()` | Vault arm routes through the chokepoint; native arm unchanged |
| `memory_service.forget()` | Vault arm de-indexes; returns `ForgetResult` with `__bool__` |
| `memory_service.get_memory_context_for_terminal()` | Injection filter via the chokepoint with `require_injectable=True`, including the related fan-out at line 2854 |
| `memory_service.get_curated_memory_context()` | **Changes.** Curator identification must be resolvable or the path falls back; the curator's own recalls are gated at the chokepoint. Its `_is_memory_enabled()` first line must not be bypassed |
| `memory_service.export_memories()` | **Refuses** a vault-bound scope with a clear error. `okf.py`'s `_collect_topics` (lines 537-560) walks `get_wiki_path` natives, so today it would silently emit an **empty bundle**. The vault is already Markdown, so refusal with a pointer to the vault is the honest release-one behavior |
| `clients/database.py` | Three tables, one NOT NULL column, one table-rebuild constraint widening, all wired into `init_db()` |
| `memory_relationship_service.py` | One new `VALID_ORIGINS` value; `_assert_endpoint_exists` (286) and `_source_updated_map` (805) gain the `source_kind` predicate |
| `graph/providers/memory.py` | Node set from the chokepoint (`status='indexed'` only); `is_vault` attr; **no path attribute and no `quarantined` node** — see below. Preserve the existing constraint that the relationship read happens **after** `run_lint` |
| `wiki_lint.py` | U2's `source_kind = 'native'` metadata predicate makes the native-only boundary explicit; without it vault rows are dropped incidentally by `base_resolved` containment and every detector sees an empty corpus. U9 reports the boundary through `GraphView.meta` with **three distinguishable causes**, not one — see below |
| `promotion_service.py` | U2's `source_kind = 'native'` predicate excludes vault rows. Without it, a vault note carrying an explicit `cao.type: project` or `feedback` and three recalls is read with **no path confinement** and promoted into a profile's learned-patterns section — a persistent agent system prompt. Native-only by intent, so the predicate is the correct fix rather than a temporary guard |
| `wiki_healer.py` | U2 owns its four `source_kind = 'native'` metadata predicates; U5 separately refuses vault-bound scopes early. Otherwise `cao memory heal --apply` deletes the metadata row at `_delete_row` (line 306) for a note whose native path does not exist — a silent de-index of a user's vault note on an LLM's judgement |
| `cleanup_service.py` | **Refuses** vault-bound scopes. Otherwise the retention sweep parses a stale native `index.md` and calls `forget()` at line 195, de-indexing vault rows unattended at 90-day retention for a scope that used to be native |
| `memory_reconciliation.py` | `discover_canonical_scope_dirs` skips vault-bound scopes, and its metadata query (948) gains `source_kind = 'native'` |
| `graph/sinks/obsidian.py` | Unchanged, plus one validation rule: an export `dest` resolving under a configured vault root is refused, so a projection can never be mistaken for a source |
| `settings_service.py` / `config_service.py` | `get_vault_config()`, `MemoryConfig.vault`, one env var |
| `cli/commands/memory.py` | New `vault` sub-group |
| `tui/src/catalog.rs` | New `CommandId` variants, `COMMAND_COUNT` 69 to 74, `DISPLAY_ORDER`, both exhaustive matches, doc-comment counts |
| `mcp_server/server.py` | `memory_recall` result gains the source fields; `memory_forget` gains `action`/`path` and its docstring is corrected. No new MCP tool |
| `api/main.py` | `MemorySummary`/`MemoryDetail` gain the source fields; read-only `GET /memory/vault/status` |
| `skills/cao-memory/SKILL.md` **and** `src/cli_agent_orchestrator/skills/cao-memory/SKILL.md` | Forget wording corrected; vault section added. Byte-identical via `python scripts/sync_skills.py` |
| `docs/configuration.md`, `docs/memory.md`, new `docs/obsidian-vault.md` | U1 owns the `docs/configuration.md` Memory section, including the `CAO_MEMORY_VAULT_ENABLED` row and `memory.vault` block; U12 owns the full user-facing vault document; `docs/memory.md` records the `vault` origin. `docs/settings.md` is only a moved-document stub |

### Graph exposure equals recall exposure

Directed fix D3, and it found a defect of revision 1's own. Revision 1 said vault nodes
would "gain `is_vault` and `quarantined`" attributes. **The `quarantined` attribute was
the leak.** `Node.label` is the key, and a path-derived key encodes the folder path and
filename, so projecting a quarantined note publishes the structure and title of a note
that is deliberately **not** recallable — to `GraphView`, the MCP Apps `ui://cao` views
and the browser dashboard, with no policy gate in between.

The rule is therefore: **the graph projects exactly what recall can reach, and nothing
more.** U9's node set comes from the same chokepoint with the same `status='indexed'`
predicate, so graph exposure is provably equal to recall exposure rather than a
superset. No `vault_relpath` or any path-shaped value appears in node or edge
attributes. Quarantined notes are visible only in `cao memory vault status`, an
operator-invoked surface. U9 must land **after** U7 so the chokepoint and its gate exist
before anything projects from it — the second hard ordering constraint in this plan.

### Absent lint enrichment has three causes and must report which

Revision 2 said U9 would report lint enrichment as "unavailable" for vault-bound scopes,
reusing the `disabled_enrichments` precedent. That conflates a deliberate design boundary
with a live failure, and a reader could not tell them apart. `GraphView.meta` must carry a
discriminated reason:

| Cause | Meaning |
| --- | --- |
| `disabled_by_setting` | `is_memory_lint_enabled()` is false — the existing, already-reported path |
| `unavailable_vault` | This scope is vault-backed, so `wiki_lint`'s `MEMORY_BASE_DIR` containment check drops its rows by construction. A **design boundary**, permanent in release one |
| `failed` | `run_lint` raised and was caught — the existing `meta["lint_error"]` path |

The distinction is not cosmetic. `graph/providers/memory.py::_build` wraps
`await wiki_lint.run_lint(...)` in a broad `except Exception` that degrades to a lint-free
graph, which is correct behavior in isolation; but if lint is failing for native scopes
too, that same silence hides it. Reporting the cause means a vault user sees a boundary and
a native user sees a fault, rather than both seeing the same blank.

**Why this does not weaken correctness anywhere else.** The vault design never *depends*
on lint being right: `wiki_lint`, `wiki_healer` and `cleanup_service` are handled by
**refusal and exclusion**, not by graceful degradation. Correctness therefore depends only
on the `source_kind` predicate being applied, never on the lint subsystem behaving. That is
a deliberate architectural choice, and it is what makes this feature's correctness
independent of lint's health in either direction: it depends only on the `source_kind`
predicate being applied, never on lint being right. That reasoning is why the F9 finding was
downgraded, and it holds whatever state the lint subsystem is in — which matters, because
the premise that it was failing turned out to be false. **CI on `main` at `6c890ca` is
green.** The architecture was not chosen to work around a broken subsystem and must not be
justified that way.

## Security posture

The vault boundary is a new trust boundary: files CAO did not write, in a directory it
does not own, frequently replicated by a third-party sync service, sometimes shared with
other people. Ten controls, each mapped to an existing primitive.

1. **Opt-in and confined.** `memory.vault.enabled` defaults false. `root` passes
   `resolve_and_validate_path`, so the blocked-system-directory policy applies.
2. **Read-path confinement is inline at every sink.** `os.path.realpath` then a single
   positive `startswith(root + os.sep)`, immediately before the `open()`, in the same
   function — the `_metadata_recall:2218` shape and the `_read_contained_spec_bytes`
   discipline. `safe_join_under_base` is deliberately **not** used here: its
   `\A[A-Za-z0-9._-]+\Z` segment charset would reject the folder and note names real
   vaults contain, so using it would trade a working feature for a false sense of
   rigour. It is retained for the write path, where segments are config-validated or
   `[a-z0-9-]`.
3. **CodeQL discipline (F16).** `scan.py`, `reader.py` and `writer.py` own **every**
   taint-reachable `open`/`isfile`/`write`; the guard is re-asserted inline beside each
   sink in the single positive form; bare `str` crosses module boundaries. Without this
   the module split reproduces exactly the shape behind alerts 166/167/168 — a guard in
   `config.py` whose checked state CodeQL drops at the `return`, and a
   `Path(...)`-wrapped value the query does not recognise at the sink.
4. **Symlinks refused below the root.** `root` itself may be a symlink, resolved once at
   load. Any symlinked component beneath it refuses the note with `symlink_refused`,
   reusing `_first_symlink_component`.
5. **Hardlinks refused, conservatively (T5-gap).** `_first_symlink_component` cannot see
   a hardlink: a hardlink inside the vault to a file outside it is indistinguishable
   from a regular file, and unlike a copy it **tracks future changes to the target**, so
   a hardlink to a credentials file keeps leaking as that file is rotated. A candidate
   whose `st_nlink > 1` is refused with `hardlink_refused` (warn). Residual, stated:
   `st_nlink` says a second link exists, not where it is, so this is a conservative
   refusal and legitimate in-vault hardlinks are refused too — hence the per-mapping
   `allow_hardlinks` escape hatch, default false.
6. **Scope isolation is unchanged and narrowed.** Mappings may not overlap, so a note
   has exactly one scope. `federated` and `session` are not mappable (settled, R4).
   Cross-scope edges are dropped by the existing relationship-service invariant.
7. **Untrusted parsing is bounded.** Frontmatter is size-capped before parsing, parsed
   with a safe loader, and refused if it uses YAML anchors or aliases. Link count,
   `cao.links` length and body size are capped. Over-bound notes are quarantined with a
   code, never truncated silently.
8. **Two independent exposure policies, one chokepoint.** Index eligibility and
   injection eligibility are separate config keys, injection default-deny, and both are
   enforced in `resolve_candidates` — including for the curator's own recall (R1) and
   for related expansion (F8). See
   [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md).
9. **Audit and content-free reporting.** Reconcile and quarantine emit whitelisted
   events. Findings, audit fields and API responses carry codes, counts and relative
   paths — never note bytes, never a secret-gate match. `_sanitize_for_log` is reused.
10. **Secret scanning applies at index time** (ruling R5), so a credential-bearing note is
    quarantined by default rather than becoming recallable. Two residuals, named rather
    than papered over. First, the gate is a **heuristic deny-list** — one of its six
    patterns is `(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}`, which matches
    ordinary vault prose such as a runbook quoting `password: hunter2` as an example — so
    `secret_gate: "warn"` exists as a documented per-mapping election for an operator who
    knows their corpus, with `reject` as the shipped default (ruling R14) and the finding
    reported in **either** mode, so the election changes what is indexed and never what the
    operator is told. Second, the gate is
    **reconcile-time**, so a secret added to an already-indexed note is exposed until the
    next reconcile. That window is a direct consequence of the no-continuous-watching
    non-goal rather than an oversight, and `index_freshness` is what makes it visible: a
    criterion cannot demand continuous enforcement while a non-goal forbids continuous
    watching.

**Divergence is never silent (F13).** `ScopeBinding` keyed on a raw `scope_id` was a
hole: `resolve_project_id` (line 210) falls back to a cwd hash, and `_get_search_dirs`
already compensates by including recorded `cwd_hash` alias directories (lines
2523-2546). If a project id churned — a renamed or moved folder — `binding.resolve()`
would silently return `NativeBinding` and `store()` would write to the native wiki with
no error: two replicas for one logical scope, arrived at silently, which is a stated
Non-Goal. Three changes close it:

- `binding.resolve()` canonicalises `scope_id` through the project-alias table
  (`get_project_id_by_alias`) before matching, which fixes the actual churn case.
- `status` warns on any mapping whose `scope_id` matches no known project id or alias
  (**orphaned mapping**), and on any mapped `project` scope_id resolved from a cwd hash
  rather than pinned or git-derived. Note the division of labour: the alias
  canonicalisation above carries the load, and these warnings cover only the irreducible
  remainder — a mapping that has gone genuinely stale, or an identity that was never
  aliased in the first place.
- A native write to a `project` scope, when a vault mapping for `project` exists and the
  resolved `scope_id` is not among the mapped ones, emits a named warning and increments
  `unmapped_project_write`. It is **not** a hard error, because a second legitimately-native
  project is indistinguishable from a churned id; removing the silence is what satisfies the
  Non-Goal. The counter is process-local today, not a durable `status` value; U8 owns both
  the `store()` call site and the durable-record decision. `docs/obsidian-vault.md` states
  that a vault-mapped `project` scope should pin `memory.project_id` or rely on a git remote,
  since cwd-hash identity breaks on folder rename.

**Compliance.** Nothing is read until a folder is mapped, nothing is injected until a
mapping opts in, nothing is written outside the managed folder, and no note is ever
deleted by CAO. `cao memory vault scan --dry-run` produces the reviewable
paths-and-scopes artifact before any row is written.

## Failure modes

| Failure | Behavior | Rationale |
| --- | --- | --- |
| Vault root missing or unreadable at load | Config load fails with a named error; native scopes unaffected | Fail closed; never degrade a vault scope into the native wiki |
| Vault root disappears at query time | Candidate `open()` fails; row skipped; `warn` finding; recall returns what it can | An unmounted sync folder must not 500 an agent call |
| Note path contains characters outside the write-path charset | Read works (inline confinement, no charset rule); a **write** to such a path is impossible because keys are `[a-z0-9-]` | The read and write charsets are deliberately different |
| Note changed since last reconcile | `index_freshness = "stale"`; write path refuses with a distinct error | Visible staleness beats invisible drift |
| Note being written by Obsidian now | Two-stat stability check retries, then `unstable_skipped`; prior row untouched | Never index a torn file |
| Cloud sync conflict copy | Filename patterns excluded before parsing | A conflict copy is not a note |
| Hardlinked note | `hardlink_refused` unless `allow_hardlinks` | Conservative; a hardlink tracks a target outside the vault |
| Malformed frontmatter | Quarantined, body not indexed | Metadata determines scope; untrusted scope means untrusted exposure |
| Duplicate `cao.key` in one scope | Both quarantined, `key_collision` | Picking one is the silent misresolution AC6 forbids |
| Path-derived key would exceed 60 chars | Deterministic digest suffix keeps it in budget; only a digest collision quarantines | Truncation was an unnamed collision source |
| Ambiguous `[[Basename]]` | No edge, `link_ambiguous` | Same reason |
| Unmapped note referenced by a link | No edge, `link_excluded` | An exclusion that leaks through a link is not an exclusion |
| Poisoned `memory_metadata.file_path` row | Inline sink guard skips it; `warn` finding | Defence in depth; the teeth test proves it |
| Curator terminal unidentifiable | Injection falls back to the gated deterministic builder | Fail closed |
| Note body matches a secret-gate pattern at reconcile | `reject` (default) quarantines with `secret_detected`, pattern **name** only; `warn` indexes and reports | Ruling R5. Index time is a pure function of content, so it is free per read and deterministic under rebuild |
| Secret added to an already-indexed note | Exposed until the next reconcile; `index_freshness` reports staleness. At that reconcile U5 retracts the vault projection: it deletes `memory_metadata` and clears `origin="vault"` edges when the note leaves `indexed`; an indexed reverse transition restores both | Reconcile-time enforcement is a consequence of the no-watcher non-goal, named rather than hidden; a quarantined note is never projected |
| A vault note carries `cao.type: project` and reaches three recalls | `promotion_service.plan()`'s `source_kind = 'native'` predicate excludes it | Without it the note is read unconfined and promoted into a persistent agent system prompt (N1) |
| A native memory's compile requests related candidates | `_candidate_keys_for_topic` filters to the same backend | Without it native and vault keys cross-contaminate each other's `related_keys` (N3) |
| `cao memory heal --apply` on a vault scope | Refused with a named error | It would otherwise de-index a vault note silently |
| Retention sweep on a formerly-native, now-mapped scope | Refused | It would otherwise de-index vault rows unattended |
| `cao memory export --scope <vault-bound>` | Refused with a pointer to the vault | Today it would silently emit an empty bundle |
| SQLite write fails after a durable vault write | `MemoryPartialWriteError`, existing repair affordance | Reuse the shipped contract |
| Derived state deleted by the user | Next `reconcile --apply` or `rebuild` restores it | The AC9 guarantee |
| Two vaults configured | Config load fails naming the release-one limit | Explicit refusal beats half-working |

## Operational surface

```
cao memory vault status    [--format table|json]
cao memory vault scan      [--dry-run]
cao memory vault reconcile [--apply] [--format table|json]
cao memory vault rebuild   --apply
cao memory vault migrate   --scope <s> [--scope-id <id>] [--apply] [--delete-source]
```

`scan` and `reconcile` without `--apply` write nothing, matching `cao memory repair`.
`rebuild` requires `--apply` because its dry run is indistinguishable from
`reconcile`'s. `migrate --delete-source` requires a second confirmation.

Read-only HTTP: `GET /memory/vault/status`. No mutating route, no MCP tool.

## Non-goals honored

| Non-goal | How |
| --- | --- |
| No unrestricted writes to arbitrary vault notes | `writer.py` composes every path under `managed_folder`; a non-writable mapping fails rather than falling back; `forget()` never unlinks; `wiki_healer` and `cleanup_service` refuse vault scopes |
| No bidirectional sync with the legacy CAO wiki | A mapped scope's native wiki is excluded from search and never written; migration is one-way and dry-run-first |
| No two writable replicas per scope | One backend per `(scope, scope_id)`; overlapping mappings refused; alias-aware binding plus non-silent divergence reporting close the churn hole |
| No Obsidian, plugin, or search daemon at runtime | Plain file reads with existing dependencies; no process, no port |
| No continuous file watching in release one | Three explicit operations; the only implicit refresh is the note a store just wrote |
| No cross-scope relationship semantics | Link resolution is scope-local; the relationship service already rejects cross-scope endpoints |
| No replacement of the memory API, BM25 ranking, or the typed relationship service | Signatures keep their meaning apart from two documented changes — `forget()`'s richer return with an identical `__bool__`, and `_apply_sort_and_increment`'s trailing keyword; the ranking mathematics in `memory_scoring` is untouched; edges go through `MemoryRelationshipService` with one new origin |

## Settled rulings

Closed by human ruling. Not open, not to be revisited in review.

| # | Ruling |
| --- | --- |
| R1 | The injection gate is enforced on the **curator's recall input**. Post-filtering the curator's returned block is rejected, because the curator paraphrases and a key-based filter over generated text misses confidential content restated in the model's own words |
| R2 | `uq_memory_key_scope` is **widened** with `source_kind`. Refusing colliding keys was rejected because `migrate` without `--delete-source` keeps the key by design, so every migrated memory would collide, making the default migration mode unusable and weakening AC12 |
| R3 | `forget()` on a vault note **de-indexes only** |
| R4 | `session` and `federated` are **not mappable** |
| R5 | The secret gate runs at **index time (reconcile)**, quarantining a secret-bearing note with a stable reported finding code. Not per-read (latency, plus re-running a heuristic over unchanged content), not a silent drop, not whole-folder refusal. This **reverses** my revision-2 recommendation |
| R6 | AC9 is **Satisfied under semantic determinism** with the proof boundary named. The refusal to bypass `MemoryRelationshipService`'s FR-2.1 single-boundary invariant is endorsed and is not to be revisited |
| R7 | `cao memory vault adopt` is **deferred to release two**; AC5 stays honestly Partial |
| R8 | `rebuild` resetting vault `access_count`/`last_accessed_at` is **accepted** and documented; a survivor table was rejected as trading one gap for a harder one |
| R9 | **U2 ships alone**, its own pull request, no other content |
| R10 | OKF export **refuses** vault-bound scopes with a clear error |
| R11 | `allow_hardlinks` defaults **false** |
| R12 | `ck_related_keys_length` is **omitted** from the rebuilt table, so U2 changes uniqueness and nothing else. `substr` truncation rejected outright — silently destroying data during a uniqueness migration is the one thing it must never do. Pre-scan-and-refuse rejected as still blocking startup |
| R13 | **U5 gets a security review.** Its four maintenance-refusal guards are the only thing preventing unattended de-indexing of a user's vault notes |
| R14 | `mappings[].secret_gate` — the same key and the same two values — **governs the index boundary as well as the write boundary**, with `reject` as the default. No third value, no second key. The finding is reported in **both** modes: the mode governs indexing, never disclosure. Rejected: always-quarantine-with-no-override (leaves a credential-documenting vault partly unrecallable with no remedy), `warn`-by-default (inverts the reason AC10 was flagged), and a per-pattern confidence policy (the natural future refinement, deferred because classifying six patterns by confidence becomes its own security-relevant review surface) |

## Open questions for a human

**None remain.** Revision 2's five open questions are closed by rulings R7-R13, and the one
that survived into revision 3 — the mode of the index-time secret gate — is closed by ruling
**R14**: `mappings[].secret_gate` governs both boundaries with `reject` as the default,
`warn` as a deliberate per-mapping election, and the finding reported in either mode. The
full reasoning and the three rejected alternatives are recorded in
[adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md) rule 9,
[adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md) and
[adr-007-configuration-surface.md](adr-007-configuration-surface.md) rules 16 and 20.

One consequence of R14 is carried into the acceptance-criteria audit rather than left here:
**AC10's mark is conditional on configuration**, because a mapping set to `warn`
reintroduces the exposure the criterion describes. It is marked `Conditional` in
[traceability.md](traceability.md) rather than Satisfied on the strength of the default
alone.

Two interpretive positions are also recorded rather than left implicit, both endorsed by
ruling R6 and the AC10 re-audit but both open to a reviewer disagreeing with the
*interpretation* rather than discovering it:

- **AC9** is Satisfied under semantic determinism, Partial under strict per-column byte
  determinism. See [traceability.md](traceability.md).
- **AC10** is Satisfied on the reading that a protection whose coverage is bounded by an
  explicitly mandated non-goal (no continuous watching) still "applies". The
  secret-added-after-index window is named, not hidden.
