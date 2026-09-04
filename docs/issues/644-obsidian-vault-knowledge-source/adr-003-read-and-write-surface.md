# ADR-003: Mapped-folder reads with managed-folder writes

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Unchanged in revision 3** — no ruling touched it; ruling R5's index-time secret gate is a boundary-table matter and lives in ADR-004. Revision 2 added rule 8 on the read/write path-validation asymmetry.
**Decision owner:** the issue's open question "validate that whole-vault reads with managed-folder writes match user expectations before allowing edits to arbitrary notes"
**Related:** [design.md](design.md), [adr-005-index-vs-injection-policies.md](adr-005-index-vs-injection-policies.md), [adr-007-configuration-surface.md](adr-007-configuration-surface.md)

---

## Context

The issue lists two shapes for release one and flags that the first needs
validating:

- **Managed folder for both reads and writes** — "safer, but does not make existing
  vault knowledge recallable without moving or importing it."
- **Whole-vault read and write** — "more seamless, but CAO cannot safely preserve
  every user schema, plugin convention, attachment, or concurrent edit in a first
  release."

And separately, in Risks and Open Questions: "Validate that whole-vault reads with
managed-folder writes match user expectations before allowing edits to arbitrary
notes."

The forcing constraint is the issue's own acceptance criteria. AC2 requires:
"Folder-to-scope mappings are explicit; private and short-lived scopes are not
silently written to a broadly shared or synced folder." AC1 requires vault access be
"confined to a configured root with explicit include/exclude rules."

A whole-vault read has to answer: what scope does a note in no mapped folder get?
There are only two answers. A default scope — which is a silent mapping, exactly
what AC2 forbids. Or no scope, meaning the note is not indexed — which is
mapped-folder reads described differently. **The issue's "whole-vault read"
alternative is therefore incoherent with its own AC2**, and that is the finding this
ADR records rather than a preference.

The other half is genuine and stands: managed-folder-only reads would defeat the
feature's stated purpose. The first user story is "recall knowledge from selected
vault folders so that I do not maintain a separate agent-only knowledge base."

## Decision

**Reads are confined to explicitly mapped folders, minus exclusions. Writes are
confined to a single managed folder. A note in no mapped folder is invisible to CAO
— no row, no BM25 candidacy, no edge, no injection.**

1. `mappings[]` is the read surface. Each mapping names a `folder` relative to the
   vault root and the `(scope, scope_id)` it maps to. A `.md` file is a candidate
   only if it is under some mapping's folder.
2. **Mappings may not overlap.** No mapping's folder may be a prefix of another's.
   Overlap would give a note two scopes, which is the ambiguity AC2 forbids.
   Overlap is a config-load error.
3. `exclude[]` holds glob patterns matched against the POSIX relative path,
   subtracting from the mapped set. `.obsidian/`, `.trash/`, `.git/` and CAO's own
   `_cao-*` prefix are always excluded and are not configurable.
4. `managed_folder` is one path, and it must itself lie inside exactly one mapping
   whose `writable` is true. This is what ties "where writes go" to "which scope
   they land in" and makes AC2's short-lived-scope warning enforceable at config
   load rather than at write time.
5. `writable` is per mapping and defaults false. Only the mapping containing
   `managed_folder` may set it true in release one; a second writable mapping is a
   config error, since two writable folders in one scope is two write targets.
6. **`cao memory vault scan --dry-run` is the validation affordance the issue
   asks for**, and it is the default form of the command. It prints exactly which
   notes would become recallable, which would be quarantined, and with which
   findings — before any row is written. The user's answer to "does this match my
   expectations" is a diff they can read, not a leap of faith.
7. A note outside every mapping is not merely unindexed; a wikilink *to* it
   resolves to nothing and produces a `link_excluded` finding rather than an edge.
   An exclusion that leaks through a link is not an exclusion.
8. **The read surface and the write surface are validated differently, and the write
   surface is the strict subset.** A mapping's `folder` is validated for shape and
   containment but not against a character allowlist, because real vault folders
   contain spaces, apostrophes and non-ASCII characters; containment is proven at use
   time by `os.path.realpath` plus a single positive `startswith(root + os.sep)`
   inline beside each filesystem sink. `managed_folder` is additionally
   charset-validated, because the write path composes targets with
   `safe_join_under_base`, which validates each segment against
   `\A[A-Za-z0-9._-]+\Z`. The consequence worth stating plainly: **the set of paths
   CAO can ever create is a charset-bounded subset of the paths it can read**, which
   is the shape you want when reads are broad and writes are narrow. Full rules in
   [adr-007-configuration-surface.md](adr-007-configuration-surface.md) rules 7 and 8.

## Consequences

**Positive.**

- Satisfies AC2 and AC3 simultaneously, which neither of the issue's two listed
  alternatives does: existing vault knowledge becomes recallable (AC3) and every
  scope assignment is explicit (AC2).
- The read surface is enumerable and auditable. An operator can answer "what can
  CAO see?" by reading the mappings list, without reasoning about the whole vault's
  contents.
- Adoption is incremental. A user maps one folder, runs a dry-run scan, reads the
  plan, and only then applies. Widening is adding a mapping.
- Confidential material is excluded by *default* rather than by remembering to
  exclude it. The default read surface is empty.
- No note is ever moved or copied to become recallable, which is what
  managed-folder-only reads would have required.

**Negative, and accepted.**

- **Configuration burden.** A user with knowledge scattered across many folders
  writes several mappings, or reorganizes. This is the direct cost of AC2, and the
  dry-run scan plus a `status` view that names unmapped-but-linked targets makes
  the gap visible instead of silent.
- **New notes in unmapped folders stay invisible with no notification.** There is
  no watcher and nothing scans outside the mappings, so CAO cannot report "you
  wrote a note somewhere I cannot see". The only signal is a `link_excluded`
  finding when a mapped note links to it. Documented as a known limitation.
- **Not literally either listed alternative.** A reader of the issue may expect one
  of the two. This ADR is the record of why, and the correction is carried in
  `design.md` under Corrections to the issue.
- **One managed folder means all agent-authored knowledge for a scope lands in one
  directory**, which will grow large. Acceptable: it is a flat directory of
  Markdown notes, which is exactly what Obsidian is built to browse, and the
  alternative (per-type subfolders) adds path composition surface for no security
  benefit.

## Alternatives rejected

### A. Whole-vault reads, managed-folder writes

Rejected as incoherent with AC2, as argued above: it requires either a default
scope for unmapped notes (a silent mapping) or non-indexing of unmapped notes
(which is this decision). Beyond the contradiction, it maximizes exposure of exactly
the material most likely to be confidential — a personal vault's daily notes,
journals and meeting minutes — by default rather than by choice, and it makes the
supported-boundary problem unbounded, since every plugin format and attachment type
anywhere in the vault becomes something the parser must classify.

### B. Managed-folder-only for both reads and writes

Rejected. It is the safest possible surface and it defeats the feature. The issue's
Overview names the problem as "existing vault notes are not directly recallable",
and its first user story asks for recall from selected vault folders. A
managed-folder-only read surface means the user must move or import notes to make
them recallable, which is what OKF export/import (#345) already offers — so this
alternative ships no new capability. It is retained as the effective behavior for a
user who maps only the managed folder, which is a legitimate conservative
configuration rather than a separate mode.

### C. Whole-vault reads with a mandatory explicit default-scope declaration

Rejected. It is the honest version of alternative A — the default is declared, so
it is not literally silent. It still fails AC2 in substance: every note the user has
not thought about lands in a declared scope, so the exposure decision is made once,
in advance, for material that does not exist yet. It also leaves the parser facing
the whole vault, so the supported-boundary and quarantine surface is unbounded. If
a future release wants a broader read surface, the right shape is an `include[]`
glob list per mapping — additive to this decision, not a replacement for it.

### D. Writes to any mapping with `writable: true`, not a single managed folder

Rejected for release one. It is a natural generalization and it removes the single
audit point that makes AC7 and AC8 cheap to prove: today a test can assert that
every write path composes its target under exactly one base. With several writable
folders the assertion becomes "under one of N bases", which is a weaker property and
a larger review. Deferred, not refused — the config schema already carries
`writable` per mapping, so relaxing this is removing the "only the managed folder's
mapping may be writable" validation.

## Security and compliance implications

- **Default-empty read surface.** Nothing in the vault is readable until a folder
  is mapped. This is the strongest available posture for a directory that may hold
  confidential material, and it is why `memory.vault.enabled` defaulting false is
  not sufficient on its own — an enabled vault with no mappings still reads nothing.
- **Non-overlapping mappings are a scope-isolation control.** Because a note
  belongs to exactly one mapping, it has exactly one scope, so the existing
  scope-based authorization and the relationship service's same-scope invariant
  continue to mean what they mean for native memory.
- **`managed_folder` inside a writable mapping is the AC2 enforcement point.** The
  criterion is about not writing a private or short-lived scope into a broadly
  shared folder. Because the managed folder's scope is fixed by the mapping that
  contains it, and because `session` and `federated` are not mappable at all
  ([ADR-007](adr-007-configuration-surface.md)), the dangerous combination is
  rejected at config load rather than caught at write time.
- **Exclusions are enforced before parsing, not after.** An excluded path is never
  opened, so an excluded note cannot contribute a parse failure, a finding
  containing its path fragments, or a timing signal. Always-excluded paths
  (`.obsidian/`, `.trash/`, `.git/`) are not configurable, so a mis-edited config
  cannot pull CAO into Obsidian's own configuration or a user's deleted notes.
- **A permissive read charset is not a weaker boundary.** What prevents escape from
  the vault root is realpath containment asserted immediately before the open, not a
  character allowlist; an allowlist is a proxy for that property, not a substitute
  for it. Insisting on the allowlist for read paths would have made this decision
  unimplementable against any real vault while adding no defence against traversal —
  see rule 8 and
  [adr-007-configuration-surface.md](adr-007-configuration-surface.md) alternative F.
- **Dry-run-first is the compliance review step.** For an organization that needs to
  approve what an agent may read, `cao memory vault scan --dry-run` produces the
  reviewable artifact — a list of paths and scopes — before any state changes. The
  approval is on evidence rather than on intent.
