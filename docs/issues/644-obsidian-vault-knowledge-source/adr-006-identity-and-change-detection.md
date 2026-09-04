# ADR-006: Note identity, change detection and rebuild determinism

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Revision 3** — the completeness assertion is now required to be a hard failure (ruling R6). Revision 2 changed path-derived keys and the determinism proof.
**Decision owner:** the issue's AC5 (external rename reflected) and AC9 (derived state deterministically rebuildable)
**Related:** [design.md](design.md), [adr-001-typed-relationship-representation.md](adr-001-typed-relationship-representation.md), [adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md), [test-strategy.md](test-strategy.md)

---

## Context

Two acceptance criteria meet here.

AC5: "External create, edit, rename, and delete operations are reflected after a
bounded, observable reconciliation step." Rename is the hard one. Obsidian renames a note
by moving the file and rewriting inbound `[[links]]` across the vault. If CAO's identity
for a note is its path, a rename destroys the identity, and with it the note's
`memory_metadata` row, its access history and every typed edge keyed to its `cao_key`.

AC9: "CAO's derived SQLite/BM25/graph state can be deleted and deterministically rebuilt
from the vault." Deterministic means two rebuilds of an unchanged vault produce identical
rows — which today's native store does not achieve, because `_upsert_metadata` mints
`id=str(uuid.uuid4())` on insert and stamps `updated_at=datetime.now(timezone.utc)`.

The existing identity contract is fixed and must be honored:
`(scope, scope_id, key)` where `key` matches `^[a-z0-9-]{1,60}$` (`models/memory.py`
`MemoryKey`, and `MemoryService._sanitize_key` at line 548, which **truncates at 60**),
enforced in SQLite by `uq_memory_key_scope` at `database.py:118`.
`MemoryRelationshipModel` stores endpoints as `source_key`/`target_key` strings, so a
changed key silently orphans edges rather than failing loudly.

## Revision 2 changes

| Change | Driver |
| --- | --- |
| Path-derived keys are **mapping-relative** and always carry a **stable 8-hex digest of the mapping-relative path**, so the 60-character truncation in `_sanitize_key` can no longer silently collide two long-titled notes in one nested folder | F14 |
| Collision causes are enumerated: user-authored duplicate `cao.key`, **and** derived-key digest collision. `status` counts each separately. Revision 1 treated collision as user-authored only | F14 |
| The determinism proof **splits** its canonical dump into byte-equal and structural column groups, because `MemoryRelationshipModel.id` is a service-minted `uuid4` this design must not bypass, and three run-provenance columns are legitimately per-run | F5, directed fix D2 |
| The fixture factory sets **fixed mtimes** from a constant table via `os.utime`, reconciled with the two-stat stability check | directed fix D2 |

## Decision

### Identity

**`cao.key` in frontmatter is canonical. Absent it, the key is derived deterministically
from the filename stem with a digest of the complete mapping-relative path. This is a
decided divergence from the earlier path-prefix design: the key is a graph-node label
rendered in the dashboard and MCP Apps views, so a stem leaks less vault structure while
the digest preserves uniqueness and determinism. Rename is absorbed by a content-hash alias
table, never by writing to an unmanaged note.**

1. **`cao.key` present** — that is the key. It survives any rename, any move between
   folders in the same mapping, and any title change, because nothing about the path
   participates. This is the recommended state, and `status` reports how many indexed
   notes lack it.
2. **`cao.key` absent** — the key is derived as:

   ```
   stem   = sanitize(filename_stem_without_extension)
   digest = sha256(NFC_normalized_mapping_relative_path_bytes) hex [:8]
   key    = stem[:51] + '-' + digest
   ```

   Three properties make this the right shape:

   - **Length is bounded by construction.** 51 + 1 + 8 = 60, exactly the `MemoryKey`
     budget, so `_sanitize_key`'s silent truncation can never be what decides identity.
     Revision 1 relied on that truncation, which made two long-titled notes in one deep
     folder — a completely ordinary vault condition — truncate to the same key and both
     get quarantined, with the cause invisible in the finding.
   - **It is a pure function of that note's own path.** It does not depend on what other
     notes exist, on discovery order, or on whether a third colliding note is added
     later. That is what distinguishes it from the arbitrary disambiguating suffix this
     ADR rejects for user-authored `cao.key` duplicates below, and it is why applying a
     digest here is not inconsistent with refusing one there.
   - **Its digest is mapping-relative, not vault-relative**, so renaming a mapped folder
     in configuration does not rewrite every key beneath it. The readable prefix is the
     filename stem rather than the relative path, deliberately avoiding directory-structure
     disclosure through graph labels while retaining the full path in the digest.

   The cost is readability: `retrieval-path-a1b2c3d4` rather than `retrieval-path`. That
   is the intended incentive — a user who wants a clean, stable, readable key sets
   `cao.key`, and the derived form advertises that it is derived.
3. **`note_uid`** is `sha256(vault_id \0 scope \0 scope_id_or_empty \0 cao_key)` rendered
   hex — a pure function of identity, deliberately **not** a `uuid4`, because AC9
   requires two rebuilds to produce the same rows. For vault rows `memory_metadata.id` is
   set to the same derived digest for the same reason. This diverges from native rows'
   `uuid4` and is documented on the column.
4. **Aliases are never identity.** An Obsidian `aliases:` entry affects link resolution
   only ([adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md)).
5. **Title, H1 and filename are display metadata.** They may feed a human-facing label
   but never the identity tuple.

### Collision causes, both named

`status` counts these separately, because the remedies differ:

| Cause | Finding | Remedy |
| --- | --- | --- |
| Two notes in one `(vault_id, scope, scope_id)` declare the same `cao.key` | `key_collision` (error), **both** quarantined | The human edits one note's `cao.key` |
| Two distinct mapping-relative paths produce the same derived key — requires both a matching 51-character stem **and** a colliding 8-hex digest | `key_collision` (error) with a `derived` detail flag, **both** quarantined | Set `cao.key` on either note |

Neither is disambiguated automatically. For the user-authored case, a generated suffix
would be unstable under insertion of a third colliding note, and picking a winner is the
silent misresolution AC6 forbids. For the derived case the probability is negligible
(roughly 2^-32 per stem-colliding pair) and quarantine is the honest backstop rather than
the expected path.

### Rename absorption

On each reconcile, after scanning:

1. Compute the set of `vault_relpath` values that disappeared since the last run and the
   set that appeared.
2. For an appeared note with **no `cao.key`**, if its `content_sha256` matches exactly one
   disappeared note in the same `(vault_id, scope, scope_id)`, treat it as a **rename**:
   keep the disappeared note's `cao_key`, update `vault_note.vault_relpath`, and record
   the former path in `vault_note_alias`. The note's `memory_metadata` row, access
   counters and `origin="vault"` edges all survive.
3. If the match is not exactly one, no rename is inferred. Zero means
   delete-plus-create. Two or more means `rename_ambiguous` (warn) and all are treated as
   new.
4. If the content **also** changed in the same window, the hash cannot match and the
   heuristic cannot fire. The note is reported as delete-plus-create with
   `rename_with_edit_unresolved` (warn); its key changes and its `origin="vault"` edges
   are dropped and re-derived. The documented remedy is for the human to add `cao.key` in
   Obsidian — CAO does not write it.
5. `vault_note_alias` is derived state and is dropped by `rebuild`. A rebuild therefore
   re-derives path-based keys from current paths, which is correct: the alias table
   carries identity across a *reconcile*, not as a second source of truth.

This mirrors `ProjectAliasModel` (`clients/database.py:215`), which already absorbs
project-identity churn the same way.

### Change detection

Three signals, cheapest first:

1. `size_bytes` — differs, so the content differs.
2. `mtime_ns` — differs, so re-read. **Never authoritative alone**: cloud sync rewrites
   mtime without changing content, and a same-second write can leave mtime unchanged on
   coarse-granularity filesystems.
3. `content_sha256` — authoritative. `frontmatter_sha256` is recorded separately so a
   metadata-only change can skip body re-tokenization.

**Text preparation is part of identity, not merely presentation.** A single leading UTF-8
BOM is stripped before frontmatter recognition, parsing, and hashing. Line endings are
normalized to `\n` before parsing as well as before hashing; otherwise a CRLF frontmatter
delimiter can be missed and the same note acquires different parsed metadata on different
platforms. Consequently `content_sha256` and `frontmatter_sha256` are calculated from the
same normalized, BOM-free text seen by the parser.

**Stability check, for partial and half-synced files.** Before hashing: `stat`, read,
`stat` again. If `(size_bytes, mtime_ns)` changed across the read, retry up to three
times with a short backoff, then mark `unstable_skipped` (warn) and leave the prior row
**untouched**. A torn file is never indexed, and a note being edited in Obsidian right
now is deferred to the next reconcile.

Additionally: filenames matching sync-conflict patterns are refused before any read
(`sync_artifact_skipped`), and a `.md` file that is now zero bytes when a non-empty
`content_sha256` is on record is treated as `unstable_skipped` rather than as an emptied
note — the likelier cause is an in-progress sync.

**Interaction with fixed-mtime fixtures (D2).** Because the check compares
`(size, mtime_ns)` across the read, a fixture vault with deliberately fixed mtimes passes
it trivially — which is what makes the determinism proof stable. The consequence is that
the *unstable* fixture case cannot rely on natural mtime drift to trigger: it must mutate
the file **during** the read through an explicit hook. Stated here so the fixture and the
check are designed against each other rather than by accident.

### Rebuild determinism

`rebuild` deletes `vault_note`, `vault_finding`, `vault_note_alias`,
`memory_metadata WHERE source_kind = 'vault'` and
`memory_relationships WHERE origin = 'vault'`, then runs a full reconcile. Six conditions
must hold, each with a named test:

1. **Traversal order is sorted, never filesystem order.** `scan.py` NFC-normalizes each
   POSIX relative path before sorting its UTF-8 bytes. This is load-bearing: `readdir`
   commonly returns NFD on macOS and NFC on Linux, so sorting before NFC normalization
   would reconcile the same vault in a different order on the two platforms. `os.scandir`
   order also varies by filesystem, inode allocation, and creation sequence.
2. **Every identifier this design controls is derived, not minted.** `note_uid`, vault
   `memory_metadata.id`, and `vault_finding.id` (=
   `sha256(reconcile_run_id \0 code \0 vault_relpath)`) are digests. No `uuid4` on the
   vault path.
3. **Every timestamp comes from the file, the frontmatter, or one captured run-start.**
   `updated_at` from the note's mtime; `created_at` from `cao.created` or `created`
   frontmatter if present, else the note's mtime, else the run-start;
   `last_reconciled_at` from the single `run_started_at`. No per-row `now()`.
4. **Case policy is fixed and platform-independent.** Relative paths are used as stored
   and never case-folded. Two paths differing only by case are both quarantined with
   `path_case_collision`. Case-folding would silently merge two distinct notes on a
   case-sensitive filesystem.
5. **Text normalization is fixed before parsing and hashing.** Content is read as UTF-8
   with errors surfaced (a non-UTF-8 note is quarantined, not lossily decoded); one leading
   BOM is stripped and line endings are normalized to `\n` before both operations, so the
   same note produces the same parsed frontmatter and hashes after a checkout on another
   platform.
6. **Findings are sorted by `(code, vault_relpath)`** before insertion.

**What this design does not control, and therefore cannot make byte-deterministic.**
`MemoryRelationshipModel.id` defaults to `str(uuid.uuid4())`, and edges must be written
through `MemoryRelationshipService` because of its single-boundary invariant (FR-2.1).
Bypassing the service to mint a deterministic id would break a documented invariant of a
shipped feature to satisfy a test, which is the wrong trade. Relationship `id`,
`created_at` and `updated_at` are therefore compared **structurally**, not byte-equally.
`source_updated_at` is the source note's `updated_at` at write time, which is the note's
mtime, so it **is** byte-equal.

The split, and the rule that keeps it from becoming vacuous, is specified in
[test-strategy.md](test-strategy.md): every column appears in exactly one of the two
groups, and a column may not be absent from both.

**Ruling R6 attaches one hard condition to that rule: the completeness assertion must be a
TEST FAILURE for an unclassified column, never a warning.** This is the whole load-bearing
part of the split. A warning is advisory, so a future author adding a column can leave it
unclassified, see a passing suite, and move on — at which point the two-group split has
degraded back into revision 1's filtering problem by a slower and less visible route. As a
failure, an unclassified column stops the suite and forces the author to decide whether the
new column is byte-deterministic or merely structural, which is exactly the decision the
split exists to force. The assertion enumerates the live schema (via
`PRAGMA table_info` on each of the four tables) and asserts set equality against the union
of the two declared groups; a column in the schema and in neither group fails, and so does
a column in a group that no longer exists in the schema.

## Consequences

**Positive.**

- A note with `cao.key` has a genuinely stable identity: rename it, move it between
  folders in the same mapping, retitle it, and its memory row, access history and typed
  edges persist. That is the strongest form of AC5.
- Rename absorption needs no write to the vault, so it composes with AC8 rather than
  fighting it.
- The digest-suffixed derived key removes truncation as a collision source entirely, and
  it removes it *by construction* rather than by hoping paths stay short.
- Derived identifiers make AC9 provable where this design owns the id, and the split
  proof makes the parts it does not own explicitly and honestly out of byte-scope rather
  than quietly filtered.
- Change detection is cheap in the common case (stat only) and correct in the uncommon one
  (hash).

**Negative, and accepted.**

- **Derived keys are ugly.** `retrieval-path-a1b2c3d4` is not what a user would have
  chosen. The remedy is `cao.key`, and `status` counts how many notes lack one.
- **Rename plus edit in one window, on a note with no `cao.key`, loses the key.** Its
  `origin="vault"` edges are re-derived from its current body links, so body-authored
  `relates_to` edges return; a hand-curated `cao.links` entry would have carried a
  `cao.key` anyway. It is reported, not silent. Fixing it properly needs either a write
  into an unmanaged note or content-similarity matching, and similarity matching
  reintroduces exactly the guessing
  [adr-004-supported-markdown-boundary.md](adr-004-supported-markdown-boundary.md)
  refuses.
- **Vault rows have derived ids while native rows have `uuid4`s.** Two id disciplines in
  one table is a wart, accepted because AC9 requires determinism and retrofitting derived
  ids onto native rows is outside this issue.
- **Relationship rows are not byte-deterministic**, so the AC9 proof is a two-part
  comparison rather than one byte-equality assertion. This is a real reduction in the
  strength of the proof, stated rather than hidden.
- **`rebuild` resets vault `access_count` and `last_accessed_at`.** Correct in principle —
  derived — but a user relying on `sort_by="usage"` will notice.
- **A non-UTF-8 note is quarantined** where a lossy decode would have indexed something.
  Deliberate: a lossy decode makes the content hash a function of the decoder's error
  handling, which breaks determinism.
- **The stability check costs two extra `stat` calls per candidate**, paid only on notes
  whose cheap gate already indicated a change.

## Alternatives rejected

### A. Path is identity — key derived from the path, always, with no digest

Rejected, and revision 1's version of this ADR was closer to it than it should have been.
Without a digest the key depends on `_sanitize_key`'s 60-character truncation, so identity
is decided by a silent length cut. With a digest the truncation is no longer
identity-bearing. Separately, pure path identity makes AC5's rename arm unsatisfiable:
every Obsidian rename becomes a delete plus a create, dropping the row, the access history
and every typed edge — and since Obsidian's rename also rewrites inbound links across the
vault, one rename would churn a large fraction of the graph for no semantic change.

### B. Write a `cao.key` or `cao.uid` into every note on first scan

Rejected. It is the cleanest identity story available and it directly violates AC8 and the
non-goal on unrestricted writes: adopting a vault would mean rewriting every note in every
mapped folder, producing a large unexplained diff in the user's version-controlled or
synced vault on first use. A narrowly-scoped, explicitly invoked
`cao memory vault adopt <path>` — one note, one audited write, human-driven — is a
reasonable future addition and is deferred rather than refused.

### C. Inode or file-id based identity

Rejected. Not portable, does not survive a cloud-sync round trip or a git checkout, and is
not reconstructible from the vault alone — which makes it unusable for AC9, since a
rebuild from vault content could never reproduce it.

### D. Content-similarity rename matching instead of exact-hash matching

Rejected for release one. It would handle the rename-plus-edit case, at the cost of a
similarity threshold — a tunable that is wrong for somebody, produces different answers as
a vault grows, and reintroduces silent misresolution. An exact hash either matches or does
not, and the non-match is reported.

### E. Trust mtime alone for change detection

Rejected. Cheap and wrong in both directions: sync and backup tools touch mtime without
changing content (spurious re-index churn), and a rapid rewrite can leave mtime unchanged
on coarse-granularity filesystems (a missed edit — the silent failure).

### F. Bypass `MemoryRelationshipService` to mint deterministic edge ids

Rejected. It would make the rebuild proof a single byte-equality assertion, which is
tempting. It would also break the FR-2.1 single-boundary invariant that a shipped feature
documents in its module docstring, bypassing endpoint resolution, scope checks, taxonomy
validation, dedup and audit emission — all to satisfy a test. The correct response is to
weaken the proof honestly and say which columns are structural, which is what
[test-strategy.md](test-strategy.md) does.

### G. Filter non-deterministic columns out of the canonical dump

Rejected, and named because revision 1's "the canonical dump excludes nothing" made this
the only available repair once the proof failed. Filtering is how a determinism test
becomes vacuous: each newly non-deterministic column gets quietly excluded until the
assertion compares almost nothing. The two-group split with the
every-column-in-exactly-one-group rule is the alternative that keeps the test both
passable and meaningful.

## Security and compliance implications

- **Identity stability is an authorization property, not just an ergonomic one.**
  `(scope, scope_id, key)` is what scope-based authorization and the relationship service's
  same-scope invariant key off. An unstable key means a note can silently acquire a
  different identity, and a later note reusing that slug would inherit its edges — the
  hazard `forget()`'s `_purge_relationships` (line 2717) was added to close for native
  memory. Quarantining on collision rather than auto-suffixing keeps it closed, and the
  digest keeps truncation from manufacturing collisions in the first place.
- **Refusing to write identity into unmanaged notes keeps AC8 total.** No read or
  reconcile path modifies a note CAO did not create. A test snapshots every unmanaged
  fixture note's hash before and after a full reconcile and asserts equality.
- **The stability check prevents indexing a partially-written file**, which matters beyond
  correctness: a half-synced note could contain a fragment of a different note, placing
  one note's content under another's identity and therefore under another's scope.
- **Determinism is an audit property.** If derived state can be deleted and rebuilt
  identically, an operator can verify that what CAO holds is exactly what the vault says,
  with no accumulated drift and no residue from a configuration that has since changed.
  The structural half of the proof preserves this for the columns it covers: same rows,
  same endpoints, same types, same origins.
- **Scoped deletion in `rebuild` is a containment control.** The deletes are keyed by
  `source_kind = 'vault'` and `origin = 'vault'`, so a rebuild cannot remove native memory
  rows or another producer's edges. An unscoped `DELETE FROM memory_metadata` would make a
  routine maintenance command destructive to unrelated data.
- **Derived keys encode path structure**, which is why the graph projection must not
  publish keys for notes that recall cannot reach — see the graph-exposure rule in
  [design.md](design.md). Identity design and exposure design are coupled here, and the
  coupling is deliberate rather than incidental.
- **Bounded, observable reconciliation is itself the security posture.** Nothing is re-read
  except during an explicit operation, so no background process reads a confidential
  directory on a schedule the operator did not choose, and every read is attributable to a
  command in the audit log.
