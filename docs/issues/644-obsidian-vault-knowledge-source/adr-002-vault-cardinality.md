# ADR-002: One vault in release one, with a plural schema from day one

**Issue:** [#644](https://github.com/awslabs/cli-agent-orchestrator/issues/644)
**Status:** Proposed. **Unchanged in revision 3** — no ruling touched it. Revision 2 restated the cardinality argument in terms of one confinement root rather than one `safe_join_under_base` base.
**Decision owner:** the issue's open question "decide whether the first release supports one vault or multiple vaults"
**Related:** [design.md](design.md), [adr-006-identity-and-change-detection.md](adr-006-identity-and-change-detection.md), [adr-007-configuration-surface.md](adr-007-configuration-surface.md)

---

## Context

The issue's Proposed Solution says "configure one vault root", and Risks and Open
Questions asks whether release one supports one vault or several. Users plausibly
have more than one — a work vault and a personal vault, or a shared team vault
alongside private notes — so the question is whether to build for that now.

What the answer changes in the code:

- **Path confinement.** With one root there is exactly one root against which the
  read-path containment guard is asserted — `os.path.realpath` followed by a single
  positive `startswith(root + os.sep)` inline beside each filesystem sink — and
  exactly one base for `safe_join_under_base` on the write path. With several roots
  there are N of each, and every containment check has to first decide which root
  applies to a given path. Choosing the wrong root is a traversal bug that a
  single-root design cannot express. (Revision 2: the read path deliberately does
  **not** use `safe_join_under_base`, whose per-segment `\A[A-Za-z0-9._-]+\Z`
  charset rejects ordinary vault folder names — see
  [adr-007-configuration-surface.md](adr-007-configuration-surface.md) rule 7. The
  cardinality argument is unaffected: it is about how many roots exist, not which
  primitive proves containment.)
- **Link resolution.** Wikilinks are resolved by note name within a scope. Two
  vaults each containing `Design.md` mapped to the same scope makes every bare
  `[[Design]]` ambiguous, and the ambiguity is invisible to the person editing
  either vault.
- **Identity namespace.** `note_uid` and `cao_key` uniqueness need a namespace. If
  it is `(scope, scope_id, cao_key)` then two vaults mapped to the same scope
  collide. If it includes a vault identifier, the schema needs one.
- **Reconcile locking and reporting.** One vault means one run, one report, one
  lock. Several means per-vault runs, partial-failure semantics, and a status view
  that has to attribute every count.

The dangerous outcome is shipping a singular scalar `vault_root` and then needing a
schema migration, a `vault_id` backfill and a rewritten confinement layer when the
second vault arrives.

## Decision

**Release one supports exactly one vault. The configuration schema and every
derived table carry a `vault_id` from day one. Configuring more than one vault is a
load-time validation error naming the release-one limit.**

Concretely:

- `memory.vault.vaults` is a **list** of vault specifications. Each entry has a
  required `id` (a validated slug, used as the namespace key, not a path).
- `len(vaults) > 1` fails config load with
  `"multiple vaults are not supported in this release; configure exactly one"`.
  `len(vaults) == 0` with `enabled: true` also fails, rather than silently
  behaving as disabled.
- `vault_note`, `vault_finding` and `vault_note_alias` all carry `vault_id NOT
  NULL`, and their unique constraints include it:
  `(vault_id, scope, scope_id, cao_key)` and `(vault_id, vault_relpath)`.
- `note_uid` is derived from a digest whose input begins with `vault_id` (see
  [ADR-006](adr-006-identity-and-change-detection.md)), so uids are already
  namespaced.
- `ScopeBinding` carries the resolved `vault_id` and root, so every path
  composition names its base explicitly rather than reading a module-level
  singleton.
- Relaxing to multi-vault later is deleting the length check plus adding
  cross-vault ambiguity handling in `links.py`. It is not a data migration.

## Consequences

**Positive.**

- One confinement root in release one, so the traversal story is the simplest it
  can be and unit `vault-scan`'s security review has one invariant to check rather
  than N.
- Bare wikilinks are unambiguous by construction; the only ambiguity `links.py` has
  to handle in release one is duplicate basenames within one vault, which is a real
  Obsidian condition and is handled explicitly in
  [ADR-004](adr-004-supported-markdown-boundary.md).
- No migration debt. Every derived row is already namespaced, so the second vault
  is a validation change.
- The error message is honest. A user with two vaults is told the limit at config
  load, not left to discover that only one of them ever appears in recall.

**Negative, and accepted.**

- A user with two vaults must pick one for release one, or consolidate the folders
  they want CAO to see into the chosen vault. Obsidian vaults are directories, so
  this is a real inconvenience rather than a trivial one.
- `vault_id` is carried in every row and every uid input while only ever holding
  one value, so the column reads as redundant to a reviewer who has not read this
  ADR. Mitigated by a comment on the column pointing here.
- A plural key that rejects plurality is mildly surprising. Mitigated by the
  explicit error text and by documenting the shape in `docs/memory.md` as
  "a list of one, today".

## Alternatives rejected

### A. Singular scalar `memory.vault.root`

Rejected. It is the smallest release-one surface and the largest future cost: the
second vault forces a settings-schema change (with a back-compat reader for the old
scalar), a `vault_id` column addition and backfill on three tables, a change to the
`note_uid` derivation — which would invalidate every existing uid and therefore
every `origin = "vault"` edge keyed off it — and a rewrite of the confinement layer
from one base to many. The cost of avoiding all that now is one required `id` field
and one length check.

### B. Full multi-vault support in release one

Rejected. It multiplies the parts of the design that are hardest to get right, for
demand that has not been demonstrated. Specifically it requires: N confinement
bases with a base-selection step that is itself a traversal risk; a cross-vault
link-ambiguity policy; per-vault reconcile runs with partial-failure semantics and
a merged status view; and a decision about whether two vaults may map to the same
`(scope, scope_id)` — which, if allowed, breaks the "exactly one content backend
per scope" invariant that the entire design rests on, and if forbidden, is another
validation rule to specify and test. None of that is hard in isolation; together it
roughly doubles the security-sensitive surface of units `vault-config` and
`vault-scan`.

### C. One vault, but allow a second *read-only* vault

Rejected. It sounds like a cheap middle but it is not: read-only is not a property
of a vault in this design, it is a property of a mapping (`writable: false`), and a
second vault brings the entire confinement, ambiguity and namespacing bill whether
or not anything writes to it. It also creates a two-tier mental model with no
corresponding user request.

## Security and compliance implications

- **Confinement simplicity is a security property, not just an implementation
  convenience.** A single root means there is no root-selection step that could pick
  the wrong root and thereby validate a path against a base it does not belong to.
  That applies identically to the read path's inline `startswith(root + os.sep)`
  guard and to the write path's `safe_join_under_base`. This is why the cardinality
  decision is security-relevant at all.
- **Blast-radius containment.** One configured vault means one configured directory
  tree CAO may read. An operator reviewing the configuration has exactly one root
  to reason about, which matters when the vault holds confidential material.
- **Namespaced identity prevents cross-source confusion later.** Because `vault_id`
  is in the uid digest and in every unique constraint from day one, a future second
  vault cannot silently merge two different notes into one CAO memory — the failure
  mode that would be hardest to notice and most damaging if the two vaults have
  different confidentiality levels.
- **Explicit refusal over partial function.** Failing config load when two vaults
  are configured is the fail-closed choice. Silently using the first entry would
  leave an operator believing a second, possibly more sensitive, vault was in scope
  when it was not — or, worse, believing it was *out* of scope when a later release
  quietly brought it in.
