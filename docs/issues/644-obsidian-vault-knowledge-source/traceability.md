# Traceability: acceptance criteria to units

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Planning record.
**Revision:** 3, amended — AC10 marked **Conditional** after N1, R5 and R14; AC9's condition made explicit. Amended after the supervisor corrected the test-baseline premise: **CI on `main` at `6c890ca` is GREEN**.
**Related:** [design.md](design.md), [implementation-boundaries.md](implementation-boundaries.md), [test-strategy.md](test-strategy.md)

---

## Contents

1. [How to read this table](#how-to-read-this-table)
2. [Revision 2 re-audit](#revision-2-re-audit)
3. [Revision 3 re-audit](#revision-3-re-audit)
4. [The table](#the-table)
5. [Note 5 rename](#note-5-rename)
6. [Note 9 the proof boundary](#note-9-the-proof-boundary)
7. [Note 10 secret scanning after ruling R5](#note-10-secret-scanning-after-ruling-r5)
8. [Note 11 Obsidian closed versus Obsidian open](#note-11-obsidian-closed-versus-obsidian-open)
9. [Note 12 seven fields are not representable](#note-12-seven-fields-are-not-representable)
10. [Criteria satisfied by construction](#criteria-satisfied-by-construction)
11. [Units with no criterion](#units-with-no-criterion)
12. [Behavior changes that are not acceptance criteria](#behavior-changes-that-are-not-acceptance-criteria)

---

## How to read this table

- **Satisfying unit** is the unit that makes the criterion true. Where several contribute,
  the one that closes it is first.
- **Release one** is `Satisfied`, `Conditional`, `Partial` or `No`. Every `Partial` and the
  one `Conditional` has an explicit note below stating what is not satisfied and why.
  Nothing is implied.
- **`Conditional`** means the criterion's satisfaction depends on a configuration value, and
  the mark therefore cannot be reduced to one word without hiding something. It is used
  exactly once, for AC10, and the note says which setting and in which direction. A single
  `Satisfied` justified only by a default would be the kind of overstatement this table
  exists to avoid.
- **Revision 1** records what the previous revision claimed, so the delta is visible rather
  than quietly corrected.

## Revision 2 re-audit

The review praised the three declared-Partial notes and asked for the same standard applied
to the ten claimed-Satisfied. Doing that changed two marks and confirmed eight.

| AC | Revision 1 | Revision 2 | Why it changed |
| --- | --- | --- | --- |
| 3 | Satisfied | **Satisfied**, but it was **not true as designed** | F2: `_parse_wiki_file` returns `None` without a `## <ISO8601Z>` heading and both readbacks drop falsy results, so an ordinary note ranked and then vanished. Now satisfied by U6's vault `Memory` builder |
| 4 | Satisfied | **Satisfied**, same caveat | Same root cause: no `Memory` object, no source fields to report |
| 9 | Satisfied | **Satisfied, with a named proof boundary** | F5/D2: the byte-equality proof was unconstructable, because `MemoryRelationshipModel.id` is a service-minted `uuid4`. The proof is now a two-group comparison, and the boundary is stated rather than filtered away |
| 10 | Satisfied | **Partial** | Re-audit finding: of the seven protection families the criterion lists, **secret scanning is applied on the write and export paths only**, deliberately not on read or index. That is a defensible design decision, but claiming the criterion fully satisfied overstates it |
| 13 | Satisfied | **Satisfied**, corpus corrected | F4/D1: the fixture corpus was all-ASCII-safe, so it would have passed while the design's `safe_join_under_base` choice rejected any vault with a folder named `CAO Design`. The corpus now carries spaces, apostrophes, commas, parentheses and non-ASCII names |
| 1, 2, 6, 7, 8, 11, 12, 5 | unchanged | unchanged | Confirmed on re-audit; 5, 11 and 12 remain Partial for the reasons already stated |

## Revision 3 re-audit

Two criteria were affected by the revision-2 review and its rulings. One mark changes.

| AC | Rev 2 | Rev 3 | Why |
| --- | --- | --- | --- |
| 10 | **Partial** | **Conditional** | Both halves of the revision-2 Partial are closed: R5 moves the secret gate to index time, so secret scanning now *applies* at the vault boundary rather than only on write and export; and N1 is fixed, which matters more than it looks, because revision 2's stated mitigation was "at `inject: false`, never reaches an agent's prompt automatically" and `promotion_service` was the path that made that claim **false**, durably, into a system prompt. But R14 keeps `secret_gate: "warn"` available, and a mapping set that way reintroduces exactly the exposure the criterion describes — so the mark is `Conditional`, not `Satisfied`. Marking it Satisfied on the strength of the default alone would be the overstatement revision 2 was corrected for |
| 9 | Satisfied, boundary named | **Satisfied, boundary named AND enforced** | Ruling R6 adopts the semantic reading and endorses the refusal to bypass FR-2.1, on the condition that the completeness check is a **hard test failure** for an unclassified column. That condition is now explicit in ADR-006 and test-strategy |
| 3, 4 | Satisfied | Satisfied | Unchanged in mark, but N1 and N3 show the *reason* they hold is now enforced statically rather than by an enumeration |

**Ten Satisfied, three Partial.** AC5, AC11 and AC12 remain Partial for the reasons already
stated; none of the three was affected by this round.

## The table

| # | Acceptance criterion | Satisfying unit | Revision 1 | Release one | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | Vault access is opt-in and confined to a configured root with explicit include/exclude rules | **U1**; U4 | Satisfied | **Satisfied** | `test_config_validation.py`, `test_scan_exclusions.py` |
| 2 | Folder-to-scope mappings are explicit; private and short-lived scopes are not silently written to a broadly shared or synced folder | **U1**; U8 | Satisfied | **Satisfied** | `test_config_validation.py` — overlap, duplicate `(scope, scope_id)`, `federated`, `session`, and a `managed_folder` outside a writable mapping all fail load |
| 3 | Existing allowed vault notes participate in `memory_recall` through a derived, ranked index | **U6** (the `Memory` builder); U5; U5's F9 guards | Satisfied *but unreachable* | **Satisfied** | `test_vault_memory_builder.py` is load-bearing; then `test_recall_source_fields.py`, `test_recall_scope_isolation.py`, `test_native_maintenance_guards.py` |
| 4 | Recall results identify the vault source, note path, mapped scope, and index freshness | **U6**; U2 | Satisfied *but unreachable* | **Satisfied** | `test_recall_source_fields.py` — all four fields, `source_path` vault-relative, `stale` on touch |
| 5 | External create, edit, rename, and delete operations are reflected after a bounded, observable reconciliation step | **U5**; U11 | Partial | **Partial** — [note 5](#note-5-rename) | `test_reconcile.py`, `test_rename_detection.py` |
| 6 | Resolvable wikilinks appear as graph relationships, with malformed or ambiguous links reported rather than silently misresolved | **U3**; U5; U9 | Satisfied | **Satisfied** | `test_links.py`, `test_findings_table_covered.py`, `test_memory_provider_vault.py` |
| 7 | `memory_store` creates and updates normal Markdown notes only in the managed folder | **U8** | Satisfied | **Satisfied** | `test_managed_write.py`, including a crafted traversal key |
| 8 | Normal memory operations never modify an unmanaged vault note | **U8**; U4 and U6 by construction | Satisfied | **Satisfied** | `test_unmanaged_never_written.py`, `test_forget_contract.py` |
| 9 | CAO's derived SQLite/BM25/graph state can be deleted and deterministically rebuilt from the vault | **U5**; U2 | Satisfied (proof unconstructable) | **Satisfied, with a proof boundary** — [note 9](#note-9-the-proof-boundary) | `test_rebuild_determinism.py`, three assertions, two column groups |
| 10 | Existing scope, authorization, secret scanning, path traversal, symlink, audit, and concurrent-write protections apply at the vault boundary | **Cross-cutting**: U3/U4 for secrets, U4, U6, U7, U8; U5 for audit and the four maintenance guards | Partial (rev 2) | **Conditional** — Satisfied under the shipped default `secret_gate: "reject"`; Partial under an operator-elected `"warn"`. [note 10](#note-10-secret-scanning-after-ruling-r5) | `test_scan_symlinks.py`, `test_recall_scope_isolation.py`, `test_managed_write.py`, `test_concurrent_edits.py`, `test_reconcile.py`, `test_curator_recall_gate.py`, `test_native_maintenance_guards.py`, `test_secret_gate_index_time.py`, `test_promotion_service_native_only.py`, plus twenty teeth tests |
| 11 | The workflow operates while the Obsidian application is closed | **Structural**, asserted by U11 | Partial | **Partial** — [note 11](#note-11-obsidian-closed-versus-obsidian-open) | `test_obsidian_closed.py` |
| 12 | An explicit migration can move selected native CAO memory into the managed folder, preserving representable metadata and reporting lossy fields | **U10** | Partial | **Partial** — [note 12](#note-12-seven-fields-are-not-representable) | `test_migrate_lossy.py` |
| 13 | Focused tests cover malformed YAML, duplicate note names, excluded paths, dangling links, symlinks, partial files, concurrent edits, and rebuilds | **U3, U4, U5, U8** through the fixture vault | Satisfied (unrepresentative corpus) | **Satisfied** | Eight named conditions, each with a fixture case and an asserting test; corpus now carries real-world names |

**Nine Satisfied, one Conditional, three Partial.** Revision 1 claimed ten Satisfied while
two of them were unreachable as designed and one protection family was overstated. Revision
3 fixes those causes — the vault `Memory` builder (AC3, AC4), the index-time secret gate and
the `promotion_service` predicate (AC10) — and then declines to claim the tenth outright,
because AC10's satisfaction now depends on a setting. That is the same standard applied when
AC10 was self-downgraded in revision 2: a mark that needs a footnote to be true should not be
a bare `Satisfied`.

## Note 5 rename

`reconcile` reflects create, edit and delete completely. Rename splits four ways; two are
satisfied and two are reported rather than resolved.

| Rename case | Release one |
| --- | --- |
| Note carries `cao.key`, renamed or moved within its mapping | **Fully satisfied.** Identity does not involve the path, so the row, access counters and typed edges survive |
| No `cao.key`, pure rename with unchanged content | **Satisfied** by content-hash alias absorption; the former path is recorded in `vault_note_alias` |
| No `cao.key`, renamed **and** edited in the same reconcile window | **Not satisfied.** The hash cannot match, so no rename is inferred. Reported as delete-plus-create with `rename_with_edit_unresolved` (warn); the `cao_key` changes and `origin="vault"` edges are dropped and re-derived from current body links |
| Two notes with byte-identical content, one renamed | **Not satisfied.** Ambiguous; reported as `rename_ambiguous` (warn); all treated as new |

Why it is not closed: the only fixes are writing `cao.key` into an unmanaged note (forbidden
by AC8 and the non-goal) or content-similarity matching (which reintroduces exactly the
guessing AC6 forbids). See
[adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md),
rejected alternatives B and D.

The documented remedy is for the human to add `cao.key` in Obsidian, and
`cao memory vault status` reports how many indexed notes lack one, so the exposure is
measurable. `cao memory vault adopt` is the release-two candidate.

**Revision 2 addition.** One thing that would have silently *worsened* this note is now
fixed: revision 1's path-derived keys relied on `_sanitize_key`'s 60-character truncation,
so two long-titled notes in one nested folder collided and both quarantined — a
false-collision that looks like a rename failure. The mapping-relative digest suffix
removes it (F14).

## Note 9 the proof boundary

Rebuild determinism holds for everything this design controls, and the proof says exactly
which columns those are.

**Byte-deterministic**, because every identifier is a digest and every timestamp comes from
the file, the frontmatter or one captured run-start: `vault_note`'s identity and content
columns, `vault_finding`'s content columns, `vault_note_alias` apart from `created_at`,
vault `memory_metadata` rows including `id`, and `memory_relationships`' endpoints, type,
origin, status, confidence, rank, attributes and `source_updated_at`.

**Not byte-deterministic, compared structurally instead:** `vault_note.last_reconciled_at`,
`vault_finding.id`/`reconcile_run_id`/`created_at`, `vault_note_alias.created_at`, and
`memory_relationships.id`/`created_at`/`updated_at`.

The relationship columns are the substantive part of the boundary.
`MemoryRelationshipModel.id` defaults to `str(uuid.uuid4())`, and edges must be written
through `MemoryRelationshipService` because of its documented single-boundary invariant
(FR-2.1). Minting a deterministic id would mean bypassing endpoint resolution, scope checks,
taxonomy validation, dedup and audit emission to satisfy a test. That trade was rejected;
see [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md)
alternative F.

**Under a strict reading — "every column of every derived row is byte-identical across two
rebuilds" — this criterion is Partial, not Satisfied.** Under the reading that matches the
criterion's stated purpose — derived state can be deleted and rebuilt from the vault with no
semantic drift and no residue — it is Satisfied. This document takes the second reading and
states the first so a reviewer can disagree with the interpretation rather than discover it.

Revision 1's "the canonical dump excludes nothing" made the proof unpassable and, worse,
made column-filtering the only available repair — which is how a determinism test becomes
vacuous. The two-group split with the every-column-in-exactly-one-group completeness
assertion is what keeps it both passable and meaningful.

**Ruling R6 adopts this reading and attaches one hard condition: the completeness check must
be a TEST FAILURE for an unclassified column, never a warning.** Without that, the split
degrades back into revision 1's filtering problem by a slower route — a future author adds a
column, leaves it unclassified, sees a passing suite, and the comparison quietly shrinks.
The assertion therefore reads the live schema with `PRAGMA table_info` on all four tables
and asserts **set equality** against the union of the two declared groups; a column in the
schema and in neither group fails, and so does a declared column that no longer exists. The
ruling also explicitly endorses the refusal to bypass `MemoryRelationshipService`'s FR-2.1
single-boundary invariant, so that trade is closed and not to be revisited.

## Note 10 secret scanning after ruling R5

Revision 2 marked this Partial because secret scanning ran only on the write and export
paths. Ruling R5 moved it to index time, and N1 closed a second hole that made revision 2's
stated mitigation false. Both halves are closed — and the mark is **Conditional** rather
than Satisfied, because ruling R14 keeps `secret_gate: "warn"` available and a mapping set
that way reintroduces the exposure the criterion describes.

**The mark in one line:** Satisfied under the shipped default `secret_gate: "reject"`;
Partial under an operator-elected `"warn"`.

**Said plainly: an operator who sets `secret_gate: "warn"` accepts the exposure this
criterion describes, knowingly.** They must write it into a specific mapping — there is no
global switch and no way to reach it by omission, since an absent value resolves to
`reject` — the `secret_detected` finding is still reported for every match, and
`cao memory vault status` names the mapping for as long as the setting holds. That is what
makes it an informed election rather than a hole. It does not make the criterion
unconditionally satisfied, which is why the mark is `Conditional` and not `Satisfied`.

All seven protection families the criterion names now apply at the vault boundary:

| Family | How it applies |
| --- | --- |
| Scope | Non-overlapping mappings give a note exactly one scope; alias-aware binding keeps `(scope, scope_id)` resolution stable; cross-scope edges dropped |
| Authorization | `scope_write_allowed(caller_scope, scope)` runs unchanged on the vault write path |
| **Secret scanning** | **Now at index time** (R5): `scan_for_secrets` runs during reconcile; `secret_gate: "reject"` (default) quarantines with a `secret_detected` finding, `"warn"` indexes and reports. Still enforced on the write path and on export |
| Path traversal | `os.path.realpath` plus a single positive `startswith(root + os.sep)` inline beside every filesystem sink; `safe_join_under_base` on the write path; **and `promotion_service` no longer reads a vault `file_path` unconfined** (N1) |
| Symlink | Any symlinked component below the root refuses the note; hardlinks refused too |
| Audit | Three whitelisted events; findings and audit fields content-free |
| Concurrent write | `locked_atomic_rewrite` plus a `content_sha256` conflict check inside the lock |

**Why N1 mattered to this mark specifically.** Revision 2's Partial was softened by the
claim that a secret-bearing note "at `inject: false`, never reaches an agent's prompt
automatically". `promotion_service.plan()` was precisely the path that broke that guarantee:
it read the note's `file_path` with no confinement and promoted the content into a profile's
learned-patterns section — a **persistent system prompt on disk**, durable across sessions
and entirely outside the injection policy. So the mitigation was not merely incomplete, it
was false. With the `source_kind = 'native'` predicate in place it is true as written.

**Two residuals, named rather than hidden.** Neither is a gap in whether the protection
applies; both bound its coverage.

1. **The gate is a heuristic deny-list.** One of its six patterns is
   `(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}`, which matches ordinary vault
   prose — a runbook quoting `password: hunter2` as an example, a design note about
   credential handling. Under `reject` such a note becomes unrecallable. `secret_gate:
   "warn"` exists as the documented per-mapping election for an operator who knows their
   corpus, with `reject` as the shipped default so the safe posture needs no configuration.
   **The mode is settled by ruling R14**: same key, same two values, `reject` default, and
   the finding reported in either mode so the election changes what is indexed and never what
   the operator is told.
2. **The gate is reconcile-time.** A secret added to an already-indexed note is exposed
   until the next reconcile. This follows directly from the no-continuous-watching non-goal
   and is surfaced through `index_freshness`; a criterion cannot demand continuous
   enforcement while a non-goal forbids continuous watching.

### Why `warn` makes this Conditional rather than simply Satisfied

Under `secret_gate: "warn"` a secret-bearing note is indexed and recallable by deliberate
query. There is a respectable argument that this is an **operator-elected relaxation**, not a
gap — the same way `memory.enabled = false` is not a gap in the memory subsystem — and that
the mark should therefore be Satisfied on the strength of the control existing and the
default being safe. Two further points support it: the finding is reported in **both** modes,
so the operator is never uninformed; and `warn` already ships on the **write** path today,
so index-time `warn` introduces no property the shipped code lacks.

That argument is recorded because it is reasonable, and it is **not** the one this table
follows. The criterion asks whether the protections *apply at the vault boundary*. Under
`warn` the scanning applies and the reporting applies, but the enforcement does not — a note
the gate identified as credential-shaped becomes part of the recall corpus. Reducing that to
a bare `Satisfied` would require the reader to already know about a setting the word does not
mention, which is precisely the overstatement revision 2 was corrected for. `Conditional`
costs one extra word and carries the whole truth.

Two things keep `Conditional` from being a hedge. The direction is stated (`reject`
Satisfied, `warn` Partial), so a reviewer can decide for their own configuration rather than
for a hypothetical one. And the maximal-exposure pairing — `warn` together with
`inject: true` — is a config-load warning and a permanent `status` line under
[adr-007-configuration-surface.md](adr-007-configuration-surface.md) rule 20, so the one
combination that would make this criterion most clearly unmet cannot be reached silently.

## Note 11 Obsidian closed versus Obsidian open

The criterion as written is satisfied and is structural: no unit launches a process, opens a
socket, or references an Obsidian binary or plugin path. Nothing requires Obsidian to have
ever run.

What is **not** guaranteed is the adjacent case the criterion does not name: Obsidian open
and actively writing. A note saved during a reconcile is caught by the two-stat stability
check, skipped with `unstable_skipped` (warn), and its prior row left intact. Correct
behavior — never index a torn file — but it means a reconcile concurrent with active editing
can legitimately skip notes, and the operator sees a warn count rather than a clean run.

Recorded so the gap is stated rather than surprising, and so nobody reads AC11 as
"concurrent with Obsidian" when it says "while Obsidian is closed".

## Note 12 seven fields are not representable

The migration itself is complete: dry-run by default, `--apply` to write,
`--delete-source` behind a second confirmation. What is partial is "preserving", and the
criterion anticipates it by requiring lossy fields to be **reported**. They are, by name.

| Native field | Representable | Disposition |
| --- | --- | --- |
| `key`, `scope`, `scope_id` | Yes | `cao.key` plus the mapping's scope |
| `memory_type` | Yes | `cao.type` |
| `tags` | Yes | frontmatter `tags` |
| `created_at` | Yes | `cao.created` |
| `updated_at` | Yes | the note's mtime |
| Content body | Yes | the note body |
| Typed `memory_relationships` rows | Yes | `cao.links`, with type, status, confidence and origin |
| `access_count` | **No** | Reported lossy — derived usage data with no canonical home in a Markdown note |
| `last_accessed_at` | **No** | Reported lossy, same reason |
| `last_compiled_at` | **No** | Reported lossy — describes a native compile pipeline that does not run on vault notes |
| `source_provider` | **No** | Reported lossy — provenance CAO cannot keep accurate after a human edit |
| `source_terminal_id` | **No** | Reported lossy, and ephemeral |
| `related_keys` (legacy text column) | **No** | Reported lossy — the compiler's computation-state marker, superseded by typed relationships, which **do** migrate |
| Append-only `## <ISO8601Z>` history beyond what fits | **Partial** | Written into the managed note when it fits `max_note_bytes`; entries beyond it reported lossy with a count, never silently truncated |

**Why ruling R2 exists.** Without the widened `uq_memory_key_scope`, `migrate` without
`--delete-source` keeps the key by design, so every migrated memory would collide with its
native original and the default migration mode would be unusable — which would weaken this
criterion further. That is why refusing colliding keys was rejected.

## Criteria satisfied by construction

Two criteria have no single implementing unit, worth calling out for a reader looking for
one.

- **AC10** is cross-cutting by nature: not a feature, but the requirement that existing
  control families keep working across a new boundary. Distributed across U4 (traversal,
  symlink, hardlink), U6 (inline confinement, scope isolation), U7 (injection policy and the
  curator gate), U8 (authorization, secret gate, concurrency) and U5 (audit, and the three
  maintenance refusal guards). The teeth tests are the substantive evidence.
- **AC11** has no implementing unit at all. It is a property the design has by refusing to
  add a runtime dependency, protected by an assertion rather than produced by code.

## Units with no criterion

| Unit | Why it exists |
| --- | --- |
| U2 `vault-schema` | Carries no criterion by itself and is a precondition for AC3, AC4 and AC9. Its own reason for separate existence is risk: a constraint widening on a shared table with no drift guard, where a nullable discriminator would silently remove native key uniqueness |
| U9 `vault-graph` | AC6 says wikilinks "appear as graph relationships". U3 and U5 make the rows exist; U9 makes them appear in a `GraphView`. Listed separately because it ships independently, touches a different module, and is an **exposure surface** requiring its own security review and a fixed position after U7 |
| U11 `vault-cli` | Carries AC5's "observable" half — a reconciliation step nobody can invoke or inspect is not observable — plus the operator surface and the `tui/src/catalog.rs` obligation |
| U12 `vault-docs` | AC4's "index freshness" and AC6's "reported" are meaningful only if a user can find out what a finding code means. Also the home for ADR-004's boundary table, which the issue asks to have documented, and for the corrected `SKILL.md` Forget wording |

## Behavior changes that are not acceptance criteria

Three deliberate behavior changes fall outside the criteria and are recorded so they are
reviewed rather than discovered.

1. **`forget()` on a vault note de-indexes rather than deletes** (ruling R3), and its return
   type changes from `bool` to a `ForgetResult` whose `__bool__` preserves the old value.
   The MCP `memory_forget` docstring currently says "Deletes the wiki topic file" while
   returning `{"deleted": true}`; both are corrected, along with **both**
   `skills/cao-memory/SKILL.md` copies, which are the agent-facing contract.
2. **`cao memory export --scope <vault-bound>` is refused.** Today `_collect_topics` walks
   native paths, so it would silently emit an empty bundle. Refusal with a pointer to the
   vault is honest for release one; the vault is already Markdown. Open question 3 in
   [design.md](design.md).
3. **`wiki_lint` enrichment is unavailable for vault-bound scopes**, reported through
   `GraphView.meta` with a **discriminated cause** — `disabled_by_setting`,
   `unavailable_vault` or `failed` — rather than a single "unavailable". Consequence: vault
   nodes carry no orphan, contradiction, stale-claim or graph-density flags. This touches no
   criterion, but it means the graph is less informative for vault scopes than for native
   ones, and without the discriminated cause a user could not tell a permanent design
   boundary from a live fault. The distinction is worth making on its own merit — a vault
   user should see a boundary and a native user should see a fault — and **not** because
   lint is currently failing: CI on `main` at `6c890ca` is green, so `_build`'s
   `except Exception` → `meta["lint_error"]` path is genuine graceful degradation that is
   not presently firing. There is no pre-existing masking here for this change to extend.
   Worth stating explicitly: the vault design never *depends* on lint being correct.
   `wiki_lint`, `wiki_healer`, `cleanup_service` and `promotion_service` are handled by
   **refusal and exclusion**, not graceful degradation, so correctness rests only on the
   `source_kind` predicate being applied. That property is why the F9 finding was
   downgraded, and it holds **whatever state the lint subsystem is in** — which is the right
   way to state it, since the premise that it was broken proved false.
4. **A credential-bearing vault note is quarantined at reconcile by default** (ruling R5),
   so a note that was recallable under revision 2's design is not under revision 3's. An
   operator can elect `secret_gate: "warn"` per mapping. Both the quarantine and the
   election are visible in `cao memory vault status`.
5. **`promotion_service` will never promote a vault-backed lesson** (N1). If
   vault-authored agent lessons are ever wanted, they must arrive through
   `resolve_candidates(require_injectable=True)` like every other vault read — a deliberate
   future decision, rather than something a missing predicate grants by accident.
