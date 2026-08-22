# Test strategy: fixture vault and per-criterion coverage

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Planning record. No test has been written.
**Revision:** 3, amended — adds the static chokepoint assertion, index-time secret tests, the U5 baseline gate, and makes the determinism completeness check a hard failure. Amended after the supervisor corrected the test-baseline premise: **CI on `main` at `6c890ca` is GREEN** and the 20 local failures are a local-environment artifact.
**Related:** [design.md](design.md), [implementation-boundaries.md](implementation-boundaries.md), [traceability.md](traceability.md)

---

## Contents

1. [Hard rule on the vault under test](#hard-rule-on-the-vault-under-test)
2. [Revision 3 changes](#revision-3-changes)
3. [Why the fixture vault is built in code](#why-the-fixture-vault-is-built-in-code)
4. [Fixture vault contents](#fixture-vault-contents)
5. [Test file layout](#test-file-layout)
6. [Coverage per acceptance criterion](#coverage-per-acceptance-criterion)
7. [Teeth tests](#teeth-tests)
8. [Determinism proof](#determinism-proof)
9. [Concurrency tests](#concurrency-tests)
10. [Existing guard tests this feature will trip](#existing-guard-tests-this-feature-will-trip)
11. [Verification commands](#verification-commands)

---

## Hard rule on the vault under test

**No test, fixture, script, example, default, documentation snippet or developer
instruction in this feature may reference, read, index, or point at a real personal
Obsidian vault.** The entire test surface targets a disposable vault the suite constructs
in a pytest `tmp_path` and discards.

Enforced three ways, not just stated:

- No committed vault content. There is no `.md` fixture tree on disk to accidentally
  widen; the vault is materialized by a factory at test time.
- `test/test_fixtures_no_personal_pii.py` already fails CI on any personal-provider email
  address under a `fixtures/` directory in `test/`. Every address the factory writes is
  `user@example.com`.
- A dedicated assertion in the factory's own test: every path it creates is under the
  `tmp_path` it was given, and it **refuses a root that is not empty**. A factory that
  could be pointed at a populated directory could be pointed at a real vault.

Documentation carries the same rule: `docs/obsidian-vault.md` must not suggest "try it on
your vault" as a first step. The suggested first step is
`cao memory vault scan --dry-run`, which writes nothing.

## Revision 3 changes

| Change | Driver |
| --- | --- |
| A **static assertion** replaces the five-consumer enumeration as the chokepoint's enforcement: no module outside `reader.py` may `open`/`read_text` a value originating from `memory_metadata.file_path`. N1 and N3 are the proof it was needed — both are pre-existing read paths an enumeration re-read would not have caught | reviewer framing |
| New tests for the **index-time secret gate** under both `reject` and `warn`, and for the finding carrying the pattern **name** only | ruling R5 |
| New teeth test for **N1**: remove `promotion_service`'s predicate, plant a vault note with `cao.type: project` and three recalls, confirm its content reaches a profile's learned-patterns section | N1 |
| New test for **N3** cross-backend contamination of `related_keys` | N3 |
| **U5's identity diff becomes a gate**, with the baseline identities **and the exact invocation** recorded. CI is the arbiter; a red local run is a local-environment artifact to be diagnosed, never "fixed" in a vault PR | supervisor baseline, amended |
| Each of U5's four refusal guards gets **its own test green on the current tree**, never an assertion inside an already-red class | self-identified |
| The determinism **completeness check is a hard test failure** for an unclassified column, never a warning | ruling R6 |
| New U2 tests: no second rebuild on a second `init_db()` (N5), all three secondary indexes survive (N6), no data truncated and no CHECK on the rebuilt table (R12) | N5, N6, R12 |

### Revision 2 changes

| Change | Driver |
| --- | --- |
| The fixture corpus gains **names with spaces, apostrophes, commas, parentheses and non-ASCII characters** in both folders and notes. Revision 1's corpus was all-ASCII-safe, which is exactly why it would have passed while the design's `safe_join_under_base` choice rejected real vaults | directed fix D1 |
| `build_fixture_vault` gains a **`fixed_mtimes` parameter** applying `os.utime` from a constant table, and the unstable case gains an explicit **mid-read mutation hook** rather than relying on natural mtime drift | directed fix D2 |
| The determinism dump **splits** into byte-equal and structural column groups. Revision 1's "the canonical dump excludes nothing" made the test unpassable — `MemoryRelationshipModel.id` is a service-minted `uuid4` — and an unpassable absolute invites the vacuous repair of filtering columns out | F5, directed fix D2 |
| New tests for the vault `Memory` builder (F2), gated related-expansion (F8), the three F9 refusal guards, both `SKILL.md` copies (F10), the `ForgetResult` contract (F11), the body budget (F15), hardlinks (T5-gap), and the U2 nullable-discriminator trap | F2, F8, F9, F10, F11, F15, T5-gap, F3 |
| Two teeth tests added, one removed as superseded | F1, F12 |

## Why the fixture vault is built in code

`test/fixtures/vault_factory.py` — a Python module exposed through `pytest_plugins` in
`test/conftest.py` alongside the existing `test.fixtures.cao_server`,
`test.fixtures.jwt_factory`, `test.fixtures.jwks_server` and
`test.fixtures.terminal_factory` entries.

Five reasons this beats a committed fixture tree:

1. **Several planted cases cannot be committed reliably.** A symlink pointing outside the
   vault, a hardlink, a file that changes size between two `stat` calls, and two filenames
   differing only by case are all artifacts of a running filesystem. A git checkout on
   Windows or a case-insensitive filesystem would mangle all four.
2. **`scripts/validate_markdown_links.py` walks every git-tracked `.md`.** Committed
   fixture notes with deliberately dangling links would need an exclusion; building them at
   runtime means there is nothing to exclude.
3. **Determinism is testable only if construction is programmatic.** The proof needs a
   second vault created in **reverse order** and a run with **fixed mtimes**. Both are
   factory parameters, not second committed trees.
4. **Non-ASCII and space-bearing filenames survive better in code than in a repository.**
   A committed `Références.md` is subject to Unicode normalization differences between
   macOS (NFD) and Linux (NFC) at checkout; a factory writes the exact bytes the test
   intends and can parameterize the normalization form.
5. **A disposable vault cannot leak.** `tmp_path` is removed by pytest.

Factory shape:

```python
build_fixture_vault(
    root: Path,
    *,
    cases: frozenset[str] = ALL_CASES,
    creation_order: Literal["forward", "reverse"] = "forward",
    fixed_mtimes: bool = False,
    on_read_mutate: Callable[[Path], None] | None = None,
) -> FixtureVault
```

`FixtureVault` exposes the vault root, the settings dict a test feeds to
`get_vault_config()`, and a named constant per planted note so a test references the case
it is about rather than a bare path string.

`fixed_mtimes=True` applies `os.utime` from a constant `(relpath -> epoch_ns)` table
**after** writing each file. `on_read_mutate` is the hook the unstable case uses: because
fixed mtimes make the two-stat stability check pass trivially, the unstable note must be
mutated *during* the read rather than left to drift. Designing the fixture and the check
against each other is deliberate — see
[adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md).

## Fixture vault contents

`CAO/` is the managed folder, empty at build time. Note the folder and file names: they are
the D1 correction, and a corpus without them would let a charset-restrictive design pass.

```
<tmp_path>/vault/
  .obsidian/app.json                          always-excluded
  .trash/Deleted.md                           always-excluded
  CAO/                                        managed folder, empty
  Projects/CAO Design/Design.md               SPACE in folder; cao.key, aliases, links out
  Projects/CAO Design/Retrieval Path.md       SPACE in filename; NO cao.key (derived key)
  Projects/CAO Design/Don't Panic.md          APOSTROPHE in filename
  Projects/CAO Design/Notes, drafts (v2).md   COMMA and PARENTHESES in filename
  Projects/CAO Design/Références.md           NON-ASCII in filename
  Projects/CAO Design/Malformed.md            unterminated quote in frontmatter
  Projects/CAO Design/Bomb.md                 YAML anchors and aliases
  Projects/CAO Design/Huge-Frontmatter.md     frontmatter over the byte cap
  Projects/CAO Design/Duplicate.md            basename collides with the next one
  Projects/CAO Design/Sub/Duplicate.md        same basename, different folder
  Projects/CAO Design/CollideA.md             same cao.key as CollideB
  Projects/CAO Design/CollideB.md             same cao.key as CollideA
  Projects/CAO Design/Deeply/Nested/<62-char title>.md   derived-key length pressure
  Projects/CAO Design/Deeply/Nested/<62-char title, differing at char 55>.md  same
  Projects/CAO Design/Dangling.md             [[No Such Note]]
  Projects/CAO Design/Excluded-Link.md        [[Private/Secret]]
  Projects/CAO Design/Symlinked.md            symlink to a file OUTSIDE the vault
  Projects/CAO Design/Escape                  symlinked DIRECTORY to outside the vault
  Projects/CAO Design/Hardlinked.md           hardlink to a file OUTSIDE the vault
  Projects/CAO Design/Traversal.md            cao.key: ../../../../etc/passwd
  Projects/CAO Design/Unstable.md             mutated via on_read_mutate
  Projects/CAO Design/Torn.sync-conflict-1.md sync-conflict filename
  Projects/CAO Design/Credential.md           an AKIA-shaped literal in the body
  Projects/CAO Design/Embed.md                ![[Design]] and ![[diagram.png]]
  Projects/CAO Design/Heading.md              [[Design#Read path]] and [[Design#^abc123]]
  Projects/CAO Design/Plugin.excalidraw.md    excalidraw-plugin frontmatter
  Projects/CAO Design/Dataview.md             field:: value inline fields, YAML comments
  Projects/CAO Design/Big.md                  over max_note_bytes
  Projects/CAO Design/Long-Body.md            body over max_recall_body_chars
  Projects/CAO Design/NotUtf8.md              invalid UTF-8 bytes
  Projects/CAO Design/CaseA.md                pairs with casea.md
  Projects/CAO Design/casea.md                case-only difference
  Projects/CAO Design/diagram.png             attachment
  Private/Secret.md                           excluded by pattern
  Reference/Glossary.md                       mapped to the global scope
  Injectable/Team Handbook.md                 mapped with inject: true
  Unmapped/Loose.md                           in no mapping
```

Configuration the factory returns: one vault, `managed_folder: "CAO"`, and four mappings —
`Projects/CAO Design` to `(project, <fixture scope_id>)` writable with `inject: false`,
`Reference` to `global` non-writable, `Injectable` to `global` with **`inject: true`** (so
the two-policy tests have both arms), and `exclude: ["Private/**"]`. All bounds set low
enough that `Big.md`, `Long-Body.md` and `Huge-Frontmatter.md` trip them without
megabyte files.

The two `Deeply/Nested/<62-char title>.md` notes are the F14 case: under revision 1 they
would truncate to the same 60-character key and both quarantine, with the cause invisible.
Under revision 2 the mapping-relative digest suffix keeps them distinct, and the test
asserts **both are indexed** — which is the assertion that would have caught the original
defect.

Every case maps to a row of the boundary table in
[adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md).
`test/services/vault/test_findings_table_covered.py` asserts the mapping is total in both
directions: every finding code has a fixture case that produces it, and every case is
asserted somewhere.

Platform-dependent cases (`Symlinked.md`, `Escape`, `Hardlinked.md`,
`CaseA.md`/`casea.md`, and the non-ASCII name on a filesystem with aggressive
normalization) are guarded by an explicit `pytest.skip` whose message names why, so a skip
reads as a platform fact rather than as absent coverage.

## Test file layout

| File | Unit | Covers |
| --- | --- | --- |
| `test/fixtures/vault_factory.py` | — | The factory, plus its self-test asserting `tmp_path` confinement, refusal of a non-empty root, and synthetic-only content |
| `test/services/vault/test_config_validation.py` | U1 | All nineteen rules, table-driven. **Includes: a mapping folder with a space and one with a non-ASCII character LOAD SUCCESSFULLY**, and a `managed_folder` with a space is REJECTED |
| `test/clients/test_vault_schema_migration.py` | U2 | Fresh database; pre-existing database; double `init_db()` is a no-op **and performs no second rebuild** (N5 — count rebuilds, not just the end state); the widened constraint rejects a duplicate vault key; **a duplicate NATIVE key is still rejected after the widening** (the nullable-discriminator trap); all three secondary indexes exist after migration (N6); the rebuilt table carries no `ck_related_keys_length` and an over-long `related_keys` value survives unmodified (R12); all **seventeen** query sites carry the predicate |
| `test/services/vault/test_parser_frontmatter.py` | U3 | Malformed, anchors, over-cap, invalid `cao` block, taxonomy validation, user-key preservation region |
| `test/services/vault/test_identity.py` | U3 | `cao.key` path; mapping-relative derived key with digest; **the two 62-char nested notes both index**; key charset refusal; authored and derived collisions distinguished |
| `test/services/vault/test_links.py` | U3 | Every link row of the boundary table |
| `test/services/vault/test_scan_exclusions.py` | U4 | Mapped, excluded, always-excluded, unmapped, attachments, plugin formats, caps |
| `test/services/vault/test_scan_symlinks.py` | U4 | Symlinked file, symlinked directory, **hardlink refused**, `allow_hardlinks` escape hatch, symlinked root permitted |
| `test/services/vault/test_scan_stability.py` | U4 | Two-stat retry via `on_read_mutate`, `unstable_skipped`, sync-conflict names, zero-byte-with-prior-hash, non-UTF-8, line-ending normalization |
| `test/services/vault/test_reconcile.py` | U5 | Create, edit, delete, quarantine, findings persistence, report counts, both audit events present in the closed whitelist |
| `test/services/vault/test_rename_detection.py` | U5 | Pure rename absorbed; rename-plus-edit reported; ambiguous rename reported; a `cao.key` note survives any move |
| `test/services/vault/test_rebuild_determinism.py` | U5 | The proof, below |
| `test/services/vault/test_native_maintenance_guards.py` | U5 | **All four guards, each as its own test function green on the current tree**: `wiki_lint` sees no vault rows; `cao memory heal --apply` refuses a vault scope; the retention sweep refuses a vault scope; `promotion_service.plan()` excludes vault rows. **This file is new and must not extend `TestMemoryHeal` or `test_wiki_lint.py`**, both of which are already failing on `main` — an assertion added to a red class can appear covered while never executing |
| `test/services/vault/test_vault_memory_builder.py` | U6 | **An ordinary Obsidian note with NO `## <ISO>` heading becomes a `Memory`**, with the full field mapping; `_parse_wiki_file` is never called on a vault arm |
| `test/services/vault/test_recall_source_fields.py` | U6 | `source_kind`, `source_path`, `indexed_at`, `index_freshness` fresh and stale, `content_truncated` |
| `test/services/vault/test_recall_body_budget.py` | U6 | `Long-Body.md` is capped; `token_estimate` populated; the injection render never sees an oversized line |
| `test/services/vault/test_recall_scope_isolation.py` | U6 | `Reference` global versus `Projects/CAO Design` project; quarantined notes never candidates; **no directory enumeration during recall** |
| `test/services/vault/test_recall_native_unchanged.py` | U6 | Native recall byte-identical with no vault configured; `_get_search_dirs` unchanged |
| `test/services/vault/test_export_refuses_vault.py` | U6 | `export_memories` on a vault scope raises rather than emitting an empty bundle |
| `test/services/vault/test_injection_gate.py` | U7 | Indexed-yes-injected-no; the `Injectable` mapping appears; unresolvable binding excluded; `memory.enabled=false` short-circuit; **the related fan-out at line 2854 is gated** |
| `test/services/vault/test_curator_recall_gate.py` | U7 | **A recall from the curator terminal cannot see a non-injectable vault note**; an unidentifiable curator falls back to the deterministic builder rather than dispatching |
| `test/services/vault/test_related_expansion_gated.py` | U6, U7 | Vault related-expansion returns results (not silently empty), and returns nothing non-injectable on the injection path |
| `test/services/vault/test_managed_write.py` | U8 | Create, update, frontmatter merge round-trip byte equality (comments, key order, Dataview fields), conflict refusal, secret gate, lock and temp file locations |
| `test/services/vault/test_unmanaged_never_written.py` | U8 | Hash snapshot of every unmanaged note before and after a full reconcile, a recall and a store |
| `test/services/vault/test_forget_contract.py` | U8 | `ForgetResult.__bool__` matches the old boolean at all seven call-site shapes; `action == "deindexed"`; the file still exists; the MCP response carries `action` and `path` |
| `test/services/vault/test_concurrent_edits.py` | U8 | Two writers, one note; writer versus out-of-band edit; reconcile versus writer; two reconciles |
| `test/graph/providers/test_memory_provider_vault.py` | U9 | Node set from the chokepoint; `origin="vault"` edges; **no node for a quarantined note**; no path-shaped attribute; lint reported unavailable in `meta`; lint-ordering preserved |
| `test/services/vault/test_migrate_lossy.py` | U10 | Dry run, apply, all seven lossy fields named, `cao.links` carries typed edges, `--delete-source` gating, **migration without `--delete-source` does not collide** (the R2 rationale) |
| `test/cli/commands/test_memory_vault.py` | U11 | Five leaves, dry-run default, `rebuild` requires `--apply`, JSON output shape |
| `test/api/test_memory_vault_status.py` | U11 | Read-only route; no mutating route exists |
| `test/services/vault/test_obsidian_closed.py` | U11 | No subprocess, no socket, no Obsidian path reference in `services/vault/` |
| `test/services/vault/test_findings_table_covered.py` | U3-U5 | Finding-code coverage total in both directions |
| `test/services/vault/test_no_enumeration_outside_scan.py` | U6, U8 | Two static invariants: (1) no `rglob`/`glob`/`iterdir`/`walk`/`scandir` in `reader.py`, `writer.py`, `binding.py`; (2) **no module outside `reader.py` calls `open` or `read_text` on a value originating from `memory_metadata.file_path`**. The second replaces revision 2's hand-maintained consumer list as the enforcement mechanism. Implemented as an AST walk over `src/cli_agent_orchestrator/`, not a grep, so an aliased or attribute-accessed path is still caught |
| `test/services/vault/test_secret_gate_index_time.py` | U3, U4 | `Credential.md` is quarantined with `secret_detected` under `secret_gate: "reject"`; indexed **and** reported under `"warn"`; the finding carries the pattern **name** and no matched bytes; the decision is a pure function of content (same body, same verdict, no I/O) |
| `test/services/vault/test_promotion_service_native_only.py` | U5 | **N1.** A vault note with `cao.type: project` and `access_count >= 3` does **not** appear in `promotion_service.plan()`'s eligible set; and the default-`reference` mitigation is asserted separately so the two defences are independently visible |
| `test/services/vault/test_related_candidates_same_backend.py` | U6 | **N3.** A native topic's `_candidate_keys_for_topic` returns no vault keys and vice versa; no vault key is ever written into a native row's `related_keys` |

## Coverage per acceptance criterion

| AC | Test evidence |
| --- | --- |
| 1 — opt-in, confined root, explicit include/exclude | `test_config_validation.py` proves `enabled` defaults false and the root policy; `test_scan_exclusions.py` proves `Unmapped/Loose.md` and `Private/Secret.md` produce no row |
| 2 — explicit folder-to-scope; private and short-lived scopes not silently written to a shared folder | `test_config_validation.py` proves overlapping mappings, duplicate `(scope, scope_id)`, `federated`, `session`, and a `managed_folder` outside a writable mapping all fail load |
| 3 — allowed vault notes participate in recall through a derived ranked index | **`test_vault_memory_builder.py` is the load-bearing test** — without the builder, notes rank and then vanish. Then `test_recall_source_fields.py` recalls `Design.md` by content, a BM25-mode recall ranks it, and `test_recall_scope_isolation.py` proves the corpus came from SQLite |
| 4 — results identify source, path, scope, freshness | `test_recall_source_fields.py` asserts all four, that `source_path` is vault-relative, and that touching a note flips `index_freshness` to `stale` with no implicit reconcile |
| 5 — external create, edit, rename, delete reflected after a bounded observable step | `test_reconcile.py`; `test_rename_detection.py` for all four rename arms; `test_memory_vault.py` for the report being printed. The two unsatisfiable sub-cases are asserted to **report** |
| 6 — resolvable wikilinks become graph relationships; malformed or ambiguous reported | `test_links.py` per boundary row; `test_memory_provider_vault.py` for `GraphView` with `origin="vault"`; `test_findings_table_covered.py` for total code coverage |
| 7 — `memory_store` only in the managed folder | `test_managed_write.py`, including a crafted `cao.key` of `../../evil` refused by `_sanitize_key` before path composition |
| 8 — normal memory operations never modify an unmanaged note | `test_unmanaged_never_written.py`; `test_forget_contract.py`; **plus `test_native_maintenance_guards.py`**, which is what stops `heal` and the retention sweep de-indexing a vault note |
| 9 — derived state deletable and deterministically rebuildable | `test_rebuild_determinism.py`, below |
| 10 — existing scope, authorization, secret, traversal, symlink, audit, concurrency protections apply | `test_scan_symlinks.py`, `test_recall_scope_isolation.py`, `test_managed_write.py`, `test_concurrent_edits.py`, `test_reconcile.py`, `test_native_maintenance_guards.py`, `test_curator_recall_gate.py`, plus the eleven teeth tests |
| 11 — works while Obsidian is closed | `test_obsidian_closed.py`; structurally, the suite runs with no Obsidian installed |
| 12 — explicit migration, representable metadata, lossy report | `test_migrate_lossy.py` |
| 13 — focused tests cover malformed YAML, duplicate names, excluded paths, dangling links, symlinks, partial files, concurrent edits, rebuilds | Each of the eight named conditions has a fixture case and an asserting test; `test_findings_table_covered.py` prevents an unexercised case. **Revision 2 adds names with spaces, apostrophes, commas, parentheses and non-ASCII**, without which the corpus was unrepresentative of a real vault |

Limitations asserted **as** limitations, matching [traceability.md](traceability.md):

- Rename plus edit in one window without `cao.key` asserts `rename_with_edit_unresolved`
  and a changed key.
- A note actively edited in Obsidian asserts `unstable_skipped`, not a successful index.
- `wiki_lint` enrichment for a vault scope asserts `meta` reports it **unavailable**,
  rather than asserting flags that cannot be produced.

## Teeth tests

One per security-sensitive guard. Remove the guard, plant the attack, confirm the leak,
restore, confirm the leak is gone, confirm `git diff` is empty afterwards. A guard whose
removal changes nothing was never load-bearing.

| Guard | Planted attack | Expected leak with the guard removed |
| --- | --- | --- |
| Read-time inline confinement (U6) | Hand-write a `memory_metadata` row whose `file_path` points outside the vault root | Out-of-vault file content returned as a memory body |
| **Curator recall gate (U7)** | An `index:true, inject:false` note; a curator terminal producing an injection block | The curator recalls it and its content — paraphrased — reaches the injected prompt |
| Deterministic-builder injection filter (U7) | Same note, curator absent so the fallback path runs | The note's body appears in `get_memory_context_for_terminal` output |
| Related-expansion gate (U6/U7) | A non-injectable note reachable only as a related target of an injectable one | It arrives via the line-2854 fan-out, bypassing the primary filter |
| Symlink refusal (U4) | `Symlinked.md` linking outside the vault | Out-of-vault content indexed as a vault note |
| **Hardlink refusal (U4)** | `Hardlinked.md` hardlinked to a file outside the vault | Out-of-vault content indexed, and tracking future changes to the target |
| Managed-folder path composition (U8) | `memory_store` with a key crafted to traverse | A file written outside `managed_folder` |
| Exclusion application before open (U4) | `Private/Secret.md` | The excluded note gets a row and becomes recallable |
| Frontmatter byte cap before parse (U3) | `Huge-Frontmatter.md` and `Bomb.md` | Unbounded parse work; anchor expansion |
| `key_collision` quarantine (U3) | `CollideA.md` and `CollideB.md` | One arbitrary note wins the key and inherits the other's edges |
| Conflict check inside the lock (U8) | Out-of-band edit between read and write | A silent overwrite of the user's edit |
| Scoped deletes in `rebuild` (U5) | Native memory rows present | Native rows deleted by a vault rebuild |
| `_is_memory_enabled()` on the vault path (U7) | `memory.enabled=false` | Vault content injected for an operator who turned memory off |
| **`source_kind` NOT NULL (U2)** | Two native rows with the same `(key, scope, scope_id)` | Both insert, because a nullable discriminator makes the UNIQUE index inert — the native uniqueness invariant silently gone |
| **`promotion_service` native-only predicate (U5, N1)** | A vault note with `cao.type: project` recalled three times | Its body is read with no confinement and promoted into a profile's learned-patterns section — a persistent agent system prompt, durable across sessions and unaffected by `inject: false` |
| **Index-time secret gate (U4, R5)** | `Credential.md`, a note carrying an `AKIA`-shaped literal | The note is indexed and recallable, with no finding recorded |
| **Same-backend related candidates (U6, N3)** | A native topic and a vault topic sharing a scope | A vault key is written into a native row's `related_keys`, then surfaces through `_expand_related`'s legacy fallback |
| **The static chokepoint assertion (U6)** | Add a module outside `reader.py` that opens a `memory_metadata.file_path` | It reads vault bytes with no confinement and no gate, and the suite stays green — which is exactly how N1 and N3 survived revision 2 |
| **Quarantined-node exclusion (U9)** | A quarantined note whose derived key encodes its folder path | Its key, and therefore its path and title, published in `GraphView` and every UI that renders it |

Removed as superseded: revision 1's "injection gate" teeth test, which planted against
`get_memory_context_for_terminal` only and would have passed while the curator path leaked.
It is replaced by the two separate curator and fallback tests above — the fix for F1 is
also a fix to the test that was supposed to catch it.

## Determinism proof

`test/services/vault/test_rebuild_determinism.py`.

### The two column groups

Revision 1 asserted "the canonical dump excludes nothing", which is unpassable:
`MemoryRelationshipModel.id` defaults to `str(uuid.uuid4())`, and edges must be written
through `MemoryRelationshipService` because of its single-boundary invariant. An
unpassable absolute is worse than a split one, because the only available repair is to
filter columns out until the assertion compares almost nothing.

So the dump is split, with one rule that keeps it honest: **every column appears in exactly
one group, and a column may not be absent from both.**

**Ruling R6 attaches a hard condition: the completeness check must be a TEST FAILURE for an
unclassified column, never a warning.** This is the load-bearing part of the split. A
warning is advisory, so a future author can add a column, leave it unclassified, see a
passing suite and move on — at which point the two-group split has degraded back into
revision 1's filtering problem by a slower and less visible route. Implementation: the
assertion reads the live schema with `PRAGMA table_info` on each of the four tables and
asserts **set equality** against the union of the two declared groups. A column present in
the schema and in neither group fails; so does a declared column that no longer exists.
Neither is a skip and neither is a warning.

| Group | Comparison | Columns |
| --- | --- | --- |
| **Byte-equal** | Two dumps must be byte-identical | `vault_note`: `note_uid`, `vault_id`, `scope`, `scope_id`, `cao_key`, `vault_relpath`, `managed`, `content_sha256`, `frontmatter_sha256`, `size_bytes`, `mtime_ns`, `status`. `vault_finding`: `vault_id`, `vault_relpath`, `code`, `severity`, `detail`. `vault_note_alias`: all columns except `created_at`. `memory_metadata` (vault rows): `id`, `key`, `memory_type`, `scope`, `scope_id`, `source_kind`, `file_path`, `tags`, `token_estimate`, `created_at`, `updated_at`, `access_count`, `last_accessed_at`. `memory_relationships` (`origin='vault'`): `scope`, `scope_id`, `source_key`, `target_key`, `type`, `origin`, `status`, `confidence`, `rank`, `attributes_json`, `source_updated_at` |
| **Structural** | Same row count; same key set; every value equal to the run's single captured value; ids well-formed and internally consistent | `vault_note.last_reconciled_at`; `vault_finding.id`, `.reconcile_run_id`, `.created_at`; `vault_note_alias.created_at`; `memory_relationships.id`, `.created_at`, `.updated_at` |

`mtime_ns` is byte-equal **only** because the proof runs with `fixed_mtimes=True`. Stated
so nobody later runs the proof without it and concludes the design is non-deterministic.

`vault_finding.id` is derived
(`sha256(reconcile_run_id \0 code \0 vault_relpath)`) and would be byte-equal but for its
`reconcile_run_id` input, which is per-run by design; hence structural.

### The three assertions

1. **Idempotence.** Build the fixture vault with `fixed_mtimes=True`.
   `reconcile --apply`. Dump both groups. `rebuild --apply`. Dump again. Byte-equal group
   compares byte-identical; structural group compares structurally.
2. **Order independence.** Build a second fixture vault in a different `tmp_path` with
   `creation_order="reverse"` and the same fixed mtimes. Reconcile against a fresh
   database. Assert its byte-equal dump equals the first, modulo the vault root path.
   **This is the assertion that catches a dependence on filesystem enumeration order**;
   assertion 1 alone would pass on a system that happens to enumerate consistently.
3. **Native isolation.** Store several native memories first. `rebuild --apply`. Assert
   every native `memory_metadata` row and every non-`vault` relationship row is unchanged,
   **by identity comparison rather than by count** — a count is ambiguous exactly when it
   matters.

## Concurrency tests

`test/services/vault/test_concurrent_edits.py`, threads against one `MemoryService` and
one `tmp_path` vault, following `test/services/test_memory_durability_and_concurrent.py`.

1. **Two writers, same managed note.** Assert no lost update (both entries present, or one
   fails visibly with the conflict error), no interleaved bytes, exactly one `vault_note`
   row, and no `.tmp` or `.lock` residue inside the vault.
2. **Writer versus out-of-band edit.** Assert the conflict check fires and the write is
   refused with the distinct error naming the remedy, rather than overwriting.
3. **Reconcile versus writer.** Assert the final `vault_note.content_sha256` matches the
   file on disk — never a hash of an intermediate state.
4. **Two reconciles.** Assert the second coalesces or completes with an identical result;
   no duplicate `vault_note` rows and no duplicate edges.

## Existing guard tests this feature will trip

| Guard | Tripped by | Required action |
| --- | --- | --- |
| `test/test_command_catalog_matches_click.py` | U11's five CLI leaves | Update `tui/src/catalog.rs`: `CommandId`, `COMMAND_COUNT` 69 to 74, `DISPLAY_ORDER`, both exhaustive matches, module doc-comment counts — same pull request |
| **`test/test_skill_packaging_parity.py`** | U8's `forget()` wording change and U12's vault section | Edit via `python scripts/sync_skills.py`; both `skills/cao-memory/SKILL.md` and `src/cli_agent_orchestrator/skills/cao-memory/SKILL.md` must stay byte-identical under `filecmp.cmp` |
| `test/test_fixtures_no_personal_pii.py` | The factory, if any address is not `@example.com` | Use `user@example.com` only |
| `test/clients/test_memory_relationships_migration.py` | U5, if it edits `_backfill_legacy_related_keys` | Do not edit that function; its source text is hashed at line 119. Adding `vault` to `VALID_ORIGINS` does not touch it |
| Existing `memory_metadata` tests | **U2's table rebuild** | The rebuild must preserve every row and every index; run the whole `test/services/test_memory_*` and `test/clients/` sets, and diff failing identities against the baseline |
| **A red local run in the lint / heal / graph subsystem is a LOCAL-ENVIRONMENT artifact** | U5 lands in exactly that territory | **CI on `main` at `6c890ca` is green across three runs.** One developer machine shows 20 failures at that same commit, and the set is **invocation-dependent** (25 from the four files in isolation, 18 from the same files inside the full run) — order dependence at that scale is not what a real code defect looks like, and two of the failures are timestamp-rendering tests consistent with a timezone or library-version difference. So: **diagnose it as environmental, never repair it inside a vault PR.** U5's PR must show the same baseline set plus only its own new passes, measured with the **same invocation** as the baseline |
| `scripts/validate_markdown_links.py` | U12's documentation, once committed | Every relative link and heading anchor must resolve |
| `uv run mypy src/` | Every unit | New modules fully annotated |
| `cargo clippy --locked --all-targets -- -D warnings` | U11 | Catalog edits clippy-clean; `--all-targets` lints test code too |

## Verification commands

Run every command from the worktree root, `/Users/fanhongy/Project/cao-wt-644`.

### One-time setup in a fresh worktree

```bash
uv sync --all-extras --dev
```

`--all-extras` is not optional. A fresh worktree's virtual environment built without it
omits the `agui` and `otel` extras, and roughly sixty AG-UI and OpenTelemetry tests then
fail for missing dependencies — which reads as a large regression caused by whatever change
is in the tree. `--dev` and `--group dev` are equivalent; CI uses `--dev`.

**Take the baseline before changing any code**, and record failing test **identities**, not
the count. Two rules make the baseline usable rather than misleading:

- **Record the invocation alongside the identities.** The failure set is
  invocation-dependent — the four lint and graph files produce 25 failures in isolation and
  18 inside the full run — so a baseline taken one way and compared against a run taken the
  other way manufactures phantom regressions. Use the CI-exact invocation for both.
- **CI is the arbiter, not this machine.** CI on `main` at `6c890ca` is green across three
  consecutive runs, so any local red at that commit is a **local-environment artifact**:
  diagnose it, do not repair it inside a vault pull request. Two of the observed failures
  are timestamp-rendering tests
  (`TestT3DetectedAtVisible::test_cli_json_uses_iso8601_z_suffix`,
  `test_cli_table_renders_detected_at`), consistent with a timezone or Click-version
  difference rather than broken logic. Note that exception type alone never rules
  environment out — a timezone or library-version mismatch surfaces as `AssertionError`,
  `TypeError` or `KeyError` just as readily as a logic bug does.

This is a **gate for U5, not advice**: record the identities and the command, and require the
PR to show the same set plus only its own new passes.

```bash
uv run pytest test/ \
  --ignore=test/providers/test_kiro_cli_integration.py \
  --ignore=test/e2e \
  -m "not e2e" \
  -q 2>&1 | tee /tmp/baseline.txt
```

Attribute any later failure by diffing failed-test identities against this file. A count is
ambiguous exactly when it matters — new passes and a new break can sum to a misleading
total. This matters most for U2, which rewrites a shared table.

### CI's exact unit-test invocation

Reproduced verbatim from `.github/workflows/ci.yml`. Do not substitute a bare
`uv run pytest test/`: that picks up `test/e2e` and
`test/providers/test_kiro_cli_integration.py`, which CI deliberately ignores and which hang
or require real providers.

```bash
uv run pytest test/ \
  --ignore=test/providers/test_kiro_cli_integration.py \
  --ignore=test/e2e \
  -m "not e2e" \
  --cov=src/cli_agent_orchestrator \
  --cov-report=xml \
  --cov-report=term-missing \
  -v
```

### Focused development loop

`pyproject.toml`'s `addopts` already supplies `--cov=src --cov-report=term-missing
-m 'not e2e'`, so a narrowed path inherits them.

```bash
uv run pytest test/services/vault -q
uv run pytest test/services/vault/test_rebuild_determinism.py -q
uv run pytest test/services/vault/test_vault_memory_builder.py -q
uv run pytest test/services/vault/test_curator_recall_gate.py -q
uv run pytest test/services/vault/test_native_maintenance_guards.py -q
uv run pytest test/clients/test_vault_schema_migration.py -q
uv run pytest test/graph/providers/test_memory_provider_vault.py -q
uv run pytest test/cli/commands/test_memory_vault.py -q
```

### Guard tests to run explicitly

```bash
uv run pytest test/test_command_catalog_matches_click.py -q
uv run pytest test/test_skill_packaging_parity.py -q
uv run pytest test/test_fixtures_no_personal_pii.py -q
uv run pytest test/clients/test_memory_relationships_migration.py -q
uv run pytest test/services/test_memory_service.py test/services/test_bm25_search.py \
  test/services/test_memory_reconciliation.py \
  test/services/test_memory_durability_and_concurrent.py \
  test/services/test_memory_service_phase2.py -q
python scripts/sync_skills.py && git diff --stat   # must be empty after a skill edit
```

### Code quality, exactly as CI runs it

```bash
uv run black --check src/ test/
uv run isort --check-only src/ test/
uv run mypy src/
uv run python scripts/validate_markdown_links.py
```

### Rust TUI, required only for U11

```bash
cd tui
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

### Manual verification, on the fixture vault only

Never on a personal vault. Build a throwaway vault with the factory, point a scratch
`CAO_HOME_DIR` at a temporary directory so nothing touches the real
`~/.aws/cli-agent-orchestrator`, and run:

```bash
export CAO_HOME_DIR=$(mktemp -d)
# write the fixture settings.json into $CAO_HOME_DIR/settings.json first
cao memory vault status
cao memory vault scan --dry-run
cao memory vault reconcile            # dry run by default; prints the plan
cao memory vault reconcile --apply
cao memory vault status --format json
cao memory list --scan-all
cao memory vault rebuild --apply
cao memory vault status --format json   # must match the previous status output
```

The last two lines are the manual form of the determinism proof: a rebuild must leave
`status` reporting exactly what it reported before.
